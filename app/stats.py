"""Shared computation core for the dashboard summary and the chat assistant's tools.
Both read from this module so a dashboard number and the assistant's answer to the
same question are always backed by identical logic — see CLAUDE.md's note on GAP
being computed once in util.py and again in app.js (kept in sync by hand) for why a
second drifting implementation of the same numbers is worth avoiding here.

Note: like GET /api/runs, these functions query the raw `runs` table directly, but
apply dedup.merge_duplicate_runs() (P3+) to collapse cross-source duplicates — Strava
and Garmin each write their own rows for the same physical run (confirmed: 135 dates
carry both spellings), which dedup collapses into one per physical activity. This is
identical to the client-side mergeDuplicateRuns() the frontend applies to /api/runs,
so backend aggregates and merged UI counts are now in sync.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from .models import (
    Activity, Run, DailySteps, Goal, Gear, UserTrainingConfig, DEFAULT_USER_ID, owned_by,
    SessionLocal, get_sync_meta, set_sync_meta, user_key,
)
from .util import local_today, compute_tss, compute_efficiency_factor

log = logging.getLogger("runlog")

# Strava's activity_type is a single PascalCase word ("Run","Ride","Walk"); Garmin's raw
# types are lowercase and sometimes multi-word ("running","trail_running",
# "treadmill_running"). Goals are created against Strava-style names (activity_types_json
# defaults to ["Run"]) — without normalizing, a race goal would never match a Garmin-
# sourced run at all. Scoped to just the race-run-matching helper below rather than
# applied to _all_runs()/run_summary() globally, to avoid changing every goal/dashboard
# number's existing (if imperfect) behavior in the same pass — noted as a related gap
# in STATUS.md rather than fixed everywhere here.
def _normalize_activity_type(t: str) -> str:
    t = (t or "").lower().replace("_", "")
    if "run" in t:
        return "run"
    if "cycl" in t or "bik" in t or t == "ride":
        return "ride"
    if "walk" in t:
        return "walk"
    if "hik" in t:
        return "hike"
    if "swim" in t:
        return "swim"
    if "weight" in t or "strength" in t:
        return "strength"
    if "yoga" in t:
        return "yoga"
    return t


def activity_family(activity_type: str) -> str:
    """Map an activity type to its family for TSS fallback factor lookup.
    Matches the frontend's runs.ts activityFamily() logic."""
    t = (activity_type or "").lower()
    if "run" in t:
        return "run"
    if "strength" in t or "weight" in t:
        return "strength"
    if "ride" in t or "cycl" in t or "bik" in t:
        return "ride"
    if "walk" in t:
        return "walk"
    if "hik" in t:
        return "hike"
    if "swim" in t:
        return "swim"
    if "yoga" in t:
        return "yoga"
    return "other"

PACE_MIN_SEC_PER_MI = 240
PACE_MAX_SEC_PER_MI = 2400
MIN_DISTANCE_MI = 0.1
ABSOLUTE_HR_MIN = 20
DEFAULT_HR_FLOOR = 30


def is_plausible_pace(pace_sec_per_mi, distance_mi=None) -> bool:
    """Port of app.js's isPlausiblePace — a distance-sensor glitch (e.g. near-zero
    distance over real elapsed time) produces a nonsense pace when divided out."""
    if pace_sec_per_mi is None:
        return False
    if distance_mi is not None and distance_mi < MIN_DISTANCE_MI:
        return False
    return PACE_MIN_SEC_PER_MI <= pace_sec_per_mi <= PACE_MAX_SEC_PER_MI


def format_pace_or_speed(pace_sec_per_mi, activity_type: str = "Run") -> str | None:
    """Human-readable pace/speed string for coach-facing (LLM) prompts — never hand a
    raw seconds-per-mile number to an LLM and expect it to reliably reformat/convert
    units on its own. Real bug caught from actual use: the daily coach report once
    echoed "596.9 sec/mi" verbatim instead of "9:57/mi". Running/walking/hiking read
    naturally as MM:SS/mi; cycling reads naturally as mph — same distinction the user
    asked for directly ("min/mile or miles/hr" depending on activity)."""
    if not pace_sec_per_mi or pace_sec_per_mi <= 0:
        return None
    t = (activity_type or "").lower()
    if "ride" in t or "cycl" in t or "bik" in t:
        return f"{3600 / pace_sec_per_mi:.1f} mph"
    minutes = int(pace_sec_per_mi // 60)
    seconds = round(pace_sec_per_mi % 60)
    return f"{minutes}:{seconds:02d}/mi"


def latest_resting_hr_bpm(db, user_id: str = DEFAULT_USER_ID):
    """Most recent resting HR from our own captured DailySteps data (populated by
    garmin_sync._sync_daily_wellness's get_stats() call) — reading from what we've
    already synced instead of maintaining a separate live-fetched cache. Replaced the
    old sync_meta-based single-global-value cache (garmin_resting_hr_bpm), which also
    meant this couldn't be scoped per-user; querying DailySteps directly fixes that for
    free since it already has a user_id column."""
    row = (
        db.query(DailySteps)
        .filter(DailySteps.resting_hr_bpm.isnot(None))
        .filter(owned_by(DailySteps.user_id, user_id))
        .order_by(DailySteps.date.desc())
        .first()
    )
    return row.resting_hr_bpm if row else None


def get_hr_floor(db, user_id: str = DEFAULT_USER_ID) -> float:
    """Port of app.js's computeHRFloor — prefers a real measured resting HR from
    Garmin (restingHR - 10%), falls back to a Strava-history-derived proxy (5th
    percentile of valid avgHR readings, minus 10%) when that's not synced yet."""
    resting = latest_resting_hr_bpm(db, user_id)
    if resting:
        return round(resting * 0.9)

    valid_hrs = sorted(
        hr for (hr,) in db.query(Run.avg_hr)
        .filter(Run.activity_type == "Run", Run.avg_hr.isnot(None), Run.avg_hr >= ABSOLUTE_HR_MIN)
        .filter(owned_by(Run.user_id, user_id))
        .all()
    )
    if not valid_hrs:
        return DEFAULT_HR_FLOOR
    p5 = valid_hrs[int(len(valid_hrs) * 0.05)]
    return round(p5 * 0.9)


def is_plausible_hr(bpm, hr_floor) -> bool:
    return bpm is not None and bpm >= hr_floor


def _linked_workout_for_run(db, run_id: str, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """Reverse lookup: find the Workout (if any) linked to this completed run via
    linked_run_id. Returns None if no link exists. Deterministic ordering (order by
    created_at DESC to get the most recent linked workout, never a bare .first()) so
    if a run is somehow linked to multiple workouts, we get the most recent one.
    P14: Used in run_detail to surface the prescribed workout for critique."""
    from .models import Workout
    workout = db.query(Workout).filter(
        Workout.linked_run_id == run_id, owned_by(Workout.user_id, user_id)
    ).order_by(Workout.created_at.desc()).first()
    if not workout:
        return None
    return {
        "id": workout.id,
        "workoutType": workout.workout_type,
        "activityType": workout.activity_type,
        "targetDistanceMi": workout.target_distance_mi,
        "targetDurationSec": workout.target_duration_sec,
        "notes": workout.notes,
        "status": workout.status,
    }


def run_detail(db, run_id: str, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """Full single-run detail (splits, HR, notes) — shared by assistant.py's
    get_run_detail chat tool and the "ask coach about this activity" hidden context
    block (coach/core.py's get_activity_context_block), so both see identical data.
    P7c: Use Activity for polymorphic query across all activity types.
    P14: Includes linkedWorkout if this run was linked to a prescribed workout."""
    r = db.query(Activity).filter(Activity.id == run_id, owned_by(Activity.user_id, user_id)).first()
    if not r:
        return None
    hr_floor = get_hr_floor(db, user_id)
    linked_workout = _linked_workout_for_run(db, run_id, user_id)
    return {
        "id": r.id, "date": r.date, "name": r.name, "activityType": r.activity_type,
        "distanceMi": r.distance_mi, "movingTimeSec": r.moving_time_sec,
        "paceSecPerMi": r.avg_pace_sec_per_mi if is_plausible_pace(r.avg_pace_sec_per_mi, r.distance_mi) else None,
        "avgHR": r.avg_hr if is_plausible_hr(r.avg_hr, hr_floor) else None,
        "maxHR": r.max_hr if is_plausible_hr(r.max_hr, hr_floor) else None,
        "elevGainFt": r.elev_gain_ft, "avgCadence": r.avg_cadence,
        "type": r.type_override or r.suggested_type, "rpe": r.rpe, "notes": r.notes,
        "splits": json.loads(r.splits_json or "[]"),
        "linkedWorkout": linked_workout,
    }


def _all_runs(db, activity_type="run", user_id: str = DEFAULT_USER_ID):
    """activity_type accepts a single type ("run", the default), a list (e.g. ["run","ride"]
    for a duathlon goal spanning multiple activity types), or None/falsy for all types.
    Input types are normalized to canonical form before filtering (P4 backfill normalized
    all stored values), so callers can still pass the original spellings.

    P6 changes default from "Run" to "run" (normalized form) — all downstream callers
    already pass "Run" or list-of-types, and _normalize_activity_type("Run") → "run".
    P7c: Changed from db.query(Run) to db.query(Activity) for proper polymorphic querying
    over the STI hierarchy — Run is now a subclass, and Activity is the base for the
    single-table inheritance on the `runs` table. This doesn't change filtering behavior
    (sti_type is still applied via activity_type), but makes the intent clearer.
    Applies dedup.merge_duplicate_runs() to the query result — see that module's
    docstring for why. P3 landed dedup before normalization was complete; P4 backfilled
    stored values and broadens this filter to canonical families, so dedup now stops the
    duplicates instead of the old exact-match filter doing it accidentally."""
    from .dedup import merge_duplicate_runs

    q = db.query(Activity).filter(owned_by(Activity.user_id, user_id))
    if activity_type:
        types = [activity_type] if isinstance(activity_type, str) else list(activity_type)
        normalized_types = [_normalize_activity_type(t) for t in types]
        q = q.filter(Activity.activity_type.in_(normalized_types))
    return merge_duplicate_runs(q.all())


def _all_distance_activities(db, user_id: str = DEFAULT_USER_ID):
    """All distance-based activities (run, ride, walk, hike, swim) for aggregates like
    weekly/monthly mileage. Strength, yoga, and other activities have no meaningful
    distance metric."""
    return _all_runs(db, ["run", "ride", "walk", "hike", "swim"], user_id)


def _all_activities(db, user_id: str = DEFAULT_USER_ID):
    """All activities regardless of type, for aggregates like training load (TSS applies
    to all activity types via P5's activity-family fallback factors)."""
    return _all_runs(db, None, user_id)


def _week_start(d):
    return d - timedelta(days=d.weekday())  # Monday


def weekly_mileage(db, weeks: int = 12, activity_type: str = "Run", user_id: str = DEFAULT_USER_ID):
    today = local_today(user_id)
    this_week_start = _week_start(today)
    earliest = this_week_start - timedelta(weeks=weeks - 1)
    runs = [
        r for r in _all_runs(db, activity_type, user_id)
        if r.date and earliest.isoformat() <= r.date <= today.isoformat()
    ]
    buckets = {}
    for r in runs:
        wk = _week_start(datetime.strptime(r.date, "%Y-%m-%d").date())
        b = buckets.setdefault(wk, {"totalMiles": 0.0, "runCount": 0})
        b["totalMiles"] += r.distance_mi or 0
        b["runCount"] += 1
    out = []
    for i in range(weeks):
        wk = earliest + timedelta(weeks=i)
        b = buckets.get(wk, {"totalMiles": 0.0, "runCount": 0})
        out.append({"weekStart": wk.isoformat(), "totalMiles": round(b["totalMiles"], 2), "runCount": b["runCount"]})
    return out


def monthly_mileage(db, months: int = 12, activity_type: str = "Run", user_id: str = DEFAULT_USER_ID):
    today = local_today(user_id)
    months_list = []
    y, m = today.year, today.month
    for _ in range(months):
        months_list.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months_list.reverse()
    runs = _all_runs(db, activity_type, user_id)
    buckets = {ym: {"totalMiles": 0.0, "runCount": 0} for ym in months_list}
    for r in runs:
        if not r.date:
            continue
        ym = (int(r.date[:4]), int(r.date[5:7]))
        if ym in buckets:
            buckets[ym]["totalMiles"] += r.distance_mi or 0
            buckets[ym]["runCount"] += 1
    return [
        {"month": f"{y:04d}-{m:02d}", "totalMiles": round(buckets[(y, m)]["totalMiles"], 2), "runCount": buckets[(y, m)]["runCount"]}
        for (y, m) in months_list
    ]


def personal_records(db, activity_type: str = "Run", user_id: str = DEFAULT_USER_ID):
    runs = _all_runs(db, activity_type, user_id)

    def pick(candidates, key):
        best = max(candidates, key=key, default=None)
        if best is None:
            return None
        return {"runId": best.id, "date": best.date, "name": best.name, "value": key(best)}

    longest_run = pick([r for r in runs if r.distance_mi], lambda r: r.distance_mi)
    most_elevation = pick([r for r in runs if r.elev_gain_ft], lambda r: r.elev_gain_ft)
    longest_duration = pick([r for r in runs if r.moving_time_sec], lambda r: r.moving_time_sec)

    plausible_pace_runs = [r for r in runs if is_plausible_pace(r.avg_pace_sec_per_mi, r.distance_mi)]
    fastest = min(plausible_pace_runs, key=lambda r: r.avg_pace_sec_per_mi, default=None)
    fastest_pace = None
    if fastest:
        fastest_pace = {"runId": fastest.id, "date": fastest.date, "name": fastest.name, "value": fastest.avg_pace_sec_per_mi}

    return {
        "longestRun": longest_run,
        "fastestPace": fastest_pace,
        "mostElevation": most_elevation,
        "longestDuration": longest_duration,
    }


def rolling_pace_trend(db, days: int = 90, window_days: int = 7, user_id: str = DEFAULT_USER_ID,
                        activity_type: str = "Run"):
    """Port of app.js's rollingPaceData — for each run in the trailing `days`, a
    distance-weighted average pace over the `window_days` ending on that run's date,
    looking back across ALL running history (not just the `days` window) so early
    points in a narrow window aren't computed from an artificially thin lookback.
    `activity_type` (Phase 14) lets a per-activity baseline (e.g. "Ride") be computed
    the same way, rather than always assuming Run."""
    today = local_today(user_id)
    cutoff = (today - timedelta(days=days)).isoformat()

    all_history = [
        r for r in _all_runs(db, activity_type, user_id)
        if r.date and is_plausible_pace(r.avg_pace_sec_per_mi, r.distance_mi)
    ]
    recent = [r for r in all_history if r.date >= cutoff]

    out = []
    for r in recent:
        end = datetime.strptime(r.date, "%Y-%m-%d").date()
        start = end - timedelta(days=window_days - 1)
        window_runs = [
            rr for rr in all_history
            if start.isoformat() <= rr.date <= end.isoformat()
        ]
        total_dist = sum(rr.distance_mi or 0 for rr in window_runs)
        if not total_dist:
            continue
        pace = sum((rr.avg_pace_sec_per_mi or 0) * (rr.distance_mi or 0) for rr in window_runs) / total_dist
        out.append({"date": r.date, "paceSecPerMi": round(pace, 1)})
    out.sort(key=lambda p: p["date"])
    return out


def training_load_trend(db, weeks: int = 8, user_id: str = DEFAULT_USER_ID, activity_type: str = None):
    """Trailing 4 weeks vs. prior 4 weeks total TSS (training stress score), using
    rolling 28-day windows (not calendar-week buckets) so the current in-progress week
    doesn't bias the comparison. P6 change: now TSS-based and spans all activities
    by default (`activity_type=None`), since P5 added TSS estimates for all families.
    Can still scope to a specific type (e.g. "run", "ride") for per-activity analysis."""
    today = local_today(user_id)
    last28_start = (today - timedelta(days=27)).isoformat()
    prior28_start = (today - timedelta(days=55)).isoformat()
    prior28_end = (today - timedelta(days=28)).isoformat()

    runs = _all_runs(db, activity_type, user_id) if activity_type else _all_activities(db, user_id)
    last28 = sum(r.tss or 0 for r in runs if r.date and last28_start <= r.date <= today.isoformat())
    prior28 = sum(r.tss or 0 for r in runs if r.date and prior28_start <= r.date <= prior28_end)

    pct_change = None
    if prior28 > 0:
        pct_change = round((last28 - prior28) / prior28 * 100, 1)
    direction = "steady"
    if pct_change is not None:
        direction = "up" if pct_change > 5 else ("down" if pct_change < -5 else "steady")

    return {
        "last28DaysTSS": round(last28, 1),
        "prior28DaysTSS": round(prior28, 1),
        "pctChange": pct_change,
        "direction": direction,
    }


_HARD_RUN_TYPES = ("Tempo", "Interval", "Long Run")


def readiness(db, user_id: str = DEFAULT_USER_ID, date=None) -> dict:
    """Phase 4.1 — single computation core for both the workout generator (4.3, both
    the endurance and strength paths share this one result for a given date) and the
    `get_readiness` chat tool. Reads DailySteps' HRV/resting-HR/sleep columns
    (garmin_sync._sync_daily_wellness) plus real Run history — no invented composite
    "readiness score", just real numbers and named flags, same "don't fabricate a
    judgment" principle as goal_progress()."""
    target = date or local_today(user_id)
    if isinstance(target, str):
        target = datetime.strptime(target, "%Y-%m-%d").date()

    def _wellness_row(d):
        return (
            db.query(DailySteps)
            .filter(DailySteps.date == d.isoformat(), owned_by(DailySteps.user_id, user_id))
            .first()
        )

    today_row = _wellness_row(target)

    def _trailing_avg(field: str):
        vals = []
        for i in range(1, 8):  # trailing 7 days, excluding target itself
            row = _wellness_row(target - timedelta(days=i))
            v = getattr(row, field, None) if row else None
            if v is not None:
                vals.append(v)
        return (sum(vals) / len(vals)) if vals else None

    flags = []

    hrv_today = today_row.hrv_last_night_avg_ms if today_row else None
    hrv_baseline = _trailing_avg("hrv_last_night_avg_ms")
    hrv_delta_ms = round(hrv_today - hrv_baseline) if (hrv_today is not None and hrv_baseline is not None) else None
    if hrv_delta_ms is not None and hrv_delta_ms < -10:
        flags.append("hrv_below_baseline")

    rhr_today = today_row.resting_hr_bpm if today_row else None
    rhr_baseline = _trailing_avg("resting_hr_bpm")
    resting_hr_delta = round(rhr_today - rhr_baseline) if (rhr_today is not None and rhr_baseline is not None) else None
    if resting_hr_delta is not None and resting_hr_delta >= 5:
        flags.append("rhr_spike")

    sleep_score = today_row.sleep_score if today_row else None
    sleep_seconds = today_row.sleep_seconds if today_row else None
    if sleep_seconds is not None and sleep_seconds < 6.5 * 3600:
        flags.append("sleep_deficit")

    # P11 — respiration baseline drift (waking rate is more stable than exercise rate)
    resp_today = today_row.avg_waking_respiration_rate if today_row else None
    resp_baseline = _trailing_avg("avg_waking_respiration_rate")
    resp_delta = round((resp_today - resp_baseline) * 10) / 10 if (resp_today is not None and resp_baseline is not None) else None
    if resp_delta is not None and resp_delta >= 1.0:
        flags.append("respiration_elevated")

    # P11 — Garmin's own training readiness score (0-100) as a cross-check
    garmin_readiness = today_row.training_readiness_score if today_row else None
    garmin_readiness_level = today_row.training_readiness_level if today_row else None
    if garmin_readiness is not None and garmin_readiness < 50:
        flags.append("garmin_readiness_low")

    runs = _all_runs(db, "Run", user_id)
    last7_start = (target - timedelta(days=6)).isoformat()
    prior28_start = (target - timedelta(days=27)).isoformat()
    target_str = target.isoformat()
    last7 = sum(r.distance_mi or 0 for r in runs if r.date and last7_start <= r.date <= target_str)
    trailing28 = sum(r.distance_mi or 0 for r in runs if r.date and prior28_start <= r.date <= target_str)
    weekly_avg_28 = trailing28 / 4
    acute_chronic_ratio = round(last7 / weekly_avg_28, 2) if weekly_avg_28 > 0 else None

    hard_dates = sorted(
        (r.date for r in runs if r.date and r.date <= target_str and r.suggested_type in _HARD_RUN_TYPES),
        reverse=True,
    )
    days_since_hard = None
    if hard_dates:
        days_since_hard = (target - datetime.strptime(hard_dates[0], "%Y-%m-%d").date()).days

    return {
        "date": target_str,
        "hrvDeltaMs": hrv_delta_ms,
        "restingHrDelta": resting_hr_delta,
        "sleepScore": sleep_score,
        "acuteChronicRatio": acute_chronic_ratio,
        "daysSinceHard": days_since_hard,
        # P11 — respiration and Garmin readiness
        "respirationDelta": resp_delta,
        "garminReadinessScore": garmin_readiness,
        "garminReadinessLevel": garmin_readiness_level,
        "flags": flags,
    }


def weekly_consistency_streak(db, min_miles: float = 1.0, min_runs: int = None,
                               activity_type="Run", user_id: str = DEFAULT_USER_ID):
    """Consecutive Mon-Sun weeks, walking backward from the current (possibly
    in-progress) week, meeting a threshold. If min_runs is given (e.g. a "3x/week"
    consistency goal), a week must have that many runs; otherwise it must meet
    min_miles. Stops at the first week below threshold or at the earliest run on
    record."""
    today = local_today(user_id)
    runs = _all_runs(db, activity_type, user_id)
    if not runs:
        return {"streakWeeks": 0, "minMiles": min_miles, "minRuns": min_runs}
    earliest_date = min(r.date for r in runs if r.date)

    buckets = {}
    for r in runs:
        if not r.date:
            continue
        wk = _week_start(datetime.strptime(r.date, "%Y-%m-%d").date())
        b = buckets.setdefault(wk, {"totalMiles": 0.0, "runCount": 0})
        b["totalMiles"] += r.distance_mi or 0
        b["runCount"] += 1

    def meets(b):
        if min_runs is not None:
            return b["runCount"] >= min_runs
        return b["totalMiles"] >= min_miles

    streak = 0
    wk = _week_start(today)
    empty_bucket = {"totalMiles": 0.0, "runCount": 0}
    while wk.isoformat() >= earliest_date:
        if meets(buckets.get(wk, empty_bucket)):
            streak += 1
            wk -= timedelta(weeks=1)
        else:
            break
    return {"streakWeeks": streak, "minMiles": min_miles, "minRuns": min_runs}


def days_since_longest_run(db, user_id: str = DEFAULT_USER_ID):
    pr = personal_records(db, user_id=user_id)
    longest = pr["longestRun"]
    if not longest:
        return None
    days = (local_today(user_id) - datetime.strptime(longest["date"], "%Y-%m-%d").date()).days
    return {"days": days, "date": longest["date"], "distanceMi": longest["value"], "runId": longest["runId"]}


def days_since_last_run(db, activity_type: str = "Run", user_id: str = DEFAULT_USER_ID):
    runs = [r for r in _all_runs(db, activity_type, user_id) if r.date]
    if not runs:
        return None
    latest = max(runs, key=lambda r: r.date)
    days = (local_today(user_id) - datetime.strptime(latest.date, "%Y-%m-%d").date()).days
    return {"days": days, "date": latest.date, "runId": latest.id, "name": latest.name}


def _header_stats(db, user_id: str = DEFAULT_USER_ID) -> dict:
    """Cheap approximation of the Home tab's stat-strip (This week / avg pace / runs
    logged) — computed from the DB directly, not from the several-MB /api/runs payload
    (full splits/weather/route data for every run) app.js normally derives these from.
    Included in dashboard_summary() (small, already cached in sync_meta) so the Home
    tab's first paint doesn't have to wait on that fetch. Approximate for one specific,
    documented reason (see module docstring): no client-side mergeDuplicateRuns() dedup,
    so counts/totals here can be off by however many Strava+Garmin duplicate pairs
    exist, until the real /api/runs data lands and app.js's updateHeaderStats() corrects
    it with the exact numbers. Deliberately does NOT include a per-activity-type weekly
    breakdown (unlike the exact version) — a duplicate pair shows up under each source's
    own raw activity_type string here (e.g. Strava's "Run" and Garmin's "running" as two
    separate lines for the same physical runs), which mergeDuplicateRuns() would collapse
    into one but this function has no way to detect without reimplementing that same-run
    matching logic — not worth it for a display that's only visible for a second or two.
    app.js simply leaves the breakdown line blank until the exact, already-merged data
    replaces this approximation."""
    week_ago = (local_today(user_id) - timedelta(days=7)).isoformat()
    all_time = run_summary(db, activity_type="Run", user_id=user_id)
    this_week = run_summary(db, start_date=week_ago, activity_type="Run", user_id=user_id)
    return {
        "totalActivityCount": len(_all_runs(db, activity_type=None, user_id=user_id)),
        "runCountAllTime": all_time["runCount"],
        "avgPaceSecPerMiAllTime": all_time["avgPaceSecPerMi"],
        "weekMileageRun": this_week["totalDistanceMi"],
    }


def dashboard_summary(db, user_id: str = DEFAULT_USER_ID) -> dict:
    """Everything the Home tab's stat-card grid needs, in one call. Moved here from
    main.py's dashboard_summary endpoint so the cached and live-fallback paths (see
    main.py's dashboard cache, keyed off _record_sync) can never compute this two
    different ways — one function, one meaning, matching this module's own discipline.

    P6 change: trainingLoad and headerStats now span all activity types (TSS applies
    to all via P5), while mileage/pace metrics stay distance/run-scoped."""
    return {
        "weeklyMileage": weekly_mileage(db, weeks=12, user_id=user_id),
        "trainingLoad": training_load_trend(db, user_id=user_id),
        "consistencyStreak": weekly_consistency_streak(db, user_id=user_id),
        "daysSinceLongestRun": days_since_longest_run(db, user_id=user_id),
        "daysSinceLastRun": days_since_last_run(db, user_id=user_id),
        "paceTrend": rolling_pace_trend(db, days=90, user_id=user_id),
        "personalRecords": personal_records(db, user_id=user_id),
        "monthlyMileage": monthly_mileage(db, months=2, user_id=user_id),
        "headerStats": _header_stats(db, user_id=user_id),
    }


def run_summary(db, start_date=None, end_date=None, activity_type="Run", user_id: str = DEFAULT_USER_ID):
    runs = _all_runs(db, activity_type, user_id)
    if start_date:
        runs = [r for r in runs if r.date and r.date >= start_date]
    if end_date:
        runs = [r for r in runs if r.date and r.date <= end_date]

    total_dist = sum(r.distance_mi or 0 for r in runs)
    plausible = [r for r in runs if is_plausible_pace(r.avg_pace_sec_per_mi, r.distance_mi)]
    avg_pace = None
    if total_dist and plausible:
        weighted = sum((r.avg_pace_sec_per_mi or 0) * (r.distance_mi or 0) for r in plausible)
        plausible_dist = sum(r.distance_mi or 0 for r in plausible)
        avg_pace = round(weighted / plausible_dist, 1) if plausible_dist else None

    return {
        "runCount": len(runs),
        "totalDistanceMi": round(total_dist, 2),
        "avgPaceSecPerMi": avg_pace,
        "totalElevGainFt": round(sum(r.elev_gain_ft or 0 for r in runs), 1),
        "totalMovingTimeSec": sum(r.moving_time_sec or 0 for r in runs),
    }


def query_runs(db, start_date=None, end_date=None, activity_type=None,
                min_distance=None, max_distance=None, sort_by="date", limit=20,
                user_id: str = DEFAULT_USER_ID):
    """General-purpose flexible lookup backing free-form chat questions the canned
    aggregates above don't cover (e.g. "runs over 10 miles this year")."""
    hr_floor = get_hr_floor(db, user_id)
    runs = _all_runs(db, activity_type, user_id)
    if start_date:
        runs = [r for r in runs if r.date and r.date >= start_date]
    if end_date:
        runs = [r for r in runs if r.date and r.date <= end_date]
    if min_distance is not None:
        runs = [r for r in runs if (r.distance_mi or 0) >= min_distance]
    if max_distance is not None:
        runs = [r for r in runs if (r.distance_mi or 0) <= max_distance]

    reverse = sort_by in ("date", "distance_mi", "elev_gain_ft")
    key_fn = {
        "date": lambda r: r.date or "",
        "distance_mi": lambda r: r.distance_mi or 0,
        "elev_gain_ft": lambda r: r.elev_gain_ft or 0,
        "pace": lambda r: r.avg_pace_sec_per_mi or 9999999,
    }.get(sort_by, lambda r: r.date or "")
    runs = sorted(runs, key=key_fn, reverse=reverse)[:limit]

    return [{
        "id": r.id, "date": r.date, "name": r.name, "activityType": r.activity_type,
        "distanceMi": r.distance_mi,
        "paceSecPerMi": r.avg_pace_sec_per_mi if is_plausible_pace(r.avg_pace_sec_per_mi, r.distance_mi) else None,
        "elevGainFt": r.elev_gain_ft,
        "avgHR": r.avg_hr if is_plausible_hr(r.avg_hr, hr_floor) else None,
        "type": r.type_override or r.suggested_type,
    } for r in runs]


def daily_steps_summary(db, days: int = 30, user_id: str = DEFAULT_USER_ID):
    cutoff = (local_today(user_id) - timedelta(days=days)).isoformat()
    rows = (db.query(DailySteps).filter(DailySteps.date >= cutoff)
            .filter(owned_by(DailySteps.user_id, user_id)).order_by(DailySteps.date).all())
    steps = [r.steps for r in rows if r.steps is not None]
    return {
        "days": [{"date": r.date, "steps": r.steps} for r in rows],
        "avgSteps": round(sum(steps) / len(steps)) if steps else None,
    }


def wellness_trend(db, days: int = 7, user_id: str = DEFAULT_USER_ID) -> dict:
    """Trailing-N-day training-readiness/ACWR/race-prediction/body-battery/stress
    trend — the Phase 24 DailySteps columns, never exposed anywhere before Phase 27
    (daily coach report). Single computation core per this module's own docstring:
    any future chart/chat tool wanting this trend should call this, not re-query.

    `days=N` means N calendar days inclusive of today (today, today-1, ..., today-
    (N-1)) — timedelta(days=days) would give an N+1-day window (a real off-by-one
    caught in testing via daily_report.py, which fed this into a user-facing "past
    7 days" claim that was actually counting 8 days)."""
    cutoff = (local_today(user_id) - timedelta(days=days - 1)).isoformat()
    rows = (db.query(DailySteps).filter(DailySteps.date >= cutoff)
            .filter(owned_by(DailySteps.user_id, user_id)).order_by(DailySteps.date).all())
    return {
        "days": [{
            "date": r.date,
            "trainingReadinessScore": r.training_readiness_score,
            "trainingReadinessLevel": r.training_readiness_level,
            "garminAcwr": r.garmin_acwr,
            "garminAcwrStatus": r.garmin_acwr_status,
            "racePredict5kSec": r.race_predict_5k_sec,
            "racePredict10kSec": r.race_predict_10k_sec,
            "racePredictHalfMarathonSec": r.race_predict_half_marathon_sec,
            "racePredictMarathonSec": r.race_predict_marathon_sec,
            "bodyBatteryCharged": r.body_battery_charged,
            "bodyBatteryDrained": r.body_battery_drained,
            "avgStressLevel": r.avg_stress_level,
        } for r in rows],
    }


def list_active_goals_with_progress(db, user_id: str = DEFAULT_USER_ID) -> list:
    """All active goals, priority order (lower first), each with real computed progress
    via goal_progress() below — the same function backing /api/goals, so the Coach's
    view of a goal's progress can never drift from what the Goals tab shows. Exists so
    the chat tool has something to actually call: previously no tool wrapped Goal data
    at all, so a goal question had nothing to query (see GitHub issue #2, comment 4)."""
    goals = (
        db.query(Goal)
        .filter(Goal.status == "active", owned_by(Goal.user_id, user_id))
        .order_by(func.coalesce(Goal.priority, 0))
        .all()
    )
    out = []
    for g in goals:
        out.append({
            "id": g.id, "goalType": g.goal_type, "name": g.name,
            "activityTypes": json.loads(g.activity_types_json or '["Run"]'),
            "targetValue": g.target_value, "targetUnit": g.target_unit, "targetDate": g.target_date,
            "priority": g.priority or 0, "notes": g.notes,
            "progress": goal_progress(db, g, user_id),
        })
    return out


def goal_progress(db, goal, user_id: str = DEFAULT_USER_ID):
    """Dispatches to the right progress calculation for a Goal row. Deliberately no
    invented "on track" verdict for race goals — just a countdown plus real recent
    training volume, so this never presents a fabricated judgment as fact."""
    types = json.loads(goal.activity_types_json or '["Run"]')
    if goal.goal_type == "race":
        return _race_goal_progress(db, goal, types, user_id)
    if goal.goal_type == "consistency":
        return _consistency_goal_progress(db, goal, types, user_id)
    if goal.goal_type == "distance_target":
        return _distance_target_progress(db, goal, types, user_id)
    return {"error": f"unknown goal_type: {goal.goal_type}"}


def _race_goal_progress(db, goal, types, user_id):
    today = local_today(user_id)
    days_until = None
    if goal.target_date:
        days_until = (datetime.strptime(goal.target_date, "%Y-%m-%d").date() - today).days

    linked_run = None
    if goal.target_date and days_until is not None and days_until <= 0:
        linked_run = _find_and_link_race_run(db, goal, types, user_id)

    recent = run_summary(db, start_date=(today - timedelta(days=27)).isoformat(),
                          end_date=today.isoformat(), activity_type=types, user_id=user_id)
    result = {
        "goalType": "race", "raceDate": goal.target_date, "daysUntil": days_until,
        "targetDistanceMi": goal.target_value,
        "recent28DayMiles": recent["totalDistanceMi"], "recent28DayRunCount": recent["runCount"],
    }
    if linked_run:
        result["linkedRun"] = {
            "runId": linked_run.id,
            "name": linked_run.name,
            "date": linked_run.date,
            "distanceMi": linked_run.distance_mi,
            "movingTimeSec": linked_run.moving_time_sec,
            "avgPaceSecPerMi": linked_run.avg_pace_sec_per_mi,
        }
    return result


def _find_and_link_race_run(db, goal, types, user_id):
    """Once a race's date has passed, find the actual matching Run (exact target_date
    match only) and persist the link — so the goal shows its real result instead of a
    stale "Race day!" that would otherwise repeat forever if the user never manually
    marks it completed. Auto-completes the goal on a confirmed match (a real
    correlated event, not a fabricated verdict); leaves it "active" with no invented
    implication of success if nothing matches, since the race may simply not be logged.

    Previously used a target_date +/-1 day tolerance window — the same class of bug
    fixed in coach/core.py's _find_and_link_workout_run: a goal with no real distance
    target (goal.target_value None) falls through to "just pick the largest nearby
    run," which could auto-complete a placeholder/test goal against an unrelated
    ordinary training run a day off from its target date. Tightened to the exact
    date only; a real off-by-a-day race mismatch can still be linked manually."""
    if goal.linked_run_id:
        return db.get(Run, goal.linked_run_id)

    try:
        target = datetime.strptime(goal.target_date, "%Y-%m-%d").date()
    except Exception:
        return None

    wanted = {_normalize_activity_type(t) for t in types}
    candidates = db.query(Activity).filter(Activity.date == target.isoformat()).filter(owned_by(Activity.user_id, user_id)).all()
    candidates = [r for r in candidates if _normalize_activity_type(r.activity_type) in wanted]
    if not candidates:
        return None

    best = (
        min(candidates, key=lambda r: abs((r.distance_mi or 0) - goal.target_value))
        if goal.target_value else
        max(candidates, key=lambda r: r.distance_mi or 0)
    )

    goal.linked_run_id = best.id
    if goal.status == "active":
        goal.status = "completed"
        goal.completed_at = datetime.now().isoformat()
    db.commit()
    return best


def _consistency_goal_progress(db, goal, types, user_id):
    if goal.target_unit == "runs_per_week":
        streak = weekly_consistency_streak(db, min_runs=int(goal.target_value), activity_type=types, user_id=user_id)
    else:
        streak = weekly_consistency_streak(db, min_miles=goal.target_value, activity_type=types, user_id=user_id)
    wk = weekly_mileage(db, weeks=1, activity_type=types, user_id=user_id)[0]
    return {
        "goalType": "consistency", "targetValue": goal.target_value, "targetUnit": goal.target_unit,
        "streakWeeks": streak["streakWeeks"],
        "currentWeekMiles": wk["totalMiles"], "currentWeekRunCount": wk["runCount"],
    }


def _distance_target_progress(db, goal, types, user_id):
    start = goal.start_date or (goal.created_at or "")[:10]
    summary = run_summary(db, start_date=start, end_date=goal.target_date, activity_type=types, user_id=user_id)
    pct = round(summary["totalDistanceMi"] / goal.target_value * 100, 1) if goal.target_value else None
    days_remaining = None
    if goal.target_date:
        days_remaining = (datetime.strptime(goal.target_date, "%Y-%m-%d").date() - local_today(user_id)).days
    return {
        "goalType": "distance_target", "targetMi": goal.target_value,
        "completedMi": summary["totalDistanceMi"], "pctComplete": pct,
        "startDate": start, "deadline": goal.target_date, "daysRemaining": days_remaining,
    }


# ---------- Dashboard cache (moved here from main.py during the routes/ split —
# routes/sync.py calls record_sync() after every sync attempt, routes/dashboard.py
# reads the cache it populates, and it already calls dashboard_summary() below, so
# this keeps "the dashboard cache" next to the computation it caches rather than
# leaving it stranded as state neither router individually owns) ----------
DASHBOARD_CACHE_KEY = "dashboard_summary_cache"
DASHBOARD_CACHE_UPDATED_AT_KEY = "dashboard_summary_cache_updated_at"


def refresh_dashboard_cache(user_id: str):
    """Recomputes the Home tab's stat-card data and caches it in sync_meta (reusing the
    existing generic key-value store rather than a new single-purpose table — same
    pattern already used for the geocode cache) so /api/dashboard/summary is a plain
    lookup instead of recomputing 8 stats functions on every page load. Called from
    record_sync, the one place every sync path (auto/manual Strava, manual Garmin,
    both backlog syncs) already funnels through, so the cache is refreshed at least as
    often as SYNC_INTERVAL_HOURS even on a day with zero new activities — day-counting
    stats like "days since longest run" still need to advance without new data."""
    db = SessionLocal()
    try:
        summary = dashboard_summary(db, user_id=user_id)
        set_sync_meta(user_key(user_id, DASHBOARD_CACHE_KEY), json.dumps(summary))
        set_sync_meta(user_key(user_id, DASHBOARD_CACHE_UPDATED_AT_KEY), datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning(f"Dashboard cache refresh failed (stale cache will keep serving): {e}")
    finally:
        db.close()


def record_sync(source: str, user_id: str, count: int = None, error: str = None):
    """Persist last-sync info to sync_meta so the UI can show real history
    across page loads instead of only reflecting the current browser session.
    A sync can partially succeed and then error (e.g. Garmin rate-limits mid-backlog
    after committing several real activities) — count and error aren't mutually
    exclusive, so both are recorded when both are given, instead of a real partial
    success being silently lost behind "Never synced". Namespaced per-user (Phase 1.4)
    so two real users' sync history never overwrites each other's."""
    if count is not None:
        set_sync_meta(user_key(user_id, f"{source}_last_synced_at"), datetime.now(timezone.utc).isoformat())
        set_sync_meta(user_key(user_id, f"{source}_last_count"), str(count))
    set_sync_meta(user_key(user_id, f"{source}_last_error"), error or "")
    refresh_dashboard_cache(user_id)


def backfill_run_metrics(db, user_id: str = DEFAULT_USER_ID) -> int:
    """Phase 6.1 — recomputes tss/efficiency_factor for every existing Run using the
    user's *current* threshold_hr, not just newly-synced ones (strava.py/
    garmin_sync.py's _process_activity only sets these at sync time going forward).
    One-shot, container-run: `docker exec <container> python3 -c "from app.models
    import SessionLocal; from app.stats import backfill_run_metrics; db =
    SessionLocal(); print(backfill_run_metrics(db)); db.close()"`. Re-running later
    (e.g. once threshold_hr gets set, auto or manually) upgrades every past run
    from the fallback estimate to real hrTSS — safe to run repeatedly, since it
    always recomputes from source fields rather than incrementally adjusting."""
    config = db.get(UserTrainingConfig, user_id)
    threshold_hr = config.threshold_hr if config else None
    runs = db.query(Activity).filter(owned_by(Activity.user_id, user_id)).all()
    for r in runs:
        r.tss = compute_tss(r.moving_time_sec, r.avg_hr, threshold_hr, r.suggested_type, activity_family(r.activity_type))
        r.efficiency_factor = compute_efficiency_factor(r.avg_pace_sec_per_mi, r.avg_hr)
    db.commit()
    return len(runs)


# Ride is the only non-foot-strike distance activity type this app really sees
# (see CLAUDE.md — this is primarily a running app); everything else distance-
# based (Run, Walk, Hike, treadmill variants, ...) wears shoes. Strength/weight
# training isn't a shoe-or-bike activity at all — returns None so
# assign_default_gear skips it entirely (a real bug until this fix: any
# strength_training/WeightTraining row, Garmin- or Hevy-sourced, was silently
# falling into the "shoe" bucket and getting a running shoe auto-assigned).
def _gear_kind_for_activity(activity_type: str) -> str | None:
    if activity_type == "Ride":
        return "bike"
    if "strength" in (activity_type or "").lower() or "weight" in (activity_type or "").lower():
        return None
    return "shoe"


def assign_default_gear(db, run: Run, user_id: str = DEFAULT_USER_ID) -> None:
    """Phase 6.3 — called once per run at sync time (strava.py/garmin_sync.py's
    _process_activity), mirroring compute_tss's "only at sync time going forward"
    scope. Only ever fills in an unset gear_id — never overwrites a manual
    reassignment made via PATCH /api/runs/{id}. A no-op if the user has no
    default gear of the matching kind, which is the common case until they've
    actually set one up in Settings."""
    if run.gear_id:
        return
    kind = _gear_kind_for_activity(run.activity_type)
    if kind is None:
        return
    default = (
        db.query(Gear)
        .filter(owned_by(Gear.user_id, user_id), Gear.kind == kind, Gear.is_default.is_(True), Gear.retired_date.is_(None))
        .first()
    )
    if default:
        run.gear_id = default.id


def gear_summary(db, user_id: str = DEFAULT_USER_ID) -> list[dict]:
    """Read-time only (see Gear's own docstring for why) — sums Run.distance_mi per
    gear_id in one query rather than N+1'ing per gear item. bike_component rows
    don't get their own Run.gear_id assignments; they track their parent bike's
    ride mileage from their own start_date onward instead, so replacing a chain
    resets only that component's counter without touching the bike's."""
    gear_list = (
        db.query(Gear)
        .filter(owned_by(Gear.user_id, user_id))
        .order_by(Gear.kind, Gear.retired_date.is_(None).desc(), Gear.created_at)
        .all()
    )
    mileage_by_gear_id = dict(
        db.query(Run.gear_id, func.sum(Run.distance_mi))
        .filter(owned_by(Run.user_id, user_id), Run.gear_id.isnot(None))
        .group_by(Run.gear_id)
        .all()
    )

    def component_mileage(component: Gear) -> float:
        if not component.parent_gear_id:
            return 0.0
        q = db.query(func.sum(Run.distance_mi)).filter(
            owned_by(Run.user_id, user_id), Run.gear_id == component.parent_gear_id,
        )
        if component.start_date:
            q = q.filter(Run.date >= component.start_date)
        return q.scalar() or 0.0

    out = []
    for g in gear_list:
        miles = component_mileage(g) if g.kind == "bike_component" else (mileage_by_gear_id.get(g.id, 0.0) or 0.0)
        wear_pct = round(miles / g.replace_at_mi * 100, 1) if g.replace_at_mi else None
        out.append({
            "id": g.id, "name": g.name, "kind": g.kind, "parentGearId": g.parent_gear_id,
            "isDefault": g.is_default, "startDate": g.start_date, "retiredDate": g.retired_date,
            "replaceAtMi": g.replace_at_mi, "notes": g.notes, "createdAt": g.created_at,
            "totalMiles": round(miles, 1), "wearPct": wear_pct,
        })
    return out
