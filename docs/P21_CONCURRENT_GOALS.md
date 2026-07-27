# P21 redesign — one intertwined plan, primary + supporting goals

Supersedes §2 of `P20_P21_DESIGN.md`. That section assumed one plan per goal and
treated "secondary activities" as an occasional ride slotted into a run block. User
direction during P20 implementation reframed it:

> "we need primary goal and supporting goals. While they are competing there are
> shared workouts that can help both goals move forward as well."
>
> "realistically the user can only do one training and the plans should intertwine.
> there are only so much hours in a day."

That is a different model, and it invalidates part of what P20 shipped.

## 1. What's wrong with the shipped model

`TrainingPlan` is `UniqueConstraint(user_id, goal_id)` — one plan per goal, N plans
per user — and `TrainingPlanSection.tsx` renders them as N independent collapsible
groups, each with its own week series and its own target/actual.

**That presents concurrent goals as parallel tracks.** Probed in a throwaway DB with
30mi/wk running and 60mi/wk cycling history, two plans (marathon + 50mi ride):

```
Marathon plan    phase=base   targetMi=30.9
50mi Ride plan   phase=build  targetMi=61.8
```

Each computed independently, so the view implies the athlete does both in full — 30
running miles *and* 62 cycling miles, every week, forever. Nothing anywhere accounts
for the fact that it is one body with one recovery budget and one calendar.

Also found and fixed during P20 (commit `4fc2a73`), listed here because it's the same
root confusion: both plans originally reported *running* mileage, because
`plan_week_view`/`project_week_series` hardcoded `"Run"`. A race goal is not
necessarily a running race.

## 2. Verified feasibility — the shared-currency question

The model needs a currency that is comparable across disciplines (30 run miles and 60
ride miles are not comparable in miles, but are in load and in hours). Checked against
production, last 90 days, deduplicated — 73 activities:

```
type            n    has TSS   has time   has dist
run            55     55/55      55/55      55/55
ride            6       5/6       6/6        6/6
walk            5       5/5       5/5        5/5
workout         3       3/3       3/3        3/3
strength        3       3/3       3/3        0/3
yoga           1        0/1       1/1        0/1
```

**TSS: 71/73 (97%). Moving time: 73/73 (100%).** Both are viable today; P5 (TSS for
non-run activities) is what made TSS usable here. Real weekly totals, showing both
currencies carry the signal that miles-per-discipline cannot:

```
week           run_mi  ride_mi      TSS    hours
2026-06-22       32.9      0.0      438      5.6
2026-06-29       33.7      0.0      457      6.3
2026-07-06       20.4      0.0      271      4.5
2026-07-13        5.9      5.3      150      3.1
2026-07-20       28.3      3.0      437      7.7
```

Note the observed range is 1.7–7.7 hrs/wk. The hours ceiling is a real constraint for
this user, not a theoretical one.

Convenient consequence: `WeeklyPlan.target_tss` is *named* for TSS but stores a mileage
proxy (documented as a known v1 approximation since Phase 4.3). This redesign is the
point where it can finally mean what it says.

## 3. The model

**One plan. Many goals. One budget.**

```python
class TrainingPlan(Base):
    """The athlete's current training block — ONE active per user, not one per goal.
    A block serves multiple goals simultaneously; goals attach via PlanGoal."""
    __tablename__ = "training_plans"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, default="active")           # "active" | "archived"
    created_at = Column(String)
    weekly_hours_cap = Column(Float, nullable=True)     # the "hours in a day" ceiling
    available_days_json = Column(Text, nullable=True)   # "[0,2,3,5]", 0=Mon
    long_session_day = Column(Integer, nullable=True)   # 0=Mon..6=Sun


class PlanGoal(Base):
    """Which goals this block serves, and in what role. Exactly one primary per plan."""
    __tablename__ = "plan_goals"
    __table_args__ = (UniqueConstraint("training_plan_id", "goal_id"),)
    id = Column(String, primary_key=True)
    training_plan_id = Column(String, ForeignKey("training_plans.id", ondelete="CASCADE"))
    goal_id = Column(String, ForeignKey("goals.id", ondelete="CASCADE"))
    role = Column(String, default="supporting")  # "primary" | "supporting"
```

**Migration note:** changing `TrainingPlan`'s unique constraint from
`(user_id, goal_id)` cannot be done by `_migrate_add_missing_columns` — it only does
`ALTER TABLE ADD COLUMN`, and SQLite needs a table rebuild to alter a UNIQUE
constraint. Production currently holds exactly **one** `TrainingPlan` row, so the
cheapest correct path is drop-and-recreate now, before this table accumulates data.
Do it while it's free.

### 3.1 Budget allocation

The primary goal owns periodization: `_phase_for_date` resolves from the **primary**
`PlanGoal`, not from whichever race is nearest. (`nearest_active_race_goal`'s
date-ordering is what currently makes a nearer B-race hijack an A-race block — and
note `Goal.priority` already exists and is ignored by that resolver.)

One weekly budget, denominated in TSS, capped by `weekly_hours_cap`, then allocated:

1. **Primary-specific essentials first.** Marathon block → the long run and the one
   quality run session. These are non-negotiable and non-transferable; nothing on a
   bike builds running-specific durability.
2. **Supporting-goal minimum viable dose.** 50mi ride → one long ride, sized to the
   minimum that keeps the goal reachable, not to what a cycling-primary block would
   prescribe.
3. **Remaining aerobic volume is fungible.** Easy/Z2 work can be run *or* ridden. This
   is where "shared workouts" live — see 3.2.

### 3.2 Shared credit, stated honestly

A session records which goals it advances:

```python
Workout.serves_goal_ids_json = Column(Text, nullable=True)   # '["goal_a","goal_b"]'
```

An easy Z2 ride during a marathon block genuinely advances both: aerobic base transfers
across modalities. But transfer is **partial and asymmetric** — cycling does not build
running economy, bone loading, or connective-tissue tolerance to impact. So:

- Shared credit applies to the **aerobic//load** component only.
- A goal's **discipline-specific** requirement can only be met in its own discipline.
- The UI must show these as different things. A marathon plan reading "you're on
  track" because of cycling volume would be the same class of fabrication the
  dashboard cards are careful to avoid — and here it has physical consequences: a
  runner arriving at a marathon aerobically fit but structurally undertrained is
  exactly how people get hurt.

Concretely, a week shows: total load (shared), plus per-goal discipline-specific
completion (not shared). Never one number claiming to cover both.

### 3.3 Conflict surfacing

With a finite budget, some goal pairs simply do not fit. The system should say so
rather than silently shortchanging one:

- If primary essentials + supporting minimum > `weekly_hours_cap` (or > a safe TSS
  ramp), surface it as an explicit conflict with the numbers.
- Never resolve it by quietly shrinking the primary's long run.
- The honest options are: raise the cap, demote/drop the supporting goal, or move its
  date. That's the user's call, not the generator's.

## 3.4 Planned interruptions — narrower than first designed

`P20_P21_DESIGN.md` §2.2 specified a `PlanAvailabilityWindow` with a
`volume_multiplier`, and a `INTERRUPTION_RAMP_SKIP_THRESHOLD` to exclude those weeks
from the ramp-base search. Probing the actual honeymoon case (Greece, ~2026-09-12 to
~2026-09-26) showed that's broader than needed, because the two disciplines fail
differently:

```
Back home, week of 2026-09-28 — what does the ramp build from?
  Run    ramp base =  4.0   pre-honeymoon was 30.0  -> CRATERED
  Ride   ramp base = 10.0   pre-honeymoon was 10.0  -> OK
```

**A discipline that drops to exactly zero is already handled.** No bike is available in
Greece, so cycling goes to 0.0 for both weeks, and `_last_nonzero_week_mileage`'s
backward walk skips zero weeks by construction (its Phase 14 docstring is explicit
about this) — it reaches past the gap to the last real week. Nothing to build.

**Only *partial* reduction breaks the ramp.** Running continues at a token level, and a
4-mile week is nonzero, so it becomes the ramp base and the post-honeymoon build starts
from 4mi instead of ~30mi. Running a little is worse for this algorithm than not
running at all.

So the interruption window only needs to cover disciplines that will be *reduced but
not stopped*, and its job is narrower than "scale the target": it marks weeks as **not
a valid ramp base**, leaving their displayed target honest. Availability is per
discipline, not one multiplier for the whole window — the same two weeks are a total
stop for cycling and a partial cut for running.

Worth noting the underlying fragility is general, not honeymoon-specific:
`_last_nonzero_week_mileage` treats the single most recent nonzero week as gospel, so
any one-off light week (illness, travel, a work crunch) drags the base down the same
way. Fixing it generally — e.g. a trailing median rather than the latest value — would
address all of them at once, but it changes the Phase 14 behavior deliberately designed
to tell "didn't run last week" apart from "never done this," so it needs its own
scoped decision rather than being smuggled in with an interruption feature.

## 4. What P20 keeps

- `plan_week_view`/`project_week_series`'s attribution rule (never read another goal's
  persisted `WeeklyPlan` row) — still correct, still needed.
- Per-discipline mileage via `plan_activity_type()` — required by this model too.
- The projection chain, `isProjection` labeling, and null-not-zero actuals.
- `Goal.periodizes_training` (the wedding fix) — orthogonal and still right.

## 5. What P20 must change

- `TrainingPlan` schema (§3) — rebuild while the table holds one row.
- `TrainingPlanSection.tsx` — stop rendering N parallel plan groups. One block, one
  week series against the shared budget, with a per-goal contribution breakdown inside
  it.
- `POST /api/plans` — takes a goal *and a role*, attaching to the single active block
  rather than minting a competing plan.

## 6. Open questions for the user

1. **Is the 50mi ride hypothetical or real?** It changes whether this is built now or
   designed now. The wedding/honeymoon/marathon calendar is already known
   (2026-09-12 / ~2 weeks / 2026-11-08); a cycling goal in that window would interact
   with the honeymoon interruption directly.
2. **What is the real weekly hours ceiling?** Observed 1.7–7.7, but observed is not the
   same as available. The cap should be what's actually sustainable, not the max ever
   hit.
3. **Does strength count against the same budget?** It consumes hours and recovery and
   already has TSS (3/3 coverage), but it's currently generated on its own track
   (`_generate_strength`) independent of the endurance budget entirely.
