"""Shared calculation helpers."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Global fallback only (Phase 12.2 made this per-user via User.timezone, browser-
# detected — see local_today() below) — still the right default for a user who's
# never opened the app since that column was added, or any background job with no
# real user_id in scope.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/New_York")


def local_today(user_id: str = None):
    """The user's local calendar date — never the container's own (UTC) clock.
    Docker containers default to UTC; once UTC has rolled past midnight but the
    user's local calendar day hasn't yet (any evening/night west of UTC), a naive
    datetime.now().date() silently runs a day ahead of the user's actual "today" —
    every "today"/"days ago" calculation in this app (stats.py's weekly/monthly
    summaries, goal countdowns, the Coach's notion of "today") must go through this
    instead. See GitHub issue #2.

    Phase 12.2: real date confusion in production chat logs traced partly to
    APP_TIMEZONE being a single global env var rather than tied to where the user
    actually is. Now looks up that user's browser-detected User.timezone (see
    routes/settings.py's PATCH /api/config) when a user_id is given, falling back to
    APP_TIMEZONE if they have none set yet (pre-upgrade accounts) or none was passed
    (background/global-scope callers)."""
    tz_name = APP_TIMEZONE
    if user_id is not None:
        from .models import SessionLocal, User
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            if user and user.timezone:
                tz_name = user.timezone
        finally:
            db.close()
    return datetime.now(ZoneInfo(tz_name)).date()


def decode_polyline(encoded: str):
    """Decode a Google-encoded polyline string (as returned by Strava's summary_polyline)
    into a list of [lat, lon] pairs."""
    if not encoded:
        return []
    points = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append([round(lat / 1e5, 6), round(lng / 1e5, 6)])
    return points


def minetti_cost(i: float) -> float:
    """Minetti et al. cost-of-running-on-gradient (J/kg/m), i = fractional grade."""
    i2, i3, i4, i5 = i * i, i ** 3, i ** 4, i ** 5
    return 155.4 * i5 - 30.4 * i4 - 43.3 * i3 + 46.3 * i2 + 19.5 * i + 3.6


def gap_sec_per_mi(pace_sec_per_mi, elev_gain_ft, distance_mi):
    """Grade-Adjusted Pace: flat-equivalent pace given elevation gain over a distance."""
    if not pace_sec_per_mi or elev_gain_ft is None or not distance_mi:
        return None
    grade = max(-0.30, min(0.30, (elev_gain_ft / 5280) / distance_mi))
    factor = minetti_cost(grade) / minetti_cost(0)
    return pace_sec_per_mi / factor


def classify_run_type(distance_mi, avg_pace_sec_per_mi, splits, avg_hr=None):
    """
    Lightweight heuristic classifier — no LLM needed.
    Looks at pace variability across splits and distance to guess a run type.
    Always editable by the user afterward; this is just a starting suggestion.
    """
    if not splits or len(splits) < 2:
        if distance_mi and distance_mi >= 9:
            return "Long Run"
        return "Easy"

    paces = [s["paceSecPerMi"] for s in splits if s.get("paceSecPerMi")]
    if len(paces) < 2:
        return "Easy"

    mean_pace = sum(paces) / len(paces)
    variance = sum((p - mean_pace) ** 2 for p in paces) / len(paces)
    stdev = variance ** 0.5
    cv = stdev / mean_pace if mean_pace else 0  # coefficient of variation

    if cv > 0.18:
        return "Interval"
    if distance_mi and distance_mi >= 9:
        return "Long Run"
    if avg_hr and avg_hr < 135:
        return "Recovery"
    if mean_pace and mean_pace < 480:  # faster than 8:00/mi average
        return "Tempo"
    return "Easy"


# Phase 6.1 — fixed intensity-factor-squared-style estimate per classify_run_type
# bucket, used only when a real threshold_hr isn't available (see compute_tss).
# Same duration * IF^2 * 100 shape as real hrTSS, just with a documented fixed IF
# per bucket instead of one computed from actual HR-vs-threshold data.
FALLBACK_INTENSITY_FACTOR = {
    "Recovery": 0.60, "Easy": 0.70, "Tempo": 0.85, "Interval": 0.95, "Long Run": 0.75,
}


def compute_tss(moving_time_sec, avg_hr=None, threshold_hr=None, suggested_type=None) -> float | None:
    """hrTSS (`duration_hr * (avg_hr/threshold_hr)^2 * 100`, the standard
    TrainingPeaks-style formula) when both a real avg_hr and threshold_hr exist;
    otherwise falls back to the same duration*(IF^2)*100 shape using a fixed
    intensity factor per classify_run_type bucket (FALLBACK_INTENSITY_FACTOR) — a
    documented v1 approximation, not a full grade-adjusted-pace TSS model."""
    if not moving_time_sec:
        return None
    duration_hr = moving_time_sec / 3600
    if avg_hr and threshold_hr:
        return round(duration_hr * (avg_hr / threshold_hr) ** 2 * 100, 1)
    intensity_factor = FALLBACK_INTENSITY_FACTOR.get(suggested_type)
    if intensity_factor is None:
        return None
    return round(duration_hr * (intensity_factor ** 2) * 100, 1)


def compute_efficiency_factor(avg_pace_sec_per_mi, avg_hr) -> float | None:
    """Aerobic efficiency: speed (mph) per heartbeat — a classic "same effort,
    faster over time" fitness-trend metric, independent of any threshold value
    (unlike compute_tss) so it's available for every run with pace + HR regardless
    of whether threshold_hr has ever been set."""
    if not avg_pace_sec_per_mi or not avg_hr:
        return None
    speed_mph = 3600 / avg_pace_sec_per_mi
    return round(speed_mph / avg_hr, 4)


def detect_intervals(laps):
    """
    Given raw Strava/Garmin laps (list of dicts with duration_sec, distance_mi, pace_sec_per_mi, ...),
    label each as warmup / work / recovery / cooldown based on relative pace and position.
    Heuristic: laps with pace >1.3x faster than the run's median pace are 'work';
    short laps immediately following work laps are 'recovery'; first/last long laps are warmup/cooldown.
    """
    if not laps or len(laps) < 3:
        return []

    paces = [l["paceSecPerMi"] for l in laps if l.get("paceSecPerMi")]
    if not paces:
        return []
    median_pace = sorted(paces)[len(paces) // 2]

    labeled = []
    for idx, lap in enumerate(laps):
        pace = lap.get("paceSecPerMi", median_pace)
        is_fast = pace < median_pace * 0.85
        is_short = lap.get("durationSec", 0) < 90

        if is_fast and is_short:
            segment = "work"
        elif idx == 0:
            segment = "warmup"
        elif idx == len(laps) - 1:
            segment = "cooldown"
        else:
            segment = "recovery"

        labeled.append({**lap, "segment": segment})
    return labeled
