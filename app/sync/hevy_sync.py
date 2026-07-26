"""Hevy strength-workout sync (Phase 6.5) — Hevy's /v1 API is official and documented
(unlike Garmin's unofficial, rate-limit-sensitive one), so this is safe to run on the
same auto-sync schedule as Strava rather than manual-only like Garmin. Requires Hevy
Pro (the workouts API is a Pro-only feature) and a personal API key, entered in
Settings -> Connections and stored in the same ProviderCredential.access_token column
Strava's OAuth token uses (provider="hevy") -- no schema change needed.

Every synced workout becomes one Run row (source="hevy", activity_type="WeightTraining",
matching Strava's own strength-activity string so lib/runs.ts's activityFamily() substring
match ("strength" or "weight") picks it up identically to a Strava/Garmin strength
session) with its full set-by-set breakdown in exercise_sets_json -- the exact same
{exercise, setType, reps, weightLb, durationSec, supersetGroup} contract Garmin's own
_fetch_exercise_sets already populates (see garmin_sync.py, ExerciseSetsTable.tsx),
except Hevy actually has real setType (warmup/dropset/failure) and superset data, which
Garmin's own sync leaves null for every set.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ..models import SessionLocal, Run, ProviderCredential, get_sync_meta, user_key, owned_by
from ..util import user_timezone, compute_tss
from .. import stats
from ..stats import _normalize_activity_type

log = logging.getLogger("runlog")

HEVY_API_BASE = "https://api.hevyapp.com/v1"
KG_TO_LB = 2.20462
PAGE_SIZE = 10
_STRENGTH_TYPE_HINTS = ["full body", "upper body", "lower body", "push", "pull", "legs", "core"]


class HevyAuthError(Exception):
    """The stored/submitted API key was rejected (401/403) -- distinguished from a
    generic request failure so callers can surface a clear "check your API key"
    message instead of a raw HTTP error."""


def _field(d: dict, *names):
    """Hevy's own MCP-observed responses used camelCase field names on the plain
    workouts list but snake_case on the events (delta-sync) endpoint for what's
    presumably the same underlying data -- rather than assume one casing convention
    holds everywhere, every read goes through this so either shape parses correctly."""
    for name in names:
        v = d.get(name)
        if v is not None:
            return v
    return None


def _headers(api_key: str) -> dict:
    return {"api-key": api_key, "Accept": "application/json"}


def _request(api_key: str, path: str, params: dict = None) -> dict:
    resp = requests.get(f"{HEVY_API_BASE}{path}", headers=_headers(api_key), params=params, timeout=20)
    if resp.status_code in (401, 403):
        raise HevyAuthError("Hevy rejected this API key -- check Settings -> Connections (requires Hevy Pro)")
    resp.raise_for_status()
    return resp.json()


def validate_api_key(api_key: str) -> None:
    """Called from the Settings save endpoint so a bad key is caught immediately
    (raises HevyAuthError) rather than only surfacing on the next scheduled sync."""
    _request(api_key, "/workouts/count")


def _get_api_key(db, user_id: str):
    cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="hevy").first()
    return cred.access_token if cred else None


def _guess_strength_type(title: str) -> str:
    """Hevy workouts are free-text titled, not typed -- a light substring match
    against this app's existing STRENGTH_TYPES options (web/src/lib/runs.ts) so a
    title like "Full Body" or "Upper body" lands on the matching option instead of
    an unrecognized raw string; anything else falls back to "Other", always
    editable afterward, same as classify_run_type's own heuristic elsewhere."""
    t = (title or "").lower()
    for hint in _STRENGTH_TYPE_HINTS:
        if hint in t:
            return hint.title()
    return "Other"


def _to_local(iso_utc: str, tz_name: str) -> datetime:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz_name))


def _exercise_sets_from_workout(workout: dict) -> list:
    out = []
    for ex in workout.get("exercises", []):
        exercise_name = _field(ex, "title", "name") or "Unknown exercise"
        # Hevy's exercise_template_id is stable across the user's Hevy display
        # language (unlike the free-text title) -- captured so a later Garmin
        # push (Phase 23) can do an exact hevy2garmin template-id lookup instead
        # of falling back to English-name matching.
        template_id = _field(ex, "exercise_template_id", "exerciseTemplateId")
        superset_raw = _field(ex, "superset_id", "supersetsId", "supersetId")
        superset_group = str(superset_raw) if superset_raw is not None else None
        for s in ex.get("sets", []):
            set_type = s.get("type")
            weight_kg = _field(s, "weight_kg", "weight")
            duration_sec = _field(s, "duration_seconds", "duration")
            out.append({
                "exercise": exercise_name,
                "exerciseTemplateId": template_id,
                "setType": set_type if set_type and set_type != "normal" else None,
                "reps": s.get("reps"),
                "weightLb": round(weight_kg * KG_TO_LB, 1) if weight_kg else None,
                "durationSec": round(duration_sec) if duration_sec else None,
                "supersetGroup": superset_group,
            })
    return out


def _upsert_workout(db, workout: dict, user_id: str, tz_name: str) -> None:
    start_iso = _field(workout, "start_time", "startTime")
    end_iso = _field(workout, "end_time", "endTime")
    start_local = _to_local(start_iso, tz_name)
    end_local = _to_local(end_iso, tz_name) if end_iso else start_local
    moving_time_sec = max(0, round((end_local - start_local).total_seconds()))

    run_id = f"hevy_{workout['id']}"
    run = db.get(Run, run_id) or Run(id=run_id)
    run.user_id = user_id
    run.source = "hevy"
    run.activity_type = _normalize_activity_type("WeightTraining")
    run.date = start_local.strftime("%Y-%m-%d")
    run.start_time = start_local.strftime("%H:%M")
    run.name = workout.get("title") or "Hevy Workout"
    run.moving_time_sec = moving_time_sec
    run.suggested_type = _guess_strength_type(workout.get("title"))
    run.notes = workout.get("description") or None
    run.exercise_sets_json = json.dumps(_exercise_sets_from_workout(workout))

    # Same call as Strava/Garmin's own sync paths -- a no-op here (no avg_hr and
    # no FALLBACK_INTENSITY_FACTOR bucket for these suggested_type values), but
    # computed uniformly rather than special-cased away, so this doesn't silently
    # miss out if a future change makes it produce a real value.
    run.tss = compute_tss(run.moving_time_sec, run.avg_hr, None, run.suggested_type)
    stats.assign_default_gear(db, run, user_id)

    db.merge(run)


def _delete_workout(db, workout_id: str, user_id: str) -> None:
    run = db.query(Run).filter(Run.id == f"hevy_{workout_id}", owned_by(Run.user_id, user_id)).first()
    if run:
        db.delete(run)


def sync_hevy_workouts(user_id: str, progress_cb=None) -> int:
    """Incremental sync via /workouts/events?since=<last sync> -- mirrors this
    codebase's general "only fetch what's new" discipline (Strava's after= param,
    Garmin's detail_synced_at dedup) instead of re-fetching the full workout list
    every time. Falls back to epoch (i.e. everything) on a genuinely first sync.
    Returns 0 (not an error) when no API key is on file, matching how a
    credential-less Strava/Garmin sync is simply skipped by the auto-sync loop."""
    db = SessionLocal()
    try:
        api_key = _get_api_key(db, user_id)
        if not api_key:
            return 0
        tz_name = user_timezone(user_id)
        since = get_sync_meta(user_key(user_id, "hevy_last_synced_at")) or "1970-01-01T00:00:00Z"
        count = 0
        page = 1
        while True:
            # Real response is an envelope, {"page", "page_count", "events": [...]}
            # -- NOT a bare list. Confirmed directly against the live API after a
            # real sync crashed ('str' object has no attribute 'get', i.e. iterating
            # the envelope dict's own string keys instead of its "events" array);
            # the Hevy MCP server used to research this shape had apparently
            # unwrapped the envelope before presenting results, hiding this.
            response = _request(api_key, "/workouts/events", {"since": since, "page": page, "pageSize": PAGE_SIZE})
            events = response.get("events", [])
            if not events:
                break
            for event in events:
                if event.get("type") == "deleted":
                    deleted_id = _field(event, "id", "workout_id", "workoutId")
                    if deleted_id:
                        _delete_workout(db, deleted_id, user_id)
                else:
                    workout = event.get("workout")
                    if workout:
                        _upsert_workout(db, workout, user_id, tz_name)
                        count += 1
                        if progress_cb:
                            progress_cb(f"Synced {workout.get('title') or 'workout'} ({(workout.get('start_time') or workout.get('startTime') or '')[:10]})", count)
            db.commit()
            if page >= response.get("page_count", 1):
                break
            page += 1
        return count
    finally:
        db.close()


def sync_all_hevy_workouts(user_id: str, progress_cb=None) -> int:
    """Full-history backlog sync -- paginates the plain /workouts list (not the
    events endpoint), so it isn't bounded by "since", matching Strava's
    sync_all_activities / Garmin's sync_all_garmin_activities counterparts."""
    db = SessionLocal()
    try:
        api_key = _get_api_key(db, user_id)
        if not api_key:
            return 0
        tz_name = user_timezone(user_id)
        count = 0
        page = 1
        while True:
            # Same envelope shape as the events endpoint -- {"page", "page_count",
            # "workouts": [...]}, see sync_hevy_workouts's comment above.
            response = _request(api_key, "/workouts", {"page": page, "pageSize": PAGE_SIZE})
            workouts = response.get("workouts", [])
            if not workouts:
                break
            for workout in workouts:
                _upsert_workout(db, workout, user_id, tz_name)
                count += 1
                if progress_cb:
                    progress_cb(f"Synced {workout.get('title') or 'workout'} ({(workout.get('start_time') or workout.get('startTime') or '')[:10]})", count)
            db.commit()
            if page >= response.get("page_count", 1):
                break
            page += 1
        return count
    finally:
        db.close()
