"""P21-lite — the once-weekly secondary ride folded into the existing rotation.

Deliberately NOT a second training plan: it reuses the skeleton's cross_train slot so
it costs no extra training day, and shares the one weekly schedule. See
docs/P21_CONCURRENT_GOALS.md for the full multi-goal model this is a first step toward.
"""
from datetime import date, timedelta

import pytest

from app.models import UserTrainingConfig, Workout
from app.coach import generator


def _crosstrain_day(config_date: date) -> date:
    """The one weekday WEEKDAY_SKELETON marks cross_train, resolved from the skeleton
    itself rather than hardcoded — so this doesn't silently rot if the skeleton changes."""
    wanted = [d for d, t in generator.WEEKDAY_SKELETON.items() if t == "cross_train"]
    assert len(wanted) == 1, f"expected exactly one cross_train slot, got {wanted}"
    monday = config_date - timedelta(days=config_date.weekday())
    return monday + timedelta(days=wanted[0])


@pytest.fixture()
def config(db, user_id):
    c = UserTrainingConfig(user_id=user_id, weekly_ramp_pct=3.0, mesocycle_pattern="3:1",
                           distribution="pyramidal", strength_days_per_week=2,
                           strength_template="full_body_ab", ride_days_per_week=0)
    db.add(c)
    db.commit()
    return c


def test_off_by_default(db, user_id, config):
    """A user who never touches this setting must see exactly the old behavior."""
    assert config.ride_days_per_week == 0
    assert generator._is_ride_day(config, _crosstrain_day(date(2026, 6, 1))) is False


def test_ride_day_is_the_crosstrain_slot(db, user_id, config):
    config.ride_days_per_week = 1
    db.commit()
    ct = _crosstrain_day(date(2026, 6, 1))
    assert generator._is_ride_day(config, ct) is True
    # Every other day of that week stays a non-ride day.
    monday = ct - timedelta(days=ct.weekday())
    others = [monday + timedelta(days=i) for i in range(7) if (monday + timedelta(days=i)) != ct]
    assert [generator._is_ride_day(config, d) for d in others] == [False] * 6


def test_once_weekly_session_is_not_sliced_by_day_share(db, user_id, config, make_activity):
    """Regression for a 0.3-mile "bike ride". day_share's fractions assume a discipline
    trained ~6-7x/week; for a once-weekly one the weekly budget IS the session. Confirmed
    against real production data before the fix: a 3.1mi weekly ride budget * 0.10 easy
    share = 0.3mi, roughly 90 seconds of cycling."""
    config.ride_days_per_week = 1
    db.commit()
    assert generator._sessions_per_week(config, "Ride") == 1
    # Running has no configured frequency — it must keep the normal day_share slicing.
    assert generator._sessions_per_week(config, "Run") is None


def test_ride_has_a_discipline_specific_distance_floor(db, user_id, config, make_activity):
    """3 miles is a real easy run and a meaningless bike ride. The generator's distance
    math is running-calibrated throughout, so a ride needs its own floor."""
    assert generator.MIN_SESSION_MILES["Ride"] >= 5.0
    assert "Run" not in generator.MIN_SESSION_MILES, "running must keep its computed value"

    config.ride_days_per_week = 1
    db.commit()
    ct = _crosstrain_day(date.today() + timedelta(weeks=1))
    # Tiny real ride history — exactly the case that produced 0.3mi.
    prev = ct - timedelta(weeks=1)
    make_activity("strava_r1", "Ride", date=prev.isoformat(), distance_mi=3.0, moving_time_sec=1200)

    result = generator._generate_endurance(
        db, user_id, ct, {"flags": [], "score": None}, config,
        activity_type="Ride", ignore_schedule=True, dry_run=True)
    assert result is not None
    assert result["activityType"] == "Ride"
    assert result["targetDistanceMi"] >= generator.MIN_SESSION_MILES["Ride"], \
        f"ride prescribed below its floor: {result['targetDistanceMi']}"


def test_placeholder_cross_train_is_cleared(db, user_id, config):
    """Opting in must not leave both the old empty placeholder and the new ride on the
    same day — they occupy different upsert domains, so nothing else would remove it."""
    from app.coach import core as coach
    ct = _crosstrain_day(date.today() + timedelta(weeks=1))
    coach.create_workout(db, ct.isoformat(), user_id=user_id, source="generator",
                         workout_type="cross_train", activity_type="Other")
    assert db.query(Workout).filter(Workout.scheduled_date == ct.isoformat()).count() == 1

    generator._clear_placeholder_cross_train(db, user_id, ct.isoformat())
    assert db.query(Workout).filter(Workout.scheduled_date == ct.isoformat()).count() == 0


def test_completed_cross_train_is_never_deleted(db, user_id, config):
    """History is immutable — the same rule _upsert_generator_workout already follows."""
    from app.coach import core as coach
    ct = _crosstrain_day(date.today() - timedelta(weeks=1))
    w = coach.create_workout(db, ct.isoformat(), user_id=user_id, source="generator",
                             workout_type="cross_train", activity_type="Other")
    row = db.get(Workout, w["id"])
    row.status = "completed"
    db.commit()

    generator._clear_placeholder_cross_train(db, user_id, ct.isoformat())
    assert db.get(Workout, w["id"]) is not None, "a completed session must survive"
