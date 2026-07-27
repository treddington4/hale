"""Workouts (manual-UI path — mirrors the schedule_workout/update_workout chat tools
exactly, both call into coach/core.py so validation can't drift between them), training
config, and the Phase 4.3 generator's force-run endpoint."""
import json

from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import SessionLocal
from ..accounts import auth

router = APIRouter()


@router.get("/api/workouts")
def get_workouts(startDate: str = None, endDate: str = None, status: str = None,
                  user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    db = SessionLocal()
    try:
        return coach.list_workouts(db, startDate, endDate, status, user_id)
    finally:
        db.close()


@router.post("/api/workouts")
async def create_workout_endpoint(request: Request, user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    body = await request.json()
    db = SessionLocal()
    try:
        return coach.create_workout(
            db, body.get("scheduledDate"), body.get("workoutType"), body.get("activityType"),
            body.get("targetDistanceMi"), body.get("targetPaceSecPerMi"), body.get("targetDurationSec"),
            body.get("notes"), body.get("steps"), user_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        db.close()


@router.patch("/api/workouts/{workout_id}")
async def update_workout_endpoint(workout_id: str, request: Request, user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    body = await request.json()
    field_map = {
        "scheduledDate": "scheduled_date", "workoutType": "workout_type", "activityType": "activity_type",
        "targetDistanceMi": "target_distance_mi", "targetPaceSecPerMi": "target_pace_sec_per_mi",
        "targetDurationSec": "target_duration_sec", "notes": "notes", "steps": "steps", "status": "status",
    }
    fields = {py_key: body[api_key] for api_key, py_key in field_map.items() if api_key in body}
    db = SessionLocal()
    try:
        return coach.update_workout(db, workout_id, user_id=user_id, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        db.close()


@router.delete("/api/workouts/{workout_id}")
def delete_workout_endpoint(workout_id: str, user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    db = SessionLocal()
    try:
        coach.delete_workout(db, workout_id, user_id)
        return {"deleted": True}
    finally:
        db.close()


@router.get("/api/training-config")
def get_training_config_endpoint(user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    db = SessionLocal()
    try:
        return coach.get_training_config(db, user_id)
    finally:
        db.close()


@router.patch("/api/training-config")
async def update_training_config_endpoint(request: Request, user_id: str = Depends(auth.current_user_id)):
    from ..coach import core as coach
    body = await request.json()
    field_map = {
        "maxHr": "max_hr", "thresholdHr": "threshold_hr", "ftpWatts": "ftp_watts",
        "weeklyRampPct": "weekly_ramp_pct", "mesocyclePattern": "mesocycle_pattern",
        "distribution": "distribution", "strengthDaysPerWeek": "strength_days_per_week",
        "strengthTemplate": "strength_template", "rideDaysPerWeek": "ride_days_per_week",
    }
    fields = {py_key: body[api_key] for api_key, py_key in field_map.items() if api_key in body}
    if "zones" in body:
        fields["zones_json"] = json.dumps(body["zones"]) if body["zones"] is not None else None
    if "seedWeeklyMiles" in body:
        seed = body["seedWeeklyMiles"]
        fields["seed_weekly_miles_json"] = json.dumps(seed) if seed else None
    db = SessionLocal()
    try:
        return coach.update_training_config(db, user_id=user_id, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        db.close()


@router.post("/api/generator/run")
def run_generator_endpoint(date: str = None, user_id: str = Depends(auth.current_user_id)):
    """Force-runs the Phase 4.3 generator for the current user (optionally for a
    specific past/future date, mainly for verification) instead of waiting for the
    04:00 local scheduled tick."""
    from ..coach import generator
    db = SessionLocal()
    try:
        return generator.run_for_user(db, user_id, date)
    finally:
        db.close()


@router.post("/api/generator/quick/{domain}")
def run_quick_generate_endpoint(domain: str, template_override: str = None, dry_run: bool = False,
                                  user_id: str = Depends(auth.current_user_id)):
    """Phase 14 — the Quick Generate button's entry point. Always today — no date
    param exposed here, unlike /api/generator/run above (that one's a verification/
    backfill tool; this one is deliberately "give me one right now, never future").
    `template_override` only matters for domain="strength" — the target picker chip
    row lets the user override the auto-picked template (see run_quick_generate).
    `dry_run=True` (Phase 14.6's New Workout preview step) computes and returns the
    exact same result without writing anything — the frontend calls this once with
    dry_run=true to preview, then again without it (on Confirm) to actually save."""
    from ..coach import generator
    db = SessionLocal()
    try:
        return generator.run_quick_generate(db, user_id, domain, template_override=template_override, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        db.close()
