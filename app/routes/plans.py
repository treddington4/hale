"""P20 — goal-tied training plan view. Read-only over the periodization math the
generator already does (see coach/generator.py's plan_week_view/project_week_series);
creating a plan here changes nothing about what the nightly generator prescribes. P21 is
where a started plan begins steering real generation."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import SessionLocal, Goal, TrainingPlan, DEFAULT_USER_ID
from ..accounts import auth
from ..coach import generator
from ..util import local_today

router = APIRouter()


def _owns_goal(goal: Goal, user_id: str) -> bool:
    """Row-level equivalent of models.owned_by()'s SQL predicate, for a Goal already
    fetched by primary key. Legacy rows predating the multi-user migration have
    user_id=NULL and belong to the default user — same read-time NULL handling used
    everywhere else in this codebase."""
    return goal.user_id == user_id or (goal.user_id is None and user_id == DEFAULT_USER_ID)


def _plan_to_dict(p: TrainingPlan, goal: Goal, driving_goal_id: str | None) -> dict:
    return {
        "id": p.id, "goalId": p.goal_id,
        "goalName": goal.name if goal else None,
        "goalTargetDate": goal.target_date if goal else None,
        "status": p.status, "createdAt": p.created_at,
        # False here is not a bug and not a "disabled" state — it means this plan's goal
        # is not what the generator is currently periodizing for, so the weeks below
        # describe this goal's own arc rather than what's actually being prescribed. The
        # UI says so out loud instead of implying a consistency that doesn't exist until P21.
        "isActivePeriodizationGoal": driving_goal_id is not None and driving_goal_id == p.goal_id,
    }


def _driving_goal_id(db, user_id) -> str | None:
    g = generator.nearest_active_race_goal(db, user_id, local_today(user_id))
    return g.id if g else None


@router.get("/api/plans")
def get_plans(user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        driving = _driving_goal_id(db, user_id)
        plans = (db.query(TrainingPlan)
                 .filter(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
                 .all())
        out = [_plan_to_dict(p, db.get(Goal, p.goal_id), driving) for p in plans]
        # Soonest race first — same ordering intent as /api/goals, and it puts the goal
        # you're actually training toward at the top when there's more than one plan.
        out.sort(key=lambda d: d["goalTargetDate"] or "9999-12-31")
        return out
    finally:
        db.close()


@router.post("/api/plans")
async def create_plan(request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    goal_id = body.get("goalId")
    if not goal_id:
        raise HTTPException(400, "goalId is required")
    db = SessionLocal()
    try:
        goal = db.get(Goal, goal_id)
        if not goal or not _owns_goal(goal, user_id):
            raise HTTPException(404, "Goal not found")
        if goal.goal_type != "race":
            raise HTTPException(400, "Only race goals can have a training plan — phase/deload/ramp "
                                     "math is defined in weeks-until-race-date and has no analog for "
                                     "consistency or distance_target goals.")
        if goal.status != "active":
            raise HTTPException(400, "Goal is not active")

        # Idempotent: "Start a Plan" has to be safely re-clickable (double-tap, stale UI,
        # retried request) without erroring or creating a second plan for the same goal.
        existing = (db.query(TrainingPlan)
                    .filter(TrainingPlan.user_id == user_id, TrainingPlan.goal_id == goal_id)
                    .first())
        if existing:
            if existing.status != "active":
                existing.status = "active"
                db.commit()
            return _plan_to_dict(existing, goal, _driving_goal_id(db, user_id))

        p = TrainingPlan(
            id=f"plan_{uuid.uuid4().hex[:12]}", user_id=user_id, goal_id=goal_id,
            status="active", created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(p)
        db.commit()
        return _plan_to_dict(p, goal, _driving_goal_id(db, user_id))
    finally:
        db.close()


@router.get("/api/plans/{plan_id}/weeks")
def get_plan_weeks(plan_id: str, weeksBack: int = 8, weeksForward: int = 12,
                   user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        p = db.get(TrainingPlan, plan_id)
        if not p or p.user_id != user_id:
            raise HTTPException(404, "Plan not found")
        goal = db.get(Goal, p.goal_id)
        if not goal:
            raise HTTPException(404, "Plan's goal no longer exists")
        payload = _plan_to_dict(p, goal, _driving_goal_id(db, user_id))
        # Calendar-week volume resets to 0.0 every Monday morning, which is accurate and
        # useless as a progress signal — on a Monday the panel reads "0.0 of 25.0" while
        # the athlete has in fact run 24 miles in the past seven days. The rolling figure
        # is what answers "where am I actually at right now"; the calendar one still
        # drives the ramp, so both are reported rather than one replacing the other.
        payload["last7DaysMi"] = round(
            generator.rolling_window_mileage(
                db, user_id, generator.plan_activity_type(goal), days=7), 1)
        # Bounds are enforced inside project_week_series, not here — one place, so a
        # future caller can't route around them.
        payload["weeks"] = generator.project_week_series(
            db, user_id, p, weeks_back=weeksBack, weeks_forward=weeksForward)
        return payload
    finally:
        db.close()
