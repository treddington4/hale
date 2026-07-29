"""Garmin adaptive-plan suggestion upsert (coach.sync_garmin_suggested_workouts).

Both behaviours here come from one real production row. Its notes field had accumulated
FIVE identical "Garmin adaptive plan: Base — 143bpm" lines, plus a revision reading
"revised on 07-27 — was: easy, 76min" immediately followed by "revised on 07-28 — was:
easy, 76min" — a 76min session revised to 76min. Garmin reissues an unchanged suggestion
under a fresh uuid, and the uuid was the only thing being compared.
"""
import uuid

import pytest

from app.models import Workout
from app.coach import core as coach


def _entry(date_iso, uuid_str, workout_type="easy", duration_sec=4560, hr=143,
           notes="Garmin adaptive plan: Base — 143bpm"):
    return {"scheduledDate": date_iso, "workoutType": workout_type, "activityType": "Run",
            "targetDurationSec": duration_sec, "targetHrBpm": hr, "notes": notes,
            "garminWorkoutUuid": uuid_str}


def _row(db, user_id, date_iso):
    return (db.query(Workout)
            .filter(Workout.scheduled_date == date_iso, Workout.source == "garmin")
            .first())


DATE = "2026-07-28"


def test_reissued_identical_suggestion_is_not_logged_as_a_revision(db, user_id):
    """The core bug: a new uuid for an unchanged session is not a revision."""
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-1")], user_id=user_id)
    before = _row(db, user_id, DATE).notes

    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-2")], user_id=user_id)
    row = _row(db, user_id, DATE)
    assert "Revised" not in (row.notes or ""), f"claimed a revision that didn't happen: {row.notes!r}"
    assert row.notes == before
    # The new id is still recorded, or the same non-change is re-detected forever.
    assert row.garmin_workout_uuid == "uuid-2"


def test_a_genuine_change_is_still_recorded(db, user_id):
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-1", duration_sec=4560)], user_id=user_id)
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-2", duration_sec=3300)], user_id=user_id)
    row = _row(db, user_id, DATE)
    assert "Revised" in row.notes
    assert "76min" in row.notes, "the previous duration should be preserved"
    assert row.target_duration_sec == 3300


def test_the_description_line_is_not_duplicated_per_revision(db, user_id):
    """Five identical copies on one row is what motivated this."""
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-1", duration_sec=3420)], user_id=user_id)
    for i, dur in enumerate([4560, 3300, 3060], start=2):
        coach.sync_garmin_suggested_workouts(db, [_entry(DATE, f"uuid-{i}", duration_sec=dur)], user_id=user_id)
    row = _row(db, user_id, DATE)
    assert row.notes.count("Garmin adaptive plan: Base") == 1, f"duplicated: {row.notes!r}"
    # ...while every real revision is still there.
    assert row.notes.count("Revised") == 3


def test_a_changed_description_is_still_added(db, user_id):
    """Deduping must not swallow genuinely new text."""
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-1")], user_id=user_id)
    coach.sync_garmin_suggested_workouts(
        db, [_entry(DATE, "uuid-2", duration_sec=3300, notes="Garmin adaptive plan: Tempo — 165bpm")],
        user_id=user_id)
    row = _row(db, user_id, DATE)
    assert "Tempo — 165bpm" in row.notes
    assert "Base — 143bpm" in row.notes


def test_a_completed_session_is_never_rewritten(db, user_id):
    """Pre-existing rule, pinned so the new early-exit can't break it: history is
    immutable, a later Garmin revision must not rewrite what was actually done."""
    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-1")], user_id=user_id)
    row = _row(db, user_id, DATE)
    row.status = "completed"
    db.commit()

    coach.sync_garmin_suggested_workouts(db, [_entry(DATE, "uuid-2", duration_sec=999)], user_id=user_id)
    row = _row(db, user_id, DATE)
    assert row.status == "completed"
    assert row.target_duration_sec == 4560
    assert row.garmin_workout_uuid == "uuid-1"
