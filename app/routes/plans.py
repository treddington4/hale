"""P20 — goal-tied training plan view. Read-only over the periodization math the
generator already does (see coach/generator.py's plan_week_view/project_week_series).

P21 (docs/P21_CONCURRENT_GOALS.md) redesigned the underlying model: ONE active
TrainingPlan per user, not one per goal — goals attach via PlanGoal in a role
("primary" | "supporting"). generator.resolve_periodization_goal() now reads the
primary PlanGoal (when one exists) to decide what the nightly generator actually
prescribes, so starting a plan and naming a primary goal is no longer purely
cosmetic the way P20 shipped it."""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import (
    SessionLocal, Goal, TrainingPlan, PlanGoal, DEFAULT_USER_ID, get_sync_meta, user_key,
)
from ..accounts import auth
from .. import stats
from ..coach import generator
from ..util import local_today

router = APIRouter()


def _owns_goal(goal: Goal, user_id: str) -> bool:
    """Row-level equivalent of models.owned_by()'s SQL predicate, for a Goal already
    fetched by primary key. Legacy rows predating the multi-user migration have
    user_id=NULL and belong to the default user — same read-time NULL handling used
    everywhere else in this codebase."""
    return goal.user_id == user_id or (goal.user_id is None and user_id == DEFAULT_USER_ID)


def _get_active_plan(db, user_id) -> TrainingPlan | None:
    return (db.query(TrainingPlan)
            .filter(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
            .first())


def _plan_goal_dict(pg: PlanGoal, goal: Goal, driving_goal_id: str | None) -> dict:
    return {
        "goalId": pg.goal_id, "goalName": goal.name if goal else None,
        "goalTargetDate": goal.target_date if goal else None,
        "role": pg.role,
        # False here is not a bug and not a "disabled" state — see
        # generator.resolve_periodization_goal: a supporting goal's own week series is a
        # real, honest projection of what IT alone would prescribe, not what's actually
        # being generated. Only the primary goal (while it's still a live active race)
        # drives real generation.
        "isActivePeriodizationGoal": driving_goal_id is not None and driving_goal_id == pg.goal_id,
    }


def _plan_to_dict(db, p: TrainingPlan, user_id: str) -> dict:
    driving = generator.resolve_periodization_goal(db, user_id, local_today(user_id))
    driving_goal_id = driving.id if driving else None
    plan_goals = db.query(PlanGoal).filter(PlanGoal.training_plan_id == p.id).all()
    goals = [_plan_goal_dict(pg, db.get(Goal, pg.goal_id), driving_goal_id) for pg in plan_goals]
    # Primary first, then soonest target date — same ordering intent as /api/goals.
    goals.sort(key=lambda g: (g["role"] != "primary", g["goalTargetDate"] or "9999-12-31"))
    return {
        "id": p.id, "status": p.status, "createdAt": p.created_at,
        "weeklyHoursCap": p.weekly_hours_cap,
        "availableDays": _json_or_none(p.available_days_json),
        "longRunDay": p.long_run_day,
        "sleepConstraintMode": p.sleep_constraint_mode or "soft",
        # None when no cap is set (nothing to be over) — see generator.weekly_hours_plan,
        # and note it covers HALE's own prescribed sessions only, not Garmin's parallel plan.
        "hours": generator.weekly_hours_plan(
            db, user_id, generator._week_start(local_today(user_id)), plan=p),
        # None until enough real nights exist; never guessed from a couple of nights.
        "typicalWakeTime": stats.typical_wake_time(db, user_id),
        # Per-weekday free hours (real: awake window minus declared work) and a
        # suggested trainable ceiling (a heuristic — see stats.TRAINING_SHARE_OF_FREE_TIME).
        "timeBudget": stats.daily_time_budget(db, user_id, p),
        # Garmin sessions are rendered with real durations; if HALE's copy of the
        # adaptive plan is stale it is quoting a number Garmin has since revised.
        "garminPlan": _garmin_plan_freshness(user_id),
        "goals": goals,
    }


# A Garmin adaptive-plan suggestion older than this is called out as possibly stale.
# Garmin revises the same day's suggestion repeatedly — production's 2026-07-28 row was
# revised on the 24th, 26th and 27th — so a copy more than a day old is genuinely likely
# to disagree with what the Garmin app is showing right now. Confirmed the hard way:
# HALE displayed a 76min session while the app said 55min, with nothing on screen to
# suggest the number might not be current.
GARMIN_PLAN_STALE_AFTER_HOURS = 24


def _garmin_plan_freshness(user_id: str) -> dict:
    """When HALE last managed to read Garmin's adaptive plan, and why it might be
    behind. Garmin sessions are shown with real durations, so if the fetch is stale the
    UI is quoting a number Garmin has since changed — that has to be visible rather than
    presented as current."""
    from ..sync.garmin_sync import (
        GARMIN_ADAPTIVE_PLAN_LAST_CHECKED_KEY, GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY,
        GARMIN_RATE_LIMIT_FAILURES_KEY,
    )
    checked = get_sync_meta(user_key(user_id, GARMIN_ADAPTIVE_PLAN_LAST_CHECKED_KEY))
    cooldown = get_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY))
    failures = get_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_FAILURES_KEY))

    age_hours = None
    if checked:
        try:
            then = datetime.fromisoformat(checked)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
        except (TypeError, ValueError):
            age_hours = None

    in_cooldown = False
    if cooldown:
        try:
            until = datetime.fromisoformat(cooldown)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            in_cooldown = until > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            in_cooldown = False

    return {
        "lastCheckedAt": checked,
        "ageHours": round(age_hours, 1) if age_hours is not None else None,
        # Never checked at all counts as stale — "no data" must not read as "fresh".
        "isStale": age_hours is None or age_hours > GARMIN_PLAN_STALE_AFTER_HOURS,
        "inCooldown": in_cooldown,
        "cooldownUntil": cooldown if in_cooldown else None,
        "consecutiveFailures": int(failures) if failures and str(failures).isdigit() else 0,
    }


def _valid_hhmm(v) -> bool:
    if not isinstance(v, str):
        return False
    parts = v.split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _json_or_none(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


@router.get("/api/plans")
def get_plans(user_id: str = Depends(auth.current_user_id)):
    """Returns the user's single active plan, or null — P21 replaced the old
    one-plan-per-goal list with one intertwined plan (docs/P21_CONCURRENT_GOALS.md)."""
    db = SessionLocal()
    try:
        plan = _get_active_plan(db, user_id)
        return _plan_to_dict(db, plan, user_id) if plan else None
    finally:
        db.close()


@router.post("/api/plans")
async def add_plan_goal(request: Request, user_id: str = Depends(auth.current_user_id)):
    """Attaches a goal to the user's single active plan (creating that plan on first
    call) in the given role — "primary" by default for the first goal attached,
    "supporting" thereafter, overridable explicitly in the body.

    Idempotent re-attachment of the same goal updates its role rather than erroring, so
    "Start a Plan" stays safely re-clickable. Setting a new "primary" demotes any
    existing primary to "supporting" — exactly one primary per plan
    (docs/P21_CONCURRENT_GOALS.md §3), reassigned explicitly rather than silently
    allowing two."""
    body = await request.json()
    goal_id = body.get("goalId")
    if not goal_id:
        raise HTTPException(400, "goalId is required")
    role = body.get("role")
    if role not in (None, "primary", "supporting"):
        raise HTTPException(400, 'role must be "primary" or "supporting"')

    db = SessionLocal()
    try:
        goal = db.get(Goal, goal_id)
        if not goal or not _owns_goal(goal, user_id):
            raise HTTPException(404, "Goal not found")
        if goal.goal_type != "race":
            raise HTTPException(400, "Only race goals can join a training plan — phase/deload/ramp "
                                     "math is defined in weeks-until-race-date and has no analog for "
                                     "consistency or distance_target goals.")
        if goal.status != "active":
            raise HTTPException(400, "Goal is not active")

        plan = _get_active_plan(db, user_id)
        if not plan:
            plan = TrainingPlan(id=f"plan_{uuid.uuid4().hex[:12]}", user_id=user_id, status="active",
                                 created_at=datetime.now(timezone.utc).isoformat())
            db.add(plan)
            db.flush()

        existing = (db.query(PlanGoal)
                    .filter(PlanGoal.training_plan_id == plan.id, PlanGoal.goal_id == goal_id)
                    .first())
        if role is None:
            # No explicit role: an already-attached goal keeps its current role (a plain
            # idempotent re-click must never silently demote an existing primary just
            # because other goals are also attached — real bug, caught while testing
            # this route: re-POSTing the same already-primary goal with no role body
            # was flipping it to "supporting" because the old logic keyed off "are any
            # OTHER goals attached" instead of "is THIS goal already primary"). A new
            # attachment defaults to primary only when the plan has no primary yet.
            if existing:
                role = existing.role
            else:
                has_primary = any(pg.role == "primary" for pg in
                                   db.query(PlanGoal).filter(PlanGoal.training_plan_id == plan.id).all())
                role = "supporting" if has_primary else "primary"

        if role == "primary" and not (existing and existing.role == "primary"):
            for pg in db.query(PlanGoal).filter(PlanGoal.training_plan_id == plan.id,
                                                 PlanGoal.role == "primary").all():
                pg.role = "supporting"

        if existing:
            existing.role = role
        else:
            db.add(PlanGoal(id=f"plangoal_{uuid.uuid4().hex[:12]}", training_plan_id=plan.id,
                             goal_id=goal_id, role=role,
                             created_at=datetime.now(timezone.utc).isoformat()))
        db.commit()
        return _plan_to_dict(db, plan, user_id)
    finally:
        db.close()


@router.patch("/api/plans/{plan_id}")
async def update_plan(plan_id: str, request: Request, user_id: str = Depends(auth.current_user_id)):
    """Plan-level scheduling config: weekly hours cap, available days, long-run day,
    sleep-constraint mode. Every field is optional and only applied when the key is
    actually present in the body — sending `{"weeklyHoursCap": null}` clears the cap,
    while omitting the key entirely leaves it untouched. That distinction matters here
    because null is a meaningful value for all four (it means "not declared", which is
    the honest default, not a fabricated number)."""
    body = await request.json()
    db = SessionLocal()
    try:
        p = db.get(TrainingPlan, plan_id)
        if not p or p.user_id != user_id:
            raise HTTPException(404, "Plan not found")

        if "weeklyHoursCap" in body:
            cap = body["weeklyHoursCap"]
            if cap is not None:
                try:
                    cap = float(cap)
                except (TypeError, ValueError):
                    raise HTTPException(400, "weeklyHoursCap must be a number or null")
                if not 0 < cap <= 168:
                    raise HTTPException(400, "weeklyHoursCap must be between 0 and 168 hours")
            p.weekly_hours_cap = cap

        if "availableDays" in body:
            days = body["availableDays"]
            if days is None:
                p.available_days_json = None
            else:
                if not isinstance(days, list) or any(
                        not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days):
                    raise HTTPException(400, "availableDays must be a list of weekday ints 0-6 (0=Mon), or null")
                # Stored sorted+deduped so the value read back is stable regardless of
                # what order the client sent, and so generator._parse_available_days
                # never has to care.
                p.available_days_json = json.dumps(sorted(set(days)))

        if "longRunDay" in body:
            d = body["longRunDay"]
            if d is not None:
                if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6:
                    raise HTTPException(400, "longRunDay must be a weekday int 0-6 (0=Mon), or null")
            p.long_run_day = d

        if "workSchedule" in body:
            ws = body["workSchedule"]
            if ws is None:
                p.work_schedule_json = None
            else:
                if not isinstance(ws, dict):
                    raise HTTPException(400, "workSchedule must be an object keyed by weekday, or null")
                cleaned = {}
                for k, v in ws.items():
                    try:
                        day = int(k)
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"workSchedule key {k!r} is not a weekday int")
                    if not 0 <= day <= 6:
                        raise HTTPException(400, "workSchedule weekdays must be 0-6 (0=Mon)")
                    if not isinstance(v, dict) or not _valid_hhmm(v.get("start")) or not _valid_hhmm(v.get("end")):
                        raise HTTPException(400, f"workSchedule[{k}] needs HH:MM start and end")
                    brk = v.get("breakHours") or 0
                    try:
                        brk = float(brk)
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"workSchedule[{k}].breakHours must be a number")
                    cleaned[str(day)] = {"start": v["start"], "end": v["end"], "breakHours": brk}
                p.work_schedule_json = json.dumps(cleaned) if cleaned else None

        if "dailyTrainingCaps" in body:
            caps = body["dailyTrainingCaps"]
            if caps is None:
                p.daily_training_cap_json = None
            else:
                if not isinstance(caps, dict):
                    raise HTTPException(400, "dailyTrainingCaps must be an object keyed by weekday, or null")
                cleaned = {}
                for k, v in caps.items():
                    try:
                        day, hours = int(k), float(v)
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"dailyTrainingCaps[{k}] must be weekday int -> hours")
                    if not 0 <= day <= 6 or not 0 <= hours <= 24:
                        raise HTTPException(400, "dailyTrainingCaps needs weekday 0-6 and 0-24 hours")
                    cleaned[str(day)] = hours
                p.daily_training_cap_json = json.dumps(cleaned) if cleaned else None

        if "sleepConstraintMode" in body:
            mode = body["sleepConstraintMode"]
            # No "hard" mode by design — see docs/P20_P21_DESIGN.md §2.4: a derived
            # wake-time pattern must never silently override an explicit day choice.
            if mode not in ("soft", "off"):
                raise HTTPException(400, 'sleepConstraintMode must be "soft" or "off"')
            p.sleep_constraint_mode = mode

        db.commit()
        return _plan_to_dict(db, p, user_id)
    finally:
        db.close()


@router.get("/api/plans/{plan_id}/weeks")
def get_plan_weeks(plan_id: str, goalId: str, weeksBack: int = 8, weeksForward: int = 12,
                   user_id: str = Depends(auth.current_user_id)):
    """Per-goal week series within the user's plan — `goalId` is required because P21's
    plan can serve several goals at once, each with its own phase/target (see
    generator.plan_week_view's attribution rule)."""
    db = SessionLocal()
    try:
        p = db.get(TrainingPlan, plan_id)
        if not p or p.user_id != user_id:
            raise HTTPException(404, "Plan not found")
        plan_goal = (db.query(PlanGoal)
                     .filter(PlanGoal.training_plan_id == plan_id, PlanGoal.goal_id == goalId)
                     .first())
        if not plan_goal:
            raise HTTPException(404, "That goal is not attached to this plan")
        goal = db.get(Goal, goalId)
        if not goal:
            raise HTTPException(404, "Goal no longer exists")

        driving = generator.resolve_periodization_goal(db, user_id, local_today(user_id))
        payload = {
            "planId": p.id, "goalId": goal.id, "goalName": goal.name,
            "goalTargetDate": goal.target_date, "role": plan_goal.role,
            "isActivePeriodizationGoal": bool(driving and driving.id == goal.id),
        }
        # Calendar-week volume resets to 0.0 every Monday morning, which is accurate and
        # useless as a progress signal — the rolling 7-day figure is reported alongside
        # the calendar-week target rather than replacing it (see TrainingPlanSection.tsx).
        payload["last7DaysMi"] = round(
            generator.rolling_window_mileage(
                db, user_id, generator.plan_activity_type(goal), days=7), 1)
        # Bounds are enforced inside project_week_series, not here — one place, so a
        # future caller can't route around them.
        payload["weeks"] = generator.project_week_series(
            db, user_id, p, goal, weeks_back=weeksBack, weeks_forward=weeksForward)
        return payload
    finally:
        db.close()
