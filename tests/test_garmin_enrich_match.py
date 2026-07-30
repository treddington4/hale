"""garmin_enrich.find_matching_garmin_run — the Hevy->Garmin match.

Pins the P4 normalization trap. The matcher filtered on the literal source spelling
`activity_type == "strength_training"`, but P4 normalizes activity_type to canonical
lowercase at write time, so real Garmin strength rows are stored as "strength". Measured
against production: 27 Garmin strength rows, 0 matching that filter — so every Hevy
workout silently found no Garmin activity and was never enriched. Same failure already
pinned for _week_mileage, in a place that had no test.
"""
import pytest

from app.models import Run
from app.sync import garmin_enrich


@pytest.fixture()
def hevy_run(db, user_id, make_activity):
    return make_activity("hevy_x", "strength", source="hevy", date="2026-07-29",
                         start_time="18:05", distance_mi=0.0)


def test_matches_a_normalized_garmin_strength_row(db, user_id, hevy_run, make_activity):
    """The regression: canonical "strength" is what's actually stored."""
    make_activity("garmin_x", "strength", source="garmin", date="2026-07-29",
                  start_time="18:10", distance_mi=0.0)
    match = garmin_enrich.find_matching_garmin_run(db, hevy_run, user_id)
    assert match is not None, "a real, normalized Garmin strength row was not matched"
    assert match.id == "garmin_x"


def test_still_matches_a_legacy_unnormalized_row(db, user_id, hevy_run, make_activity):
    """Rows written before P4 kept the raw source spelling — both must match."""
    make_activity("garmin_legacy", "strength_training", source="garmin", date="2026-07-29",
                  start_time="18:10", distance_mi=0.0)
    assert garmin_enrich.find_matching_garmin_run(db, hevy_run, user_id) is not None


def test_a_run_is_never_matched_as_strength(db, user_id, hevy_run, make_activity):
    """Loosening the filter must not let an unrelated activity match."""
    make_activity("garmin_run", "Run", source="garmin", date="2026-07-29",
                  start_time="18:10", distance_mi=5.0)
    assert garmin_enrich.find_matching_garmin_run(db, hevy_run, user_id) is None


def test_a_far_apart_session_is_not_matched(db, user_id, hevy_run, make_activity):
    """The time window still has to bound it — two genuinely separate sessions on one
    day must not be conflated."""
    make_activity("garmin_far", "strength", source="garmin", date="2026-07-29",
                  start_time="06:00", distance_mi=0.0)
    assert garmin_enrich.find_matching_garmin_run(db, hevy_run, user_id) is None


def test_closest_candidate_wins(db, user_id, hevy_run, make_activity):
    make_activity("garmin_near", "strength", source="garmin", date="2026-07-29",
                  start_time="18:08", distance_mi=0.0)
    make_activity("garmin_less_near", "strength", source="garmin", date="2026-07-29",
                  start_time="18:20", distance_mi=0.0)
    assert garmin_enrich.find_matching_garmin_run(db, hevy_run, user_id).id == "garmin_near"
