"""Daily steps, resting HR/VO2max/sleep wellness rows (Garmin-only), and Runs CRUD."""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Query, Depends

from ..models import SessionLocal, DailySteps, Run, Activity, DailyMetrics, owned_by
from ..accounts import auth
from ..util import local_today

router = APIRouter()


# start/end/all take precedence over `days` when given — same precedence as
# GET /api/runs. Added so Insights' shared FilterBar can actually drive every
# chart on the page: Steps/Wellness/Metrics previously only ever accepted a
# fixed trailing-day count, so changing the filter silently did nothing for the
# charts backed by them (a real, user-reported gap — Temperature's Effect/Weekly
# Mileage/etc respond because they're backed by /api/runs, which already had
# this; Daily Steps/Resting HR/VO2 Max/Sleep/Training Load didn't).
def _apply_date_range(q, date_column, days: int, start: str | None, end: str | None, all_time: bool):
    if all_time:
        return q
    if not start and not end:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        return q.filter(date_column >= cutoff)
    if start:
        q = q.filter(date_column >= start)
    if end:
        q = q.filter(date_column <= end)
    return q


# ---------- Daily steps (Garmin-only) ----------
@router.get("/api/steps")
def get_steps(days: int = 30, start: str = None, end: str = None, all_time: bool = Query(False, alias="all"),
              user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        q = db.query(DailySteps).filter(owned_by(DailySteps.user_id, user_id))
        q = _apply_date_range(q, DailySteps.date, days, start, end, all_time)
        rows = q.order_by(DailySteps.date).all()
        return [{"date": r.date, "steps": r.steps} for r in rows]
    finally:
        db.close()


# ---------- Phase 6.2: PMC (fitness/fatigue/form) daily metrics ----------
@router.get("/api/metrics")
def get_metrics(days: int = 180, start: str = None, end: str = None, all_time: bool = Query(False, alias="all"),
                 user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        q = db.query(DailyMetrics).filter(owned_by(DailyMetrics.user_id, user_id))
        q = _apply_date_range(q, DailyMetrics.date, days, start, end, all_time)
        rows = q.order_by(DailyMetrics.date).all()
        return [
            {"date": r.date, "dailyLoad": r.daily_load, "ctl": r.ctl, "atl": r.atl, "tsb": r.tsb}
            for r in rows
        ]
    finally:
        db.close()


# ---------- Wellness: resting HR / VO2max / sleep (Garmin-only) ----------
@router.get("/api/wellness")
def get_wellness(days: int = 90, start: str = None, end: str = None, all_time: bool = Query(False, alias="all"),
                  user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        q = db.query(DailySteps).filter(owned_by(DailySteps.user_id, user_id))
        q = _apply_date_range(q, DailySteps.date, days, start, end, all_time)
        rows = q.order_by(DailySteps.date).all()
        return [{
            "date": r.date,
            "restingHrBpm": r.resting_hr_bpm,
            "vo2max": r.vo2max,
            "sleepScore": r.sleep_score,
            "sleepSeconds": r.sleep_seconds,
            "deepSleepSeconds": r.deep_sleep_seconds,
            "lightSleepSeconds": r.light_sleep_seconds,
            "remSleepSeconds": r.rem_sleep_seconds,
            "awakeSleepSeconds": r.awake_sleep_seconds,
            # Phase 24.3/24.2 — first surfaced anywhere in the UI here (previously
            # only fed into the Phase 27 daily coach report's prose). Same DailySteps
            # rows this endpoint already queries, so this is additive-only.
            "racePredict5kSec": r.race_predict_5k_sec,
            "racePredict10kSec": r.race_predict_10k_sec,
            "racePredictHalfMarathonSec": r.race_predict_half_marathon_sec,
            "racePredictMarathonSec": r.race_predict_marathon_sec,
            "trainingReadinessScore": r.training_readiness_score,
            "trainingReadinessLevel": r.training_readiness_level,
        } for r in rows]
    finally:
        db.close()


@router.get("/api/wellness/sleep-stages")
def get_sleep_stages(date: str = None, user_id: str = Depends(auth.current_user_id)):
    """Per-night sleep stage timeline (deep/light/rem/awake segments), for a real
    hypnogram rather than just daily totals — see garmin_sync._extract_sleep_stages().
    Returns every date that has stage data (for a night-picker) plus the requested
    (or most recent) night's segments."""
    db = SessionLocal()
    try:
        rows = (db.query(DailySteps)
                .filter(DailySteps.sleep_stages_json.isnot(None))
                .filter(DailySteps.sleep_stages_json != "[]")
                .filter(owned_by(DailySteps.user_id, user_id))
                .order_by(DailySteps.date).all())
        available_dates = [r.date for r in rows]
        if not available_dates:
            return {"availableDates": [], "date": None, "segments": []}
        target_row = next((r for r in rows if r.date == date), rows[-1])
        return {
            "availableDates": available_dates,
            "date": target_row.date,
            "segments": json.loads(target_row.sleep_stages_json or "[]"),
        }
    finally:
        db.close()


# ---------- Runs CRUD ----------
def _run_to_dict(r: Activity | Run):
    """Convert an Activity (or Run subclass) to API response dict. P7d: accepts Activity
    base class since /api/activities now queries Activity polymorphically; Run type hint
    kept for backward compatibility with sync/coach code that still uses Run."""
    return {
        "id": r.id, "source": r.source, "activityType": r.activity_type, "date": r.date, "startTime": r.start_time,
        "name": r.name, "distanceMi": r.distance_mi, "movingTimeSec": r.moving_time_sec,
        "elevGainFt": r.elev_gain_ft, "avgHR": r.avg_hr, "maxHR": r.max_hr,
        "avgCadence": r.avg_cadence, "avgPaceSecPerMi": r.avg_pace_sec_per_mi,
        "isTreadmill": r.is_treadmill, "tempF": r.temp_f, "weatherCondition": r.weather_condition,
        "heatIndexF": r.heat_index_f, "wetBulbF": r.wet_bulb_f,
        "suggestedType": r.suggested_type, "type": r.type_override or r.suggested_type,
        "rpe": r.rpe, "notes": r.notes,
        "splits": json.loads(r.splits_json or "[]"),
        "intervals": json.loads(r.intervals_json or "[]"),
        "recovery": json.loads(r.recovery_json or "[]"),
        "route": json.loads(r.route_json or "[]"),
        "routeMetrics": json.loads(r.route_metrics_json or "[]"),
        "verticalOscillationMm": r.vertical_oscillation_mm, "groundContactTimeMs": r.ground_contact_time_ms,
        "verticalRatioPct": r.vertical_ratio_pct, "strideLengthM": r.stride_length_m, "avgPowerWatts": r.avg_power_watts,
        "exerciseSets": json.loads(r.exercise_sets_json) if r.exercise_sets_json else None,
        "tss": r.tss, "efficiencyFactor": r.efficiency_factor,
        "gearId": r.gear_id,
    }


DEFAULT_RUNS_WINDOW_DAYS = 90


def _get_activities(start: str | None = None, end: str | None = None, all_time: bool = False,
                     user_id: str = None):
    """Core logic for querying activities (runs, rides, etc.) — shared by both
    /api/activities (P7d primary) and /api/runs (legacy alias for backward compatibility).

    This docstring previously claimed "Run queries work fine with NULL discriminators."
    That was wrong, and it was the belief that shipped the outage: a polymorphic subclass
    query appends `WHERE sti_type = 'run'`, so against production's all-NULL discriminator
    column this endpoint returned an empty list for every request while the table held 554
    rows. models._migrate_backfill_sti_type() now guarantees the discriminator is populated
    (see its docstring for the full story); querying Run here is safe *because of* that
    migration, not because subclass queries tolerate NULLs."""
    db = SessionLocal()
    try:
        q = db.query(Run).filter(owned_by(Run.user_id, user_id))
        if not all_time:
            if not start and not end:
                start = (local_today(user_id) - timedelta(days=DEFAULT_RUNS_WINDOW_DAYS - 1)).isoformat()
            if start:
                q = q.filter(Run.date >= start)
            if end:
                q = q.filter(Run.date <= end)
        activities = q.order_by(Run.date.desc()).all()
        return [_run_to_dict(a) for a in activities]
    finally:
        db.close()


@router.get("/api/activities")
def get_activities(start: str | None = None, end: str | None = None, all_time: bool = Query(False, alias="all"),
                   user_id: str = Depends(auth.current_user_id)):
    """P7d primary endpoint: all activities (runs, rides, strength, etc.), replacing
    the runs-only endpoint. Windowed by default (Phase 0.5 — this used to return every
    activity ever synced unconditionally, a multi-MB payload on every page load).
    `all=true` bypasses the window entirely — used by callers that need true all-time
    totals (e.g. the Home tab's exact stat-strip numbers) rather than trying to guess a
    "big enough" default range for every caller."""
    return _get_activities(start, end, all_time, user_id)


@router.get("/api/runs")
def get_runs(start: str | None = None, end: str | None = None, all_time: bool = Query(False, alias="all"),
             user_id: str = Depends(auth.current_user_id)):
    """P7d legacy alias for backward compatibility. See /api/activities (the primary
    endpoint). Both endpoints return identical data and will coexist for one release."""
    return _get_activities(start, end, all_time, user_id)


@router.patch("/api/runs/{run_id}")
async def update_run(run_id: str, request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id, owned_by(Run.user_id, user_id)).first()
        if not run:
            raise HTTPException(404, "Run not found")
        if "type" in body:
            run.type_override = body["type"]
        if "tempF" in body:
            run.temp_f = body["tempF"]
        if "weatherCondition" in body:
            run.weather_condition = body["weatherCondition"]
        if "rpe" in body:
            run.rpe = body["rpe"]
        if "isTreadmill" in body:
            run.is_treadmill = body["isTreadmill"]
        if "notes" in body:
            run.notes = body["notes"]
        if "gearId" in body:
            run.gear_id = body["gearId"]  # None explicitly unassigns
        db.commit()
        return _run_to_dict(run)
    finally:
        db.close()
