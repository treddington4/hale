# P20/P21 Design — Goal-Tied Training Plan View + Builder

Design pass for PLAN.md's P20 (view) and P21 (builder), done jointly per the
plan's instruction that P21 expands P20 rather than competing with it.
Grounded in `app/models.py`, `app/coach/generator.py`, `app/routes/*.py`,
`web/src/pages/WorkoutsPage.tsx`, and a read-only inspection of production
(`goals` table, `weekly_plan` table, `user_training_config`).

## 0. Facts this design relies on (verified, not re-derived here)

- `WeeklyPlan.actual_tss` is written once as `0.0` at row creation
  (`app/coach/generator.py:267-269`) and never updated anywhere else. Confirmed
  in production: both existing rows (`2026-07-20`, `2026-07-27`) read
  `actual_tss=0.0`.
- Only 2 `WeeklyPlan` rows exist in production, both `activity_type` implicitly
  `"Run"` (the column doesn't even exist on the table — see `_get_or_create_weekly_plan`,
  `app/coach/generator.py:250-273`, which is Run-only by construction).
- `_phase_for_date` (`app/coach/generator.py:165-186`), `_is_deload_week`
  (`:189-192`), and `_compute_weekly_budget` (`:235-247`) are pure functions.
  `_last_nonzero_week_mileage` (`:206-219`) and `_week_mileage` (`:195-203`)
  read real `Run` rows and are replay-stable since activity history is immutable.
- `WeeklyPlan.frozen` (`:355-357`) is set only when 2+ readiness flags cap a
  week — genuinely stateful, not derivable after the fact.
- Production `goals` table has an active **`Wedding`** race goal
  (`goal_6c95ee60ec22`, target `2026-09-12`, priority `0`) nearer than the
  **`Manchester City Marathon`** race goal (`goal_9c92a0717066`, target
  `2026-11-08`, priority `1`). `_phase_for_date`'s single call site
  (`app/coach/generator.py:311`, inside `_generate_endurance`) picks the
  *nearest* active race goal — today that's the wedding, not the marathon,
  which is why the generator currently reports `build` phase.
- `_phase_for_date` has exactly one call site in the whole codebase
  (`app/coach/generator.py:311`) — a low-risk refactor target.
- `WeeklyPlan`/`_get_or_create_weekly_plan`/`_last_nonzero_week_mileage` are
  **not goal-scoped** — there is one global weekly-plan stream per user, driven
  by whichever goal `_phase_for_date` currently resolves to. This matters a lot
  for the multi-goal (wedding vs. marathon) design below.
- No "available days/week" or "bed/wake time" concept exists anywhere today.
  Sleep data exists only as `DailySteps.sleep_seconds`/`sleep_stages_json`
  (per-segment hypnogram, `app/sync/garmin_sync.py:578-592`); there is no bed/
  wake-time column or helper. `GET /api/wellness/sleep-stages`
  (`app/routes/wellness.py:104-124`) exposes one night's segments; nothing
  aggregates across nights today.
- `WorkoutsCalendar.tsx` has no per-plan grouping today — it's a flat day-picker
  calendar. "Collapsible per-plan grouping" is new UI, not a rename of
  something existing.

---

## 1. P20 — Goal-tied training plan view (build now)

### 1.1 Scope boundary

P20 is **read-only visualization** of the existing periodization math, tied to
a goal, with an explicit opt-in ("start a plan") gate. **P20 makes zero changes
to what the nightly generator actually prescribes.** This is what keeps P20
independently shippable — P21 is the phase that starts changing real
generation behavior.

### 1.2 New table: `TrainingPlan`

```python
class TrainingPlan(Base):
    """P20 — the 'plan' entity the Workouts tab's plan view is tied to and that
    'Start a Plan' creates. Deliberately thin at this phase: visualization only,
    no generator behavior depends on this table's existence yet (see P21 for
    where that changes). Restricted to race-type goals — phase/deload/ramp math
    has no defined analog for consistency/distance_target goals, and inventing
    one is out of scope (this is visualizing the existing periodization, not a
    competing planner)."""
    __tablename__ = "training_plans"
    __table_args__ = (UniqueConstraint("user_id", "goal_id", name="uq_training_plan_user_goal"),)

    id = Column(String, primary_key=True)  # f"plan_{uuid.uuid4().hex[:12]}"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    goal_id = Column(String, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, default="active")  # "active" | "archived"
    created_at = Column(String)

    # --- P21 additive columns, added later; nullable so P20's rows are valid
    # without them. NULL means "generator falls back to today's hardcoded
    # WEEKDAY_SKELETON/no-secondary-activity behavior" — see §2.
    available_days_json = Column(Text, nullable=True)     # e.g. "[0,2,3,5]", 0=Mon..6=Sun
    long_run_day = Column(Integer, nullable=True)          # 0=Mon..6=Sun
    secondary_activities_json = Column(Text, nullable=True)  # e.g. '[{"activityType":"Ride","daysPerWeek":1}]'
    sleep_constraint_mode = Column(String, default="soft")   # "soft" | "off" — see §2.4, no "hard" mode
```

A whole new table — `create_all()` picks it up automatically, no
`_MIGRATABLE_TABLES` entry needed for this initial version (matches
`ApiToken`/`PushSubscription`'s precedent, `app/models.py:351-353`).

**Judgment call — restricting to race goals.** Rejected: allowing
`TrainingPlan` on any goal type. Phase/deload/ramp math is fundamentally
"weeks until race date" — there's no defined equivalent for a consistency or
distance-target goal, and building one is explicitly out of scope per the plan
doc's "not inventing a competing planner."

**Judgment call — one goal per plan.** Rejected: a `goalIds` list on
`TrainingPlan` for "primary + secondary goal." Nothing in the requirements
needs a plan tied to *two* goals — the "secondary activities" concept (an
occasional ride during a run block) is an activity-type slot, not a second
`Goal` row. Keep it 1:1, enforced by `uq_training_plan_user_goal`.

### 1.3 Generator refactor (P20-scope, zero behavior change)

Extract the inline nearest-race query out of `_phase_for_date`
(`app/coach/generator.py:165-186`) into its own function, and let
`_phase_for_date` accept an explicit goal:

```python
def nearest_active_race_goal(db, user_id, date) -> Goal | None:
    """Exact query _phase_for_date used to run inline — extracted verbatim so
    P20's read-only week view can reuse it, and so P21's resolve_periodization_
    goal (see §2.1) can wrap it without duplicating the query."""
    return (
        db.query(Goal)
        .filter(Goal.goal_type == "race", Goal.status == "active",
                Goal.target_date >= date.isoformat(), owned_by(Goal.user_id, user_id))
        .order_by(Goal.target_date)
        .first()
    )

def _phase_for_date(db, user_id, date, goal: Goal | None = None) -> str:
    if goal is None:
        goal = nearest_active_race_goal(db, user_id, date)  # unchanged default behavior
    if not goal:
        return "base"
    race_date = datetime.strptime(goal.target_date, "%Y-%m-%d").date()
    weeks_until = (race_date - date).days / 7
    if weeks_until <= 1: return "taper"
    if weeks_until <= 4: return "peak"
    if weeks_until <= 12: return "build"
    return "base"
```

The one existing call site (`_generate_endurance`, `:311`) is unchanged —
still calls `_phase_for_date(db, user_id, date)` with no goal, so real
generator output is byte-for-byte identical after this refactor. This is a
pure enabling change for §1.4.

### 1.4 Week-series computation — where it lives

**Decision: lives in `app/coach/generator.py`, not `app/stats.py`.**
Rejected alternative: put the new week-view functions in `stats.py` per
CLAUDE.md's "`stats.py` is the single computation core" rule. Reason: that
rule exists to stop a *second implementation* of the same math (its own
cautionary example is GAP being duplicated). The pure functions this view
composes (`_phase_for_date`, `_is_deload_week`, `_compute_weekly_budget`,
`_last_nonzero_week_mileage`) already live in `generator.py` — duplicating
them into `stats.py` would be exactly that mistake. Exposing a couple of new
public (non-underscore) functions from `generator.py` keeps one source of
truth and is a closer parallel to how `race_pack.py` (P17) reuses
`weather.py`'s functions rather than re-deriving them.

**The multi-plan attribution problem (important, and why it isn't simpler).**
Because `WeeklyPlan` is a single global stream driven by whichever goal
`nearest_active_race_goal` resolves to, a `TrainingPlan` for a goal that *isn't*
currently winning that race (e.g. the marathon, while the wedding is nearer)
must **never** read the shared `WeeklyPlan` row — that row's `frozen`/
`is_deload`/`target_tss` belong to whichever goal is actually driving it, and
attributing them to a different plan would silently corrupt the one thing this
view exists to make trustworthy. Concretely: only use a persisted `WeeklyPlan`
row for a given week when the plan's own goal equals
`nearest_active_race_goal()` for that week; otherwise **always** replay the
week purely from `_compute_weekly_budget` fed by that goal's own phase — this
holds even for the "current" week of a non-driving plan.

```python
def plan_week_view(db, user_id, plan, week_start, config=None) -> dict:
    """One week for one TrainingPlan. Reads the persisted WeeklyPlan row only
    when this plan's goal is genuinely the one driving it that week (see module
    note above) — otherwise replays deterministically so a non-driving plan's
    numbers are never borrowed from another goal's state."""
    goal = db.get(Goal, plan.goal_id)
    config = config or _get_training_config(db, user_id)
    phase = _phase_for_date(db, user_id, week_start, goal=goal)
    is_deload = _is_deload_week(config, week_start)
    driving_goal = nearest_active_race_goal(db, user_id, week_start)
    is_driving = bool(driving_goal and driving_goal.id == plan.goal_id)

    persisted = None
    if is_driving:
        persisted = (db.query(WeeklyPlan)
                     .filter(WeeklyPlan.user_id == user_id, WeeklyPlan.week_start == week_start.isoformat())
                     .first())

    if persisted:
        target_mi, is_deload, frozen = persisted.target_tss or 0.0, persisted.is_deload, persisted.frozen
    else:
        last_nonzero, is_cold_start = _last_nonzero_week_mileage(db, user_id, week_start, "Run")
        weeks_active = _weeks_active_in_activity(db, user_id, week_start, "Run") if is_cold_start else 0
        target_mi = round(_compute_weekly_budget(last_nonzero, is_cold_start, config.weekly_ramp_pct or 3.0,
                                                   phase, is_deload, weeks_active), 1)
        frozen = False
    return {"phase": phase, "isDeload": is_deload, "frozen": frozen,
            "targetMi": target_mi, "isPersisted": persisted is not None}
```

**Forward projection — must chain off *projected targets*, not real data.**
A week 6 out has no real activity data yet, so calling
`_last_nonzero_week_mileage` for it would just return today's real last-nonzero
week for *every* future week — flat, non-ramping, and useless as a preview.
Instead, chain sequentially: seed `rolling_base_mi` from the current week's own
target (persisted or replayed), then for each future week feed
`_compute_weekly_budget` the *previous projected week's target* as its
`last_nonzero_mileage` input.

**Judgment call — do NOT "fix" the deload-plateau quirk in projection.**
`_compute_weekly_budget` applies deload *after* computing budget
(`budget *= 0.75`, `:245-246`), and the real (non-projected) generator flow
feeds `_last_nonzero_week_mileage` real post-deload mileage as next week's
base — meaning a deload week permanently lowers the following week's ramp base
rather than "bouncing back." That's an existing property of the real
generator, not something P20/21 is asked to fix. The projection chain must
faithfully reproduce it (feed the deloaded target straight through as next
week's base, no "undo the deload" correction) — otherwise the projection would
show a rosier future than the real generator will actually produce once those
weeks arrive, which is its own kind of dishonesty. If this quirk is worth
fixing, that's a separate, explicitly-scoped generator-math change — flag it,
don't silently correct it inside a visualization feature.

```python
def project_week_series(db, user_id, plan, weeks_back=8, weeks_forward=12) -> list[dict]:
    goal = db.get(Goal, plan.goal_id)
    config = _get_training_config(db, user_id)
    today = local_today(user_id)
    current_week_start = _week_start(today)
    weeks_forward = min(weeks_forward, 20)  # bound compute cost; see §1.5

    weeks, rolling_base_mi, cursor = [], None, current_week_start - timedelta(weeks=weeks_back)
    end = current_week_start + timedelta(weeks=weeks_forward)
    while cursor <= end:
        is_future = cursor > current_week_start
        phase = _phase_for_date(db, user_id, cursor, goal=goal)
        is_deload = _is_deload_week(config, cursor)

        if not is_future:
            wv = plan_week_view(db, user_id, plan, cursor, config)
            target_mi, is_deload, frozen, persisted = wv["targetMi"], wv["isDeload"], wv["frozen"], wv["isPersisted"]
            rolling_base_mi = target_mi
        else:
            if rolling_base_mi is None:  # shouldn't happen (current week always processed first)
                rolling_base_mi, _ = _last_nonzero_week_mileage(db, user_id, cursor, "Run")
            target_mi = round(_compute_weekly_budget(rolling_base_mi, False, config.weekly_ramp_pct or 3.0,
                                                       phase, is_deload, 0), 1)
            frozen, persisted = False, False
            rolling_base_mi = target_mi

        actual_mi = None if is_future else _week_mileage(db, user_id, cursor, "Run")
        weeks.append({
            "weekStart": cursor.isoformat(), "phase": phase, "isDeload": is_deload, "frozen": frozen,
            "targetMi": target_mi, "actualMi": actual_mi, "isProjection": is_future,
            "isPersisted": persisted, "isCurrentWeek": cursor == current_week_start,
        })
        cursor += timedelta(weeks=1)
    return weeks
```

**Judgment call — `actualMi: null` for not-yet-started weeks**, not `0.0`.
`_week_mileage` would honestly return `0.0` for a future week (no runs exist
yet), but surfacing that as "0 miles" risks being read as "you ran nothing this
week" for a week that hasn't started. `null` (rendered as a dash in the UI) is
the "never fabricate" reading of an honestly-inapplicable number.

### 1.5 API — `app/routes/plans.py` (new)

```
GET /api/plans
  -> [{
       "id": "plan_abc123", "goalId": "goal_9c92a0717066",
       "goalName": "Manchester City Marathon", "goalTargetDate": "2026-11-08",
       "status": "active", "createdAt": "2026-07-24T18:00:00Z",
       "isActivePeriodizationGoal": false,   # true only if this plan's goal is what
                                              # nearest_active_race_goal() resolves to today
       "availableDays": null, "longRunDay": null,
       "secondaryActivities": null, "sleepConstraintMode": "soft"
     }, ...]

POST /api/plans   body: {"goalId": "goal_9c92a0717066"}
  - 400 if goal missing/not owned/not goal_type=="race"/not status=="active"
  - idempotent: if an active TrainingPlan already exists for (user, goal),
    return it (200) rather than erroring — "Start a Plan" must be safely
    re-clickable
  -> same shape as one GET item (201 on create, 200 on idempotent return)

GET /api/plans/{plan_id}/weeks?weeksBack=8&weeksForward=12
  -> {
       "planId": "plan_abc123", "goalId": "goal_9c92a0717066",
       "goalName": "Manchester City Marathon", "goalTargetDate": "2026-11-08",
       "isActivePeriodizationGoal": false,
       "weeks": [
         {"weekStart": "2026-07-13", "phase": "build", "isDeload": false,
          "frozen": false, "targetMi": 5.9, "actualMi": 6.3,
          "isProjection": false, "isPersisted": true, "isCurrentWeek": false},
         {"weekStart": "2026-07-27", "phase": "build", "isDeload": false,
          "frozen": false, "targetMi": 3.0, "actualMi": 1.2,
          "isProjection": false, "isPersisted": true, "isCurrentWeek": true},
         {"weekStart": "2026-08-03", "phase": "build", "isDeload": false,
          "frozen": false, "targetMi": 3.1, "actualMi": null,
          "isProjection": true, "isPersisted": false, "isCurrentWeek": false}
       ]
     }
```

`weeksForward` capped server-side at 20 (each week's replay does a handful of
`Run` queries; 20 weeks × ~8-week lookback per query is cheap, an open-ended
range isn't). The frontend can re-request a larger `weeksForward` on demand
("show more weeks") rather than the server ever computing an unbounded series.

`isActivePeriodizationGoal: false` is the honest disclosure for the
wedding-vs-marathon situation: **in the P20-only world, a plan's shown
phase/target can legitimately differ from what the nightly generator is
actually prescribing that week**, because the generator still always uses
`nearest_active_race_goal` with no way to prefer a started plan (that's the
P21 change, §2.1). This is not a display bug — it's an accurate reflection of
a real mismatch, and the field lets the UI say so explicitly rather than
implying a consistency that doesn't exist yet.

Register in `app/main.py` alongside the existing routers
(`from .routes import ..., plans` / `app.include_router(plans.router)`).

### 1.6 Frontend

- `web/src/lib/api.ts` — add `TrainingPlan`, `PlanWeek`, `PlanWeeksResponse`
  types (mirroring the `Goal`/`GoalProgress` pattern at `:186-213`) and
  `api.plans()`, `api.startPlan(goalId)`, `api.planWeeks(planId, opts)`.
- `web/src/hooks/usePlans.ts` (new, mirrors `useGoals.ts` exactly):
  `usePlans()`, `usePlanWeeks(planId, { weeksBack, weeksForward })`,
  `useStartPlan()` (invalidates `["plans"]`).
- `web/src/components/workouts/TrainingPlanSection.tsx` (new) — sits on
  `WorkoutsPage.tsx` **above** the existing Calendar/List toggle (`:94-118`),
  visible regardless of which view is selected — this is the "collapsible
  per-plan grouping" the calendar phase deferred, and it's a different axis of
  organization than calendar-vs-list, not something baked into
  `WorkoutsCalendar.tsx` itself. One collapsible `<Collapsible>` (shadcn) per
  active `TrainingPlan`, default-expanded when there's exactly one plan,
  default-collapsed for any beyond the first. Shows a "Start a Plan" CTA per
  active race `Goal` that has no `TrainingPlan` yet, and an `EmptyState`
  (matching the existing pattern at `WorkoutsPage.tsx:129`) directing to Goals
  when there's no active race goal at all.
- `web/src/components/workouts/WeeklyPlanCard.tsx` (new) — one week: phase
  badge, deload badge, target-vs-actual (bar or `"1.2 / 3.0 mi"`), a distinct
  "Frozen" badge (readiness-driven) vs. a distinct P21 "Reduced — {label}"
  badge (planned interruption) — **these must render differently**, matching
  the "must not share state" constraint (§2.2). Projected weeks render with a
  dashed border / muted style and a small "Projected" tag; a one-line banner
  at the top of the section states "Future weeks assume every prior week hits
  its target — actuals will differ" (the projection-honesty disclaimer, §1.4).
- `web/src/components/workouts/StartPlanDialog.tsx` (new) — goal picker
  (active race goals without a plan yet) + Start button → `useStartPlan()`.

### 1.7 Vestigial `WeeklyPlan.actual_tss`

**Keep the column, stop implying it's used.** Dropping it isn't supported by
`_migrate_add_missing_columns` (ADD COLUMN only, no DROP COLUMN capability,
matching "no migration framework"), and hand-rolling a one-off DROP COLUMN
migration for a harmless dead column isn't worth the risk. Update its
docstring to state plainly it is vestigial, always `0.0`, and that real
"actual" is computed live via `_week_mileage()` (§1.4) — never read this
column for anything real. Rejected alternative: repurpose it to cache a
frozen-at-week-close actual snapshot — rejected because it reintroduces a
second write path the investigation specifically flagged as unnecessary
complexity ("no backfill, no second write path to keep in sync").

---

## 2. P21 — Multi-activity weekly plan builder (design now, build later)

P21 is where a started `TrainingPlan` starts actually changing what the
generator does. Every item below is additive/opt-in: a user who never starts a
plan, or starts one but never sets its P21 fields, gets byte-identical
generator behavior to today.

### 2.1 Making the generator obey a started plan (the wedding/marathon fix)

Today `_generate_endurance`'s only call to `_phase_for_date` always resolves
`nearest_active_race_goal` — so even after "starting a plan" for the marathon
in P20, the *real* nightly generator keeps periodizing off the wedding. P21
closes this gap:

```python
def resolve_periodization_goal(db, user_id, date) -> Goal | None:
    """Prefer a started, active TrainingPlan's goal over pure 'nearest' —
    starting a plan is an explicit statement of intent ('periodize for THIS
    goal'), and should win over an incidentally-nearer race goal. Falls back to
    nearest_active_race_goal (today's exact behavior) when no plan is started,
    so a user who never touches P20/21 sees no change at all."""
    plans = (db.query(TrainingPlan)
             .filter(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
             .all())
    candidates = []
    for p in plans:
        g = db.get(Goal, p.goal_id)
        if g and g.status == "active" and g.target_date >= date.isoformat() \
           and (g.periodizes_training is not False):
            candidates.append(g)
    if candidates:
        return min(candidates, key=lambda g: g.target_date)
    return nearest_active_race_goal(db, user_id, date)  # already filters periodizes_training below
```

`_generate_endurance` (`:311`) switches its one call from
`_phase_for_date(db, user_id, date)` to
`_phase_for_date(db, user_id, date, goal=resolve_periodization_goal(db, user_id, date))`.
`nearest_active_race_goal` also gains the `periodizes_training` filter (see
§2.5) at the same time.

### 2.2 Planned interruption windows — one concept, not two

**Decision: a single `PlanAvailabilityWindow` table serves both "available
days/week" (P21.1) and "planned interruption" (the honeymoon), by treating the
recurring pattern as the *default* and an interruption as a *dated override* of
that default** — not two separate mechanisms.

```python
class PlanAvailabilityWindow(Base):
    """A bounded exception to a TrainingPlan's steady-state availability
    (TrainingPlan.available_days_json/long_run_day). The plan's own columns are
    the indefinite default; a row here overrides them for a specific date
    range — a honeymoon, an injury-driven cutback, a work trip. Always
    date-bounded (both dates required) — there is no open-ended override row;
    the plan's own columns already cover 'forever'."""
    __tablename__ = "plan_availability_windows"

    id = Column(String, primary_key=True)  # f"avail_{uuid.uuid4().hex[:12]}"
    training_plan_id = Column(String, ForeignKey("training_plans.id", ondelete="CASCADE"), nullable=False)
    start_date = Column(String, nullable=False)  # YYYY-MM-DD inclusive
    end_date = Column(String, nullable=False)    # YYYY-MM-DD inclusive
    label = Column(String, nullable=False)       # e.g. "Honeymoon"
    volume_multiplier = Column(Float, default=0.3)  # applied to the computed target for weeks in range
    available_days_json = Column(Text, nullable=True)  # optional day-of-week override; NULL = inherit plan default
```

Rejected alternative: make the plan's own steady-state pattern *also* a row in
this table (so there's literally one table with zero special cases). Rejected
because it forces a mandatory bootstrap row with an open-ended `end_date` for
every plan, for no real query benefit — "one honest concept" doesn't require
one literal table row per state; it requires one shape of override applying
consistently, which the design above already gives.

**The ramp-after-interruption fix (the concrete, buildable resolution).**
`_last_nonzero_week_mileage`'s backward walk (`:206-219`) currently treats any
nonzero week as a valid ramp base. Add: skip any week whose range overlaps a
`PlanAvailabilityWindow` with `volume_multiplier < INTERRUPTION_RAMP_SKIP_THRESHOLD`
(constant, `0.5` — judgment call: a mild 0.8 cutback shouldn't be treated as if
it never happened, but a honeymoon-scale 0.3 clearly should be excluded from
"what's my real base") when searching for the ramp base — i.e. these weeks
never count as "the last nonzero week" regardless of their actual mileage,
zero or not. The walk continues past them to the last genuine pre-window week.
This is what prevents the post-honeymoon build from cratering: the *ramp base*
for the weeks after the window is the last real pre-honeymoon week, not the
honeymoon's own depressed mileage — while the honeymoon's own weeks still show
their honestly reduced target (`base_pre_window × volume_multiplier`, held
constant through the window) so the fitness loss is never hidden, just never
used as a foundation to build on. Resumption after the window still goes
through the existing per-phase ceiling multiplier (`PHASE_CEILING_MULTIPLIER`,
`~1.15-1.30×/week`), so it's a gradual re-ramp, not an instant jump back to
full volume — no new cap logic needed there, the existing one already does
this job once the base is correct.

**Frozen vs. interruption must not share state (as required).** Add
`WeeklyPlan.availability_window_id` (nullable String, no FK enforcement needed
— same "loose reference" style as `Goal.linked_run_id`) set when
`_get_or_create_weekly_plan` finds an overlapping window; leave `frozen`
untouched by this path entirely. The view (§1.6) renders these as visually
distinct badges. `("weekly_plan", WeeklyPlan)` must be added to
`_MIGRATABLE_TABLES` (`app/models.py:764-766`) at this point — it is not there
today, and P21's new column needs it to actually get added via
`_migrate_add_missing_columns`.

Also add `("training_plans", TrainingPlan)` to `_MIGRATABLE_TABLES` when P21's
four additive columns land on it (not needed for P20's initial creation —
`create_all()` handles a brand-new table — but needed the moment columns are
added to an already-existing one).

### 2.3 Generator: available days, long-run day, secondary activities

`WEEKDAY_SKELETON` (`:162`) is a fixed dict today, ignoring any per-user plan.
P21 makes it plan-aware, but **only when a plan's P21 fields are populated** —
absent that, behavior is exactly today's hardcoded skeleton (this is what
keeps "starting a plan" from being a silent trap for someone who only wanted
the P20 view):

- If the resolved plan (via `resolve_periodization_goal`) has
  `available_days_json` set: days not in that set become forced "rest" (or a
  secondary-activity slot, see below) instead of whatever `WEEKDAY_SKELETON`
  would have said.
- `long_run_day`, if set, pins which weekday hosts the `"long"` skeleton slot
  (today hardcoded to weekday 5/Saturday via `WEEKDAY_SKELETON[5] = "long"`).
- `secondary_activities_json` (e.g. `[{"activityType":"Ride","daysPerWeek":1}]`)
  fills a day that would otherwise be `"cross_train"`/`"rest"` with a real
  quick-generate-style session for that activity type instead — reusing
  `_generate_endurance`'s existing non-Run branch (`:324-331`, already
  generalized for Ride) rather than inventing new budget math.

### 2.4 Sleep-schedule awareness — soft warning, not a hard constraint

**Decision: soft, informational, never blocking.** `sleep_constraint_mode`
supports `"soft"` (default) or `"off"` — deliberately no `"hard"` option.

Reasoning: `long_run_day` in P21.1 is an *explicit user choice*. A derived
signal like "your typical wake time is late on this weekday" is exactly that —
derived, imperfect, and can be wrong on any given week. Silently overriding an
explicit choice with an inferred pattern removes agency the day-placement
feature was built to give the user, and this codebase's existing precedent for
*hard* gating (severe health note → forced rest, 2+ readiness flags → forced
rest) is reserved for same-day, high-confidence, "don't train today" signals —
not a multi-week scheduling preference built on a rolling average. A hard
block here risks a plan that simply can't place a long run at all if every
available day looks "late" by the same historical pattern.

Concretely: new `stats.typical_wake_time(db, user_id, lookback_days=28,
min_nights=5) -> str | None` — median of each night's last `sleep_stages_json`
segment's `end`, converted to local time-of-day; returns `None` (never
fabricates) below `min_nights` of real data. Lives in `stats.py` (general
wellness-data derivation, not periodization-specific — matches `readiness()`'s
own use of `sleep_seconds`). When `sleep_constraint_mode == "soft"` and the
long-run day's typical wake time is later than a threshold (default 8:00 AM),
append an informational `trigger_notes` entry to that day's workout (same
mechanism already used for readiness downgrades, `:346-365`) — never changes
`workout_type`, `target_distance_mi`, or which day it lands on.

### 2.5 Non-race goal driving periodization — flag for the user, don't decide it

Add nullable `Goal.periodizes_training` (Boolean, default `NULL`, read as
"true unless explicitly `False`" — same legacy-NULL convention as
`Goal.priority` elsewhere). `nearest_active_race_goal` gains
`Goal.periodizes_training.is_not(False)` in its filter. **Defaulting to NULL
(→ true) means this change alone alters nothing today** — the wedding keeps
periodizing exactly as it does now until someone explicitly sets its flag to
`False`.

This is flagged for the user, not decided here: setting the wedding's
`periodizes_training` to `False` would shift the reported phase from `build`
to `base` today (per `_phase_for_date`'s thresholds against the marathon's
14.9-weeks-out vs. the wedding's 6.7-weeks-out). That's a real, visible
consequence, not a cosmetic one — see the open question below.

---

## 3. Summary of files touched

**P20 (build now):**
- `app/models.py` — add `TrainingPlan`.
- `app/coach/generator.py` — extract `nearest_active_race_goal`, add optional
  `goal` param to `_phase_for_date`, add `plan_week_view`/`project_week_series`.
- `app/routes/plans.py` (new) — `GET/POST /api/plans`, `GET /api/plans/{id}/weeks`.
- `app/main.py` — register the new router.
- `web/src/lib/api.ts`, `web/src/hooks/usePlans.ts` (new).
- `web/src/components/workouts/TrainingPlanSection.tsx`, `WeeklyPlanCard.tsx`,
  `StartPlanDialog.tsx` (new); `web/src/pages/WorkoutsPage.tsx` (insert section).
- `tests/test_generator_plan_view.py` (new, per this repo's P1-established
  pytest convention) — pin `plan_week_view`/`project_week_series` against a
  synthetic multi-week `Run` history, including the non-driving-plan
  attribution case (§1.4).

**P21 (design only, build later):**
- `app/models.py` — 4 additive `TrainingPlan` columns + `_MIGRATABLE_TABLES`
  entry; new `PlanAvailabilityWindow` table; `Goal.periodizes_training`
  additive column; `WeeklyPlan.availability_window_id` additive column +
  `_MIGRATABLE_TABLES` entry (`weekly_plan` isn't in that list today).
- `app/coach/generator.py` — `resolve_periodization_goal`, interruption-skip
  in `_last_nonzero_week_mileage`, plan-aware `WEEKDAY_SKELETON` resolution,
  secondary-activity slot generation.
- `app/stats.py` — `typical_wake_time`.
- `app/routes/plans.py` — `PATCH /api/plans/{id}`, availability-window CRUD
  (`GET/POST /api/plans/{id}/availability-windows`,
  `DELETE /api/plans/{id}/availability-windows/{window_id}`).
- Frontend: `PlanBuilderSettingsDialog.tsx`, `AvailabilityWindowDialog.tsx`,
  `SecondaryActivityPicker.tsx` (new).
