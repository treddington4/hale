"""P20 — plan view (generator.plan_week_view / project_week_series).

The behavior most worth pinning here isn't the arithmetic — it's the *attribution* rule.
`WeeklyPlan` is a single global stream per user, not goal-scoped: its row for a given
week belongs to whatever goal won `nearest_active_race_goal` at the time. A plan for a
different goal that read those rows would render another goal's target/deload/frozen as
its own, silently and plausibly. That's the failure this module exists to prevent.
"""
import uuid
from datetime import date, timedelta

import pytest

from app.models import Goal, TrainingPlan, PlanGoal, WeeklyPlan, UserTrainingConfig
from app.coach import generator


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@pytest.fixture()
def today():
    return date.today()


@pytest.fixture()
def make_goal(db, user_id):
    def _make(name, target_date, goal_type="race", status="active", periodizes_training=None):
        g = Goal(
            id=f"goal_{uuid.uuid4().hex[:12]}", user_id=user_id, goal_type=goal_type,
            name=name, status=status, target_date=target_date.isoformat(),
            periodizes_training=periodizes_training, created_at="2026-01-01T00:00:00+00:00",
        )
        db.add(g)
        db.commit()
        return g
    return _make


@pytest.fixture()
def make_plan(db, user_id):
    """P21: TrainingPlan no longer carries goal_id directly — a goal joins via PlanGoal.
    Returns the plan (not the PlanGoal), matching what real callers hold; tests pass the
    goal explicitly to plan_week_view/project_week_series, same as the route layer does."""
    def _make(goal, role="primary"):
        p = TrainingPlan(
            id=f"plan_{uuid.uuid4().hex[:12]}", user_id=user_id,
            status="active", created_at="2026-01-01T00:00:00+00:00",
        )
        db.add(p)
        db.flush()
        db.add(PlanGoal(id=f"plangoal_{uuid.uuid4().hex[:12]}", training_plan_id=p.id,
                        goal_id=goal.id, role=role, created_at="2026-01-01T00:00:00+00:00"))
        db.commit()
        return p
    return _make


@pytest.fixture()
def training_config(db, user_id):
    c = UserTrainingConfig(user_id=user_id)
    db.add(c)
    db.commit()
    return c


# ---------- activity_type casing (shipped regression, P4 -> P20) ----------


def test_week_mileage_matches_regardless_of_caller_casing(db, user_id, make_activity):
    """Regression. P4 made the stored activity_type canonical-lowercase at write time,
    but every caller in generator.py still passes source-style "Run". The equality filter
    silently matched nothing, so _week_mileage returned 0.0 for every week in existence.

    Consequence was not cosmetic: _last_nonzero_week_mileage therefore always reported a
    cold start, and the nightly generator prescribed ~3-6 mile weeks to an athlete running
    ~52. Verified against production before the fix. Both spellings must resolve here."""
    wk = date(2026, 6, 1)  # a Monday
    make_activity("strava_a", "Run", date=wk.isoformat(), distance_mi=6.0)
    make_activity("strava_b", "running", date=(wk + timedelta(days=3)).isoformat(), distance_mi=4.0)

    assert generator._week_mileage(db, user_id, wk, "Run") == 10.0
    assert generator._week_mileage(db, user_id, wk, "run") == 10.0
    assert generator._week_mileage(db, user_id, wk, "running") == 10.0


def test_week_mileage_deduplicates_cross_source_pairs(db, user_id, make_activity):
    """Regression, unmasked by fixing the casing bug above (both bugs together
    returned 0.0 either way, so casing hid this completely until it was fixed).

    Strava and Garmin each write their own copy of the same physical run — CLAUDE.md
    documents this is never deduplicated in storage, and every mileage consumer must
    route through stats._all_runs()/merge_duplicate_runs() before summing. This
    function queried Run directly and summed both copies: confirmed in production, a
    week that was really ~28mi (one real run per day, Strava+Garmin both syncing it)
    read as ~52mi — roughly double, prescribing volume proportional to a runner who
    doesn't exist."""
    wk = date(2026, 6, 1)  # a Monday
    make_activity("strava_a", "Run", source="strava", date=wk.isoformat(),
                  start_time="17:00", distance_mi=5.0)
    make_activity("garmin_a", "Run", source="garmin", date=wk.isoformat(),
                  start_time="17:00", distance_mi=5.0)  # same physical run, both sources

    assert generator._week_mileage(db, user_id, wk, "Run") == 5.0


def test_established_athlete_is_not_treated_as_cold_start(db, user_id, make_activity):
    """The user-visible half of the bug above: real history must produce a real ramp
    base, never the cold-start branch."""
    wk = date(2026, 6, 1)
    make_activity("strava_hist", "Run", date=wk.isoformat(), distance_mi=52.0)

    mileage, is_cold_start = generator._ramp_base_mileage(
        db, user_id, wk + timedelta(weeks=1), "Run")
    assert is_cold_start is False, "an athlete with real mileage must never read as a cold start"
    assert mileage == 52.0


# ---------- nearest_active_race_goal / periodizes_training ----------


def test_nearest_race_goal_picks_soonest(db, user_id, today, make_goal):
    make_goal("Marathon", today + timedelta(weeks=15))
    near = make_goal("Half", today + timedelta(weeks=5))
    assert generator.nearest_active_race_goal(db, user_id, today).id == near.id


def test_periodizes_training_false_is_skipped(db, user_id, today, make_goal):
    """The real scenario this shipped for: a wedding sits nearer than the goal race.
    NULL means yes (legacy rows keep working); only an explicit False opts out."""
    marathon = make_goal("Marathon", today + timedelta(weeks=15))
    make_goal("Wedding", today + timedelta(weeks=6), periodizes_training=False)
    assert generator.nearest_active_race_goal(db, user_id, today).id == marathon.id


def test_null_periodizes_training_still_counts(db, user_id, today, make_goal):
    wedding = make_goal("Wedding", today + timedelta(weeks=6), periodizes_training=None)
    make_goal("Marathon", today + timedelta(weeks=15))
    assert generator.nearest_active_race_goal(db, user_id, today).id == wedding.id


def test_past_race_goal_never_drives_phase(db, user_id, today, make_goal):
    make_goal("Last year's race", today - timedelta(weeks=4))
    assert generator.nearest_active_race_goal(db, user_id, today) is None


# ---------- resolve_periodization_goal (P21) ----------


def test_resolve_periodization_goal_falls_back_with_no_plan(db, user_id, today, make_goal):
    """A user who never starts a plan sees zero behavior change from P20/earlier —
    nearest active race goal still wins."""
    near = make_goal("Half", today + timedelta(weeks=5))
    make_goal("Marathon", today + timedelta(weeks=15))
    assert generator.resolve_periodization_goal(db, user_id, today).id == near.id


def test_resolve_periodization_goal_prefers_plan_primary_over_nearest(db, user_id, today, make_goal, make_plan):
    """The core P21 behavior change: starting a plan and naming a primary goal steers
    real generation, even when a nearer race would otherwise win."""
    make_goal("Wedding", today + timedelta(weeks=6))  # nearest -> would otherwise win
    marathon = make_goal("Marathon", today + timedelta(weeks=15))
    make_plan(marathon)  # role="primary" by default
    assert generator.resolve_periodization_goal(db, user_id, today).id == marathon.id


def test_resolve_periodization_goal_ignores_supporting_goal(db, user_id, today, make_goal, make_plan):
    """A supporting goal must never accidentally steer periodization — only the plan's
    one primary goal can."""
    import uuid as _uuid
    from app.models import PlanGoal as _PlanGoal
    wedding = make_goal("Wedding", today + timedelta(weeks=6))
    marathon = make_goal("Marathon", today + timedelta(weeks=15))
    plan = make_plan(wedding)  # role="primary"
    db.add(_PlanGoal(id=f"plangoal_{_uuid.uuid4().hex[:12]}", training_plan_id=plan.id,
                     goal_id=marathon.id, role="supporting", created_at="2026-01-01T00:00:00+00:00"))
    db.commit()
    assert generator.resolve_periodization_goal(db, user_id, today).id == wedding.id


def test_resolve_periodization_goal_falls_back_when_primary_no_longer_active(db, user_id, today, make_goal, make_plan):
    """A stale primary (completed, or its race date has passed) must degrade to the
    normal nearest-race fallback rather than orphaning periodization."""
    make_plan_goal_completed = make_goal("Old Marathon", today + timedelta(weeks=1), status="completed")
    still_active = make_goal("Half", today + timedelta(weeks=8))
    make_plan(make_plan_goal_completed)
    assert generator.resolve_periodization_goal(db, user_id, today).id == still_active.id


# ---------- explicit-goal phase lookup ----------


def test_phase_uses_explicit_goal_over_nearest(db, user_id, today, make_goal):
    """A plan must be able to ask for *its own* goal's phase even when another goal is
    the one actually driving the generator."""
    make_goal("Wedding", today + timedelta(weeks=6))          # nearest -> would be "build"
    marathon = make_goal("Marathon", today + timedelta(weeks=15))
    assert generator._phase_for_date(db, user_id, today) == "build"
    assert generator._phase_for_date(db, user_id, today, goal=marathon) == "base"


# ---------- attribution: the core P20 rule ----------


def test_driving_plan_reads_persisted_weekly_plan(db, user_id, today, make_goal, make_plan, training_config):
    goal = make_goal("Marathon", today + timedelta(weeks=15))
    plan = make_plan(goal)
    wk = _monday(today)
    db.add(WeeklyPlan(user_id=user_id, week_start=wk.isoformat(), target_tss=42.0,
                      actual_tss=0.0, is_deload=True, frozen=True))
    db.commit()

    view = generator.plan_week_view(db, user_id, plan, goal, wk)
    assert view["isPersisted"] is True
    assert view["targetMi"] == 42.0
    # frozen is the one field that cannot be recomputed — it must come through intact.
    assert view["frozen"] is True
    assert view["isDeload"] is True


def test_non_driving_plan_never_borrows_the_shared_row(db, user_id, today, make_goal, make_plan, training_config):
    """The regression this module exists for, updated for P21: a plan's PRIMARY goal now
    drives periodization (resolve_periodization_goal), so a SUPPORTING goal attached to
    that same plan is the "non-driving" case — it must NOT surface the primary's
    persisted WeeklyPlan target or frozen flag as its own."""
    wedding = make_goal("Wedding", today + timedelta(weeks=6))
    marathon = make_goal("Marathon", today + timedelta(weeks=15))
    plan = make_plan(wedding)  # role="primary" by default
    db.add(PlanGoal(id=f"plangoal_{uuid.uuid4().hex[:12]}", training_plan_id=plan.id,
                    goal_id=marathon.id, role="supporting", created_at="2026-01-01T00:00:00+00:00"))
    wk = _monday(today)
    db.add(WeeklyPlan(user_id=user_id, week_start=wk.isoformat(), target_tss=42.0,
                      actual_tss=0.0, is_deload=True, frozen=True))
    db.commit()

    view = generator.plan_week_view(db, user_id, plan, marathon, wk)
    assert view["isPersisted"] is False, "a non-driving (supporting) goal must replay, not read the shared row"
    assert view["targetMi"] != 42.0
    assert view["frozen"] is False
    assert view["phase"] == "base", "phase must come from the marathon, not the driving (primary) wedding"


# ---------- project_week_series ----------


def test_series_spans_requested_window_and_marks_current(db, user_id, today, make_goal, make_plan, training_config):
    goal = make_goal("Marathon", today + timedelta(weeks=20))
    plan = make_plan(goal)
    weeks = generator.project_week_series(db, user_id, plan, goal, weeks_back=3, weeks_forward=4)

    assert len(weeks) == 8  # 3 back + current + 4 forward
    assert sum(1 for w in weeks if w["isCurrentWeek"]) == 1
    assert weeks[3]["isCurrentWeek"] is True
    assert [w["isProjection"] for w in weeks] == [False] * 4 + [True] * 4


def test_future_weeks_have_null_actual_not_zero(db, user_id, today, make_goal, make_plan, training_config):
    """0.0 would be an honest number and a dishonest claim — it reads as "you ran
    nothing" for a week that hasn't happened."""
    goal = make_goal("Marathon", today + timedelta(weeks=20))
    plan = make_plan(goal)
    weeks = generator.project_week_series(db, user_id, plan, goal, weeks_back=1, weeks_forward=3)

    for w in weeks:
        if w["isProjection"]:
            assert w["actualMi"] is None
        else:
            assert isinstance(w["actualMi"], float)


def test_actual_comes_from_real_activities(db, user_id, today, make_goal, make_plan,
                                           training_config, make_activity):
    """`actual` is computed live from activity rows — WeeklyPlan.actual_tss is vestigial
    (always 0.0) and must never be the source."""
    goal = make_goal("Marathon", today + timedelta(weeks=20))
    plan = make_plan(goal)
    wk = _monday(today)
    make_activity("strava_1", "Run", date=wk.isoformat(), distance_mi=4.0)
    make_activity("strava_2", "Run", date=(wk + timedelta(days=2)).isoformat(), distance_mi=3.0)

    weeks = generator.project_week_series(db, user_id, plan, goal, weeks_back=0, weeks_forward=0)
    assert weeks[0]["actualMi"] == 7.0


def test_projection_ramps_rather_than_flatlining(db, user_id, today, make_goal, make_plan,
                                                 training_config, make_activity):
    """Future weeks must chain off each other. Feeding _last_nonzero_week_mileage for every
    future week instead would return the same real week each time and produce a flat line."""
    goal = make_goal("Marathon", today + timedelta(weeks=30))
    plan = make_plan(goal)
    prev_wk = _monday(today) - timedelta(weeks=1)
    make_activity("strava_hist", "Run", date=prev_wk.isoformat(), distance_mi=20.0)

    weeks = [w for w in generator.project_week_series(db, user_id, plan, goal, weeks_back=0, weeks_forward=5)
             if w["isProjection"]]
    assert len(weeks) == 5
    assert len({w["targetMi"] for w in weeks}) > 1, f"projection flatlined: {weeks}"

    # Deliberately not asserting a monotonic ramp: a deload week legitimately dips
    # (_compute_weekly_budget applies its 0.75 multiplier last), so the honest invariant
    # is "rises except across deloads", not "never decreases".
    prev = None
    for w in weeks:
        if prev is not None and not w["isDeload"]:
            assert w["targetMi"] >= prev, f"non-deload week went backwards: {weeks}"
        prev = w["targetMi"]
    assert any(w["isDeload"] for w in weeks), "expected the 4-week mesocycle to include a deload"


def test_weeks_forward_is_capped(db, user_id, today, make_goal, make_plan, training_config):
    """Bound lives in project_week_series, not the route, so no caller can route around it."""
    goal = make_goal("Marathon", today + timedelta(weeks=60))
    plan = make_plan(goal)
    weeks = generator.project_week_series(db, user_id, plan, goal, weeks_back=0, weeks_forward=9999)
    assert len(weeks) == generator.PLAN_VIEW_MAX_WEEKS_FORWARD + 1


# ---------- workout_duration_hours (P21 §6 Q2/Q3) ----------


def test_workout_duration_prefers_explicit_duration():
    from app.models import Workout
    w = Workout(target_duration_sec=3600, target_distance_mi=20.0, target_pace_sec_per_mi=600)
    assert generator.workout_duration_hours(w) == 1.0


def test_workout_duration_falls_back_to_distance_times_pace():
    """The long-run-grows-toward-peak case from the design doc's Q2: a longer distance
    at the workout's own (slower) prescribed pace must yield more hours, not a flat
    distance-based shortcut."""
    from app.models import Workout
    short = Workout(target_distance_mi=6.0, target_pace_sec_per_mi=480)   # 8:00/mi easy run
    long = Workout(target_distance_mi=20.0, target_pace_sec_per_mi=600)  # 10:00/mi long run
    assert generator.workout_duration_hours(short) == pytest.approx(0.8)
    assert generator.workout_duration_hours(long) == pytest.approx(20 * 600 / 3600)
    assert generator.workout_duration_hours(long) > generator.workout_duration_hours(short)


def test_workout_duration_none_when_unset():
    """Never fabricates a number — a strength session before target_duration_sec is set
    must read as unknown, not zero."""
    from app.models import Workout
    assert generator.workout_duration_hours(Workout()) is None


def test_strength_workout_gets_a_duration_estimate(db, user_id, today, training_config):
    """_generate_strength previously left target_duration_sec unset entirely, so a
    prescribed session had no a priori hours cost at all — verified gap, not a
    regression guard for something that already worked."""
    from app.models import Workout
    from app.stats import readiness
    readiness_result = readiness(db, user_id)
    w = generator._generate_strength(db, user_id, today, readiness_result,
                                     training_config, ignore_schedule=True)
    assert w is not None
    assert generator.workout_duration_hours(db.get(Workout, w["id"])) == pytest.approx(
        generator.STRENGTH_SESSION_DURATION_MIN / 60)
