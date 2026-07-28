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
from statistics import median

from sqlalchemy import or_

from ..models import (
    SessionLocal, Workout, Goal, User, UserTrainingConfig, HealthNote,
    WeeklyPlan, RecoverySession, TrainingPlan, PlanGoal, DEFAULT_USER_ID, owned_by,
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

# Trailing window the ramp base's median is taken over. 6 weeks spans a full 3:1
# mesocycle plus buffer, so a scheduled deload can't by itself look like a decline.
RAMP_BASE_WINDOW_WEEKS = 6
# How far back to look for a *returning* athlete once the recent window is empty.
RETURNING_ATHLETE_LOOKBACK_WEEKS = 52
# What fraction of their old median to resume at. Detraining after a multi-week layoff
# is real, so resuming at the previous volume outright would be wrong — but so would
# restarting a marathoner at COLD_START_INITIAL_MILES. A conservative fraction is the
# honest middle, and the normal weekly ramp climbs back from there. Not derived from a
# specific study; it's a deliberately cautious v1 constant, and erring low is the safe
# direction for the one it can hurt.
RETURNING_ATHLETE_FRACTION = 0.60

# Shortest session worth prescribing, per discipline. The generator's distance math is
# calibrated for running throughout (COLD_START_INITIAL_MILES above is a running number),
# and the same figure means very different things on foot vs on a bike — 3 miles is a
# real easy run and a ~12-minute bike ride. Only disciplines listed here get a floor;
# an unlisted one keeps the pure computed value rather than inheriting a wrong default.
MIN_SESSION_MILES = {"Ride": 8.0}
_EPOCH_MONDAY = datetime(2020, 1, 6).date()  # any fixed Monday — used only to derive a stable week index

# 1-flag readiness downgrade ladder: interval -> tempo -> easy (stands in for "Z2",
# there's no distinct Zone-2 workout_type) -> "recovery" (also `easy`, shorter/lighter,
# distinguished only by a note — VALID_WORKOUT_TYPES has no separate recovery value).
DOWNGRADE_LADDER = ["interval", "tempo", "easy", "easy"]

# Very simple fixed weekly skeleton (Monday=0..Sunday=6) — "what would today be,
# absent any readiness gating." Real periodization systems vary this by phase; this
# is a deliberately small v1 template, not a full training-plan generator.
WEEKDAY_SKELETON = {0: "easy", 1: "quality", 2: "easy", 3: "cross_train", 4: "rest", 5: "long", 6: "easy"}

# "HH:MM" local. Past this, a long run on that day gets an informational note (never a
# schedule change) — see the sleep block in _generate_endurance. String comparison is
# safe only because both sides are zero-padded HH:MM from typical_wake_time.
LATE_WAKE_THRESHOLD = "08:00"


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


def _active_training_plan(db, user_id) -> TrainingPlan | None:
    """The user's single active plan, or None. P21 allows exactly one (see TrainingPlan's
    docstring); .first() rather than .one() so a hand-edited DB with two can't crash the
    nightly generator over a data problem it isn't responsible for fixing."""
    return (db.query(TrainingPlan)
            .filter(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
            .first())


def resolve_periodization_goal(db, user_id, date) -> Goal | None:
    """P21 — which goal actually drives the generator's real prescriptions. An active
    TrainingPlan's "primary" PlanGoal wins once one exists: starting a plan and naming a
    primary goal is the explicit "this is what I'm training for" signal the plan builder
    exists to give the user, per docs/P21_CONCURRENT_GOALS.md §3.1 ("the primary goal owns
    periodization"). Falls back to nearest_active_race_goal (P20 and earlier's implicit
    behavior — nearest upcoming race wins) whenever no plan has been started, or the
    started plan has no primary goal, or that goal is no longer a live active race — so a
    user who never touches the Workouts-tab plan builder sees zero behavior change, and a
    stale/completed primary goal can never orphan periodization instead of degrading."""
    plan = _active_training_plan(db, user_id)
    if plan:
        primary = (db.query(PlanGoal)
                   .filter(PlanGoal.training_plan_id == plan.id, PlanGoal.role == "primary")
                   .first())
        if primary:
            goal = db.get(Goal, primary.goal_id)
            if (goal and goal.goal_type == "race" and goal.status == "active"
                    and goal.target_date >= date.isoformat() and goal.periodizes_training is not False):
                return goal
    return nearest_active_race_goal(db, user_id, date)


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


def weekly_mileage_map(db, user_id, activity_type) -> dict:
    """{week_start_date: miles} for every week with activity, in ONE pass.

    _week_mileage routes through stats._all_runs (required — it's the only
    dedup-and-normalize entrypoint), which loads and merges the entire activity set on
    every call. Examining N weeks one at a time therefore did N full scans; the ramp
    base now looks at up to 52 weeks and the plan view examines ~20 weeks, which
    multiplied out to thousands of redundant scans. Bucketing once collapses that to one."""
    out = {}
    for r in stats._all_runs(db, activity_type, user_id):
        if not r.date:
            continue
        wk = _week_start(datetime.strptime(r.date, "%Y-%m-%d").date())
        out[wk] = out.get(wk, 0.0) + (r.distance_mi or 0)
    return out


def rolling_window_mileage(db, user_id, activity_type="Run", days=7, as_of=None) -> float:
    """Mileage over the trailing `days`, inclusive of today — a rolling counterpart to the
    calendar-week figure the ramp math uses. Not interchangeable with it: on a Monday the
    calendar week is legitimately 0.0 while the trailing week is a full training load, and
    only the rolling number answers "where am I right now"."""
    as_of = as_of or local_today(user_id)
    start = (as_of - timedelta(days=days - 1)).isoformat()
    end = as_of.isoformat()
    return sum(
        r.distance_mi or 0 for r in stats._all_runs(db, activity_type, user_id)
        if r.date and start <= r.date <= end
    )


def _ramp_base_mileage(db, user_id, before_week_start, activity_type, config=None, weekly_map=None):
    """The volume the next week's ramp builds from. Returns (miles, is_cold_start).

    Uses the MEDIAN of nonzero weeks in a trailing window, not the most recent nonzero
    week. The old behavior trusted a single week as gospel and was fragile in both
    directions — measured against real history (which swings 5.9 to 33.7 mi/wk):

        scenario                current (last nonzero)   median/6w
        normal                            28.3              24.4
        one light 5mi week                 5.0  <- crater   24.4
        one freak 50mi week               50.0  <- inflate  30.6

    A median is unmoved by a single outlier but still tracks a sustained change, which
    is exactly the wanted behavior: one skipped or travel-shortened week doesn't restart
    the ramp from scratch, while a genuine multi-week reduction does lower the base.

    MEAN, MODE AND TREND WERE EVALUATED AND REJECTED — don't reintroduce them without new
    evidence. Measured over 12 real weeks (excluding deloads, n=9):

        mean 24.74 | median 28.30 | Theil-Sen slope -0.10 mi/wk | OLS slope -0.57 mi/wk

    - Mean is worse: the distribution is left-skewed by occasional sub-7-mile weeks, so
      the mean lets a barely-run week drag down what the athlete builds from.
    - Trend is indistinguishable from flat. The robust (Theil-Sen) and least-squares
      slopes disagree by 6x, which is itself the tell that OLS is being yanked by two
      light weeks rather than tracking anything real. A trend term here would fit noise,
      and a spurious negative slope would silently shrink the plan.
    - Mode is undefined on continuous weekly mileage — no two weeks are equal, so it only
      exists after binning, and the answer then depends on where the bin edges land. It
      restated the median and added an arbitrary choice.

    Zero weeks are excluded rather than averaged in, preserving the Phase 14 distinction
    this function was originally written for: a complete gap (no bike on holiday, a
    week off) must not drag the base down, because "didn't train" is not "trained a
    little". Only real weeks describe real fitness.

    Falls through three seeds when the window is empty, most-trusted first:
      1. Returning athlete — real history further back, discounted for detraining.
      2. A user-declared seed (they know their own baseline; we don't yet).
      3. Genuine cold start, the conservative linear ramp.
    """
    weekly_map = weekly_map if weekly_map is not None else weekly_mileage_map(db, user_id, activity_type)

    def _window(start_offset, end_offset, skip_deloads=True):
        vals = []
        for i in range(start_offset, end_offset + 1):
            wk = before_week_start - timedelta(days=7 * i)
            mi = weekly_map.get(wk, 0.0)
            if mi <= 0:
                continue
            # A deload is a PLANNED reduction, so it describes the schedule, not the
            # athlete's capacity. Letting it into the base means every mesocycle ratchets
            # the ramp downward: _compute_weekly_budget multiplies the deload week by 0.75,
            # and the next block then builds 3% up from that deflated number instead of
            # resuming at the pre-deload level, which is backwards from how periodization
            # is supposed to work. Verified on real data — the deload weeks here (20.4,
            # 26.1) sit right in the middle of the distribution and were dragging the
            # median down purely by rank.
            if skip_deloads and config is not None and _is_deload_week(config, wk):
                continue
            vals.append(mi)
        return vals

    recent = _window(1, RAMP_BASE_WINDOW_WEEKS)
    if not recent:
        # Every recent week was a deload (or the window is short). Falling through to the
        # returning-athlete/cold-start seeds here would be badly wrong for someone who is
        # training normally, so use the deload weeks rather than pretending there's no
        # history — a slightly low base beats a fabricated one.
        recent = _window(1, RAMP_BASE_WINDOW_WEEKS, skip_deloads=False)
    if recent:
        return median(recent), False

    # Nothing recent. Real history further back means a returning athlete, not a beginner —
    # resuming at their old volume outright would ignore genuine detraining, but restarting
    # at the 3-mile cold-start floor is equally wrong and far more discouraging.
    older = _window(RAMP_BASE_WINDOW_WEEKS + 1, RETURNING_ATHLETE_LOOKBACK_WEEKS)
    if older:
        return median(older) * RETURNING_ATHLETE_FRACTION, False

    seeded = _seed_weekly_miles(config, activity_type)
    if seeded:
        return seeded, False

    return 0.0, True


def _seed_weekly_miles(config, activity_type: str) -> float | None:
    """A user-declared starting volume for a discipline with no history at all — they
    know they already ride 20mi/wk on a bike this app has never seen; the app doesn't.
    Absent one, the cold-start ramp applies unchanged."""
    if config is None or not getattr(config, "seed_weekly_miles_json", None):
        return None
    try:
        seeds = json.loads(config.seed_weekly_miles_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(seeds, dict):
        return None
    val = seeds.get(activity_type) or seeds.get(stats._normalize_activity_type(activity_type))
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _weeks_active_in_activity(db, user_id, before_week_start, activity_type, max_weeks=20,
                               weekly_map=None) -> int:
    """How many of the trailing max_weeks had any mileage in this activity — a simple
    proxy for "how many weeks into this activity's cold-start ramp am I," used to
    size the linear increment. Only meaningful once _ramp_base_mileage has already
    concluded this is a cold start."""
    weekly_map = weekly_map if weekly_map is not None else weekly_mileage_map(db, user_id, activity_type)
    return sum(
        1 for i in range(1, max_weeks + 1)
        if weekly_map.get(before_week_start - timedelta(days=7 * i), 0.0) > 0
    )


# Relative weight of each day type within a week. These are RATIOS, not fractions of the
# budget — see _normalized_day_share for why that distinction matters.
DAY_SHARE = {"long": 0.30, "tempo": 0.18, "interval": 0.15, "easy": 0.10, "cross_train": 0.10, "rest": 0}


def resolve_weekday_skeleton(plan=None) -> dict:
    """P21 — WEEKDAY_SKELETON adjusted for one plan's declared availability and long-run
    day. `plan=None` (or a plan with neither field set) returns the module default
    unchanged, so every pre-P21 caller and any user who never opens the plan builder gets
    byte-identical behavior.

    Two adjustments, applied in this order for a reason:

    1. `long_run_day` swaps the long session onto the requested weekday, trading places
       with whatever sat there — a swap rather than an overwrite, so the displaced session
       isn't silently lost.
    2. `available_days_json` turns every unavailable day into a rest day.

    Availability is applied second and therefore wins, because "I cannot train Tuesday" is
    a fact about the athlete's week while "I prefer my long run on Tuesday" is a
    preference. But a long run must never be silently deleted by that rule — it's the
    single most important session in an endurance week — so if the requested (or default)
    long day turns out to be unavailable, the long run relocates to the latest available
    day instead of vanishing. If NO day is available the week is honestly all rest; that's
    a real answer to a real input, not a failure to handle it."""
    skeleton = dict(WEEKDAY_SKELETON)
    if plan is None:
        return skeleton

    long_day = getattr(plan, "long_run_day", None)
    if long_day is not None and 0 <= long_day <= 6 and skeleton.get(long_day) != "long":
        current_long = next((d for d, t in skeleton.items() if t == "long"), None)
        if current_long is not None:
            skeleton[current_long], skeleton[long_day] = skeleton[long_day], "long"
        else:
            skeleton[long_day] = "long"

    available = _parse_available_days(getattr(plan, "available_days_json", None))
    if available is not None:
        long_day_now = next((d for d, t in skeleton.items() if t == "long"), None)
        for d in range(7):
            if d not in available:
                skeleton[d] = "rest"
        if long_day_now is not None and long_day_now not in available and available:
            skeleton[max(available)] = "long"
    return skeleton


def _parse_available_days(raw) -> set | None:
    """None means "no availability declared" — every day usable, today's behavior. An
    explicitly empty list is NOT the same thing and is preserved as an empty set (a week
    with no training days), rather than being quietly treated as "all days"."""
    if not raw:
        return None
    try:
        days = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(days, list):
        return None
    return {d for d in days if isinstance(d, int) and 0 <= d <= 6}


def _skeleton_workout_type(weekday: int, phase: str, skeleton: dict | None = None) -> str:
    """The base session type for a weekday, before any readiness gating. Shared by the
    generator and by _normalized_day_share so the two can't disagree about what a week
    is supposed to contain — which is also why `skeleton` has to be threaded through both
    rather than resolved independently inside each."""
    skeleton_type = (skeleton or WEEKDAY_SKELETON)[weekday]
    if skeleton_type == "quality":
        return "interval" if phase == "peak" else "tempo"
    return skeleton_type  # "easy" | "long" | "rest" | "cross_train"


def _normalized_day_share(workout_type: str, phase: str, skeleton: dict | None = None) -> float:
    """This day's fraction of the weekly budget, scaled so a full compliant week actually
    sums to that budget.

    The raw DAY_SHARE values don't add up to 1.0 across a real week — the skeleton's rest
    and cross-train days carry no distance, so the days that DO only totalled ~0.78. The
    weekly target was therefore unreachable by construction: following every prescription
    perfectly produced 19.5mi against a stated 25.0mi target, which is exactly the kind of
    number-that-doesn't-add-up this view exists to avoid showing. Normalizing by the week's
    own total makes the target mean what it says.

    Downgrades (a readiness-gated tempo becoming easy) still legitimately land under
    budget — that's a real reduction, not an accounting error."""
    total = sum(
        DAY_SHARE.get(_skeleton_workout_type(d, phase, skeleton), 0.10)
        for d in range(7)
        if _skeleton_workout_type(d, phase, skeleton) not in ("rest", "cross_train")
    )
    if total <= 0:
        return 0.0
    return DAY_SHARE.get(workout_type, 0.10) / total


def _sessions_per_week(config, activity_type: str) -> int | None:
    """How many times a week this discipline is trained, when that's known. Returns None
    for the primary discipline (running), whose frequency is set by WEEKDAY_SKELETON
    rather than a config number — callers treat None as "use the normal day_share
    slicing". See _session_share's use in _generate_endurance."""
    if activity_type == "Ride":
        return config.ride_days_per_week or 0
    return None


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
    last_nonzero, is_cold_start = _ramp_base_mileage(db, user_id, week_start, "Run", config)
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


def plan_week_view(db, user_id, plan, goal, week_start, config=None, weekly_map=None) -> dict:
    """One already-started or in-progress week for one (TrainingPlan, goal) pair — P21's
    plan can serve several goals at once (docs/P21_CONCURRENT_GOALS.md §3), so `goal` is
    passed explicitly rather than read off a single `plan.goal_id` the way P20 shipped it;
    the route layer calls this once per PlanGoal attached to the plan.

    Reads the persisted WeeklyPlan row only when BOTH hold:

    1. This goal is genuinely the one driving periodization that week — real-time
       resolution via resolve_periodization_goal (P21), not just "nearest race" (P20).
       WeeklyPlan is a single global stream per user (not goal-scoped) whose target/
       deload/frozen belong to whatever goal is actually driving generation, so a goal
       that isn't currently driving it would display another goal's numbers as its own,
       silently — worse than showing nothing.
    2. This goal's discipline is running. WeeklyPlan is Run-only by construction
       (_get_or_create_weekly_plan hardcodes "Run"); its target is a *running* budget,
       so a cycling goal reading it would be the same category of lie as (1).

    Otherwise the week is replayed deterministically from this goal's own discipline."""
    activity_type = plan_activity_type(goal)
    config = config or _get_training_config(db, user_id)
    phase = _phase_for_date(db, user_id, week_start, goal=goal)
    is_deload = _is_deload_week(config, week_start)

    driving = resolve_periodization_goal(db, user_id, week_start)
    persisted = None
    if activity_type == "Run" and driving is not None and driving.id == goal.id:
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
        weekly_map = weekly_map if weekly_map is not None else weekly_mileage_map(db, user_id, activity_type)
        last_nonzero, is_cold_start = _ramp_base_mileage(
            db, user_id, week_start, activity_type, config, weekly_map=weekly_map)
        weeks_active = (_weeks_active_in_activity(db, user_id, week_start, activity_type,
                                                   weekly_map=weekly_map) if is_cold_start else 0)
        target_mi = round(_compute_weekly_budget(
            last_nonzero, is_cold_start, config.weekly_ramp_pct or 3.0, phase, is_deload, weeks_active), 1)
        frozen = False

    return {"phase": phase, "isDeload": is_deload, "frozen": frozen, "activityType": activity_type,
            "targetMi": target_mi, "isPersisted": persisted is not None}


def project_week_series(db, user_id, plan, goal, weeks_back=8, weeks_forward=12) -> list[dict]:
    """Week-by-week target vs actual for one (plan, goal) pair, spanning past and
    projected future. `goal` is explicit for the same reason as plan_week_view's — one
    P21 plan can serve several goals, so the route layer calls this once per attached
    PlanGoal.

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

    activity_type = plan_activity_type(goal)
    config = _get_training_config(db, user_id)
    current_week_start = _week_start(local_today(user_id))

    # Bucketed once and threaded through every week below. Each _week_mileage /
    # _ramp_base_mileage call would otherwise re-scan and re-deduplicate the whole
    # activity set, and this loop makes ~20 of them, each looking back up to 52 weeks.
    weekly_map = weekly_mileage_map(db, user_id, activity_type)

    cursor = current_week_start - timedelta(weeks=weeks_back)
    end = current_week_start + timedelta(weeks=weeks_forward)
    weeks, rolling_base_mi = [], None

    while cursor <= end:
        is_future = cursor > current_week_start
        if not is_future:
            wv = plan_week_view(db, user_id, plan, goal, cursor, config, weekly_map=weekly_map)
            phase, is_deload, frozen = wv["phase"], wv["isDeload"], wv["frozen"]
            target_mi, is_persisted = wv["targetMi"], wv["isPersisted"]
            # This plan's own discipline, not a hardcoded "Run" — a cycling goal's
            # "actual" must be cycling miles. float() so a zero-mileage week serializes
            # as 0.0, not 0 — a type that flips with the data is a nuisance downstream.
            actual_mi = float(round(weekly_map.get(cursor, 0.0), 1))
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


def workout_duration_hours(workout, fallback_pace_sec_per_mi: float | None = None) -> float | None:
    """P21 — the one place a Workout's time cost is derived, for §3.3's essential-hours-
    vs-weekly_hours_cap conflict check (docs/P21_CONCURRENT_GOALS.md §6 Q2/Q3). Prefers
    the workout's own target_duration_sec; falls back to target_distance_mi ×
    target_pace_sec_per_mi when only those are set. Deliberately NOT a flat distance ×
    constant-pace shortcut — a workout's own prescribed pace is genuinely slower for a
    long run than an easy run, and the distance itself grows toward peak, so reading the
    real per-workout fields is what makes the hours estimate track that growth honestly
    instead of underestimating a peak-block long run.

    `fallback_pace_sec_per_mi` (stats.recent_typical_pace_sec_per_mi) covers HALE's own
    generator output, which prescribes distance with no pace and was therefore reporting
    every session as unknown-duration. A workout's own pace still wins when it has one —
    the fallback is a population median and shouldn't override a specific prescription.

    Returns None (never 0) when duration genuinely can't be derived, e.g. a strength
    session with no target_duration_sec and nothing to estimate from."""
    if workout.target_duration_sec:
        return workout.target_duration_sec / 3600.0
    pace = workout.target_pace_sec_per_mi or fallback_pace_sec_per_mi
    if workout.target_distance_mi and pace:
        return (workout.target_distance_mi * pace) / 3600.0
    return None


def weekly_hours_plan(db, user_id, week_start, plan=None) -> dict | None:
    """P21 §3.3 — what HALE's own prescribed week costs in hours, against the plan's
    declared `weekly_hours_cap`. Returns None when no plan exists or no cap is set: with
    no cap there is nothing to be over, and inventing one from training history is the
    fabrication this whole design refuses (see TrainingPlan.weekly_hours_cap).

    **Counts only `source="generator"` sessions, deliberately.** Up to three planners
    write rows for the same date — Garmin's adaptive plan, a coach/chat-scheduled
    session, and HALE's own — and the athlete does *one* of them, not all three
    (confirmed in production: 2026-07-28 held a Garmin 76min easy, a Coach 78min
    interval, and a HALE 3.7mi easy simultaneously). Summing every source would report
    ~3.9 hours for a single day and turn the cap into noise. So this answers a bounded,
    honest question — "does the plan HALE prescribes fit your week?" — and the UI has to
    say that's what it is rather than implying it covers everything you'll actually do.

    `unknownSessions` is reported rather than silently treated as zero: a session whose
    duration can't be derived (workout_duration_hours returns None) makes the total a
    floor, not a complete figure, and a cap comparison that hid that would understate
    the demand. Real case at time of writing — strength rows generated before
    target_duration_sec was populated."""
    plan = plan if plan is not None else _active_training_plan(db, user_id)
    week_end = week_start + timedelta(days=6)
    rows = (
        db.query(Workout)
        .filter(Workout.scheduled_date >= week_start.isoformat(),
                Workout.scheduled_date <= week_end.isoformat(),
                Workout.workout_type != "rest",
                owned_by(Workout.user_id, user_id))
        .all()
    )
    fallback_pace = stats.recent_typical_pace_sec_per_mi(db, user_id)

    # Per planner, NOT summed. Up to three planners write rows for the same date and the
    # athlete does one of them (production's 2026-07-28 held a Garmin 76min easy, a Coach
    # 78min interval and a HALE 3.7mi easy at once), so a combined total would report
    # ~3.9h for a single day. Reporting them side by side is the honest shape: two plans
    # genuinely disagree, and by how much is the useful signal.
    by_source: dict[str, dict] = {}
    longest_session = 0.0
    for w in rows:
        src = w.source or "coach"  # legacy-NULL rows predate the column
        bucket = by_source.setdefault(src, {"hours": 0.0, "unknownSessions": 0})
        h = workout_duration_hours(w, fallback_pace_sec_per_mi=fallback_pace)
        if h is None:
            bucket["unknownSessions"] += 1
        else:
            bucket["hours"] += h
            longest_session = max(longest_session, h)
    for v in by_source.values():
        v["hours"] = round(v["hours"], 2)

    result = {
        "bySource": by_source,
        "longestSessionHours": round(longest_session, 2) or None,
        "capHours": None, "demandHours": None, "overBy": None, "isOverCap": False,
        "unknownSessions": 0,
    }
    if plan is None or plan.weekly_hours_cap is None:
        # No cap set is not "a cap of zero" — there is nothing to be over, and inventing
        # one from history is what this design refuses. The per-source breakdown above is
        # still real and useful, so it's returned either way.
        return result

    # The cap comparison uses HALE's own plan: it's the only week this app is actually
    # responsible for, and the figure has to mean one specific thing to be actionable.
    hale = by_source.get(GENERATOR_SOURCE, {"hours": 0.0, "unknownSessions": 0})
    cap, demand = float(plan.weekly_hours_cap), hale["hours"]
    result.update({
        "capHours": cap,
        "demandHours": demand,
        "unknownSessions": hale["unknownSessions"],
        "overBy": round(demand - cap, 2) if demand > cap else None,
        # A conflict is *surfaced*, never auto-resolved. Silently shrinking the primary
        # goal's long run to fit a cap is explicitly rejected in the design doc: the
        # honest options (raise the cap, drop a supporting goal, move a race date) are
        # the athlete's call, not the generator's.
        "isOverCap": demand > cap,
    })
    return result


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
    phase = _phase_for_date(db, user_id, date, goal=resolve_periodization_goal(db, user_id, date))
    # P21 — the athlete's declared available days / long-run day, when a plan exists.
    # `training_plan` (not `plan`, which below is the per-week WeeklyPlan row) is the
    # TrainingPlan. Resolved once and threaded into BOTH the day-type lookup and the
    # day-share normalization: those two must agree about what the week contains, or
    # the weekly target stops being reachable (see _normalized_day_share's docstring).
    training_plan = _active_training_plan(db, user_id)
    skeleton = resolve_weekday_skeleton(training_plan)
    flags = readiness_result["flags"]
    severe_health = _has_severe_health_note(db, user_id)

    # Computed regardless of which branch below actually sources the budget — needed
    # by both (Run's persisted WeeklyPlan doesn't record whether *it* used the
    # cold-start branch internally) for the day_share decision further down.
    _, is_cold_start = _ramp_base_mileage(db, user_id, week_start, activity_type, config)

    if activity_type == "Run":
        plan = _get_or_create_weekly_plan(db, user_id, week_start, phase, config)
        budget = plan.target_tss or 20.0
        is_deload = plan.is_deload
    else:
        last_nonzero, _ = _ramp_base_mileage(db, user_id, week_start, activity_type, config)
        weeks_active = _weeks_active_in_activity(db, user_id, week_start, activity_type) if is_cold_start else 0
        is_deload = _is_deload_week(config, week_start)
        budget = _compute_weekly_budget(
            last_nonzero, is_cold_start, config.weekly_ramp_pct or 3.0, phase, is_deload, weeks_active,
        )
        plan = None

    base_type = "easy" if ignore_schedule else _skeleton_workout_type(date.weekday(), phase, skeleton)

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

    # P21 §2.4 — sleep awareness is a SOFT note and nothing else: it never changes the
    # workout type, the distance, or which day this lands on. long_run_day is an explicit
    # user choice, and a derived multi-week average must not silently override an explicit
    # choice. Deliberately no "hard" mode exists (see TrainingPlan.sleep_constraint_mode).
    if (workout_type == "long" and training_plan is not None
            and (training_plan.sleep_constraint_mode or "soft") == "soft"):
        wake = stats.typical_wake_time(db, user_id, weekday=date.weekday())
        if wake and wake > LATE_WAKE_THRESHOLD:
            trigger_notes.append(
                f"Heads up — you typically wake around {wake} on this day, so an early "
                f"long run may cut sleep short. Schedule unchanged.")

    target_distance_mi = None
    if workout_type not in ("rest",) and workout_type != "cross_train":
        if is_cold_start:
            # Real bug caught in testing: a cold-start `budget` already represents a
            # sensible single-session distance (see COLD_START_INITIAL_MILES), not a
            # weekly total to slice up — running it through day_share produced an
            # absurd "first run" (3mi budget * 10% easy-day share = 0.3mi).
            target_distance_mi = round(budget, 1)
        else:
            # DAY_SHARE's fractions assume the discipline is trained ~6-7x/week, which is
            # only true of the primary one. For a discipline done once a week the weekly
            # budget IS the session, and slicing it again produces the same absurdity the
            # cold-start branch above already guards against — confirmed against real data:
            # a 3.1mi weekly ride budget * 0.10 easy share = a 0.3mi "bike ride".
            sessions = _sessions_per_week(config, activity_type)
            if sessions and sessions <= 1:
                target_distance_mi = round(budget, 1)
            else:
                target_distance_mi = round(budget * _normalized_day_share(workout_type, phase, skeleton), 1)
        if target_distance_mi is not None:
            floor = MIN_SESSION_MILES.get(activity_type)
            if floor and target_distance_mi < floor:
                # Distance floors are discipline-specific: 3 miles is a real easy run and
                # a meaningless bike ride (~12 min). Without this, a user just starting a
                # discipline gets prescriptions too short to be worth doing, and the ramp
                # takes months to reach a useful session.
                target_distance_mi = floor

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
# P21 — a priori duration estimate for workout_duration_hours' §3.3 conflict check.
# _generate_strength runs before a session happens, so unlike endurance (whose distance/
# pace ARE the prescription) there's no real duration to read yet; a flat estimate is
# honest here specifically because every template above has the same 5-exercise shape —
# a per-template value would imply a precision none of them actually have. Real completed-
# session TSS (P5) is what should count once a session is logged; this is a planning
# input only, never allowed to overwrite that.
STRENGTH_SESSION_DURATION_MIN = 45
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
        target_duration_sec=STRENGTH_SESSION_DURATION_MIN * 60,
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


def _is_ride_day(config, date) -> bool:
    """Whether the weekly secondary ride lands on this date. It takes over the skeleton's
    cross_train slot, which otherwise generates a contentless "Cross-train (Other)"
    placeholder — a real prescribed ride is strictly more useful there, and reusing the
    slot means the ride costs no extra training day.

    Only one ride/week is supported: WEEKDAY_SKELETON has exactly one cross_train slot,
    so any value >1 still yields one ride. Placing N rides needs the real availability
    model in docs/P21_CONCURRENT_GOALS.md, not a bigger number here."""
    return bool(config.ride_days_per_week or 0) and WEEKDAY_SKELETON[date.weekday()] == "cross_train"


def _clear_placeholder_cross_train(db, user_id, date_str) -> None:
    """Drop a still-planned cross_train placeholder the Run path left on this date before
    the ride was switched on. Without this, opting in leaves both the old empty
    placeholder and the new ride on the same day, since they occupy different upsert
    domains ("endurance" vs "endurance_ride"). Only ever removes a generator-created,
    still-`planned`, distance-less cross_train row — never a completed one (history is
    immutable) and never a user's own workout."""
    existing = _existing_generator_workout(db, user_id, date_str, "endurance")
    if existing is not None and existing.status == "planned" and existing.workout_type == "cross_train":
        db.delete(existing)
        db.commit()


def run_for_user(db, user_id: str = DEFAULT_USER_ID, date=None) -> dict:
    target = date or local_today(user_id)
    if isinstance(target, str):
        target = datetime.strptime(target, "%Y-%m-%d").date()
    config = _get_training_config(db, user_id)
    readiness_result = stats.readiness(db, user_id, target)

    if _is_ride_day(config, target):
        # ignore_schedule=True so the ride is a real "easy" session — without it the
        # skeleton lookup returns cross_train for this very day and the ride would
        # inherit the same empty placeholder it's replacing. Every readiness/health gate
        # below that still applies unchanged.
        endurance = _generate_endurance(db, user_id, target, readiness_result, config,
                                         activity_type="Ride", ignore_schedule=True)
        _clear_placeholder_cross_train(db, user_id, target.isoformat())
    else:
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
