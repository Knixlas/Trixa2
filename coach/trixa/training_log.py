"""Gemensam läsning av MASTER ``training_log``: dedup tvärs källor.

Samma fysiska pass kan ligga i loggen två gånger — en gång från
TrainingPeaks, en gång från Strava — och legacy-synken skapade ännu fler.
Tre lager hade var sin dedup (docs/12 G4/I9): dashboarden slog ihop på
(dag, gren, ±10 %) oavsett källa och åt därför upp två riktiga
pendlingspass; planeraren jämförde exakt duration till en decimal och
räknade tp 60,0 + strava 60,2 som två pass i veckovolymen; agent-API:t
dedupade inte alls och gav coachen dubbel volym.

En regel, här: två rader är samma pass om de har samma dag och gren,
kommer från OLIKA källor, och ligger inom ±10 % (minst 2 min) i tid.
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


def dedup_cross_source(rows: list[dict]) -> list[dict]:
    """Behåll en rad per fysiskt pass, tvärs källor."""
    kept: list[dict] = []
    for r in sorted(rows, key=lambda x: SOURCE_RANK.get((x.get("source") or "").lower(), 3)):
        day = str(r.get("date"))[:10]
        sport = sports.canon(r.get("sport"), "other")
        source = (r.get("source") or "").lower()
        dur = _dur(r)
        tol = max(2.0, dur * 0.10)
        if any(
            str(k.get("date"))[:10] == day
            and sports.canon(k.get("sport"), "other") == sport
            and (k.get("source") or "").lower() != source
            and abs(_dur(k) - dur) <= tol
            for k in kept
        ):
            continue
        kept.append(r)
    return kept
