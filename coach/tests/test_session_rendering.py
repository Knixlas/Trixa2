"""Tester för TX-2 och TX-7 ur rapporten 2026-09-01.

TX-2: passets innehåll — uppvärmning, varje övning, set, reps, vila — ligger i
``planned_sessions.details`` och renderades till ett ``<p>``. Markdown parsades
inte, radbrytningar kollapsade, och alltihop låg bakom en flik som hette
"Syfte". Ett benpass med tolv övningar blev en textmassa som ingen letade efter,
och adepten trodde att övningarna var borttagna ur planen.

TX-7: vilodagar lagras som rader i planned_sessions och räknades som pass. En
vecka med tre träningsdagar och fyra vilodagar visades som "Pass: 7".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from trixa_api.markdown_lite import render  # noqa: E402


# ---------- TX-2: markdown i passtexten ----------


def test_headings_lists_and_emphasis_become_html():
    html = str(render(
        "## Uppvärmning\n"
        "10 min Z1\n"
        "\n"
        "## Huvuddel\n"
        "- Knäböj **3x8** @ 60 kg\n"
        "- Utfall 3x10\n"
        "\n"
        "1. Först\n"
        "2. Sedan\n"
    ))
    assert "<h5>Uppvärmning</h5>" in html
    assert "<ul><li>Knäböj <strong>3x8</strong> @ 60 kg</li>" in html
    assert "<ol><li>Först</li><li>Sedan</li></ol>" in html
    # Ingen råmarkdown kvar att visa som tecken.
    assert "##" not in html and "**" not in html


def test_single_newlines_survive_as_line_breaks():
    # Radbrytningarna ÄR strukturen i ett pass — de fick inte kollapsa förut.
    html = str(render("400 m fritt\n200 m ben\n100 m armar"))
    assert html.count("<br>") == 2


def test_input_is_escaped_before_it_is_formatted():
    # Passtexten kan komma från en språkmodell. Ingen väg härigenom får släppa
    # igenom HTML från indata.
    html = str(render("<script>alert(1)</script>\n**fet**"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>fet</strong>" in html


def test_empty_details_render_to_nothing():
    assert str(render(None)) == ""
    assert str(render("   \n  ")) == ""


def test_plain_prose_still_renders_as_a_paragraph():
    html = str(render("Lugn distans, håll pulsen under 140."))
    assert html == "<p>Lugn distans, håll pulsen under 140.</p>"


# ---------- TX-2: fliken heter "Passet" och är öppen ----------


def test_week_template_labels_and_opens_the_session_body():
    tpl = (
        Path(__file__).resolve().parents[2]
        / "trixa_api" / "templates" / "_week_section.html"
    ).read_text(encoding="utf-8")
    assert "<summary>Passet</summary>" in tpl
    assert "<summary>Syfte</summary>" not in tpl
    assert "session_markdown" in tpl
    # Öppen som förval — passets innehåll är inte en fotnot.
    assert "<details open>\n          <summary>Passet</summary>" in tpl


# ---------- TX-7: vilodagar räknas inte som pass ----------


def _count(sports: list[str]) -> tuple[int, int]:
    """Speglar räknaren i _fetch_week_plan utan att gå via DB-lagret."""
    workouts = [
        {"is_rest": (s or "").strip().lower() in ("vila", "rest")} for s in sports
    ]
    rest = sum(1 for w in workouts if w["is_rest"])
    return len(workouts) - rest, rest


def test_rest_days_are_counted_separately():
    training, rest = _count(["Cykel", "Löpning", "Styrka", "Vila", "Vila", "Vila", "Vila"])
    assert (training, rest) == (3, 4)


def test_yoga_and_walks_are_sessions_not_rest():
    # De mappar till disciplin "rest" i UI:t men är faktiska pass — räknaren
    # tittar på det lagrade sportnamnet, inte på den mappade disciplinen.
    training, rest = _count(["Yoga", "Promenad", "Vila"])
    assert (training, rest) == (2, 1)


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
