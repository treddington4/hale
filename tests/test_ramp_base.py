"""Ramp base: rolling median, returning-athlete seed, user seed.

Replaces "most recent nonzero week", which trusted a single week as gospel and was
fragile in BOTH directions — one light week restarted the ramp from scratch, one freak
big week inflated it. Measured against real production history (which genuinely swings
5.9 to 33.7 mi/wk):

    scenario              current (last nonzero)   median/6w
    normal                          28.3             24.4
    one light 5mi week               5.0  <- crater  24.4
    one freak 50mi week             50.0  <- inflate 30.6
"""
import json
from datetime import date, timedelta

import pytest

from app.models import UserTrainingConfig
from app.coach import generator


MONDAY = date(2026, 6, 1)  # a Monday; "target week" for every test below


@pytest.fixture()
def config(db, user_id):
    c = UserTrainingConfig(user_id=user_id, weekly_ramp_pct=3.0, mesocycle_pattern="3:1",
                           distribution="pyramidal", strength_days_per_week=2,
                           strength_template="full_body_ab", ride_days_per_week=0)
    db.add(c)
    db.commit()
    return c


@pytest.fixture()
def seed_weeks(db, make_activity):
    """Seed one run per listed week. `weeks` maps weeks-before-MONDAY -> miles."""
    def _seed(weeks: dict, activity_type="Run"):
        for i, mi in weeks.items():
            if mi <= 0:
                continue
            wk = MONDAY - timedelta(weeks=i)
            make_activity(f"strava_{activity_type}_{i}", activity_type,
                          date=wk.isoformat(), distance_mi=mi, moving_time_sec=int(mi * 540))
    return _seed


def _base(db, user_id, config=None, activity_type="Run"):
    return generator._ramp_base_mileage(db, user_id, MONDAY, activity_type, config)


# ---------- the core fix: robustness in both directions ----------


def test_single_light_week_does_not_crater_the_base(db, user_id, config, seed_weeks):
    """The bug this replaces. Five solid weeks then one light one — the light week must
    not become the ramp base."""
    seed_weeks({6: 30.0, 5: 32.0, 4: 28.0, 3: 31.0, 2: 29.0, 1: 5.0})
    base, cold = _base(db, user_id, config)
    assert cold is False
    assert base >= 28.0, f"a single light week dragged the base to {base}"


def test_single_huge_week_does_not_inflate_the_base(db, user_id, config, seed_weeks):
    """Symmetric failure, equally real: one outlier long week must not become the base
    the next week ramps 3% ON TOP of."""
    seed_weeks({6: 30.0, 5: 32.0, 4: 28.0, 3: 31.0, 2: 29.0, 1: 50.0})
    base, _ = _base(db, user_id, config)
    assert base <= 35.0, f"one freak week inflated the base to {base}"


def test_sustained_reduction_does_lower_the_base(db, user_id, config, seed_weeks):
    """A median must still track a genuine multi-week decline — otherwise it would just
    be a high-water mark that never comes down."""
    seed_weeks({6: 30.0, 5: 30.0, 4: 8.0, 3: 7.0, 2: 8.0, 1: 7.0})
    base, _ = _base(db, user_id, config)
    assert base <= 12.0, f"sustained reduction was ignored; base still {base}"


def test_zero_weeks_are_excluded_not_averaged(db, user_id, config, seed_weeks):
    """A complete gap (no bike on holiday, a week fully off) must not drag the base down
    — "didn't train" is not "trained a little". Preserves the Phase 14 distinction."""
    seed_weeks({6: 30.0, 5: 30.0, 4: 30.0, 3: 0, 2: 0, 1: 0})
    base, cold = _base(db, user_id, config)
    assert cold is False
    assert base == 30.0, f"zero weeks were averaged in, giving {base}"


def test_honeymoon_scenario_running(db, user_id, config, seed_weeks):
    """The concrete case: solid running, then two token weeks abroad. Coming home must
    not restart the ramp from 4 miles."""
    seed_weeks({6: 30.0, 5: 31.0, 4: 29.0, 3: 30.0, 2: 4.0, 1: 4.0})
    base, _ = _base(db, user_id, config)
    assert base >= 25.0, f"post-honeymoon base cratered to {base}"


# ---------- seeds ----------


def test_returning_athlete_seeded_from_older_history(db, user_id, config, seed_weeks):
    """Real history further back means a returning athlete, not a beginner. Resuming at
    the full old volume would ignore detraining; restarting at the 3-mile absolute-
    beginner floor is worse. Land between, and never above the old level."""
    seed_weeks({20: 30.0, 19: 30.0, 18: 30.0})  # well outside the 6-week window
    base, cold = _base(db, user_id, config)
    assert cold is False, "an athlete with a year of history is not a cold start"
    assert 0 < base < 30.0
    assert base == pytest.approx(30.0 * generator.RETURNING_ATHLETE_FRACTION)


def test_user_seed_used_when_no_history_at_all(db, user_id, config):
    """Someone who already rides 20mi/wk on a bike this app has never synced."""
    config.seed_weekly_miles_json = json.dumps({"Ride": 20.0})
    db.commit()
    base, cold = generator._ramp_base_mileage(db, user_id, MONDAY, "Ride", config)
    assert cold is False
    assert base == 20.0


def test_real_history_beats_a_user_seed(db, user_id, config, seed_weeks):
    """A seed is a starting guess, not an override — real data must always win."""
    config.seed_weekly_miles_json = json.dumps({"Run": 99.0})
    db.commit()
    seed_weeks({3: 20.0, 2: 22.0, 1: 21.0})
    base, _ = _base(db, user_id, config)
    assert base <= 25.0, f"user seed overrode real history, giving {base}"


def test_true_cold_start_still_cold(db, user_id, config):
    """No history, no seed — the conservative linear ramp must still apply."""
    base, cold = _base(db, user_id, config)
    assert cold is True
    assert base == 0.0


def test_malformed_seed_is_ignored_not_crashed(db, user_id, config):
    for bad in ('{"Run": "abc"}', "not json at all", '["Run", 30]', '{"Run": -5}', "{}"):
        config.seed_weekly_miles_json = bad
        db.commit()
        base, cold = _base(db, user_id, config)
        assert cold is True and base == 0.0, f"bad seed {bad!r} was not ignored"


def test_no_config_is_safe(db, user_id, seed_weeks):
    """_ramp_base_mileage is called with config=None in places; must not blow up."""
    seed_weeks({2: 20.0, 1: 22.0})
    base, cold = generator._ramp_base_mileage(db, user_id, MONDAY, "Run", None)
    assert cold is False and base > 0


# ---------- the one-pass bucketing ----------


def test_weekly_mileage_map_matches_per_week_lookup(db, user_id, seed_weeks):
    """The bucketed map exists purely for speed — it must agree exactly with the
    per-week function it replaces, including its cross-source deduplication."""
    seed_weeks({4: 12.0, 3: 0, 2: 18.5, 1: 7.25})
    wmap = generator.weekly_mileage_map(db, user_id, "Run")
    for i in (1, 2, 3, 4):
        wk = MONDAY - timedelta(weeks=i)
        assert wmap.get(wk, 0.0) == pytest.approx(
            generator._week_mileage(db, user_id, wk, "Run")), f"mismatch at week -{i}"


def test_weekly_mileage_map_deduplicates(db, user_id, make_activity):
    """Same physical run from both sources counts once, same as _week_mileage."""
    wk = MONDAY - timedelta(weeks=1)
    make_activity("strava_dup", "Run", source="strava", date=wk.isoformat(),
                  start_time="17:00", distance_mi=6.0)
    make_activity("garmin_dup", "Run", source="garmin", date=wk.isoformat(),
                  start_time="17:00", distance_mi=6.0)
    assert generator.weekly_mileage_map(db, user_id, "Run").get(wk) == 6.0


# ---------- deload weeks ----------


def test_deload_weeks_excluded_from_ramp_base(db, user_id, config, seed_weeks, monkeypatch):
    """A deload is a PLANNED reduction — it describes the schedule, not the athlete's
    capacity. Counting it as ramp base makes every mesocycle ratchet the plan downward:
    _compute_weekly_budget cuts the deload week by 0.75, then the next block builds 3% up
    from that deflated number instead of resuming at the pre-deload level."""
    from app.coach import generator

    # Force weeks 2 and 5 back to read as deloads, independent of the real epoch phase.
    deload_weeks = {MONDAY - timedelta(weeks=2), MONDAY - timedelta(weeks=5)}
    monkeypatch.setattr(generator, "_is_deload_week", lambda cfg, wk: wk in deload_weeks)

    seed_weeks({6: 30.0, 5: 15.0, 4: 32.0, 3: 31.0, 2: 15.0, 1: 30.0})
    base, cold = _base(db, user_id, config)
    assert cold is False
    # Median of the non-deload weeks (30, 32, 31, 30) is ~30.5; including the deloads
    # would drag it toward the mid-20s.
    assert base >= 29.0, f"deload weeks dragged the base to {base}"


def test_all_deload_window_falls_back_rather_than_cold_starting(db, user_id, config, seed_weeks, monkeypatch):
    """Someone training normally must never read as a cold start just because every week
    in the window happened to be marked deload."""
    from app.coach import generator
    monkeypatch.setattr(generator, "_is_deload_week", lambda cfg, wk: True)

    seed_weeks({4: 25.0, 3: 24.0, 2: 26.0, 1: 25.0})
    base, cold = _base(db, user_id, config)
    assert cold is False, "an all-deload window must not fabricate a cold start"
    assert base > 20.0
