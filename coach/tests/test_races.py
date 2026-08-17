"""Tester för tävlingskalender-helpern (public.races, 2026-07-02)."""

from datetime import date

from coach.engine.phases import AthleteState, determine_phase, transition_days_for
from coach.trixa.planner import _build_athlete_state, _last_race_info
from coach.trixa.races import fetch_last_race, fetch_next_a_race, fetch_upcoming_races


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def gte(self, _col, value):
        self._data = [r for r in self._data if r["date"] >= value]
        return self

    def lt(self, _col, value):
        self._data = [r for r in self._data if r["date"] < value]
        return self

    def order(self, _col, desc=False, **_k):
        self._data = sorted(self._data, key=lambda r: r["date"], reverse=desc)
        return self

    def limit(self, n):
        self._data = self._data[:n]
        return self

    def execute(self):
        return _FakeResult(self._data)


class _FakeClient:
    def __init__(self, data):
        self._data = data

    def table(self, _name):
        return _FakeQuery(list(self._data))


TODAY = date(2026, 7, 2)

RACES = [
    {"name": "Sprint i juli", "date": "2026-07-12", "priority": "C", "distance": "sprint"},
    {"name": "Olympisk i juli", "date": "2026-07-26", "priority": "B", "distance": "olympic"},
    {"name": "Ironman Kalmar", "date": "2026-08-15", "priority": "A", "distance": "full"},
    {"name": "Gammalt race", "date": "2025-09-20", "priority": "A", "distance": "full"},
]


def test_a_race_wins_over_earlier_b_and_c():
    race = fetch_next_a_race(_FakeClient(RACES), "athlete-x", TODAY)
    assert race["name"] == "Ironman Kalmar"


def test_fallback_to_b_when_no_a():
    no_a = [r for r in RACES if r["priority"] != "A"]
    race = fetch_next_a_race(_FakeClient(no_a), "athlete-x", TODAY)
    assert race["name"] == "Olympisk i juli"


def test_past_races_ignored():
    race = fetch_next_a_race(_FakeClient(RACES), "athlete-x", date(2026, 9, 1))
    assert race is None


def test_empty_calendar():
    assert fetch_next_a_race(_FakeClient([]), "athlete-x", TODAY) is None


def test_upcoming_sorted_by_priority_then_date():
    upcoming = fetch_upcoming_races(_FakeClient(RACES), "athlete-x", TODAY)
    assert [r["priority"] for r in upcoming] == ["A", "B", "C"]


def test_last_race_is_most_recent_past():
    race = fetch_last_race(_FakeClient(RACES), "athlete-x", date(2026, 8, 17))
    assert race["name"] == "Ironman Kalmar"


def test_last_race_none_when_no_history():
    assert fetch_last_race(_FakeClient([]), "athlete-x", TODAY) is None


def test_last_race_info_feeds_transition_rule():
    # IM 2026-08-15, idag 2026-08-17 → (2 dagar, full) → engine i transition.
    days, dist = _last_race_info(
        {"id": "athlete-x"}, date(2026, 8, 17), _FakeClient(RACES)
    )
    assert (days, dist) == (2, "full")
    assert _last_race_info({"id": "x"}, TODAY, None) == (None, None)


def test_transition_window_by_distance():
    assert transition_days_for("full") == 21      # IM → 3 v
    assert transition_days_for("half") == 14
    assert transition_days_for("sprint") == 7
    assert transition_days_for(None) == 14        # okänd → default


def test_engine_transition_respects_distance():
    base = dict(weekly_training_hours=10.0, weeks_until_next_race=52)
    # 16 dagar efter en full IM: fortfarande transition (21 d fönster)...
    rec = determine_phase(AthleteState(
        **base, last_race_completed_within_days=16, last_race_distance="full",
    ))
    assert rec.phase == "transition"
    # ...men efter en sprint är fönstret (7 d) passerat.
    rec = determine_phase(AthleteState(
        **base, last_race_completed_within_days=16, last_race_distance="sprint",
    ))
    assert rec.phase != "transition"


def test_floor_skipped_inside_transition_window():
    # Inom transition-fönstret får planerad volym gå under 5h-golvet.
    athlete = {"id": "athlete-x", "weekly_hours": 12}
    state = _build_athlete_state(
        athlete, None, date(2026, 8, 17),
        actual_weekly_hours=2.0, client=_FakeClient(RACES),
    )
    assert state.weekly_training_hours == 2.0
    assert state.last_race_completed_within_days == 2
    assert state.last_race_distance == "full"
    # Utan nyligt race gäller golvet.
    state = _build_athlete_state(
        athlete, None, date(2026, 8, 17), actual_weekly_hours=2.0, client=None,
    )
    assert state.weekly_training_hours == 5.0
