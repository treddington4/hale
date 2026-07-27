"""Phase 4.3 — deterministic, no-LLM workout generator. Two independent paths per
(user, date), both gated by one shared stats.readiness() result:

- Endurance: goal-driven periodization (phase from a race goal's date, weekly mileage
  ramp capped by phase, readiness-gated intensity downgrade, a distribution audit,
  two-a-days in build/peak).
- Strength (Phase 4.4 follow-on): a small hardcoded exercise-rotation template,
  readiness-gated, with double-progression state in ExerciseProgress.

Both paths are idempotent per (user, date): each only ever creates/updates its own
`source="generator"` Workout rows, never touching a "coach" (manual/chat-scheduled)
or "garmin" (adaptive-plan) row for the same date — mirrors coach.py's
sync_garmin_suggested_workouts, the one existing precedent for exactly this kind of
per-source-per-date upsert.

Known v1 approximations, called out explicitly rather than silently:
- `WeeklyPlan.target_tss`/`actual_tss` store a mileage-based proxy, not a real
  Training Stress Score (Phase 6.1's per-activity TSS hasn't shipped yet) — same
  "real number now, real thing later" tradeoff stats.readiness()'s acuteChronicRatio
  already makes.
- The distribution audit approximates "time-in-zone" with a coarse hard/easy day-type
  ratio (tempo/interval count as hard) over the trailing 7 days, not true per-second
  HR-zone time (this app doesn't store zone-time breakdowns at sync time).
- The strength exercise template is a small, hardcoded 2-day A/B full-body rotation
  (see STRENGTH_TEMPLATES) — not a real exercise-library/selection system.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from ..models import (
    SessionLocal, Workout, Goal, User, UserTrainingConfig, HealthNote,
    WeeklyPlan, RecoverySession, DEFAULT_USER_ID, owned_by,
)
from .. import stats
from . import core as coach
from ..util import local_today

log = logging.getLogger("runlog")

GENERATOR_SOURCE = "generator"

# ---------- Shared helpers ----------


def _week_start(d):
    return d - timedelta(days=d.weekday())  # Monday


def _get_training_config(db, user_id) -> UserTrainingConfig:
    config = db.get(UserTrainingConfig, user_id)
    return config or UserTrainingConfig(
        user_id=user_id, weekly_ramp_pct=3.0, mesocycle_pattern="3:1", distribution="pyramidal",
        strength_days_per_week=2, strength_template=_default_strength_template(db, user_id),
    )


def _has_severe_health_note(db, user_id) -> bool:
    return (
        db.query(HealthNote)
        .filter(HealthNote.status.in_(("active", "monitoring")), HealthNote.suspected_severity == "severe",
                owned_by(HealthNote.user_id, user_id))
        .first()
        is not None
    )


def _existing_generator_workout(db, user_id, date_str, domain: str):
    """`domain` disambiguates which of this module's (up to 3) rows for a single
    date this is — "endurance" (primary session), "endurance_second" (the two-a-day
    slot, identified by scheduled_time being set — strength never sets it), or
    "strength". Without this, the endurance and strength paths would collide on the
    same "first generator row for this date" slot and overwrite each other (a real
    bug caught during verification, not theoretical)."""
    q = db.query(Workout).filter(
        Workout.scheduled_date == date_str, Workout.source == GENERATOR_SOURCE, owned_by(Workout.user_id, user_id),
    )
    if domain == "endurance_second":
        q = q.filter(Workout.scheduled_time.isnot(None))
    elif domain == "strength":
        q = q.filter(Workout.scheduled_time.is_(None), Workout.workout_type == "strength")
    elif domain == "endurance":
        # Run (the nightly auto-generator's only activity, or Run via quick-generate) —
        # also matches an existing cross-train/"Other" row from this same slot, since
        # WEEKDAY_SKELETON's cross_train day is produced by this exact path.
        q = q.filter(Workout.scheduled_time.is_(None), Workout.workout_type != "strength",
                      Workout.activity_type.in_(("Run", "Other")))
    elif domain.startswith("endurance_"):
        # "endurance_<activity>" — quick-generate only (e.g. "endurance_ride"), never
        # produces cross_train (quick-generate always forces easy), so match exactly.
        # Without this, a same-day Ride quick-generate would match/overwrite an
        # already-generated Run row instead of getting its own slot (a real bug
        # caught during verification).
        expected_activity = domain.split("_", 1)[1].capitalize()
        q = q.filter(Workout.scheduled_time.is_(None), Workout.workout_type != "strength",
                      Workout.activity_type == expected_activity)
    else:
        q = q.filter(Workout.scheduled_time.is_(None), Workout.workout_type != "strength")
    return q.first()


def _preview_workout_dict(date_str: str, **fields) -> dict:
    """Same key shape as coach._workout_to_dict, but describing a workout that
    hasn't been (and — for a pure preview — won't be) written to the DB: no id,
    no createdAt, nothing else that implies a real persisted row exists yet."""
    return {
        "id": None, "scheduledDate": date_str, "workoutType": fields.get("workout_type"),
        "activityType": fields.get("activity_type"), "targetDistanceMi": fields.get("target_distance_mi"),
        "targetPaceSecPerMi": fields.get("target_pace_sec_per_mi"), "targetDurationSec": fields.get("target_duration_sec"),
        "notes": fields.get("notes"), "steps": fields.get("steps"), "status": "planned",
        "linkedRunId": None, "critiqueText": None, "createdAt": None,
        "source": GENERATOR_SOURCE, "scheduledTime": fields.get("scheduled_time"),
    }


def _upsert_generator_workout(db, user_id, date_str, domain: str, dry_run: bool = False, **fields) -> dict:
    """Idempotent per (user, date, domain): a `planned` row from a prior run of this
    same date is recomputed/overwritten in place (rerunning the generator for an
    already-generated day is a no-op-if-nothing-changed, not a duplicate); a row
    that's since been completed/skipped is left alone entirely — history is
    immutable, matching sync_garmin_suggested_workouts' own rule.

    `dry_run=True` (Phase 14's New Workout preview-before-confirm flow) skips the
    DB entirely — no existing-row lookup, no create/update — and just echoes back
    what *would* be written, in the same dict shape a real call returns, so the
    frontend can render an identical preview either way."""
    if dry_run:
        return _preview_workout_dict(date_str, **fields)
    existing = _existing_generator_workout(db, user_id, date_str, domain)
    if existing and existing.status != "planned":
        return coach._workout_to_dict(existing)
    if existing:
        return coach.update_workout(db, existing.id, user_id=user_id, **fields)
    return coach.create_workout(db, date_str, user_id=user_id, source=GENERATOR_SOURCE, **fields)


# ---------- Endurance path ----------

PHASE_CEILING_MULTIPLIER = {"base": 1.15, "build": 1.30, "peak": 1.10, "taper": 0.70}
MESOCYCLE_LENGTHS = {"3:1": 4, "2:1": 3, "4:1": 5}

# Phase 14 — cold-start ramp for an activity with no real history at all (a brand-new
# runner, or any user's first-ever ride via the Quick Generate button). Deliberately
# small and linear, same "start conservative, ramp by a fixed amount" philosophy
# Phase 12.3's strength challenge-safety logic already established for a different
# domain — see the real bug this fixes, below.
COLD_START_INITIAL_MILES = 3.0
COLD_START_WEEKLY_INCREMENT_MILES = 1.5
COLD_START_LOOKBACK_WEEKS = 8  # weeks checked before concluding "no experience in this activity at all"
_EPOCH_MONDAY = datetime(2020, 1, 6).date()  # any fixed Monday — used only to derive a stable week index

# 1-flag readiness downgrade ladder: interval -> tempo -> easy (stands in for "Z2",
# there's no distinct Zone-2 workout_type) -> "recovery" (also `easy`, shorter/lighter,
# distinguished only by a note — VALID_WORKOUT_TYPES has no separate recovery value).
DOWNGRADE_LADDER = ["interval", "tempo", "easy", "easy"]

# Very simple fixed weekly skeleton (Monday=0..Sunday=6) — "what would today be,
# absent any readiness gating." Real periodization systems vary this by phase; this
# is a deliberately small v1 template, not a full training-plan generator.
WEEKDAY_SKELETON = {0: "easy", 1: "quality", 2: "easy", 3: "cross_train", 4: "rest", 5: "long", 6: "easy"}


def nearest_active_race_goal(db, user_id, date) -> Goal | None:
    """The goal that drives periodization on `date` — the soonest upcoming active race
    goal. Extracted from _phase_for_date (was inline) so P20's plan view can ask "which
    goal is actually driving this week?" without duplicating the query.

    `periodizes_training is not False` (P20): a NULL means yes, matching the legacy-NULL
    convention used throughout this codebase, so existing rows are unaffected. Only an
    explicit False opts a goal out — for a dated milestone that's worth counting down to
    but wrong to build/peak/taper for."""
    return (
        db.query(Goal)
        # target_date >= today: an "active" race goal whose date has already passed
        # (never marked completed) must not pin the phase to a degenerate/negative
        # "weeks until" indefinitely — only a genuinely upcoming race counts.
        .filter(Goal.goal_type == "race", Goal.status == "active", Goal.target_date >= date.isoformat(),
                Goal.periodizes_training.is_not(False), owned_by(Goal.user_id, user_id))
        .order_by(Goal.target_date)
        .first()
    )


def _phase_for_date(db, user_id, date, goal: Goal | None = None) -> str:
    """`goal=None` keeps the original behavior exactly (resolve the nearest race goal),
    which is what the live generator still passes. P20's plan view passes an explicit
    goal so it can show the phase *for that plan's own goal*, which is not necessarily
    the one currently driving the generator."""
    if goal is None:
        goal = nearest_active_race_goal(db, user_id, date)
    if not goal:
        return "base"
    race_date = datetime.strptime(goal.target_date, "%Y-%m-%d").date()
    weeks_until = (race_date - date).days / 7
    if weeks_until <= 1:
        return "taper"
    if weeks_until <= 4:
        return "peak"
    if weeks_until <= 12:
        return "build"
    return "base"


def _is_deload_week(config, week_start) -> bool:
    cycle_len = MESOCYCLE_LENGTHS.get(config.mesocycle_pattern, 4)
    weeks_since_epoch = (week_start - _EPOCH_MONDAY).days // 7
    return weeks_since_epoch % cycle_len == cycle_len - 1


def _week_mileage(db, user_id, week_start, activity_type="Run") -> float:
    """Two real, shipped bugs lived here, found back to back while building P20's plan
    view — surfaced one at a time because the first one was masking the second.

    Bug 1 (casing): P4 made the stored value canonical-lowercase ("run"/"ride") at write
    time, while every caller in this module still passed source-style PascalCase
    ("Run"/"Ride"). A literal `Run.activity_type == activity_type` matched nothing, so
    this returned 0.0 for every week, and the nightly generator prescribed ~3-6 mile
    weeks to an athlete actually running real mileage.

    Bug 2 (dedup, unmasked by fixing bug 1): CLAUDE.md documents that Strava and Garmin
    each write their own copy of the same physical run, never deduplicated in storage —
    every consumer is required to call stats._all_runs()/merge_duplicate_runs() before
    summing anything. This function queried `Run` directly and summed both copies,
    reporting ~52 mi/week for someone actually running ~28 (confirmed: production had a
    strava_ and a garmin_ row for nearly every date that week). Bug 1 hid this completely
    — 0.0 either way looks the same as "not deduplicated" until the casing is fixed.

    Fixed by routing through stats._all_runs(), the one dedup-and-normalize entrypoint
    every other aggregate in this codebase already uses (see its own docstring), instead
    of hand-querying Run — so this can't drift from that behavior again, in either
    direction."""
    week_end = week_start + timedelta(days=6)
    return sum(
        r.distance_mi or 0 for r in stats._all_runs(db, activity_type, user_id)
        if week_start.isoformat() <= r.date <= week_end.isoformat()
    )


def _last_nonzero_week_mileage(db, user_id, before_week_start, activity_type, lookback_weeks=COLD_START_LOOKBACK_WEEKS):
    """Real bug fix (Phase 14): distinguishes "an established athlete who just didn't
    run/ride last week" (real history exists further back — that week's mileage
    becomes the ramp base) from "genuinely never done this activity" (no nonzero
    week anywhere in the lookback — a true cold start). The previous code only ever
    checked the immediately preceding week, so a quiet week for an established
    athlete and a brand-new activity looked identical (both `0`) and both fell
    through to the same flat-20-mile fallback. Returns (mileage, is_cold_start)."""
    for i in range(1, lookback_weeks + 1):
        wk_start = before_week_start - timedelta(days=7 * i)
        mileage = _week_mileage(db, user_id, wk_start, activity_type)
        if mileage > 0:
            return mileage, False
    return 0.0, True


def _weeks_active_in_activity(db, user_id, before_week_start, activity_type, max_weeks=20) -> int:
    """How many of the trailing max_weeks had any mileage in this activity — a simple
    proxy for "how many weeks into this activity's cold-start ramp am I," used to
    size the linear increment. Only meaningful once _last_nonzero_week_mileage has
    already concluded this is a cold start."""
    count = 0
    for i in range(1, max_weeks + 1):
        wk_start = before_week_start - timedelta(days=7 * i)
        if _week_mileage(db, user_id, wk_start, activity_type) > 0:
            count += 1
    return count


def _compute_weekly_budget(last_nonzero_mileage, is_cold_start, ramp_pct, phase, is_deload, weeks_active=0) -> float:
    """Pure budget-math core, shared by the persisted Run path (_get_or_create_weekly_plan)
    and Ride's lighter-weight direct path (_generate_endurance) — see the real bug
    this fixes in _last_nonzero_week_mileage's docstring above."""
    if is_cold_start:
        budget = COLD_START_INITIAL_MILES + COLD_START_WEEKLY_INCREMENT_MILES * weeks_active
    else:
        uncapped = last_nonzero_mileage * (1 + ramp_pct / 100)
        ceiling = last_nonzero_mileage * PHASE_CEILING_MULTIPLIER.get(phase, 1.15)
        budget = min(uncapped, ceiling)
    if is_deload:
        budget *= 0.75
    return budget


def _get_or_create_weekly_plan(db, user_id, week_start, phase, config):
    """Run-specific and persisted (via WeeklyPlan) — carries deload/frozen state
    across the nightly periodization loop. Ride has no equivalent persisted plan
    (see _generate_endurance): it's quick-generate-only in this v1, with no two-a-day/
    deload cycle to track, so a direct _compute_weekly_budget call is enough there."""
    plan = (
        db.query(WeeklyPlan)
        .filter(WeeklyPlan.user_id == user_id, WeeklyPlan.week_start == week_start.isoformat())
        .first()
    )
    if plan:
        return plan
    is_deload = _is_deload_week(config, week_start)
    last_nonzero, is_cold_start = _last_nonzero_week_mileage(db, user_id, week_start, "Run")
    weeks_active = _weeks_active_in_activity(db, user_id, week_start, "Run") if is_cold_start else 0
    ramp_pct = config.weekly_ramp_pct or 3.0
    budget = _compute_weekly_budget(last_nonzero, is_cold_start, ramp_pct, phase, is_deload, weeks_active)
    plan = WeeklyPlan(
        user_id=user_id, week_start=week_start.isoformat(), target_tss=round(budget, 1),
        actual_tss=0.0, is_deload=is_deload, frozen=False,
    )
    db.add(plan)
    db.commit()
    return plan


# ---------- P20: read-only plan view ----------
#
# These two are the only public (non-underscore) functions here, deliberately: they're
# consumed by routes/plans.py. They live in generator.py rather than stats.py because the
# pure functions they compose (_phase_for_date/_is_deload_week/_compute_weekly_budget/
# _last_nonzero_week_mileage) already live here — moving them to stats.py would mean
# reimplementing that math there, which is exactly the duplication CLAUDE.md's
# "stats.py is the single computation core" rule exists to prevent (see GAP, its own
# cautionary example). Neither function writes anything.

PLAN_VIEW_MAX_WEEKS_FORWARD = 20
PLAN_VIEW_MAX_WEEKS_BACK = 52


def plan_activity_type(goal) -> str:
    """Which discipline a goal is actually about. A race goal is not necessarily a
    *running* race — Goal.activity_types_json has carried this since the goal table was
    written (its own comment cites ["Run","Ride"] for a duathlon), but P20's first cut
    hardcoded "Run" throughout the plan view, so a 50-mile-ride goal rendered the user's
    running mileage under the cycling goal's name. Falls back to "Run" only when the
    column is empty/unparseable, never as a silent default for a real other value."""
    try:
        types = json.loads(goal.activity_types_json or "[]") if goal else []
    except (ValueError, TypeError):
        types = []
    return types[0] if types else "Run"


def plan_week_view(db, user_id, plan, week_start, config=None) -> dict:
    """One already-started or in-progress week for one TrainingPlan.

    Reads the persisted WeeklyPlan row only when BOTH hold:

    1. This plan's goal is genuinely the one driving periodization that week. WeeklyPlan
       is a single global stream per user (not goal-scoped) whose target/deload/frozen
       belong to whatever goal won `nearest_active_race_goal` at the time, so a plan for
       a non-driving goal that read those rows would display another goal's numbers as
       its own, silently — worse than showing nothing.
    2. This plan's discipline is running. WeeklyPlan is Run-only by construction
       (_get_or_create_weekly_plan hardcodes "Run"); its target is a *running* budget,
       so a cycling plan reading it would be the same category of lie as (1).

    Otherwise the week is replayed deterministically from this plan's own discipline."""
    goal = db.get(Goal, plan.goal_id)
    activity_type = plan_activity_type(goal)
    config = config or _get_training_config(db, user_id)
    phase = _phase_for_date(db, user_id, week_start, goal=goal)
    is_deload = _is_deload_week(config, week_start)

    driving = nearest_active_race_goal(db, user_id, week_start)
    persisted = None
    if activity_type == "Run" and driving is not None and driving.id == plan.goal_id:
        persisted = (
            db.query(WeeklyPlan)
            .filter(WeeklyPlan.user_id == user_id, WeeklyPlan.week_start == week_start.isoformat())
            .first()
        )

    if persisted is not None:
        # `frozen` is the one field here that genuinely cannot be recomputed — it records
        # that a 2+-flag readiness day capped this week, which depends on that day's state.
        target_mi, is_deload, frozen = persisted.target_tss or 0.0, bool(persisted.is_deload), bool(persisted.frozen)
    else:
        last_nonzero, is_cold_start = _last_nonzero_week_mileage(db, user_id, week_start, activity_type)
        weeks_active = _weeks_active_in_activity(db, user_id, week_start, activity_type) if is_cold_start else 0
        target_mi = round(_compute_weekly_budget(
            last_nonzero, is_cold_start, config.weekly_ramp_pct or 3.0, phase, is_deload, weeks_active), 1)
        frozen = False

    return {"phase": phase, "isDeload": is_deload, "frozen": frozen, "activityType": activity_type,
            "targetMi": target_mi, "isPersisted": persisted is not None}


def project_week_series(db, user_id, plan, weeks_back=8, weeks_forward=12) -> list[dict]:
    """Week-by-week target vs actual for one plan, spanning past and projected future.

    Future weeks CANNOT be read from history: _compute_weekly_budget ramps off the last
    nonzero week's real mileage, and a week 6 out has none — asking for it would return
    today's last-nonzero week for every future week, producing a flat non-ramping line.
    So future weeks chain off each other, each one's target feeding the next as its ramp
    base. That assumes every week hits its target exactly, which is why isProjection is
    on the wire and the UI states the assumption rather than presenting these as
    commitments.

    The chain deliberately reproduces an existing generator quirk rather than smoothing
    it: _compute_weekly_budget applies the deload multiplier last, and the real generator
    then ramps the following week off that deflated number, so a deload permanently
    lowers the base instead of bouncing back. Projecting a bounce-back would show a
    rosier future than the generator will actually deliver. If that quirk is worth
    fixing, it's a generator-math change, not something to paper over in a view."""
    weeks_forward = max(0, min(weeks_forward, PLAN_VIEW_MAX_WEEKS_FORWARD))
    weeks_back = max(0, min(weeks_back, PLAN_VIEW_MAX_WEEKS_BACK))

    goal = db.get(Goal, plan.goal_id)
    activity_type = plan_activity_type(goal)
    config = _get_training_config(db, user_id)
    current_week_start = _week_start(local_today(user_id))

    cursor = current_week_start - timedelta(weeks=weeks_back)
    end = current_week_start + timedelta(weeks=weeks_forward)
    weeks, rolling_base_mi = [], None

    while cursor <= end:
        is_future = cursor > current_week_start
        if not is_future:
            wv = plan_week_view(db, user_id, plan, cursor, config)
            phase, is_deload, frozen = wv["phase"], wv["isDeload"], wv["frozen"]
            target_mi, is_persisted = wv["targetMi"], wv["isPersisted"]
            # This plan's own discipline, not a hardcoded "Run" — a cycling goal's
            # "actual" must be cycling miles. float() so a zero-mileage week serializes
            # as 0.0, not 0 — sum() over an empty set returns int, and a type that flips
            # with the data is a nuisance for any consumer doing arithmetic on it.
            actual_mi = float(round(_week_mileage(db, user_id, cursor, activity_type), 1))
        else:
            phase = _phase_for_date(db, user_id, cursor, goal=goal)
            is_deload = _is_deload_week(config, cursor)
            target_mi = round(_compute_weekly_budget(
                rolling_base_mi or 0.0, False, config.weekly_ramp_pct or 3.0, phase, is_deload, 0), 1)
            frozen, is_persisted = False, False
            # Not 0.0 — _week_mileage would honestly return 0 for a week that hasn't
            # happened, but rendering "0 mi" reads as "you ran nothing", which is a claim
            # about a week nobody has had the chance to run yet. null renders as a dash.
            actual_mi = None
        rolling_base_mi = target_mi

        weeks.append({
            "weekStart": cursor.isoformat(), "phase": phase, "isDeload": is_deload,
            "frozen": frozen, "targetMi": target_mi, "actualMi": actual_mi,
            "activityType": activity_type,
            "isProjection": is_future, "isPersisted": is_persisted,
            "isCurrentWeek": cursor == current_week_start,
        })
        cursor += timedelta(weeks=1)
    return weeks


def _distribution_would_break(db, user_id, date, candidate_hard: bool) -> bool:
    """Coarse day-type-ratio approximation of a real time-in-zone distribution audit
    (see module docstring). tempo/interval count as "hard"; everything else (incl.
    long, which is hard on volume, not intensity) counts as "easy" for this ratio."""
    if not candidate_hard:
        return False
    week_start = date - timedelta(days=6)
    rows = (
        db.query(Workout)
        .filter(Workout.scheduled_date >= week_start.isoformat(), Workout.scheduled_date < date.isoformat(),
                Workout.workout_type.in_(("easy", "tempo", "interval", "long")), owned_by(Workout.user_id, user_id))
        .all()
    )
    hard_count = sum(1 for w in rows if w.workout_type in ("tempo", "interval"))
    total = len(rows) + 1  # +1 for the candidate day itself
    ratio = (hard_count + 1) / total
    return ratio > 0.2  # polarized/pyramidal both cap hard-day share at ~20% in this v1 approximation


def _generate_endurance(db, user_id, date, readiness_result, config, activity_type: str = "Run",
                         ignore_schedule: bool = False, dry_run: bool = False) -> dict | None:
    """`activity_type="Run"` is the original nightly-periodization path (persisted
    WeeklyPlan, real deload/frozen/two-a-day semantics). Any other activity_type
    (currently only "Ride", via Phase 14's Quick Generate button) uses the same
    phase/readiness-gate/distribution logic but a lighter-weight, non-persisted
    budget computation — no deload-week or two-a-day cycle to track for an
    on-demand quick-generate action that isn't part of the nightly weekly skeleton.

    `ignore_schedule` (Quick Generate) skips the WEEKDAY_SKELETON lookup entirely —
    without it, pressing "Run" on a skeleton rest/cross_train day would generate a
    rest/cross_train "session" instead of an actual run, defeating the whole point
    of an explicit "give me one now" button. Forces a plain "easy" base type instead,
    still subject to every real readiness/distribution gate below unchanged."""
    date_str = date.isoformat()
    week_start = _week_start(date)
    phase = _phase_for_date(db, user_id, date)
    flags = readiness_result["flags"]
    severe_health = _has_severe_health_note(db, user_id)

    # Computed regardless of which branch below actually sources the budget — needed
    # by both (Run's persisted WeeklyPlan doesn't record whether *it* used the
    # cold-start branch internally) for the day_share decision further down.
    _, is_cold_start = _last_nonzero_week_mileage(db, user_id, week_start, activity_type)

    if activity_type == "Run":
        plan = _get_or_create_weekly_plan(db, user_id, week_start, phase, config)
        budget = plan.target_tss or 20.0
        is_deload = plan.is_deload
    else:
        last_nonzero, _ = _last_nonzero_week_mileage(db, user_id, week_start, activity_type)
        weeks_active = _weeks_active_in_activity(db, user_id, week_start, activity_type) if is_cold_start else 0
        is_deload = _is_deload_week(config, week_start)
        budget = _compute_weekly_budget(
            last_nonzero, is_cold_start, config.weekly_ramp_pct or 3.0, phase, is_deload, weeks_active,
        )
        plan = None

    if ignore_schedule:
        base_type = "easy"
    else:
        skeleton_type = WEEKDAY_SKELETON[date.weekday()]
        if skeleton_type == "quality":
            base_type = "interval" if phase == "peak" else "tempo"
        elif skeleton_type == "rest":
            base_type = "rest"
        elif skeleton_type == "cross_train":
            base_type = "cross_train"
        else:
            base_type = skeleton_type  # "easy" | "long"

    trigger_notes = []
    workout_type = base_type

    if severe_health:
        workout_type = "rest"
        trigger_notes.append("Active health note flagged severe — micro-deload rest day.")
    elif len(flags) >= 2:
        workout_type = "rest"
        trigger_notes.append(f"Readiness flags {flags} — rest day, week's budget frozen.")
        if plan is not None and not plan.frozen:
            plan.frozen = True
            db.commit()
    elif len(flags) == 1 and workout_type in DOWNGRADE_LADDER:
        downgraded = DOWNGRADE_LADDER[min(DOWNGRADE_LADDER.index(workout_type) + 1, len(DOWNGRADE_LADDER) - 1)]
        if downgraded != workout_type:
            trigger_notes.append(f"Readiness flag {flags} — downgraded {workout_type} -> {downgraded}.")
            workout_type = downgraded
    elif workout_type in ("tempo", "interval") and _distribution_would_break(db, user_id, date, candidate_hard=True):
        trigger_notes.append(f"Distribution audit ({config.distribution}) — downgraded {workout_type} -> easy to keep hard-day share in check.")
        workout_type = "easy"

    if is_deload and workout_type == "long":
        trigger_notes.append("Deload week — trimmed long run.")

    day_share = {"long": 0.30, "tempo": 0.18, "interval": 0.15, "easy": 0.10, "cross_train": 0.10, "rest": 0}
    target_distance_mi = None
    if workout_type not in ("rest",) and workout_type != "cross_train":
        if is_cold_start:
            # Real bug caught in testing: a cold-start `budget` already represents a
            # sensible single-session distance (see COLD_START_INITIAL_MILES), not a
            # weekly total to slice up — running it through day_share produced an
            # absurd "first run" (3mi budget * 10% easy-day share = 0.3mi).
            target_distance_mi = round(budget, 1)
        else:
            share = day_share.get(workout_type, 0.10)
            target_distance_mi = round(budget * share, 1)

    # Neither "rest" nor "cross_train" are really "a Run"/"a Ride" — normalizing both
    # to "Other" avoids a rest day (nothing done at all) misleadingly reading as
    # "Rest (Run)" just because the Run-domain generator happened to produce it.
    result_activity_type = "Other" if workout_type in ("rest", "cross_train") else activity_type
    notes = " ".join(trigger_notes) or None

    # "endurance" stays the key for Run (the nightly auto-generator's only activity,
    # unchanged for backward compat with rows it already created); other activities
    # (currently just Ride, via quick-generate) get their own suffixed key so a same-day
    # Ride quick-generate can't silently overwrite an already-generated Run, and vice versa.
    endurance_domain = "endurance" if activity_type == "Run" else f"endurance_{activity_type.lower()}"

    result = _upsert_generator_workout(
        db, user_id, date_str, domain=endurance_domain, dry_run=dry_run,
        workout_type=workout_type, activity_type=result_activity_type,
        target_distance_mi=target_distance_mi, notes=notes,
    )

    # Two-a-days: build/peak, clean readiness, on the day's quality/long session only.
    # Run-only — an ad-hoc Ride quick-generate never gets a second session appended.
    if (activity_type == "Run" and phase in ("build", "peak") and not flags and not severe_health
            and workout_type in ("tempo", "interval", "long")):
        second = _upsert_generator_workout(
            db, user_id, date_str, domain="endurance_second", dry_run=dry_run,
            workout_type="cross_train", activity_type="Other",
            notes="Second session — easy recovery-intensity, modality split from the main session.",
            scheduled_time="18:00",
        )
        result = {"primary": result, "secondSession": second}

    return result


# ---------- Strength path (Phase 4.4 follow-on) ----------

STRENGTH_TEMPLATES = {
    "full_body_ab": {
        "A": [
            {"exercise": "Goblet Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Push-up", "targetType": "reps", "category": "push"},
            {"exercise": "Bent-over Row", "targetType": "reps", "category": "pull"},
            {"exercise": "Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Glute Bridge", "targetType": "reps", "category": "hinge"},
        ],
        "B": [
            {"exercise": "Romanian Deadlift", "targetType": "reps", "category": "hinge"},
            {"exercise": "Overhead Press", "targetType": "reps", "category": "push"},
            {"exercise": "Pull-up", "targetType": "reps", "category": "pull"},
            {"exercise": "Side Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Dead Bug", "targetType": "reps", "category": "core"},
        ],
    },
    # Phase 14 — glute/hip/core/hinge-heavy, supporting running/cycling economy and
    # injury prevention rather than general full-body strength. Reuses the exact
    # same categories as full_body_ab (no new progression-increment logic needed) —
    # an explicitly bounded v1 exercise pick, same discipline as full_body_ab's own.
    "runner_focus": {
        "A": [
            {"exercise": "Bulgarian Split Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Single-leg Romanian Deadlift", "targetType": "reps", "category": "hinge"},
            {"exercise": "Clamshell", "targetType": "reps", "category": "hinge"},
            {"exercise": "Side Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Calf Raise", "targetType": "reps", "category": "squat"},
        ],
        "B": [
            {"exercise": "Glute Bridge", "targetType": "reps", "category": "hinge"},
            {"exercise": "Lateral Band Walk", "targetType": "reps", "category": "hinge"},
            {"exercise": "Step-up", "targetType": "reps", "category": "squat"},
            {"exercise": "Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Dead Bug", "targetType": "reps", "category": "core"},
        ],
    },
    # Phase 14 — the "back and legs" target area requested directly (pull + hinge +
    # squat-focused), for whenever the user wants to explicitly pick a focus rather
    # than the auto-picked default.
    "back_and_legs": {
        "A": [
            {"exercise": "Romanian Deadlift", "targetType": "reps", "category": "hinge"},
            {"exercise": "Bent-over Row", "targetType": "reps", "category": "pull"},
            {"exercise": "Goblet Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Pull-up", "targetType": "reps", "category": "pull"},
            {"exercise": "Glute Bridge", "targetType": "reps", "category": "hinge"},
        ],
        "B": [
            {"exercise": "Deadlift", "targetType": "reps", "category": "hinge"},
            {"exercise": "Bulgarian Split Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Lat Pulldown", "targetType": "reps", "category": "pull"},
            {"exercise": "Side Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Back Extension", "targetType": "reps", "category": "hinge"},
        ],
    },
    # Phase 28 — general, free/no-equipment-required strength content for women
    # (confirmed with the user: general evidence-based scope, not based on any
    # specific external guide/document). Explicitly no HIIT/plyometric/cardio-
    # interval moves — straightforward progressive-overload strength work, reusing
    # the exact same category vocab as every other template above (no new
    # progression logic needed).
    "womens_at_home": {
        "A": [
            {"exercise": "Bodyweight Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Incline Push-up", "targetType": "reps", "category": "push"},
            {"exercise": "Resistance Band Row", "targetType": "reps", "category": "pull"},
            {"exercise": "Glute Bridge", "targetType": "reps", "category": "hinge"},
            {"exercise": "Dead Bug", "targetType": "reps", "category": "core"},
        ],
        "B": [
            {"exercise": "Reverse Lunge", "targetType": "reps", "category": "squat"},
            {"exercise": "Single-leg Glute Bridge", "targetType": "reps", "category": "hinge"},
            {"exercise": "Resistance Band Pull-apart", "targetType": "reps", "category": "pull"},
            {"exercise": "Plank", "targetType": "hold_sec", "category": "core"},
            {"exercise": "Wall Push-up", "targetType": "reps", "category": "push"},
        ],
    },
    # Phase 28 — equipment-based variant of the same focus above, for a gym setting.
    "womens_at_gym": {
        "A": [
            {"exercise": "Goblet Squat", "targetType": "reps", "category": "squat"},
            {"exercise": "Lat Pulldown", "targetType": "reps", "category": "pull"},
            {"exercise": "Romanian Deadlift", "targetType": "reps", "category": "hinge"},
            {"exercise": "Cable Chest Press", "targetType": "reps", "category": "push"},
            {"exercise": "Side Plank", "targetType": "hold_sec", "category": "core"},
        ],
        "B": [
            {"exercise": "Leg Press", "targetType": "reps", "category": "squat"},
            {"exercise": "Seated Cable Row", "targetType": "reps", "category": "pull"},
            {"exercise": "Hip Thrust", "targetType": "reps", "category": "hinge"},
            {"exercise": "Dumbbell Overhead Press", "targetType": "reps", "category": "push"},
            {"exercise": "Dead Bug", "targetType": "reps", "category": "core"},
        ],
    },
}
# Weeks of trailing Run/Ride mileage checked before auto-picking runner_focus over
# full_body_ab as the quick-generate default (see run_quick_generate) — a real
# recent cardio habit, not just one lucky week.
STRENGTH_AUTO_TARGET_LOOKBACK_WEEKS = 4
STRENGTH_AUTO_TARGET_MIN_MILES = 8.0
WEIGHT_INCREMENT_LB = {"squat": 10, "hinge": 10, "push": 5, "pull": 5, "core": 0}
HOLD_INCREMENT_SEC = 5
HOLD_CAP_SEC = 60
# strength_days_per_week -> which weekdays (Mon=0) host a session
WEEKDAY_STRENGTH_SLOTS = {1: [1], 2: [0, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4]}


def _build_exercise_step(ex: dict, progress: dict, light: bool) -> dict:
    rest_seconds = 90 if ex["category"] in ("squat", "hinge") else 60
    set_count = 2 if light else 3
    if ex["targetType"] == "hold_sec":
        hold_sec = 20 if light else (progress["currentHoldSec"] or 20)
        sets = [
            {"index": i, "targetType": "hold_sec", "targetReps": None, "targetHoldSec": hold_sec,
             "targetWeightLb": None, "actualReps": None, "actualHoldSec": None, "actualWeightLb": None,
             "completedAt": None}
            for i in range(set_count)
        ]
    else:
        reps = progress["currentRepsTarget"] or 8
        weight = None if light else progress["currentWeightLb"]
        sets = [
            {"index": i, "targetType": "reps", "targetReps": reps, "targetHoldSec": None,
             "targetWeightLb": weight, "actualReps": None, "actualHoldSec": None, "actualWeightLb": None,
             "completedAt": None}
            for i in range(set_count)
        ]
    return {"stepType": "strength_exercise", "exercise": ex["exercise"], "restSeconds": rest_seconds, "sets": sets}


def _default_strength_template(db, user_id) -> str:
    """Phase 28 — the generic (non-cardio-linked) strength-template default, shared
    by _auto_pick_strength_template below and both training-config default-
    construction points (this module's _get_training_config and core.py's
    get_training_config) so a female user gets the tailored template as her
    starting point everywhere, not just one path."""
    user = db.get(User, user_id)
    return "womens_at_home" if user and user.sex == "female" else "full_body_ab"


def _auto_pick_strength_template(db, user_id, date) -> str:
    """Phase 14 — quick-generate default when the user hasn't explicitly chosen a
    target: complement real recent cardio training (runner_focus) rather than
    always defaulting to the generic default. Checked against a real trailing-weeks
    lookback, not just one lucky week, to avoid flip-flopping. The cardio-volume-
    linked runner_focus branch is activity-based and unaffected by Phase 28's
    sex-aware fallback below (which only changes what "no strong cardio habit"
    falls through to)."""
    week_start = _week_start(date)
    total = 0.0
    for i in range(STRENGTH_AUTO_TARGET_LOOKBACK_WEEKS):
        wk_start = week_start - timedelta(days=7 * i)
        total += _week_mileage(db, user_id, wk_start, "Run") + _week_mileage(db, user_id, wk_start, "Ride")
    return "runner_focus" if total >= STRENGTH_AUTO_TARGET_MIN_MILES else _default_strength_template(db, user_id)


def _generate_strength(db, user_id, date, readiness_result, config, template_override: str = None,
                        ignore_schedule: bool = False, dry_run: bool = False) -> dict | None:
    """`ignore_schedule` (Phase 14's Quick Generate button) forces today's occurrence
    regardless of WEEKDAY_STRENGTH_SLOTS — the button is an explicit "give me one
    now" action, not bound to the nightly rotation's day assignment."""
    days_per_week = config.strength_days_per_week or 2
    slots = WEEKDAY_STRENGTH_SLOTS.get(days_per_week, WEEKDAY_STRENGTH_SLOTS[2])
    if not ignore_schedule and date.weekday() not in slots:
        return None

    template_name = template_override or config.strength_template
    template = STRENGTH_TEMPLATES.get(template_name, STRENGTH_TEMPLATES["full_body_ab"])
    # A/B rotation: use the schedule-slot position when today really is a scheduled
    # day; otherwise (an off-schedule quick-generate) fall back to ISO day-of-year
    # parity so it still alternates sensibly across repeated presses on other days.
    if date.weekday() in slots:
        slot_index = slots.index(date.weekday())
    else:
        slot_index = date.timetuple().tm_yday
    half = "A" if slot_index % 2 == 0 else "B"
    exercises = template[half]

    flags = readiness_result["flags"]
    severe_health = _has_severe_health_note(db, user_id)
    light = severe_health or len(flags) >= 2

    steps = [
        _build_exercise_step(ex, coach.get_exercise_progress(db, ex["exercise"], user_id), light)
        for ex in exercises
    ]
    if light:
        notes = "Readiness/health flagged — light bodyweight session, no progression check this time."
    elif len(flags) == 1:
        notes = "Holding at current weights/targets — readiness flagged, pausing progression this session."
    else:
        notes = f"{template_name.replace('_', ' ').title()} {half} — prescribed from current progression."

    return _upsert_generator_workout(
        db, user_id, date.isoformat(), domain="strength", dry_run=dry_run,
        workout_type="strength", activity_type="Other", steps=steps, notes=notes,
    )


def apply_strength_progression(db, workout: Workout) -> None:
    """Called by coach.update_workout once a strength Workout's status transitions to
    "completed" with actuals logged. Double progression, evaluated per exercise: if
    every logged set in this session hit (or exceeded) its target, bump the exercise's
    ExerciseProgress for next time; otherwise hold steady (v1 never auto-decreases)."""
    steps = coach._steps_from_json(workout.steps_json) or []
    for step in steps:
        if step.get("stepType") != "strength_exercise":
            continue
        sets = step.get("sets", [])
        if not sets or any(s.get("actualReps") is None and s.get("actualHoldSec") is None for s in sets):
            continue  # not actually logged — nothing to evaluate
        progress = coach.get_exercise_progress(db, step["exercise"], workout.user_id or DEFAULT_USER_ID)
        now_iso = datetime.now(timezone.utc).isoformat()
        if sets[0]["targetType"] == "hold_sec":
            hit_all = all((s.get("actualHoldSec") or 0) >= (s.get("targetHoldSec") or 0) for s in sets)
            if hit_all:
                new_hold = min((progress["currentHoldSec"] or sets[0]["targetHoldSec"] or 20) + HOLD_INCREMENT_SEC, HOLD_CAP_SEC)
                coach.upsert_exercise_progress(db, step["exercise"], workout.user_id or DEFAULT_USER_ID,
                                                current_hold_sec=new_hold, last_completed_at=now_iso)
            else:
                coach.upsert_exercise_progress(db, step["exercise"], workout.user_id or DEFAULT_USER_ID, last_completed_at=now_iso)
        else:
            hit_all = all((s.get("actualReps") or 0) >= (s.get("targetReps") or 0) for s in sets)
            if hit_all and sets[0].get("targetWeightLb") is not None:
                category = next(
                    (e["category"] for tpl in STRENGTH_TEMPLATES.values() for half in tpl.values()
                     for e in half if e["exercise"] == step["exercise"]),
                    "push",
                )
                increment = WEIGHT_INCREMENT_LB.get(category, 5)
                new_weight = (progress["currentWeightLb"] or sets[0]["targetWeightLb"] or 0) + increment
                coach.upsert_exercise_progress(db, step["exercise"], workout.user_id or DEFAULT_USER_ID,
                                                current_weight_lb=new_weight, last_completed_at=now_iso)
            else:
                coach.upsert_exercise_progress(db, step["exercise"], workout.user_id or DEFAULT_USER_ID, last_completed_at=now_iso)


# ---------- Recovery quick-generate (Phase 14) ----------


def _pick_recovery_tool(db, user_id) -> dict | None:
    """Auto-picks a tool for the Quick Generate button — the user's only tool if
    there's just one (the common case; create_recovery_tool isn't even chat-exposed
    yet), otherwise whichever was used in the most recent RecoverySession."""
    tools = coach.list_recovery_tools(db, user_id)
    if not tools:
        return None
    if len(tools) == 1:
        return tools[0]
    sessions = coach.list_recovery_sessions(db, user_id=user_id)
    if sessions:
        last_tool_id = sorted(sessions, key=lambda s: s["createdAt"])[-1]["toolId"]
        match = next((t for t in tools if t["id"] == last_tool_id), None)
        if match:
            return match
    return tools[0]


def _generate_recovery(db, user_id, date, readiness_result, dry_run: bool = False) -> dict | None:
    """Level/duration scale with the current readiness flag count, within the
    tool's own supported range/increment — mirrors RECOVERY_GUIDANCE_PROMPT's
    existing escalation logic for the coach itself. Idempotent per (user, date),
    matching the Workout quick-generate domains — a second press the same day
    updates the existing planned session in place rather than creating a duplicate
    (recommend_recovery_session itself always creates fresh, since its other caller,
    the chat tool, has no such day-collision concern). `dry_run=True` skips the DB
    entirely (no existing-row lookup, no create/update), same contract as
    _upsert_generator_workout."""
    tool = _pick_recovery_tool(db, user_id)
    if not tool:
        return None
    date_str = date.isoformat()
    flag_count = len(readiness_result["flags"])
    scale = min(flag_count, 2) / 2  # 0 flags -> 0.0, 1 -> 0.5, 2+ -> 1.0
    level = round(tool["minLevel"] + (tool["maxLevel"] - tool["minLevel"]) * scale)
    level = max(tool["minLevel"], min(tool["maxLevel"], level))
    increment = tool["durationIncrementMin"] or 15
    raw_duration = tool["minDurationMin"] + (tool["maxDurationMin"] - tool["minDurationMin"]) * scale
    duration = tool["minDurationMin"] + round((raw_duration - tool["minDurationMin"]) / increment) * increment
    duration = max(tool["minDurationMin"], min(tool["maxDurationMin"], duration))
    rationale = (
        f"Quick-generated — {flag_count} readiness flag{'s' if flag_count != 1 else ''}, scaled level/duration accordingly."
        if flag_count else "Quick-generated — readiness looks clean, moderate session."
    )

    if dry_run:
        return {
            "id": None, "toolId": tool["id"], "scheduledDate": date_str,
            "level": level, "durationMin": duration, "zoneBoost": False,
            "rationale": rationale, "status": "planned", "createdAt": None,
        }

    existing = (
        db.query(RecoverySession)
        .filter(RecoverySession.scheduled_date == date_str, RecoverySession.status == "planned",
                owned_by(RecoverySession.user_id, user_id))
        .first()
    )
    if existing:
        existing.tool_id = tool["id"]
        existing.level = level
        existing.duration_min = duration
        existing.rationale = rationale
        db.commit()
        return coach._recovery_session_to_dict(existing)
    return coach.recommend_recovery_session(
        db, tool["id"], date_str, level, duration, zone_boost=False, rationale=rationale, user_id=user_id,
    )


# ---------- Orchestration ----------

QUICK_GENERATE_DOMAINS = ("run", "ride", "strength", "recovery")


def run_quick_generate(db, user_id: str, domain: str, date=None, template_override: str = None,
                        dry_run: bool = False) -> dict:
    """Phase 14 — the Quick Generate button's entry point. Forces generation of
    exactly the requested domain for `date` (defaults to today), overriding whatever
    the day-of-week/strength schedule would otherwise decide for that day — the
    button is an explicit "give me one right now" action, not a scheduling action.
    Still uses the real phase/budget/readiness-gate (and, for run/ride, cold-start-
    aware) logic underneath; only *which* day gets a session is overridden, not how
    it's computed.

    `dry_run=True` (Phase 14.6's preview-before-confirm New Workout flow) computes
    the exact same result without writing anything to the DB — calling again with
    `dry_run=False` right after must reproduce the identical prescription, since
    nothing about the underlying computation is randomized or preview-specific."""
    if domain not in QUICK_GENERATE_DOMAINS:
        raise ValueError(f"domain must be one of {QUICK_GENERATE_DOMAINS}")
    target = date or local_today(user_id)
    if isinstance(target, str):
        target = datetime.strptime(target, "%Y-%m-%d").date()
    config = _get_training_config(db, user_id)
    readiness_result = stats.readiness(db, user_id, target)

    if domain == "run":
        result = _generate_endurance(db, user_id, target, readiness_result, config,
                                      activity_type="Run", ignore_schedule=True, dry_run=dry_run)
    elif domain == "ride":
        result = _generate_endurance(db, user_id, target, readiness_result, config,
                                      activity_type="Ride", ignore_schedule=True, dry_run=dry_run)
    elif domain == "strength":
        chosen_template = template_override or _auto_pick_strength_template(db, user_id, target)
        result = _generate_strength(db, user_id, target, readiness_result, config,
                                     template_override=chosen_template, ignore_schedule=True, dry_run=dry_run)
    else:  # "recovery"
        result = _generate_recovery(db, user_id, target, readiness_result, dry_run=dry_run)

    return {"date": target.isoformat(), "domain": domain, "readiness": readiness_result, "result": result}


def run_for_user(db, user_id: str = DEFAULT_USER_ID, date=None) -> dict:
    target = date or local_today(user_id)
    if isinstance(target, str):
        target = datetime.strptime(target, "%Y-%m-%d").date()
    config = _get_training_config(db, user_id)
    readiness_result = stats.readiness(db, user_id, target)
    endurance = _generate_endurance(db, user_id, target, readiness_result, config)
    strength = _generate_strength(db, user_id, target, readiness_result, config)
    return {"date": target.isoformat(), "readiness": readiness_result, "endurance": endurance, "strength": strength}


def run_for_all_users(date=None) -> dict:
    db = SessionLocal()
    try:
        users = db.query(User).filter(or_(User.is_demo == False, User.is_demo.is_(None))).all()  # noqa: E712
        results = {}
        for user in users:
            try:
                result = run_for_user(db, user.id, date)
                results[user.id] = result
                # P12 — push notification when workout is prescribed (best-effort, no-op if unsubscribed)
                has_workout = result.get("endurance") or result.get("strength")
                if has_workout:
                    try:
                        from ..push import send_push
                        workout_type = "endurance" if result.get("endurance") else "strength"
                        send_push(db, user.id, "Workout Ready", f"Your {workout_type} workout for tomorrow is ready.", "/workouts")
                    except Exception:
                        log.exception(f"push notification failed for generator user {user.id}")
            except Exception as e:
                log.warning(f"generator: run failed for {user.id}: {e}")
                results[user.id] = {"error": str(e)}
        return results
    finally:
        db.close()
