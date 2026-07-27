"""Estimating distance for HR + duration prescriptions.

Garmin's adaptive plan prescribes heart rate and time and leaves distance null, so a
planned session shows no distance at all. These pin the estimator that fills that gap.
"""
from datetime import date, timedelta

import pytest

from app import stats
from app.sync.garmin_sync import _parse_target_hr_bpm


TODAY = date(2026, 7, 27)


@pytest.fixture()
def runs_at(db, make_activity):
    """Seed runs `days_ago` back with a given avg_hr and pace."""
    def _seed(entries):
        for i, (days_ago, hr, pace_sec) in enumerate(entries):
            d = TODAY - timedelta(days=days_ago)
            make_activity(f"strava_hr_{i}", "Run", date=d.isoformat(),
                          distance_mi=5.0, moving_time_sec=int(pace_sec * 5),
                          avg_hr=hr, avg_pace_sec_per_mi=pace_sec)
    return _seed


# ---------- parsing Garmin's free-text target ----------


@pytest.mark.parametrize("desc,expected", [
    ("143bpm", 143),
    ("Base — 143bpm", 143),
    ("150 bpm", 150),
    ("Tempo 165BPM", 165),
    (None, None),
    ("", None),
    ("no numbers here", None),
    ("2x5x0:40@Very Hard", None),   # interval notation, no bpm — must not misread
    ("12bpm", None),                # implausible, rejected
    ("400bpm", None),               # implausible, rejected
])
def test_parse_target_hr(desc, expected):
    assert _parse_target_hr_bpm(desc) == expected


# ---------- recency weighting ----------


def test_recent_runs_dominate_old_ones(db, user_id, runs_at):
    """The whole reason this is weighted. Measured on real data, efficiency factor rose
    81% over four years — the same HR that once gave ~17:00/mi now gives ~9:53/mi, so an
    old run is not merely less relevant, it is actively misleading."""
    runs_at([
        (350, 145, 1020.0), (340, 146, 1020.0), (330, 145, 1020.0), (320, 147, 1020.0),  # ~17:00/mi, a year ago
        (7, 145, 600.0), (14, 146, 600.0), (21, 145, 600.0), (28, 147, 600.0),           # ~10:00/mi, recent
    ])
    est = stats.estimate_pace_at_hr(db, 146, user_id=user_id, as_of=TODAY)
    assert est is not None
    # A plain mean would land near 13:30/mi; recency weighting must sit near the recent runs.
    assert est["basePaceSecPerMi"] < 700, f"old runs dominated: {est['basePaceSecPerMi']}"


def test_returns_none_without_enough_data(db, user_id, runs_at):
    """Never fabricate an estimate from one or two runs."""
    runs_at([(7, 145, 600.0)])
    assert stats.estimate_pace_at_hr(db, 145, user_id=user_id, as_of=TODAY) is None


def test_band_widens_when_tight_band_is_sparse(db, user_id, runs_at):
    runs_at([(7, 132, 600.0), (14, 133, 610.0), (21, 158, 590.0), (28, 159, 600.0)])
    est = stats.estimate_pace_at_hr(db, 145, user_id=user_id, as_of=TODAY)
    assert est is not None
    assert est["hrBandBpm"] == stats.PACE_AT_HR_WIDE_BAND_BPM
    assert est["sampleSize"] == 4


def test_treadmill_runs_excluded(db, user_id, make_activity):
    """Treadmill pace is set by the belt, not by the athlete's response to effort, so it
    says nothing about outdoor pace at a given HR."""
    for i in range(6):
        make_activity(f"tm_{i}", "Run", date=(TODAY - timedelta(days=i + 1)).isoformat(),
                      distance_mi=5.0, moving_time_sec=3000, avg_hr=145,
                      avg_pace_sec_per_mi=600.0, is_treadmill=True)
    assert stats.estimate_pace_at_hr(db, 145, user_id=user_id, as_of=TODAY) is None


# ---------- heat ----------


def test_heat_multiplier_is_neutral_below_threshold_and_when_unknown():
    assert stats.heat_pace_multiplier(None) == 1.0
    assert stats.heat_pace_multiplier(50.0) == 1.0
    assert stats.heat_pace_multiplier(stats.HEAT_NEUTRAL_F) == 1.0


def test_heat_multiplier_slows_pace_and_is_capped():
    assert stats.heat_pace_multiplier(70.0) > 1.0
    assert stats.heat_pace_multiplier(90.0) > stats.heat_pace_multiplier(70.0)
    assert stats.heat_pace_multiplier(200.0) == pytest.approx(1.0 + stats.HEAT_MAX_PENALTY)


def test_heat_makes_the_estimate_slower(db, user_id, runs_at):
    runs_at([(7, 145, 600.0), (14, 146, 600.0), (21, 145, 600.0), (28, 147, 600.0)])
    cool = stats.estimate_pace_at_hr(db, 146, heat_index_f=55.0, user_id=user_id, as_of=TODAY)
    hot = stats.estimate_pace_at_hr(db, 146, heat_index_f=85.0, user_id=user_id, as_of=TODAY)
    assert hot["paceSecPerMi"] > cool["paceSecPerMi"]
    # The unadjusted figure is reported alongside, so the UI can show what's the athlete
    # and what's the weather rather than blending them into one opaque number.
    assert cool["basePaceSecPerMi"] == hot["basePaceSecPerMi"]


# ---------- distance ----------


def test_distance_from_hr_and_duration(db, user_id, runs_at):
    runs_at([(7, 145, 600.0), (14, 146, 600.0), (21, 145, 600.0), (28, 147, 600.0)])
    est = stats.estimate_workout_distance(db, 146, 3600, user_id=user_id, as_of=TODAY)
    assert est is not None
    assert est["distanceMi"] == pytest.approx(6.0, abs=0.2)  # 60 min at ~10:00/mi


def test_distance_none_when_inputs_missing(db, user_id, runs_at):
    runs_at([(7, 145, 600.0), (14, 146, 600.0), (21, 145, 600.0), (28, 147, 600.0)])
    assert stats.estimate_workout_distance(db, 146, 0, user_id=user_id, as_of=TODAY) is None
    assert stats.estimate_workout_distance(db, 0, 3600, user_id=user_id, as_of=TODAY) is None
