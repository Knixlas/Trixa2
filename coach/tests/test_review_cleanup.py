"""Städ-/konventionsfynden ur kodöversynen 2026-09-02 (docs/12, avsnitt I).

I1  coach/engine/garmin.py var död (0 importer) och pekade på en modul
    som inte finns.
I2  persisted_week_id — PK i droppad tabell, alltid None, plumbad genom
    sex filer.
I3  Niklas UUID som literal default i fem filer.
I4  "Kalmar" i generella passtexter.
I6  Engine-beslut utan reason (kategorival, styrkeprotokoll).
I7  Testfaken filtrerade inte datumintervall.
I8  ACWR 1.3 som literal på fyra ställen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

ROOT = Path(__file__).resolve().parents[2]


# ---------- I1 / I2 / I3 / I4 ----------


def test_dod_adapter_ar_borta():
    assert not (ROOT / "coach" / "engine" / "garmin.py").exists()
    assert not (ROOT / "coach" / "adapters").exists()


def test_persisted_week_id_ar_borta():
    hits = []
    for rel in ("coach/trixa", "trixa_api", "trixa_api/templates"):
        for p in (ROOT / rel).glob("*.*"):
            if p.suffix in (".py", ".html") and "persisted_week_id" in p.read_text(encoding="utf-8"):
                hits.append(p.name)
    assert hits == [], hits


def test_inga_adept_uuid_som_literal_i_koden():
    hits = []
    for rel in ("coach/trixa", "coach/integrations/trainingpeaks", "trixa_api"):
        for p in (ROOT / rel).glob("*.py"):
            src = p.read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("#") or '"""' in line:
                    continue
                if "09db449d-b8fd" in line or "98057fa1-4fb9" in line:
                    hits.append(f"{p.name}:{i}")
    assert hits == [], hits


def test_config_ger_none_utan_miljo(monkeypatch):
    from coach.trixa import config

    monkeypatch.delenv("TRIXA_DEFAULT_USER_ID", raising=False)
    assert config.default_user_id() is None
    import pytest

    with pytest.raises(SystemExit):
        config.require(None, "--user", "TRIXA_DEFAULT_USER_ID")
    assert config.require("x", "--user", "TRIXA_DEFAULT_USER_ID") == "x"


def test_inga_adeptspecifika_ord_i_passbanken():
    hits = []
    for p in (ROOT / "coach" / "data" / "workouts").glob("*.yaml"):
        text = p.read_text(encoding="utf-8")
        for word in ("Kalmar", "Niklas"):
            if word in text:
                hits.append(f"{p.name}: {word}")
    assert hits == [], hits


# ---------- I6 ----------


def test_kategorival_bar_skal():
    from coach.engine.workouts import select_workout_types, workout_type_decisions

    decisions = workout_type_decisions("base", "base_2", 3, 3)   # sista veckan i cykeln
    by_code = {d["code"]: d for d in decisions}
    excluded = [d for d in decisions if not d["allowed"]]
    assert excluded, "sista veckan ska utesluta något"
    assert all("uteslutet" in d["reason"] for d in excluded)
    assert select_workout_types("base", "base_2", 3, 3) == [
        d["code"] for d in decisions if d["allowed"]
    ]
    assert by_code   # sanity


def test_styrkeprotokollet_bar_skal():
    from coach.engine.strength import current_strength_protocol

    mt = current_strength_protocol("base", "base_1", 2, 6)
    ms = current_strength_protocol("base", "base_1", 5, 6)
    assert "första halvan" in mt.reason and mt.protocol_code == "MT"
    assert "andra halvan" in ms.reason and ms.protocol_code == "MS"


# ---------- I7 ----------


def test_faken_filtrerar_datumintervall():
    from coach.tests.test_agent_api import _C

    fake = _C({"t": [{"date": "2026-09-01"}, {"date": "2026-09-05"}, {"date": "2026-09-09"}]})
    got = fake.table("t").select("*").gte("date", "2026-09-02").lte("date", "2026-09-08").execute().data
    assert [r["date"] for r in got] == ["2026-09-05"]
    got = fake.table("t").select("*").lt("date", "2026-09-05").execute().data
    assert [r["date"] for r in got] == ["2026-09-01"]


# ---------- I8 ----------


def test_acwr_granserna_kommer_ur_yaml():
    from coach.engine.overtraining import acwr_thresholds

    low, high = acwr_thresholds()
    assert (low, high) == (0.8, 1.3)
    assert acwr_thresholds({"acwr_high": 1.2})[1] == 1.2
    import trixa_api.readiness as readiness

    assert readiness.RAMP_WARN == high
