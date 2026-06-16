from coach.trixa.planner import _profile_sports, _resolve_activity_sources


def test_empty_sports_is_respected_as_explicit_choice():
    assert _profile_sports({"sports": []}) == []


def test_missing_sports_uses_legacy_triathlon_default():
    assert _profile_sports({}) == ["swim", "bike", "run"]


def test_strava_reserve_does_not_disable_tp_recovery_cache():
    garmin_id, strava_uid = _resolve_activity_sources(
        {"user_id": "u1", "garmin_athlete_id": "g1", "use_strava": True}
    )

    assert garmin_id == "g1"
    assert strava_uid == "u1"
