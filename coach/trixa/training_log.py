"""Gemensam läsning av MASTER ``training_log``: dedup tvärs källor.

Samma fysiska pass kan ligga i loggen två gånger — en gång från
TrainingPeaks, en gång från Strava — och legacy-synken skapade ännu fler.
Tre lager hade var sin dedup (docs/12 G4/I9): dashboarden slog ihop på
(dag, gren, ±10 %) oavsett källa och åt därför upp två riktiga
pendlingspass; planeraren jämförde exakt duration till en decimal och
räknade tp 60,0 + strava 60,2 som två pass i veckovolymen; agent-API:t
dedupade inte alls och gav coachen dubbel volym.

En regel, här: två rader är samma pass om de har samma dag och gren,
kommer från OLIKA källor, och antingen ligger inom ±10 % (minst 2 min) i
tid ELLER är länkade till samma planerade pass (planned_session_id). Det
senare fångar manuell loggning av total passtid (sim 54 min inkl. vila)
mot TP:s rörelsetid (37 min) — samma pass, för olika för tidsregeln.
Två rader från samma källa är två pass. Källrang tp > strava > manual
avgör vilken som behålls.
"""

from __future__ import annotations

from coach.trixa import sports

SOURCE_RANK = {"tp": 0, "strava": 1, "manual": 2, "chat": 2}


def _dur(row: dict) -> float:
    try:
        return float(row.get("duration_min") or 0)
    except (TypeError, ValueError):
        return 0.0


LOG_COLUMNS = (
    "date, sport, title, duration_min, distance_km, avg_hr, max_hr,"
    " avg_power, normalized_power, tss, source, planned_session_id"
)


def clean_log_rows(rows: list[dict]) -> list[dict]:
    """Bara träningsgrenar, en rad per fysiskt pass. Vad varje aggregat vill ha."""
    relevant = [r for r in rows if sports.is_training(sports.canon(r.get("sport")))]
    return dedup_cross_source(relevant)


def fetch_clean_log(client, user_id: str, start: str, end: str) -> list[dict]:
    """training_log för [start, end] (ISO-datum, inklusive), städad.

    En läsning som sedan skivas per konsument — dashboarden läste förut
    samma tabell tre gånger över överlappande fönster (docs/12 H4/I9).
    """
    if not user_id:
        return []
    res = (
        client.table("training_log")
        .select(LOG_COLUMNS)
        .eq("user_id", user_id)
        .gte("date", start)
        .lte("date", end)
        .order("date")
        .execute()
    )
    return clean_log_rows(res.data or [])


def slice_by_date(rows: list[dict], start: str, end: str, key: str = "date") -> list[dict]:
    """Rader med ``key`` i [start, end] (ISO-datum, inklusive)."""
    return [r for r in rows if start <= str(r.get(key) or "")[:10] <= end]


# Fält en bortvald dubblett får bidra med om den behållna raden saknar dem
# (adeptens RPE/känsla från manuell logg ska inte försvinna bakom TP-raden).
_CARRY_FIELDS = ("rpe", "feeling", "notes", "athlete_description", "rating")


def dedup_cross_source(rows: list[dict]) -> list[dict]:
    """Behåll en rad per fysiskt pass, tvärs källor."""
    kept: list[dict] = []
    for r in sorted(rows, key=lambda x: SOURCE_RANK.get((x.get("source") or "").lower(), 3)):
        day = str(r.get("date"))[:10]
        sport = sports.canon(r.get("sport"), "other")
        source = (r.get("source") or "").lower()
        dur = _dur(r)
        tol = max(2.0, dur * 0.10)
        psid = r.get("planned_session_id")
        match = next((
            k for k in kept
            if str(k.get("date"))[:10] == day
            and sports.canon(k.get("sport"), "other") == sport
            and (k.get("source") or "").lower() != source
            and (
                abs(_dur(k) - dur) <= tol
                or (psid and str(k.get("planned_session_id")) == str(psid))
            )
        ), None)
        if match is not None:
            for f in _CARRY_FIELDS:
                if r.get(f) not in (None, "") and match.get(f) in (None, ""):
                    match[f] = r[f]
            continue
        kept.append(dict(r))   # kopia: carry-over ovan får inte mutera anroparens rader
    return kept
