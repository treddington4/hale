"""P17 — Race-Day Pack: deterministic pacing/taper/forecast for a race goal.
No LLM, no magic — all computations are transparent and reproducible.
Handles activity-specific pacing (min/mi for running, mph for cycling)."""
from datetime import datetime, timedelta


def compute_pacing_plan(target_time_sec: int, distance_mi: float, activity_type: str) -> dict:
    """Compute an even-split pacing strategy for the race.

    Returns: {
        "strategy": "Even split",
        "splits": [
            {"distance": 5, "unit": "km", "targetTime": "23:45", "targetPace": "4:45/km"},
            ...
        ],
        "explanation": "Maintain consistent effort throughout..."
    }
    """
    if not target_time_sec or distance_mi <= 0:
        return {"strategy": None, "splits": [], "explanation": "No valid target time set"}

    # Determine units based on activity type
    is_cycling = activity_type and activity_type.lower() in ("ride", "cycling")

    if is_cycling:
        # Cycling uses mph; compute average speed
        distance_km = distance_mi * 1.60934
        time_hours = target_time_sec / 3600
        avg_speed_mph = distance_mi / time_hours if time_hours > 0 else 0

        return {
            "strategy": "Even pace",
            "avgSpeedMph": round(avg_speed_mph, 2),
            "totalTime": _format_duration(target_time_sec),
            "explanation": "Maintain consistent power output and sustainable pace throughout the event. Monitor heart rate zones and effort level.",
        }
    else:
        # Running/walking uses min/mi
        avg_pace_sec_per_mi = target_time_sec / distance_mi if distance_mi > 0 else 0
        avg_pace_str = _format_pace(avg_pace_sec_per_mi)

        # Build splits for standard race distances
        splits = _build_run_splits(distance_mi, avg_pace_sec_per_mi)

        return {
            "strategy": "Even split",
            "distance": round(distance_mi, 2),
            "unit": "miles",
            "avgPace": avg_pace_str,
            "totalTime": _format_duration(target_time_sec),
            "splits": splits,
            "explanation": "Run even splits to maintain consistent effort. Adjust for course profile and conditions on race day.",
        }


def _build_run_splits(total_distance: float, avg_pace_sec_per_mi: float) -> list:
    """Build pacing splits for a running race at standard intervals."""
    # Use standard split intervals based on distance
    if total_distance <= 5:
        interval_mi = 1
    elif total_distance <= 10:
        interval_mi = 2
    elif total_distance <= 26:
        interval_mi = 5
    else:
        interval_mi = 10

    splits = []
    for i in range(int(total_distance / interval_mi)):
        dist = (i + 1) * interval_mi
        if dist > total_distance:
            break
        elapsed_sec = dist * avg_pace_sec_per_mi
        splits.append({
            "distance": dist,
            "unit": "miles",
            "targetTime": _format_duration(elapsed_sec),
            "targetPace": _format_pace(avg_pace_sec_per_mi),
        })

    # Add final split if needed
    if total_distance % interval_mi > 0:
        dist = total_distance
        elapsed_sec = dist * avg_pace_sec_per_mi
        splits.append({
            "distance": round(dist, 2),
            "unit": "miles",
            "targetTime": _format_duration(elapsed_sec),
            "targetPace": _format_pace(avg_pace_sec_per_mi),
        })

    return splits


def compute_taper(race_date: str, today: str = None) -> dict:
    """Compute taper countdown (display-only; does not alter generator.py prescriptions).

    Returns: {
        "daysUntil": 7,
        "status": "peak_week",
        "recommendation": "Peak week: maintain volume but reduce intensity..."
    }
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    try:
        race_dt = datetime.strptime(race_date, "%Y-%m-%d")
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        days_until = (race_dt - today_dt).days
    except Exception:
        return {"daysUntil": None, "status": "unknown", "recommendation": "Invalid date"}

    if days_until < 0:
        return {"daysUntil": days_until, "status": "race_past", "recommendation": "Race day has passed"}
    elif days_until == 0:
        return {"daysUntil": 0, "status": "race_day", "recommendation": "Race day! Trust your training."}
    elif days_until <= 7:
        return {
            "daysUntil": days_until,
            "status": "peak_week",
            "recommendation": "Final peak week: maintain volume but reduce intensity. Focus on rest and preparation.",
        }
    elif days_until <= 14:
        return {
            "daysUntil": days_until,
            "status": "taper",
            "recommendation": "Taper week: reduce volume by 20-30%, keep intensity strides sharp. Focus on recovery.",
        }
    elif days_until <= 21:
        return {
            "daysUntil": days_until,
            "status": "peak",
            "recommendation": "Peak training weeks: this is race simulation time. Volume up, intensity sharp.",
        }
    else:
        return {
            "daysUntil": days_until,
            "status": "build",
            "recommendation": f"{days_until} days to race. Build phase: progressive volume and intensity work.",
        }


def _format_duration(seconds: int) -> str:
    """Format seconds as HH:MM:SS or MM:SS."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_pace(sec_per_mi: float) -> str:
    """Format seconds-per-mile as MM:SS/mi."""
    minutes = int(sec_per_mi) // 60
    seconds = int(sec_per_mi) % 60
    return f"{minutes}:{seconds:02d}/mi"
