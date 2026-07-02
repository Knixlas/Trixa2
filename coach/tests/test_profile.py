"""Tester för den konsoliderade profil-läsvägen (2026-07-02)."""

from coach.engine.profile import (
    DEMO_PROFILE,
    load_profile,
    profile_from_athlete_row,
)


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

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _FakeResult(self._data)


class _FakeClient:
    def __init__(self, data):
        self._data = data

    def table(self, _name):
        return _FakeQuery(self._data)


NIKLAS_ROW = {
    "swim_css": "2:15",
    "ftp": 198,
    "lthr": 170,
    "run_threshold_pace": "5:15",
    "lthr_bike": 162,
    "max_hr": 185,
}


def test_parses_text_formats():
    p = profile_from_athlete_row(NIKLAS_ROW)
    assert p.css_sec_per_100m == 135.0
    assert p.threshold_pace_sec_per_km == 315.0
    assert p.ftp_watts == 198
    assert p.lthr_bike_bpm == 162
    assert p.max_hr_bpm == 185
    assert p.at_hr_run_bpm == 170


def test_tolerates_missing_and_numeric_values():
    p = profile_from_athlete_row({"swim_css": 130, "ftp": None})
    assert p.css_sec_per_100m == 130.0
    assert p.ftp_watts is None
    assert p.lthr_bike_bpm is None
    assert p.max_hr_bpm is None


def test_tolerates_garbage():
    p = profile_from_athlete_row({"swim_css": "snabbt", "run_threshold_pace": ""})
    assert p.css_sec_per_100m is None
    assert p.threshold_pace_sec_per_km is None


def test_load_profile_prefers_athlete_profiles():
    client = _FakeClient([NIKLAS_ROW])
    p = load_profile(athlete_user_id="uuid-x", client=client)
    assert p.ftp_watts == 198
    assert p is not DEMO_PROFILE


def test_load_profile_falls_back_when_row_missing():
    client = _FakeClient([])
    p = load_profile(athlete_user_id="uuid-x", client=client)
    # Faller till YAML-fixturen (athlete_config.example.yaml, FTP 220)
    # eller DEMO_PROFILE — aldrig krasch.
    assert p.ftp_watts is not None
