"""Strava OAuth + activity sync. Uses Strava's official REST API directly — no LLM involved."""
import os
import time
import json
import bisect
import requests
from datetime import datetime, timezone

from ..models import SessionLocal, Run, ProviderCredential, UserTrainingConfig, run_needs_detail_sync, resolve_run_id
from .weather import get_historical_weather
from ..util import gap_sec_per_mi, classify_run_type, detect_intervals, decode_polyline, compute_tss, compute_efficiency_factor

# STRAVA_CLIENT_ID/SECRET are the OAuth *application's* credentials (registered once per
# self-hosted deployment at strava.com) — infrastructure config, not a per-user secret.
# What's per-user is the resulting access/refresh token after each user completes their
# own /auth/strava/login, stored in ProviderCredential keyed by (user_id, "strava").
CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("STRAVA_REDIRECT_URI", "http://localhost:8000/auth/strava/callback")

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

METERS_PER_MILE = 1609.34


def get_authorize_url() -> str:
    params = (
        f"client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
        f"&response_type=code&approval_prompt=auto"
        f"&scope=activity:read_all"
    )
    return f"{AUTH_URL}?{params}"


def exchange_code_for_token(user_id: str, code: str):
    resp = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _store_token(user_id, data)
    return data


def _store_token(user_id: str, data):
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="strava").first()
        if not cred:
            cred = ProviderCredential(user_id=user_id, provider="strava",
                                       created_at=datetime.now(timezone.utc).isoformat())
            db.add(cred)
        cred.access_token = data["access_token"]
        cred.refresh_token = data["refresh_token"]
        cred.expires_at = data["expires_at"]
        db.commit()
    finally:
        db.close()


def get_valid_access_token(user_id: str):
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="strava").first()
        if not cred or not cred.access_token:
            return None
        if cred.expires_at - 60 > int(time.time()):
            return cred.access_token
        # refresh
        resp = requests.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _store_token(user_id, data)
        return data["access_token"]
    finally:
        db.close()


def _resample_mile_splits(streams: dict):
    """Turn raw time-series streams into true per-mile splits (distance-resampled)."""
    dist = streams.get("distance", {}).get("data", [])
    time_s = streams.get("time", {}).get("data", [])
    hr = streams.get("heartrate", {}).get("data", [])
    cad = streams.get("cadence", {}).get("data", [])
    alt = streams.get("altitude", {}).get("data", [])

    if not dist or not time_s:
        return []

    splits = []
    next_mark = METERS_PER_MILE
    seg_start_t = time_s[0]
    seg_start_alt = alt[0] if alt else 0
    hr_buf, cad_buf = [], []
    mile_num = 1

    for i, d in enumerate(dist):
        if hr and i < len(hr):
            hr_buf.append(hr[i])
        if cad and i < len(cad):
            cad_buf.append(cad[i])

        if d >= next_mark:
            elapsed = time_s[i] - seg_start_t
            elev_gain_m = max(0, (alt[i] if alt else 0) - seg_start_alt)
            splits.append({
                "mile": mile_num,
                "paceSecPerMi": elapsed,
                "elevGainFt": round(elev_gain_m * 3.28084, 1),
                "avgHR": round(sum(hr_buf) / len(hr_buf)) if hr_buf else None,
                "maxHR": max(hr_buf) if hr_buf else None,
                # Strava reports run cadence per-foot; double for true steps/min
                "avgCadence": round((sum(cad_buf) / len(cad_buf)) * 2, 1) if cad_buf else None,
            })
            mile_num += 1
            next_mark += METERS_PER_MILE
            seg_start_t = time_s[i]
            seg_start_alt = alt[i] if alt else 0
            hr_buf, cad_buf = [], []

    # trailing partial mile
    if dist and dist[-1] > (next_mark - METERS_PER_MILE) + 100:  # only if meaningfully more than a few meters
        partial_mi = (dist[-1] - (next_mark - METERS_PER_MILE)) / METERS_PER_MILE
        elapsed = time_s[-1] - seg_start_t
        if partial_mi > 0.05 and elapsed > 0:
            splits.append({
                "mile": round(mile_num - 1 + partial_mi, 2),
                "paceSecPerMi": round(elapsed / partial_mi),
                "elevGainFt": round(max(0, (alt[-1] if alt else 0) - seg_start_alt) * 3.28084, 1),
                "avgHR": round(sum(hr_buf) / len(hr_buf)) if hr_buf else None,
                "maxHR": max(hr_buf) if hr_buf else None,
                "avgCadence": round((sum(cad_buf) / len(cad_buf)) * 2, 1) if cad_buf else None,
            })
    return splits


def _build_route_metrics(streams: dict, max_points: int = 300):
    """Decimate raw streams into a geo-tagged points series for the pace/HR/cadence/grade
    heatmaps (as opposed to _resample_mile_splits, which is geo-agnostic and mile-granular)."""
    latlng = streams.get("latlng", {}).get("data", [])
    hr = streams.get("heartrate", {}).get("data", [])
    cad = streams.get("cadence", {}).get("data", [])
    vel = streams.get("velocity_smooth", {}).get("data", [])
    dist = streams.get("distance", {}).get("data", [])
    alt = streams.get("altitude", {}).get("data", [])

    if not latlng:
        return []

    n = len(latlng)
    step = max(1, n // max_points)
    points = []
    prev_dist, prev_alt = None, None
    for i in range(0, n, step):
        lat, lon = latlng[i]
        speed = vel[i] if i < len(vel) else None
        pace = round(METERS_PER_MILE / speed, 1) if speed and speed > 0.3 else None

        # Grade between this sample and the previous decimated one, using the real
        # distance stream (not a lat/lon approximation) for horizontal distance. Skip
        # over short/near-zero horizontal deltas (e.g. paused at a light) — grade there
        # is just noise, not a meaningful slope.
        grade = None
        if i < len(dist) and i < len(alt):
            if prev_dist is not None:
                d_delta = dist[i] - prev_dist
                if d_delta > 2:
                    grade = round((alt[i] - prev_alt) / d_delta * 100, 1)
            prev_dist, prev_alt = dist[i], alt[i]

        points.append({
            "lat": lat,
            "lon": lon,
            "paceSecPerMi": pace,
            "hr": hr[i] if i < len(hr) else None,
            # Strava reports run cadence per-foot; double for true steps/min
            "cadence": round(cad[i] * 2) if i < len(cad) and cad[i] else None,
            "gradePct": grade,
        })
    return points


def _compute_recovery_times(streams: dict, intervals: list, drop_bpm: int = 20):
    """For each work -> recovery transition, seconds from the work rep's peak HR until
    HR drops by `drop_bpm` within the following recovery rep, using the same per-second
    HR+time stream already fetched for splits — no extra API calls. A relative drop (not
    a fixed target HR) scales to the individual and to the specific effort, rather than
    favoring low- or high-HR athletes; 20bpm is a standard, achievable-within-a-typical-
    recovery-rep threshold used in interval coaching. `recoverySec: None` means the
    recovery rep ended before reaching that drop — a real result (recovery took longer
    than the rest given), not a missing data point.

    Lap boundaries come from cumulative elapsed time, not Strava's lap start_index/
    end_index fields — those were tried first but came back as 0 for every lap on every
    real activity checked, so they're not reliably populated by Strava's API in practice."""
    hr = streams.get("heartrate", {}).get("data", [])
    time_s = streams.get("time", {}).get("data", [])
    if not hr or not time_s:
        return []

    cursor = 0.0
    windows = []
    for iv in intervals:
        dur = iv.get("elapsedTimeSec") or iv.get("durationSec") or 0
        windows.append((cursor, cursor + dur))
        cursor += dur

    def slice_indices(t0, t1):
        return bisect.bisect_left(time_s, t0), bisect.bisect_left(time_s, t1)

    results = []
    for i in range(len(intervals) - 1):
        work, rec = intervals[i], intervals[i + 1]
        if work.get("segment") != "work" or rec.get("segment") != "recovery":
            continue
        w0, w1 = slice_indices(*windows[i])
        r0, r1 = slice_indices(*windows[i + 1])
        if w1 <= w0 or r1 <= r0:
            continue

        peak = max(hr[w0:w1], default=None)
        if peak is None:
            continue
        target = peak - drop_bpm

        recovery_sec = None
        rec_start_t = time_s[r0]
        for j in range(r0, r1):
            if hr[j] <= target:
                recovery_sec = time_s[j] - rec_start_t
                break

        results.append({"repIndex": len(results) + 1, "peakHR": peak, "recoverySec": recovery_sec})
    return results


def _process_activity(act: dict, headers: dict, db, user_id: str) -> bool:
    """Fetch streams/laps/weather for one Strava activity of any type and upsert it as a Run
    row. Always returns True — every activity type is captured (not just Run), so the user
    has a complete copy of their own data; only Run activities get the running-specific
    classification/interval-detection heuristics, since those are calibrated for running
    paces and patterns and would produce nonsense for a ride or swim."""
    activity_type = act.get("type", "Run")
    is_run = activity_type == "Run"

    run_id = resolve_run_id(db, "strava", act['id'], user_id)
    # Strava's "trainer" flag just means "recorded on stationary/indoor equipment" —
    # true for an indoor bike trainer, and (per Hevy's Strava export) even weight
    # training. "Treadmill" is specifically a running concept, so only running
    # activities should ever get labeled with it — everything else stays False
    # rather than showing a nonsensical "Treadmill" badge on a weight session.
    is_treadmill = is_run and bool(act.get("trainer"))

    streams_resp = requests.get(
        f"{API_BASE}/activities/{act['id']}/streams",
        headers=headers,
        params={"keys": "time,distance,heartrate,cadence,altitude,latlng,velocity_smooth", "key_by_type": "true"},
        timeout=20,
    )
    streams = streams_resp.json() if streams_resp.ok else {}

    splits = _resample_mile_splits(streams)
    route_metrics = _build_route_metrics(streams)

    distance_mi = act["distance"] / METERS_PER_MILE
    moving_time = act["moving_time"]
    avg_pace = moving_time / distance_mi if distance_mi else None
    avg_cadence = (act.get("average_cadence") or 0) * 2 or None
    elev_gain_ft = (act.get("total_elevation_gain") or 0) * 3.28084

    run_type = classify_run_type(distance_mi, avg_pace, splits, act.get("average_heartrate")) if is_run else activity_type

    intervals_json = "[]"
    recovery_json = "[]"
    if is_run and run_type == "Interval":
        laps_resp = requests.get(f"{API_BASE}/activities/{act['id']}/laps", headers=headers, timeout=20)
        if laps_resp.ok:
            raw_laps = [{
                "durationSec": l["moving_time"],
                "distanceMi": round(l["distance"] / METERS_PER_MILE, 4),
                "paceSecPerMi": round(l["moving_time"] / (l["distance"] / METERS_PER_MILE), 1) if l["distance"] else None,
                "elevGainFt": round((l.get("total_elevation_gain") or 0) * 3.28084, 1),
                "avgHR": round(l["average_heartrate"]) if l.get("average_heartrate") else None,
                "maxHR": l.get("max_heartrate"),
                "avgCadence": round((l.get("average_cadence") or 0) * 2, 1) if l.get("average_cadence") else None,
                "elapsedTimeSec": l.get("elapsed_time") or l.get("moving_time"),
            } for l in laps_resp.json()]
            intervals = detect_intervals(raw_laps)
            intervals_json = json.dumps(intervals)
            recovery_json = json.dumps(_compute_recovery_times(streams, intervals))

    temp_f, condition, heat_index_f, wet_bulb_f = None, None, None, None
    if not is_treadmill:
        latlng = act.get("start_latlng") or []
        start_dt = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        if len(latlng) == 2:
            temp_f, condition, heat_index_f, wet_bulb_f = get_historical_weather(
                latlng[0], latlng[1], start_dt.strftime("%Y-%m-%d"), start_dt.hour
            )

    start_dt = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))

    existing = db.get(Run, run_id)
    run = existing or Run(id=run_id)
    run.user_id = user_id
    run.source = "strava"
    run.activity_type = activity_type
    run.date = start_dt.strftime("%Y-%m-%d")
    run.start_time = start_dt.strftime("%H:%M")
    run.name = act.get("name", "Run")
    run.distance_mi = round(distance_mi, 3)
    run.moving_time_sec = moving_time
    run.elev_gain_ft = round(elev_gain_ft, 1)
    run.avg_hr = round(act["average_heartrate"]) if act.get("average_heartrate") else None
    run.max_hr = act.get("max_heartrate")
    run.avg_cadence = avg_cadence
    run.avg_pace_sec_per_mi = round(avg_pace, 1) if avg_pace else None
    run.is_treadmill = is_treadmill
    run.temp_f = temp_f
    run.weather_condition = condition
    run.heat_index_f = heat_index_f
    run.wet_bulb_f = wet_bulb_f
    run.suggested_type = run_type
    run.splits_json = json.dumps(splits)
    run.intervals_json = intervals_json
    run.recovery_json = recovery_json
    run.route_json = json.dumps(decode_polyline((act.get("map") or {}).get("summary_polyline")))
    run.route_metrics_json = json.dumps(route_metrics)
    run.detail_synced_at = datetime.now(timezone.utc).isoformat()

    # Phase 6.1 — computed once at sync time, same discipline as GAP/run-type above.
    training_config = db.get(UserTrainingConfig, user_id)
    threshold_hr = training_config.threshold_hr if training_config else None
    run.tss = compute_tss(run.moving_time_sec, run.avg_hr, threshold_hr, run.suggested_type)
    run.efficiency_factor = compute_efficiency_factor(run.avg_pace_sec_per_mi, run.avg_hr)

    db.merge(run)
    return True


def sync_activities(user_id: str, limit: int = 10, progress_cb=None):
    """Main sync entrypoint — pulls the most recent `limit` activities for this user.
    Returns number of runs upserted."""
    token = get_valid_access_token(user_id)
    if not token:
        raise RuntimeError("Not authenticated with Strava — visit /auth/strava/login first")

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{API_BASE}/athlete/activities", headers=headers,
                         params={"per_page": limit}, timeout=20)
    resp.raise_for_status()
    activities = resp.json()

    db = SessionLocal()
    count = 0
    try:
        for act in activities:
            # Activities are newest-first, so the first already-synced one means
            # everything after it is too — stop here instead of re-fetching full
            # details (splits/route/weather) for activities we already have. This is
            # what makes "quick sync" naturally mean "today's/recent new data only".
            if not run_needs_detail_sync(db, resolve_run_id(db, "strava", act['id'], user_id)):
                break
            if _process_activity(act, headers, db, user_id):
                count += 1
                if progress_cb:
                    progress_cb(f"Synced {act.get('name', 'run')}", count)
        db.commit()
    finally:
        db.close()

    return count


def sync_all_activities(user_id: str, progress_cb=None):
    """Backlog sync — paginates through this user's entire activity history.
    Commits incrementally so progress survives if the run is interrupted."""
    token = get_valid_access_token(user_id)
    if not token:
        raise RuntimeError("Not authenticated with Strava — visit /auth/strava/login first")

    headers = {"Authorization": f"Bearer {token}"}
    per_page = 100
    page = 1

    db = SessionLocal()
    count = 0
    try:
        while True:
            resp = requests.get(f"{API_BASE}/athlete/activities", headers=headers,
                                 params={"per_page": per_page, "page": page}, timeout=20)
            resp.raise_for_status()
            activities = resp.json()
            if not activities:
                break
            if progress_cb:
                progress_cb(f"Fetched page {page} ({len(activities)} activities)")

            skipped = 0
            for act in activities:
                # Backlog sync still walks full history (confirming nothing's missing
                # via this cheap paginated list call) but skips the expensive detail
                # fetch entirely for anything already stored — no API cost for a skip.
                if not run_needs_detail_sync(db, resolve_run_id(db, "strava", act['id'], user_id)):
                    skipped += 1
                    continue
                if _process_activity(act, headers, db, user_id):
                    count += 1
                    db.commit()
                    if progress_cb:
                        progress_cb(f"Synced {act.get('name', 'run')} ({(act.get('start_date_local') or '')[:10]})", count)

            if progress_cb and skipped:
                progress_cb(f"Skipped {skipped} already-synced activities (page {page})")

            if len(activities) < per_page:
                break
            page += 1
        db.commit()
    finally:
        db.close()

    return count
