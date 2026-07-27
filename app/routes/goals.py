"""Goals CRUD — race/consistency/distance_target, driving both Home tab goal cards and
the header's race countdown (see app/stats.py's goal_progress)."""
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import SessionLocal, Goal, owned_by
from ..accounts import auth
from .. import stats

router = APIRouter()

_VALID_GOAL_TYPES = ("race", "consistency", "distance_target")


def _goal_to_dict(g: Goal, db):
    # goal_progress() can mutate g.status/g.completed_at as a side effect (auto-completing
    # a race goal once it finds a matching run — see stats._find_and_link_race_run). Must
    # run before status/completedAt are read below: a dict literal evaluates its values in
    # written order, so reading them first would silently capture the pre-mutation values.
    progress = stats.goal_progress(db, g)
    return {
        "id": g.id, "goalType": g.goal_type, "name": g.name, "status": g.status,
        "activityTypes": json.loads(g.activity_types_json or "[]"),
        "targetValue": g.target_value, "targetUnit": g.target_unit, "targetDate": g.target_date,
        "startDate": g.start_date, "notes": g.notes, "priority": g.priority or 0,
        "createdAt": g.created_at, "completedAt": g.completed_at,
        "progress": progress,
    }


@router.get("/api/goals")
def get_goals(user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        goals = (db.query(Goal).filter(owned_by(Goal.user_id, user_id))
                 .order_by(Goal.status, func.coalesce(Goal.priority, 0), Goal.target_date).all())
        return [_goal_to_dict(g, db) for g in goals]
    finally:
        db.close()


@router.post("/api/goals")
async def create_goal(request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    if body.get("goalType") not in _VALID_GOAL_TYPES:
        raise HTTPException(400, f"goalType must be one of {_VALID_GOAL_TYPES}")
    db = SessionLocal()
    try:
        g = Goal(
            id=f"goal_{uuid.uuid4().hex[:12]}", user_id=user_id,
            goal_type=body["goalType"], name=body.get("name") or "Untitled goal", status="active",
            activity_types_json=json.dumps(body.get("activityTypes") or ["Run"]),
            target_value=body.get("targetValue"), target_unit=body.get("targetUnit"),
            target_date=body.get("targetDate"), start_date=body.get("startDate"),
            notes=body.get("notes"), priority=body.get("priority") or 0,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(g)
        db.commit()
        return _goal_to_dict(g, db)
    finally:
        db.close()


@router.patch("/api/goals/{goal_id}")
async def update_goal(goal_id: str, request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    db = SessionLocal()
    try:
        g = db.query(Goal).filter(Goal.id == goal_id, owned_by(Goal.user_id, user_id)).first()
        if not g:
            raise HTTPException(404, "Goal not found")
        if "name" in body:
            g.name = body["name"]
        if "activityTypes" in body:
            g.activity_types_json = json.dumps(body["activityTypes"])
        if "targetValue" in body:
            g.target_value = body["targetValue"]
        if "targetUnit" in body:
            g.target_unit = body["targetUnit"]
        if "targetDate" in body:
            g.target_date = body["targetDate"]
        if "startDate" in body:
            g.start_date = body["startDate"]
        if "notes" in body:
            g.notes = body["notes"]
        if "priority" in body:
            g.priority = body["priority"] or 0
        if "status" in body and body["status"] != g.status:
            g.status = body["status"]
            g.completed_at = datetime.now(timezone.utc).isoformat() if body["status"] in ("completed", "abandoned") else None
        db.commit()
        return _goal_to_dict(g, db)
    finally:
        db.close()


@router.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: str, user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        g = db.query(Goal).filter(Goal.id == goal_id, owned_by(Goal.user_id, user_id)).first()
        if not g:
            raise HTTPException(404, "Goal not found")
        db.delete(g)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()


@router.get("/api/goals/{goal_id}/race-pack")
def get_race_pack(goal_id: str, user_id: str = Depends(auth.current_user_id)):
    """P17: Race-Day Pack — pacing, taper countdown, weather forecast for a race goal."""
    from .. import race_pack
    from ..sync import weather

    db = SessionLocal()
    try:
        g = db.query(Goal).filter(Goal.id == goal_id, owned_by(Goal.user_id, user_id)).first()
        if not g:
            raise HTTPException(404, "Goal not found")
        if g.goal_type != "race":
            raise HTTPException(400, "Only race goals have a race pack")

        result = {"id": goal_id, "name": g.name, "raceDate": g.target_date}

        # Pacing plan
        if g.target_time_sec and g.target_value:
            activity_type = (json.loads(g.activity_types_json or "[]") or ["Run"])[0]
            pacing = race_pack.compute_pacing_plan(g.target_time_sec, g.target_value, activity_type)
            result["pacing"] = pacing
        else:
            result["pacing"] = {"strategy": None, "explanation": "Set a target time and distance to generate a pacing plan"}

        # Taper countdown
        result["taper"] = race_pack.compute_taper(g.target_date)

        # Weather forecast
        if g.race_lat and g.race_lon and g.target_date:
            forecast = weather.get_weather_forecast(g.race_lat, g.race_lon, g.target_date)
            result["forecast"] = forecast
            result["raceLocation"] = g.race_location_label
        else:
            result["forecast"] = {"available": False, "reason": "Race location not set"}

        return result
    finally:
        db.close()
