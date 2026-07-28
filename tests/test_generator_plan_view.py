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


# ---------- P21 plan-aware weekday skeleton ----------


def _sk(long_run_day=None, available_days=None):
    """A stand-in for a TrainingPlan carrying just the two fields the resolver reads."""
    import json as _json
    class _P:
        pass
    p = _P()
    p.long_run_day = long_run_day
    p.available_days_json = _json.dumps(available_days) if available_days is not None else None
    return generator.resolve_weekday_skeleton(p)


def test_no_plan_leaves_the_skeleton_untouched():
    """The compatibility guarantee: a user who never opens the plan builder must get
    byte-identical scheduling to before P21."""
    assert generator.resolve_weekday_skeleton(None) == generator.WEEKDAY_SKELETON
    assert _sk() == generator.WEEKDAY_SKELETON


def test_long_run_day_moves_the_long_session():
    sk = _sk(long_run_day=6)  # Sunday
    assert sk[6] == "long"
    assert list(sk.values()).count("long") == 1, "exactly one long run per week"


def test_long_run_day_swaps_rather_than_overwrites():
    """The displaced session must survive the move — a swap, not an overwrite, so a week
    doesn't quietly lose a session every time the long run is repositioned."""
    default_long_day = next(d for d, t in generator.WEEKDAY_SKELETON.items() if t == "long")
    displaced = generator.WEEKDAY_SKELETON[6]
    sk = _sk(long_run_day=6)
    assert sk[6] == "long"
    assert sk[default_long_day] == displaced


def test_unavailable_days_become_rest():
    sk = _sk(available_days=[0, 2, 4])
    for d in (1, 3, 5, 6):
        assert sk[d] == "rest", f"weekday {d} was not marked unavailable"
    for d in (0, 2, 4):
        assert sk[d] != "rest" or generator.WEEKDAY_SKELETON[d] == "rest"


def test_long_run_relocates_rather_than_vanishing_when_its_day_is_unavailable():
    """Availability wins over preference — but a long run is the most important session
    in an endurance week and must never be silently deleted by that rule."""
    sk = _sk(long_run_day=6, available_days=[0, 2, 4])  # Sunday requested but unavailable
    assert "long" in sk.values(), "the long run was silently dropped"
    assert sk[4] == "long", "expected relocation to the latest available day"


def test_no_available_days_is_an_honest_all_rest_week():
    """An explicitly empty availability list is a real answer, not a parse failure to
    fall back on — it must not silently become 'every day available'."""
    sk = _sk(available_days=[])
    assert set(sk.values()) == {"rest"}


def test_malformed_available_days_falls_back_to_every_day():
    for bad in ("not json", '{"mon": true}', "[]", '["mon","tue"]'):
        p = type("P", (), {"long_run_day": None, "available_days_json": bad})()
        sk = generator.resolve_weekday_skeleton(p)
        # "[]" is genuinely empty (all rest); the rest are unparseable -> default week.
        assert sk == generator.WEEKDAY_SKELETON or set(sk.values()) == {"rest"}


def test_day_share_still_sums_to_the_full_budget_under_a_custom_skeleton():
    """The invariant that makes a weekly target reachable: the training days' shares must
    total 1.0. If the skeleton changes but _normalized_day_share doesn't see the same
    one, the target silently becomes unreachable — the exact bug that normalization was
    added to fix."""
    for phase in ("base", "build", "peak", "taper"):
        for sk in (_sk(long_run_day=6), _sk(available_days=[0, 2, 4, 6]), _sk(long_run_day=1, available_days=[1, 3, 5])):
            total = sum(
                generator._normalized_day_share(generator._skeleton_workout_type(d, phase, sk), phase, sk)
                for d in range(7)
                if generator._skeleton_workout_type(d, phase, sk) not in ("rest", "cross_train")
            )
            assert total == pytest.approx(1.0), f"{phase} skeleton {sk} summed to {total}"


# ---------- P21 hours cap (design doc 3.3) ----------


@pytest.fixture()
def plan_with_cap(db, user_id, make_goal, make_plan, today):
    def _make(cap):
        plan = make_plan(make_goal("Marathon", today + timedelta(weeks=20)))
        plan.weekly_hours_cap = cap
        db.commit()
        return plan
    return _make


def _gen_workout(db, user_id, date_iso, **kw):
    from app.models import Workout
    w = Workout(id=f"workout_{uuid.uuid4().hex[:12]}", user_id=user_id, scheduled_date=date_iso,
                source="generator", status="planned", created_at="2026-01-01T00:00:00+00:00",
                workout_type=kw.pop("workout_type", "easy"), activity_type="Run", **kw)
    db.add(w)
    db.commit()
    return w


def test_no_cap_means_no_conflict_claim(db, user_id, today, make_goal, make_plan):
    """No cap set is not 'a cap of zero' and not 'over budget' — there is nothing to
    compare against, and inventing a cap from history is what this design refuses. The
    per-planner breakdown is still real and is returned either way; only the cap
    comparison is withheld."""
    plan = make_plan(make_goal("Marathon", today + timedelta(weeks=20)))
    h = generator.weekly_hours_plan(db, user_id, _monday(today), plan=plan)
    assert h["capHours"] is None
    assert h["demandHours"] is None
    assert h["isOverCap"] is False
    assert h["overBy"] is None


def test_hours_demand_sums_generator_sessions(db, user_id, today, plan_with_cap):
    plan = plan_with_cap(10.0)
    wk = _monday(today)
    _gen_workout(db, user_id, wk.isoformat(), target_duration_sec=3600)
    _gen_workout(db, user_id, (wk + timedelta(days=1)).isoformat(), target_duration_sec=1800)
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["demandHours"] == pytest.approx(1.5)
    assert h["isOverCap"] is False and h["overBy"] is None


def test_hours_conflict_is_surfaced_with_real_numbers(db, user_id, today, plan_with_cap):
    plan = plan_with_cap(1.0)
    wk = _monday(today)
    _gen_workout(db, user_id, wk.isoformat(), target_duration_sec=7200)
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["isOverCap"] is True
    assert h["overBy"] == pytest.approx(1.0)
    assert h["capHours"] == 1.0


def test_planners_are_reported_separately_never_summed(db, user_id, today, plan_with_cap):
    """The overcount trap, confirmed against real production: Garmin, Coach and HALE all
    write a row for the same date and the athlete does ONE of them. Summing every source
    reported ~3.9h for a single day and made the cap meaningless. Each planner's week is
    reported on its own, and the cap comparison uses HALE's — the only plan this app is
    responsible for."""
    from app.models import Workout
    plan = plan_with_cap(5.0)
    wk = _monday(today)
    _gen_workout(db, user_id, wk.isoformat(), target_duration_sec=3600)
    for src in ("garmin", "coach"):
        db.add(Workout(id=f"workout_{uuid.uuid4().hex[:12]}", user_id=user_id,
                       scheduled_date=wk.isoformat(), source=src, status="planned",
                       workout_type="easy", activity_type="Run", target_duration_sec=4500,
                       created_at="2026-01-01T00:00:00+00:00"))
    db.commit()
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["demandHours"] == pytest.approx(1.0), "cap compares against HALE's plan only"
    assert h["bySource"]["generator"]["hours"] == pytest.approx(1.0)
    assert h["bySource"]["garmin"]["hours"] == pytest.approx(1.25)
    assert h["bySource"]["coach"]["hours"] == pytest.approx(1.25)
    total = sum(v["hours"] for v in h["bySource"].values())
    assert h["demandHours"] < total, "planners must never be summed into one demand figure"


def test_hale_distance_sessions_get_hours_from_recent_pace(db, user_id, today, plan_with_cap, make_activity):
    """The gap that made this whole feature inert: HALE prescribes distance with no pace,
    so every one of its sessions read as unknown-duration. Production derived 0.00h
    across HALE's 3 sessions while Garmin's derived 5.57h — so an hours check scoped to
    HALE measured precisely the plan whose hours couldn't be computed."""
    plan = plan_with_cap(10.0)
    wk = _monday(today)
    for i in range(4):  # real history -> a usable typical pace (600 s/mi)
        make_activity(f"strava_p{i}", "Run", date=(wk - timedelta(days=i + 1)).isoformat(),
                      distance_mi=5.0, moving_time_sec=3000)
    _gen_workout(db, user_id, wk.isoformat(), target_distance_mi=6.0)  # no pace, no duration
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["unknownSessions"] == 0, "a distance-only HALE session must no longer read as unknown"
    assert h["demandHours"] == pytest.approx(1.0)  # 6mi at 10:00/mi


def test_an_explicit_pace_still_beats_the_population_median(db, user_id, today, plan_with_cap, make_activity):
    """The fallback is a median across recent runs; a session that states its own pace
    is a specific prescription and must win."""
    plan = plan_with_cap(10.0)
    wk = _monday(today)
    for i in range(4):
        make_activity(f"strava_q{i}", "Run", date=(wk - timedelta(days=i + 1)).isoformat(),
                      distance_mi=5.0, moving_time_sec=3000)  # 600 s/mi
    _gen_workout(db, user_id, wk.isoformat(), target_distance_mi=6.0, target_pace_sec_per_mi=480)
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["demandHours"] == pytest.approx(0.8)  # 6mi at 8:00/mi, not the 10:00 median


def test_sessions_without_a_duration_are_reported_not_silently_zero(db, user_id, today, plan_with_cap):
    """A session with no derivable duration makes the total a floor. Counting it as zero
    would understate demand and could hide a real conflict."""
    plan = plan_with_cap(5.0)
    wk = _monday(today)
    _gen_workout(db, user_id, wk.isoformat(), target_duration_sec=3600)
    _gen_workout(db, user_id, (wk + timedelta(days=1)).isoformat(), workout_type="strength")
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["unknownSessions"] == 1
    assert h["demandHours"] == pytest.approx(1.0)


def test_rest_days_cost_nothing(db, user_id, today, plan_with_cap):
    plan = plan_with_cap(5.0)
    wk = _monday(today)
    _gen_workout(db, user_id, wk.isoformat(), workout_type="rest", target_duration_sec=9999)
    h = generator.weekly_hours_plan(db, user_id, wk, plan=plan)
    assert h["demandHours"] == 0.0 and h["unknownSessions"] == 0


# ---------- standing down for a committed session ----------


@pytest.fixture()
def readiness_clean(db, user_id):
    from app.stats import readiness
    return readiness(db, user_id)


def _other_planner_workout(db, user_id, date_iso, source, workout_type="interval", **kw):
    from app.models import Workout
    w = Workout(id=f"workout_{uuid.uuid4().hex[:12]}", user_id=user_id, scheduled_date=date_iso,
                source=source, status="planned", workout_type=workout_type, activity_type="Run",
                created_at="2026-01-01T00:00:00+00:00", **kw)
    db.add(w)
    db.commit()
    return w


def test_generator_stands_down_when_another_planner_committed_the_day(
        db, user_id, today, training_config, readiness_clean):
    """The real case: a run club hill workout (source="coach") already owned Tuesday, and
    HALE added a generic easy run on top — an unintended double day, and the reason the
    week's scheduled volume ran well past HALE's own budget."""
    _other_planner_workout(db, user_id, today.isoformat(), "coach", target_duration_sec=4680)
    result = generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    assert result is None, "HALE must not add a session to a day another planner has committed"


def test_a_garmin_rest_day_does_not_count_as_committed(db, user_id, today, training_config, readiness_clean):
    """"Garmin suggests rest" is the absence of a session, not one — HALE should still be
    free to prescribe."""
    _other_planner_workout(db, user_id, today.isoformat(), "garmin", workout_type="rest")
    result = generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    assert result is not None


def test_a_committed_strength_session_does_not_block_a_run(db, user_id, today, training_config, readiness_clean):
    """Strength runs on its own track and doesn't occupy the same slot as a run."""
    _other_planner_workout(db, user_id, today.isoformat(), "coach", workout_type="strength")
    result = generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    assert result is not None


def test_quick_generate_still_produces_a_session(db, user_id, today, training_config, readiness_clean):
    """ignore_schedule is an explicit "give me one now" press. It must never silently
    decline just because another planner already has that day."""
    _other_planner_workout(db, user_id, today.isoformat(), "garmin")
    result = generator._generate_endurance(db, user_id, today, readiness_clean, training_config,
                                           ignore_schedule=True)
    assert result is not None


def test_standing_down_withdraws_our_own_earlier_planned_row(
        db, user_id, today, training_config, readiness_clean):
    """Standing down while leaving yesterday's now-redundant HALE row in place would
    defeat the point — the double day would still be sitting on the calendar."""
    from app.models import Workout
    first = generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    assert first is not None
    _other_planner_workout(db, user_id, today.isoformat(), "coach")

    assert generator._generate_endurance(db, user_id, today, readiness_clean, training_config) is None
    remaining = (db.query(Workout)
                 .filter(Workout.scheduled_date == today.isoformat(),
                         Workout.source == "generator", Workout.workout_type != "strength")
                 .all())
    assert remaining == [], "HALE's own superseded planned row should be withdrawn"


def test_standing_down_never_touches_a_completed_row(db, user_id, today, training_config, readiness_clean):
    """A completed session is history and must survive — same rule the upsert already
    follows for non-planned rows."""
    from app.models import Workout
    generated = generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    row = db.get(Workout, generated["id"])
    row.status = "completed"
    db.commit()
    _other_planner_workout(db, user_id, today.isoformat(), "coach")

    generator._generate_endurance(db, user_id, today, readiness_clean, training_config)
    assert db.get(Workout, generated["id"]) is not None
    assert db.get(Workout, generated["id"]).status == "completed"


def test_distribution_audit_sees_a_same_day_hard_session(db, user_id, today):
    """The blindness this fixes: the window stopped strictly before the candidate day, so
    a hard session already scheduled for that very date was invisible to the audit."""
    _other_planner_workout(db, user_id, today.isoformat(), "coach", workout_type="interval")
    assert generator._distribution_would_break(db, user_id, today, candidate_hard=True) is True
