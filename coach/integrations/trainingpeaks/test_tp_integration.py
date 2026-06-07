"""Tester för TP-integrationen (rena transformer, ingen nätverk).

Kör: `pytest coach/integrations/trainingpeaks/ -q` från repo-roten.
"""

from __future__ import annotations

import json
import types
from datetime import date
from pathlib import Path

import yaml

from coach.integrations.trainingpeaks.mapping import build_tp_structure, _intensity_pct
from coach.integrations.trainingpeaks.structure import (
    AUTOSYNC_ELIGIBLE,
    build_create_payload,
    build_wire,
    compute_if_tss,
)
from coach.integrations.trainingpeaks import sync
from coach.integrations.trainingpeaks.workout_writer import (
    create_planned_workout,
    sync_planned_week_to_tp,
)

_ROOT = Path(__file__).resolve().parents[3]
_WORKOUTS = _ROOT / "coach" / "data" / "workouts"


def _load(code_file: str) -> list[dict]:
    doc = yaml.safe_load((_WORKOUTS / code_file).read_text(encoding="utf-8"))
    return doc["workouts"]


def _pass(code_file: str, code: str) -> dict:
    return next(w for w in _load(code_file) if w["code"] == code)


# ---------- mapping ----------

def test_bike_me_structure_shape():
    wk = _pass("bike_ME.yaml", "ME1_bike_01")
    res = build_tp_structure(wk, total_duration_min=70)
    assert res.sport == "Bike"
    assert res.structure["primaryIntensityMetric"] == "percentOfFtp"
    assert res.warnings == []
    steps = res.structure["steps"]
    assert steps[0]["intensityClass"] == "warmUp"
    rep = steps[1]
    assert rep["type"] == "repetition" and rep["reps"] == 4
    work, rest = rep["steps"]
    assert work["duration_seconds"] == 480            # 8 min
    assert (work["intensity_min"], work["intensity_max"]) == (91.0, 105.0)  # Z4 %FTP
    assert work["cadence_min"] == 85 and work["cadence_max"] == 95
    assert rest["intensityClass"] == "rest" and rest["duration_seconds"] == 240
    assert steps[-1]["intensityClass"] == "coolDown"


def test_run_uses_pace_metric_and_inverts_zone():
    # Syntetiskt tidsbaserat löppass → deterministisk pace-inverteringskoll.
    wk = {"code": "RUN_T", "discipline": "run", "name": "Tröskel 4×5",
          "main_set": [
              {"segment": "warmup", "duration_pct": 0.2, "zone": 1},
              {"segment": "main", "sets": 4, "duration_min": 5, "zone": 4, "rest_sec": 90},
              {"segment": "cooldown", "duration_pct": 0.2, "zone": 1},
          ]}
    res = build_tp_structure(wk, total_duration_min=45)
    assert res.sport == "Run"
    assert res.structure["primaryIntensityMetric"] == "percentOfThresholdPace"
    work = res.structure["steps"][1]["steps"][0]
    # Z4 tid-fraktion (0.99, 1.04) → fart-% (100/1.04, 100/0.99) ≈ (96.2, 101.0)
    assert work["intensity_min"] == 96.2 and work["intensity_max"] == 101.0


def test_run_distance_needs_pace_then_converts():
    wk = {"code": "RUN_D", "discipline": "run", "name": "6×1000m",
          "main_set": [{"segment": "main", "sets": 6, "distance_m": 1000,
                        "zone": 4, "rest_sec": 90}]}
    # utan pace → distanssteg hoppas, varning
    res = build_tp_structure(wk, total_duration_min=50)
    assert any("utan pace/CSS" in w for w in res.warnings)
    # med tröskelfart 240 s/km → 1000 m i Z4 (~1.015×240) ≈ 243 s
    res2 = build_tp_structure(wk, total_duration_min=50, threshold_pace_sec_per_km=240)
    work = res2.structure["steps"][0]["steps"][0]
    assert 230 <= work["duration_seconds"] <= 255


def _leaf_steps(structure):
    out = []
    for b in structure["steps"]:
        if b.get("type") == "repetition":
            out.extend(b["steps"])
        else:
            out.append(b)
    return out


# ---------- pattern (crisscross / over-under) ----------

def test_pattern_bike_crisscross_block():
    # ME3_bike_01: 3×10 min, 1 min Z4 / 1 min Z3, vila 5 min mellan block.
    wk = _pass("bike_ME.yaml", "ME3_bike_01")
    res = build_tp_structure(wk, total_duration_min=75)
    assert res.warnings == []
    reps = [b for b in res.structure["steps"] if b.get("type") == "repetition"]
    assert len(reps) == 3                                  # 3 block
    for b in reps:
        assert b["reps"] == 5                              # 5 cykler/block (10 min / 2 min)
        z4, z3 = b["steps"]
        assert z4["duration_seconds"] == 60 and z3["duration_seconds"] == 60
        assert (z4["intensity_min"], z4["intensity_max"]) == _intensity_pct("bike", 4)
        assert (z3["intensity_min"], z3["intensity_max"]) == _intensity_pct("bike", 3)
    work = sum(b["reps"] * sum(s["duration_seconds"] for s in b["steps"]) for b in reps)
    assert work == 30 * 60                                 # 3×10 min crisscross (vila räknas separat)
    rests = [s for s in res.structure["steps"] if s.get("intensityClass") == "rest"]
    assert len(rests) == 2 and all(s["duration_seconds"] == 300 for s in rests)
    _, _, total_s = compute_if_tss(res.structure)
    assert total_s == 75 * 60                              # budget exakt


def test_pattern_run_crisscross_continuous_not_skipped():
    # Regressionsskydd: pattern utan duration_min hoppades tyst över förr →
    # hela crisscross-huvudsetet försvann ur TP-strukturen.
    wk = _pass("run_ME.yaml", "ME3_run_01")
    res = build_tp_structure(wk, total_duration_min=60)
    assert res.sport == "Run"
    reps = [b for b in res.structure["steps"]
            if b.get("type") == "repetition" and b["reps"] == 6]
    assert len(reps) == 1                                  # 6×(2 min Z4 / 1 min Z3)
    z4, z3 = reps[0]["steps"]
    assert z4["duration_seconds"] == 120 and z3["duration_seconds"] == 60
    assert z4["intensity_max"] > z3["intensity_max"]       # Z4 snabbare än Z3 (fart-%)
    _, _, total_s = compute_if_tss(res.structure)
    assert total_s == 60 * 60


def test_pattern_over_under_uses_exact_pct():
    # ME1_bike_03: over-under 103 % vs 99 % — båda Z4, måste skiljas via pct.
    wk = _pass("bike_ME.yaml", "ME1_bike_03")
    res = build_tp_structure(wk, total_duration_min=70)
    assert res.warnings == []
    leafs = list(_leaf_steps(res.structure))
    highs = [s for s in leafs if (s["intensity_min"], s["intensity_max"]) == (101.0, 105.0)]
    lows = [s for s in leafs if (s["intensity_min"], s["intensity_max"]) == (98.0, 100.0)]
    assert len(highs) == 5 and len(lows) == 5
    assert all(s["duration_seconds"] == 120 for s in highs)   # 2 min hög
    assert all(s["duration_seconds"] == 240 for s in lows)    # 4 min låg
    rests = [s for s in leafs if s.get("intensityClass") == "rest"]
    assert len([r for r in rests if r["duration_seconds"] == 180]) == 4   # vila mellan 5 reps
    _, _, total_s = compute_if_tss(res.structure)
    assert total_s == 70 * 60


# ---------- structure / wire / payload ----------

def test_if_tss_and_payload():
    wk = _pass("bike_ME.yaml", "ME1_bike_01")
    res = build_tp_structure(wk, total_duration_min=70)
    IF, tss, total_s = compute_if_tss(res.structure)
    assert total_s == 70 * 60          # budget-normaliserad: warmup/cooldown fyller exakt
    assert 0.7 < IF < 1.0
    assert tss > 0

    payload = build_create_payload(res, date(2026, 6, 9), title=wk["name"])
    assert payload["workoutTypeFamilyId"] == 2 and payload["workoutTypeValueId"] == 2
    assert abs(payload["totalTimePlanned"] - total_s / 3600.0) < 1e-3
    parsed = json.loads(payload["structure"])
    assert parsed["primaryIntensityMetric"] == "percentOfFtp"
    assert parsed["structure"][1]["length"] == {"value": 4, "unit": "repetition"}
    assert len(parsed["polyline"]) > 0


def test_datetime_start_time_planned():
    from datetime import datetime
    wk = _pass("bike_ME.yaml", "ME1_bike_01")
    res = build_tp_structure(wk, total_duration_min=70)
    payload = build_create_payload(res, datetime(2026, 6, 9, 16, 45, 0), title="x")
    assert payload["startTimePlanned"].startswith("2026-06-09T16:45")


# ---------- sync transforms ----------

def test_sleep_hours_to_score():
    assert sync.sleep_hours_to_score(8) == 100
    assert sync.sleep_hours_to_score(4) == 50
    assert sync.sleep_hours_to_score(10) == 100   # clamp
    assert sync.sleep_hours_to_score(None) is None


def test_metrics_to_daily_rows():
    days = [
        {"timeStamp": "2026-06-01T00:00:00", "details": [
            {"type": sync.TYPE_PULSE, "value": 48},
            {"type": sync.TYPE_HRV, "value": 72},
            {"type": sync.TYPE_SLEEP_HOURS, "value": 8},
        ]},
        {"timeStamp": "2026-06-02T00:00:00", "details": [
            {"type": sync.TYPE_PULSE, "value": 52},
        ]},
    ]
    rows = sync.metrics_to_daily_rows(days, "athlete-x")
    assert rows[0]["resting_hr"] == 48
    assert rows[0]["hrv_last_night_ms"] == 72.0
    assert rows[0]["sleep_score"] == 100
    assert rows[1]["resting_hr"] == 52
    assert rows[1]["hrv_last_night_ms"] is None
    assert rows[0]["readiness_score"] is None   # degraderar


def test_hrv_baselines_computed():
    rows = [
        {"metric_date": f"2026-06-{d:02d}", "hrv_last_night_ms": 70 + (d % 3)}
        for d in range(1, 13)
    ]
    sync.add_hrv_baselines(rows, window=60)
    assert rows[0]["hrv_baseline_low"] is None          # ingen historik
    assert rows[-1]["hrv_baseline_low"] is not None      # efter ≥7 dagar
    assert rows[-1]["hrv_baseline_high"] >= rows[-1]["hrv_baseline_low"]
    assert rows[-1]["hrv_weekly_avg_ms"] is not None


def test_pmc_to_load_and_merge():
    pmc = [
        {"workoutDay": "2026-06-01T00:00:00", "ctl": 50.0, "atl": 65.0, "tsb": -15.0},
        {"workoutDay": "2026-06-02T00:00:00", "ctl": 0.0, "atl": 10.0},
    ]
    by_date = sync.pmc_to_load_by_date(pmc)
    assert by_date["2026-06-01"]["acute_load"] == 65.0
    assert by_date["2026-06-01"]["chronic_load"] == 50.0
    assert by_date["2026-06-01"]["load_ratio"] == round(65.0 / 50.0, 3)
    assert by_date["2026-06-02"]["load_ratio"] is None    # division by zero skyddad

    rows = [{"metric_date": "2026-06-01"}, {"metric_date": "2026-06-03"}]
    sync.merge_load_into_daily(rows, by_date)
    assert rows[0]["load_ratio"] == round(65.0 / 50.0, 3)
    assert rows[1]["load_ratio"] is None


def test_workouts_to_activity_rows():
    workouts = [
        {"workoutId": 111, "startTime": "2026-06-01T07:00:00",
         "workoutTypeValueId": 2, "totalTime": 1.5},                  # genomfört bike
        {"workoutId": 222, "workoutDay": "2026-06-02T00:00:00",
         "workoutTypeValueId": 3, "totalTimePlanned": 1.0},           # bara planerat → hoppas
    ]
    rows = sync.workouts_to_activity_rows(workouts, "athlete-x")
    assert len(rows) == 1
    assert rows[0]["garmin_activity_id"] == 111           # bigint, numeriskt TP-id
    assert rows[0]["duration_sec"] == 5400                # 1.5h
    assert rows[0]["activity_type"] == "cycling"


# ---------- writer eligibility ----------

def test_writer_dry_run_bike_reaches_watch():
    wk = _pass("bike_ME.yaml", "ME1_bike_01")
    res = create_planned_workout(None, wk, date(2026, 6, 9), 70, dry_run=True)
    assert res.dry_run and res.reaches_watch and res.sport == "Bike"


def test_writer_strength_does_not_reach_watch():
    wk = {"code": "ST_01", "discipline": "strength", "name": "Styrka",
          "main_set": [{"segment": "main", "duration_min": 45, "zone": 2}]}
    res = create_planned_workout(None, wk, date(2026, 6, 9), 45, dry_run=True)
    assert "Strength" in res.sport
    assert res.reaches_watch is False
    assert any("AutoSync" in w for w in res.warnings)


assert AUTOSYNC_ELIGIBLE  # import-sanity


# ---------- orkestrering (mockad klient) ----------

class _FakeClient:
    def __init__(self):
        self.created: list[dict] = []

    def create_workout(self, payload):
        self.created.append(payload)
        return {"workoutId": 900 + len(self.created)}

    def close(self):
        pass


def test_create_week_with_fake_client():
    from coach.integrations.trainingpeaks.workout_writer import create_week
    items = [
        {"workout": _pass("bike_ME.yaml", "ME1_bike_01"), "day": date(2026, 6, 9),
         "total_duration_min": 70, "title": "Bike ME"},
        {"workout": {"code": "ST", "discipline": "strength", "name": "Styrka",
                     "main_set": [{"segment": "main", "duration_min": 45, "zone": 2}]},
         "day": date(2026, 6, 10), "total_duration_min": 45},
    ]
    c = _FakeClient()
    res = create_week(c, items, dry_run=False)
    assert len(res) == 2
    assert res[0].workout_id and res[0].reaches_watch          # bike → klockan
    assert res[1].reaches_watch is False                        # styrka → ej klockan
    assert c.created[0]["workoutTypeFamilyId"] == 2             # Bike
    assert "structure" in c.created[0]


class _FakeTP:
    def get_metrics(self, s, e):
        return [{"timeStamp": "2026-06-08T00:00:00", "details": [
            {"type": 5, "value": 50}, {"type": 60, "value": 70}, {"type": 6, "value": 7.5}]}]

    def get_performance_data(self, s, e):
        return [{"workoutDay": "2026-06-08T00:00:00", "ctl": 40, "atl": 50}]

    def get_workouts(self, s, e):
        return [{"workoutId": 7, "startTime": "2026-06-08T06:30:00",
                 "workoutTypeValueId": 2, "totalTime": 1.0}]


class _RecExec:
    def __init__(self, rec, table):
        self.rec, self.table, self.rows = rec, table, None

    def upsert(self, rows, on_conflict=None):
        self.rows = rows
        self.rec[self.table] = rows
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _FakePG:
    def __init__(self):
        self.rec: dict = {}

    def schema(self, name):
        rec = self.rec
        return types.SimpleNamespace(from_=lambda table: _RecExec(rec, table))


def test_sync_daily_and_activities_write_shapes():
    tp, pg = _FakeTP(), _FakePG()
    r = sync.sync_daily(tp, "ath", date(2026, 6, 8), date(2026, 6, 8), pg=pg)
    assert r.status == "success" and r.records == 1
    daily = pg.rec["daily_metrics"][0]
    assert daily["resting_hr"] == 50
    assert daily["hrv_last_night_ms"] == 70.0
    assert daily["chronic_load"] == 40.0 and daily["acute_load"] == 50.0
    assert daily["load_ratio"] == round(50 / 40, 3)            # fylls (var NULL i Garmin)

    r2 = sync.sync_activities(tp, "ath", date(2026, 6, 8), date(2026, 6, 8), pg=pg)
    assert r2.status == "success"
    act = pg.rec["activities"][0]
    assert act["garmin_activity_id"] == 7
    assert act["duration_sec"] == 3600 and act["activity_type"] == "cycling"


# ---------- token-exchange retry (TP 500:ar sporadiskt) ----------

class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {"message": "err"}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


class _FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        s = self.statuses[self.calls]
        self.calls += 1
        if s == 200:
            return _FakeResp(200, {"success": True, "token": {"access_token": "tok", "expires_in": 3600}})
        return _FakeResp(s)

    def close(self):
        pass


def test_token_exchange_retries_transient_500():
    import coach.integrations.trainingpeaks.client as cl
    orig_sleep = cl.time.sleep
    cl.time.sleep = lambda *a, **k: None     # snabba upp backoff
    try:
        c = cl.TPClient(cookie="x")
        c._session = _FakeSession([500, 500, 200])
        c._exchange_cookie_for_token()
        assert c._access_token == "tok"
        assert c._session.calls == 3          # två retries innan 200
    finally:
        cl.time.sleep = orig_sleep


def test_token_exchange_auth_error_no_retry():
    import coach.integrations.trainingpeaks.client as cl
    c = cl.TPClient(cookie="x")
    c._session = _FakeSession([401, 200])
    raised = False
    try:
        c._exchange_cookie_for_token()
    except cl.TPAuthError:
        raised = True
    assert raised
    assert c._session.calls == 1              # ingen retry på auth-fel


# ---------- TP → training_log (master) + dedup ----------

def test_canon_sport_folds_variants():
    assert sync.canon_sport("Lopning") == "run"
    assert sync.canon_sport("Löpning") == "run"
    assert sync.canon_sport("Cykel") == "bike"
    assert sync.canon_sport("Cykling") == "bike"
    assert sync.canon_sport("Sim") == "swim"
    assert sync.canon_sport("Styrka") == "strength"
    assert sync.canon_sport("VirtualRun") == "run"


def test_tp_to_training_log_row():
    w = {"workoutId": 555, "startTime": "2026-06-03T07:00:00", "workoutTypeValueId": 2,
         "totalTime": 1.0, "distance": 28000, "heartRateAverage": 140, "tssActual": 75}
    r = sync.tp_workout_to_training_log_row(w, "user-x")
    assert r["user_id"] == "user-x" and r["tp_workout_id"] == 555
    assert r["sport"] == "Cykel" and r["source"] == "tp"
    assert r["duration_min"] == 60.0 and r["distance_km"] == 28.0
    assert r["avg_hr"] == 140 and r["tss"] == 75.0
    # bara planerat (ingen totalTime) → None
    assert sync.tp_workout_to_training_log_row(
        {"workoutId": 1, "workoutTypeValueId": 3, "totalTimePlanned": 1.0,
         "startTime": "2026-06-03T07:00:00"}, "u") is None


def test_valid_env_cookie_filters_garbage():
    from coach.integrations.trainingpeaks.client import valid_env_cookie
    assert valid_env_cookie(None) is None
    assert valid_env_cookie('cd "C:\\x" python -c garbage') is None   # whitespace → trasig
    assert valid_env_cookie("short") is None                          # för kort
    good = "V001" + "x" * 300
    assert valid_env_cookie(good) == good                             # äkta TP-cookie


def test_dedup_training_log_skips_strava_match():
    tp = [
        {"date": "2026-06-03", "sport": "Cykel", "duration_min": 60.0, "tp_workout_id": 1},
        {"date": "2026-06-04", "sport": "Lopning", "duration_min": 30.0, "tp_workout_id": 2},
    ]
    existing = [{"date": "2026-06-03", "sport": "cycling", "duration_min": 62.0, "source": "strava"}]
    fresh, skipped = sync.dedup_training_log_rows(tp, existing)
    assert [r["tp_workout_id"] for r in fresh] == [2]      # bara det nya passet
    assert [r["tp_workout_id"] for r in skipped] == [1]    # dubbletten mot strava


# ---------- idempotent vecko-sync (replace-by-id, skip-if-unchanged) ----------

_SYNC_STEPS = [
    {"segment": "warmup", "duration_pct": 0.2, "zone": 1},
    {"segment": "main", "sets": 3, "duration_min": 8, "zone": 4, "rest_sec": 120},
    {"segment": "cooldown", "duration_pct": 0.1, "zone": 1},
]


def _planned_row(**kw):
    base = {"id": "r1", "date": "2026-07-06", "sport": "Cykel", "title": "Cykel Z2",
            "workout_code": "AE2_bike_02", "duration_min": 60, "steps": list(_SYNC_STEPS),
            "tp_workout_id": None, "tp_synced_hash": None}
    base.update(kw)
    return base


class _SyncQ:
    def __init__(self, store):
        self.store = store
        self._upd = None
        self._eqs: dict = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eqs[col] = val
        return self

    def gte(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def update(self, vals):
        self._upd = vals
        return self

    def execute(self):
        if self._upd is not None:
            rid = self._eqs.get("id")
            for r in self.store:
                if r.get("id") == rid:
                    r.update(self._upd)
            return types.SimpleNamespace(data=None)
        return types.SimpleNamespace(data=list(self.store))


class _SyncPG:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return _SyncQ(self.rows)


class _SyncTP:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.created: list = []
        self.deleted: list = []
        self._next = 9000

    def get_workouts(self, s, e):
        return self.existing

    def create_workout(self, payload):
        self._next += 1
        self.created.append(payload)
        return {"workoutId": self._next}

    def delete_workout(self, wid):
        self.deleted.append(wid)


def test_sync_creates_then_skips_unchanged():
    rows = [_planned_row()]
    pg, c = _SyncPG(rows), _SyncTP()
    r1 = sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=False)
    assert r1[0].action == "created" and r1[0].workout_id
    assert len(c.created) == 1
    assert rows[0]["tp_workout_id"] == r1[0].workout_id and rows[0]["tp_synced_hash"]
    # andra körningen: oförändrat → hoppa (ingen ny create, ingen delete)
    r2 = sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=False)
    assert r2[0].action == "unchanged"
    assert len(c.created) == 1 and c.deleted == []


def test_sync_replaces_on_change():
    rows = [_planned_row()]
    pg, c = _SyncPG(rows), _SyncTP()
    sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=False)
    first_id = rows[0]["tp_workout_id"]
    rows[0]["title"] = "Cykel Z2 (justerad)"   # innehåll ändrat → hash ändras
    r = sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=False)
    assert r[0].action == "replaced"
    assert c.deleted == [first_id] and len(c.created) == 2
    assert rows[0]["tp_workout_id"] != first_id


def test_sync_fallback_matches_day_sport():
    # Rad utan lagrat id, men ett planerat TP-pass finns samma dag+sport → adoptera.
    rows = [_planned_row(tp_workout_id=None, tp_synced_hash=None)]
    existing = [{"workoutId": 555, "workoutDay": "2026-07-06T00:00:00",
                 "workoutTypeValueId": 2, "totalTime": None}]
    pg, c = _SyncPG(rows), _SyncTP(existing=existing)
    r = sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=False)
    assert r[0].action == "replaced" and 555 in c.deleted


def test_sync_dry_run_writes_nothing():
    rows = [_planned_row()]
    pg, c = _SyncPG(rows), _SyncTP()
    r = sync_planned_week_to_tp(c, pg, "u", date(2026, 7, 6), dry_run=True)
    assert r[0].action == "would_create"
    assert c.created == [] and c.deleted == []
    assert rows[0]["tp_workout_id"] is None
