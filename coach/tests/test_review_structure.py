"""Strukturfynden ur kodöversynen 2026-09-02 (docs/12, I10–I13).

I10  Sju talhjälpare på fem ställen, två med samma namn och olika semantik.
I11  Statusens utseende låg i två tabeller (Python + Jinja), bg/fg döda.
I12  generate_week var 380 rader med sju faser och ett dött if-block.
I13  origin-policyn låg som strängjämförelser på sex ställen.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")


# ---------- I10 ----------


def test_talhjalparna_har_en_semantik():
    from coach.engine.numbers import positive_float, to_float, to_int

    assert to_float("3.5") == 3.5 and to_float("") is None and to_float(None, 0.0) == 0.0
    assert to_int("3") == 3 and to_int("3.0") == 3 and to_int("x", 7) == 7
    assert positive_float(0) is None and positive_float("60") == 60.0


def test_gamla_namnen_delegerar():
    import trixa_api.ui as ui
    from coach.engine import profile
    from coach.trixa import strength_progression as sp

    assert ui._safe_float("2.5") == 2.5 and ui._safe_int("", 4) == 4
    assert profile._as_float("1.5") == 1.5 and profile._as_int(None) is None
    assert sp._as_float(0) is None            # lastvärde: 0 = saknas, avsiktligt


# ---------- I11 ----------


def test_statusen_bar_sitt_utseende():
    import trixa_api.ui as ui

    for key, s in ui._STATUS.items():
        assert {"emoji", "label", "css", "style", "accent"} <= set(s), key
        assert "bg" not in s and "fg" not in s
    tpl = (Path(__file__).resolve().parents[2] / "trixa_api" / "templates"
           / "_week_section.html").read_text(encoding="utf-8")
    assert "statusbadge" not in tpl
    assert "w.status.css" in tpl and "w.status.accent" in tpl


# ---------- I12 ----------


def test_generate_week_ar_uppdelad():
    from coach.trixa import planner

    assert callable(planner._select_week_workouts) and callable(planner._persist_week)
    body = inspect.getsource(planner.generate_week)
    assert len(body.splitlines()) < 245                 # var 380
    assert "if \"AE\" not in categories" not in inspect.getsource(planner._select_week_workouts)
    assert "noqa: F841" not in inspect.getsource(planner._select_week_workouts)


# ---------- I13 ----------


def test_origin_policyn_ar_samlad():
    from coach.trixa import origins

    assert origins.is_human("nils") and origins.is_human("manual") and origins.is_human(None)
    assert not origins.is_human("trixa2")
    assert origins.reps_prescribed("nils") and not origins.reps_prescribed("trixa2")
    assert origins.athlete_deletable("manual") and not origins.athlete_deletable("nils")
    assert origins.swappable("trixa2", "bike") and not origins.swappable("trixa2", "rest")
    assert not origins.swappable("nils", "bike")
    assert origins.plan_source([{"origin": "nils"}, {"origin": "trixa2"}]) == "mixed"
    assert origins.plan_source([{"origin": None}]) == "engine"


def test_inga_origin_literaler_kvar_i_lagren():
    import re

    root = Path(__file__).resolve().parents[2]
    hits = []
    for rel in ("trixa_api/ui.py", "trixa_api/agent_api.py", "coach/trixa/planner.py"):
        for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or '"""' in line:
                continue
            if re.search(r'origin"?\)?\s*(==|!=)\s*"(trixa2|nils|manual)"', line) or \
               re.search(r'\.eq\("origin",\s*"(trixa2|manual)"\)', line):
                hits.append(f"{rel}:{i}")
    assert hits == [], hits
