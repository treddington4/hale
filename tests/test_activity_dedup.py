"""Regression tests guarding the Strava+Garmin double-count trap.

THE TRAP, stated plainly:

`stats._all_runs(activity_type="Run")` filters with an EXACT string match. The
Garmin account auto-forwards every activity to Strava, so most real runs exist
twice in the table: once as Strava's `"Run"`, once as Garmin's `"running"`. The
exact-match filter silently drops the Garmin copy — so today's run totals are
correct *by accident*. Deduplication is a side effect of a string comparison
nobody wrote for that purpose.

Measured against the real database: 342 run-family rows across 4 spellings, but
only 205 counted; 135 dates carry both spellings and ZERO dates carry a Garmin
spelling alone. So the moment P4 makes `"running"` normalize to `"run"` — the
obviously-correct-looking change — every run statistic inflates ~67% with
nothing to catch it.

These tests assert the DESIRED INVARIANT (one physical activity counts once),
not the current mechanism. They pass today because the exact-match filter
happens to produce that result, and must still pass after P3/P4 replace that
accident with real dedup + normalization. A failure here after P4 means the
dedup was forgotten.
"""
import pytest

from app import stats


def test_strava_garmin_duplicate_pair_counts_once(db, make_activity, user_id):
    """The core invariant: one physical run synced from both sources under two
    different spellings contributes to aggregates exactly once."""
    make_activity(id="strava_1", source="strava", activity_type="Run",
                  date="2026-07-20", start_time="08:00", distance_mi=5.0, moving_time_sec=2700)
    make_activity(id="garmin_1", source="garmin", activity_type="running",
                  date="2026-07-20", start_time="08:00", distance_mi=5.0, moving_time_sec=2700)

    summary = stats.run_summary(db, user_id=user_id)

    assert summary["runCount"] == 1, (
        "Strava+Garmin copies of ONE run were counted separately — the double-count "
        "regression this file exists to prevent."
    )
    assert summary["totalDistanceMi"] == pytest.approx(5.0)
    assert summary["totalMovingTimeSec"] == 2700


def test_treadmill_and_trail_spellings_also_dedupe(db, make_activity, user_id):
    """Garmin uses `treadmill_running`/`trail_running` too — all normalize into the
    run family, and all are duplicates of a Strava `"Run"` row."""
    make_activity(id="strava_2", source="strava", activity_type="Run",
                  date="2026-07-21", start_time="07:00", distance_mi=3.0, moving_time_sec=1800)
    make_activity(id="garmin_2", source="garmin", activity_type="treadmill_running",
                  date="2026-07-21", start_time="07:00", distance_mi=3.0, moving_time_sec=1800)

    summary = stats.run_summary(db, user_id=user_id)
    assert summary["runCount"] == 1
    assert summary["totalDistanceMi"] == pytest.approx(3.0)


def test_two_genuinely_separate_runs_are_both_counted(db, make_activity, user_id):
    """Over-correction guard: dedup must not collapse a real double-run day.
    Same date, but hours apart and different distances."""
    make_activity(id="strava_3", source="strava", activity_type="Run",
                  date="2026-07-22", start_time="06:00", distance_mi=4.0, moving_time_sec=2160)
    make_activity(id="strava_4", source="strava", activity_type="Run",
                  date="2026-07-22", start_time="18:00", distance_mi=6.0, moving_time_sec=3240)

    summary = stats.run_summary(db, user_id=user_id)
    assert summary["runCount"] == 2
    assert summary["totalDistanceMi"] == pytest.approx(10.0)


def test_distinct_days_are_not_collapsed(db, make_activity, user_id):
    """Identical-looking runs on different days are different runs."""
    make_activity(id="strava_5", source="strava", activity_type="Run", date="2026-07-23")
    make_activity(id="strava_6", source="strava", activity_type="Run", date="2026-07-24")

    summary = stats.run_summary(db, user_id=user_id)
    assert summary["runCount"] == 2


@pytest.mark.xfail(
    reason="KNOWN GAP, documented not accidental — P4 fixes this. A Garmin-only run "
           "(no Strava counterpart) is silently excluded from every stat by the "
           "exact-match \"Run\" filter. Harmless on the current real dataset "
           "(verified: 0 such rows, because Garmin auto-forwards to Strava) but it "
           "breaks the moment that forwarding is disconnected or a device syncs only "
           "to Garmin. Remove this marker in P4.",
    strict=True,
)
def test_garmin_only_run_is_still_counted(db, make_activity, user_id):
    make_activity(id="garmin_solo", source="garmin", activity_type="running",
                  date="2026-07-25", distance_mi=7.0, moving_time_sec=3780)

    summary = stats.run_summary(db, user_id=user_id)
    assert summary["runCount"] == 1
    assert summary["totalDistanceMi"] == pytest.approx(7.0)


def test_non_run_activities_excluded_from_run_summary(db, make_activity, user_id):
    """Pins today's intended scoping so P6 (generalizing stats off "Run") is a
    deliberate, visible change rather than an accidental one. When P6 lands and
    run_summary's default broadens, updating this test is the signal that the
    behavior change was intended."""
    make_activity(id="strava_run", source="strava", activity_type="Run", date="2026-07-20")
    make_activity(id="garmin_lift", source="garmin", activity_type="strength_training",
                  date="2026-07-20", start_time="17:00", distance_mi=0.0,
                  moving_time_sec=3600, avg_pace_sec_per_mi=None)

    summary = stats.run_summary(db, user_id=user_id)
    assert summary["runCount"] == 1, "a strength session leaked into the run-only summary"
