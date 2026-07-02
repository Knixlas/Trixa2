"""Tester för tävlingskalender-helpern (public.races, 2026-07-02)."""

from datetime import date

from coach.trixa.races import fetch_next_a_race, fetch_upcoming_races


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

    def order(self, _col, **_k):
        self._data = sorted(self._data, key=lambda r: r["date"])
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
    {"name": "Sprint i juli", "date": "2026-07-12", "priority": "C"},
    {"name": "Olympisk i juli", "date": "2026-07-26", "priority": "B"},
    {"name": "Ironman Kalmar", "date": "2026-08-15", "priority": "A"},
    {"name": "Gammalt race", "date": "2025-09-20", "priority": "A"},
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
