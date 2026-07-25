"""Push Hevy strength-workout data onto Garmin (Phase 23).

Reuses the `hevy2garmin` OSS project (MIT license, github.com/drkostas/hevy2garmin)
for FIT-file generation and its exercise_template_id -> Garmin FIT category
mapping (400+ exercises), rather than hand-rolling FIT encoding and a mapping
table ourselves -- keeps that mapping "someone else's problem to maintain"
(a plain `pip install -U hevy2garmin` picks up improvements) instead of a copy we'd
hand-update. Only its data/logic modules are imported (`fit`, `garmin`) -- never
its own `auth`/`db`/`server` modules; we authenticate with our own existing
`garmin_sync._login(user_id)` session and run no separate service.

Why not just PUT Hevy's sets onto the existing Garmin activity? Confirmed live
(hevy2garmin issue #159): Garmin's `exerciseSets` endpoint returns success (204)
on a *watch-recorded* activity but silently drops the exercise names, rendering
every exercise as "Unknown" in the Garmin Connect app despite the write
succeeding. Named exercises only render correctly on activities Garmin considers
"uploaded" (FIT manufacturer=DEVELOPMENT), never watch-recorded ones. So instead:

  - Match found (a Garmin strength_training Run already overlaps this Hevy
    workout in time -- checked via our own already-synced Run rows, no extra
    Garmin API call needed to find a candidate): "replace, but human-gated."
    preview_push() uploads a brand-new, correctly-named duplicate activity; the
    original watch-recorded one is untouched at this step. Only confirm_push()
    (an explicit second user action, after they've visually checked the
    duplicate in Garmin Connect themselves) deletes the original. discard_preview()
    removes the duplicate instead, leaving the original untouched -- either way
    nothing real is ever lost without the user's explicit say-so.
  - No match (nothing recorded on Garmin for this workout at all): nothing to
    replace or lose, so create_new() just uploads it directly, no confirm step --
    HR is not fused in (there's no watch recording to pull it from), an accepted
    tradeoff since the exercise/weight/rep data is the actual point.
"""
import os
import tempfile
from datetime import datetime, timedelta

from hevy2garmin import fit as h2g_fit
from hevy2garmin import garmin as h2g_garmin

from ..models import SessionLocal, Run, owned_by
from . import garmin_sync, hevy_sync

MATCH_WINDOW_MIN = 30


def _hevy_workout_id(run_id: str) -> str:
    # Run.id is f"hevy_{workout_id}" (models.resolve_run_id / hevy_sync._upsert_workout).
    return run_id.split("hevy_", 1)[1]


def _garmin_activity_id(run_id: str) -> int:
    # Run.id is f"garmin_{activity_id}" in the common case (models.resolve_run_id).
    return int(run_id.rsplit("_", 1)[-1])


def find_matching_garmin_run(db, hevy_run: Run, user_id: str) -> Run | None:
    """Local DB-only match -- both sources are already synced into our own Run
    table, so no live Garmin API call is needed just to find a candidate (unlike
    hevy2garmin's own matcher.py, which re-fetches Garmin's activity list live)."""
    hevy_start = datetime.strptime(f"{hevy_run.date} {hevy_run.start_time}", "%Y-%m-%d %H:%M")
    window_dates = {
        (hevy_start.date() + timedelta(days=d)).strftime("%Y-%m-%d") for d in (-1, 0, 1)
    }
    candidates = (
        db.query(Run)
        .filter(
            Run.source == "garmin",
            Run.activity_type == "strength_training",
            Run.date.in_(window_dates),
            owned_by(Run.user_id, user_id),
        )
        .all()
    )
    best, best_diff = None, None
    for cand in candidates:
        cand_start = datetime.strptime(f"{cand.date} {cand.start_time}", "%Y-%m-%d %H:%M")
        diff = abs((cand_start - hevy_start).total_seconds())
        if diff <= MATCH_WINDOW_MIN * 60 and (best_diff is None or diff < best_diff):
            best, best_diff = cand, diff
    return best


def _fetch_raw_hevy_workout(user_id: str, hevy_run_id: str) -> dict:
    """Re-fetches the raw workout from Hevy's API rather than reconstructing it
    from our own flattened `exercise_sets_json` -- hevy2garmin's `generate_fit`
    expects Hevy's exact native shape (`exercises[].title`/`.exercise_template_id`,
    `sets[].type`/`.weight_kg`/`.duration_seconds`), and this is a rare,
    user-initiated action rather than a bulk sync loop, so the extra call is cheap
    and avoids a whole class of subtle reconstruction bugs."""
    db = SessionLocal()
    try:
        api_key = hevy_sync._get_api_key(db, user_id)
    finally:
        db.close()
    if not api_key:
        raise RuntimeError("No Hevy API key on file")
    return hevy_sync._request(api_key, f"/workouts/{_hevy_workout_id(hevy_run_id)}")


def _build_and_upload(client, workout: dict, exclude_activity_ids=None, progress_cb=None) -> dict:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    _p(f"Generating FIT file for \"{workout.get('title') or 'Workout'}\"…")
    with tempfile.TemporaryDirectory() as tmp:
        fit_path = os.path.join(tmp, "workout.fit")
        h2g_fit.generate_fit(workout, None, fit_path)  # hr_samples=None -- see module docstring
        _p("Uploading to Garmin Connect…")
        result = h2g_garmin.upload_fit(
            client, fit_path,
            workout_start=workout.get("start_time"),
            exclude_activity_ids=exclude_activity_ids,
        )
    if result.get("activity_id"):
        _p(f"Uploaded as Garmin activity {result['activity_id']}")
        try:
            h2g_garmin.rename_activity(client, result["activity_id"], workout.get("title") or "Hevy Workout")
            _p("Renamed activity to match the Hevy workout title")
        except Exception as e:
            _p(f"Upload succeeded but rename failed (cosmetic only): {e}")
    else:
        _p("Uploaded, but couldn't confirm the new activity's id yet (Garmin indexing lag) — check Garmin Connect directly")
    return result


def _garmin_link(activity_id) -> str | None:
    return f"https://connect.garmin.com/modern/activity/{activity_id}" if activity_id else None


def preview_push(user_id: str, hevy_run_id: str, progress_cb=None) -> dict:
    """Match exists: upload a new, correctly-named duplicate activity. The
    original watch-recorded activity is untouched at this step -- the caller
    must show the returned link to the user and only call confirm_push() after
    they've verified it themselves in Garmin Connect."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    db = SessionLocal()
    try:
        hevy_run = db.get(Run, hevy_run_id)
        if not hevy_run or hevy_run.source != "hevy":
            raise ValueError("Not a Hevy run")
        _p("Looking for a matching Garmin activity…")
        match = find_matching_garmin_run(db, hevy_run, user_id)
    finally:
        db.close()
    if not match:
        raise ValueError("No matching Garmin activity found -- use create_new instead")

    original_activity_id = _garmin_activity_id(match.id)
    _p(f"Matched Garmin activity {original_activity_id} — fetching the Hevy workout…")
    workout = _fetch_raw_hevy_workout(user_id, hevy_run_id)
    _p("Logging into Garmin…")
    client = garmin_sync._login(user_id)
    # Excludes the original from the post-upload "find by start time" lookup --
    # both activities share nearly the same start time, so without this the
    # lookup could mistake the pre-existing original for the just-created upload.
    result = _build_and_upload(client, workout, exclude_activity_ids=[original_activity_id], progress_cb=progress_cb)
    _p("Preview ready — review it in Garmin Connect before confirming")
    return {
        "previewActivityId": result.get("activity_id"),
        "originalActivityId": original_activity_id,
        "garminLink": _garmin_link(result.get("activity_id")),
    }


def confirm_push(user_id: str, original_activity_id: int, progress_cb=None) -> None:
    """User has visually verified the preview duplicate looks correct in Garmin
    Connect -- deletes the original watch-recorded activity, keeping only the
    correctly-named duplicate."""
    if progress_cb:
        progress_cb(f"Deleting original Garmin activity {original_activity_id}…")
    client = garmin_sync._login(user_id)
    h2g_garmin.delete_activity(client, original_activity_id)
    if progress_cb:
        progress_cb("Done — original replaced with the correctly-named upload")


def discard_preview(user_id: str, preview_activity_id: int, progress_cb=None) -> None:
    """Preview didn't look right -- removes the duplicate; the original stays
    untouched, so nothing is lost and the user can retry."""
    if progress_cb:
        progress_cb(f"Discarding preview activity {preview_activity_id}…")
    client = garmin_sync._login(user_id)
    h2g_garmin.delete_activity(client, preview_activity_id)
    if progress_cb:
        progress_cb("Done — preview discarded, original untouched")


def create_new(user_id: str, hevy_run_id: str, progress_cb=None) -> dict:
    """No matching Garmin activity at all for this workout -- nothing to replace
    or lose, so upload it directly as a single action. No HR is fused in (there's
    no watch recording to pull it from for this workout) -- an accepted tradeoff,
    confirmed with the user, since the exercise/weight/rep data is the point."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    db = SessionLocal()
    try:
        hevy_run = db.get(Run, hevy_run_id)
        if not hevy_run or hevy_run.source != "hevy":
            raise ValueError("Not a Hevy run")
        _p("Confirming no Garmin activity already exists for this workout…")
        match = find_matching_garmin_run(db, hevy_run, user_id)
    finally:
        db.close()
    if match:
        raise ValueError("A matching Garmin activity exists -- use preview_push instead")

    workout = _fetch_raw_hevy_workout(user_id, hevy_run_id)
    _p("Logging into Garmin…")
    client = garmin_sync._login(user_id)
    result = _build_and_upload(client, workout, progress_cb=progress_cb)
    _p("Done — created on Garmin (no watch HR data to fuse in for this workout)")
    return {
        "activityId": result.get("activity_id"),
        "garminLink": _garmin_link(result.get("activity_id")),
    }
