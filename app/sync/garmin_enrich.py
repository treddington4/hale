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
succeeding. That limitation is specific to that live REST endpoint, though --
confirmed live, exercise names render correctly on a fresh FIT *upload* even
with the original watch's own real manufacturer left untouched (no need to
spoof FileId.manufacturer=DEVELOPMENT the way hevy2garmin's own from-scratch
uploads do). So instead:

  - Match found (a Garmin strength_training Run already overlaps this Hevy
    workout in time -- checked via our own already-synced Run rows, no extra
    Garmin API call needed to find a candidate): the ORIGINAL watch activity's
    real FIT bytes are backed up to disk, then Hevy's exercise_title/set
    records are spliced in (see fit_binary.py) -- every other field survives,
    including ones neither fitparse nor fit_tool have names for (Training
    Effect, Respiration Rate, Recovery HR, Est. Sweat Loss all confirmed
    correct in the Garmin Connect app after upload, not just byte-identical
    locally). Garmin's own duplicate-detection rejects uploading this as a
    second activity while the original still exists (confirmed live: 409
    "Duplicate Activity") -- so replace_with_enriched() deletes the original
    FIRST, then uploads the spliced file; if the upload fails for any reason
    after the delete, it automatically re-uploads the backup so the workout is
    never left with nothing on Garmin. revert_to_backup() is a manual undo if
    the user doesn't like the result after seeing it live: deletes the new
    upload and restores the backup.
  - No match (nothing recorded on Garmin at all): nothing to replace or lose,
    so create_new() just uploads a fresh FIT built purely from Hevy data (no
    original to splice into) -- no HR fused in, an accepted tradeoff since the
    exercise/weight/rep data is the actual point. This from-scratch path still
    uses hevy2garmin's own manufacturer=DEVELOPMENT convention since there's no
    real original to preserve identity from anyway.

Why splice raw bytes instead of parsing the original into objects and rebuilding
it via fit_tool's FitFileBuilder? Tried that first -- it corrupts real watch
recordings. A real Garmin FIT contains thousands of vendor-specific messages
(GenericMessage/TimestampCorrelationMessage) that fit_tool's bundled profile
doesn't fully understand; feeding all of them back through
FitFileBuilder(auto_define=True) produced a file that "wrote successfully" but
was unparseable afterward ("invalid local message type"). The byte-splice
approach never asks fit_tool to re-encode any of the original's real content --
it only builds the new (simple, from-scratch, already-proven-reliable)
exercise_title/set records via hevy2garmin's own `fit.generate_fit()`, then
inserts their raw bytes into the untouched original and fixes up the two
values that describe the whole file (the header's data-size field and the
trailing CRC) -- see fit_binary.py's own docstring for why this only requires
understanding record *boundaries*, not full field semantics.
"""
import os
import struct
import tempfile
from datetime import datetime, timedelta

from fit_tool.utils.crc import crc16
from hevy2garmin import fit as h2g_fit
from hevy2garmin import garmin as h2g_garmin

from ..models import SessionLocal, Run, owned_by, DB_PATH
from . import garmin_sync, hevy_sync
from . import fit_binary

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


def _splice_exercise_data(original_fit_bytes: bytes, workout: dict, tmp_dir: str) -> bytes:
    """Returns a new FIT byte string: the original watch recording, untouched
    except for (a) FileId.serial_number patched to a placeholder -- needed to
    dodge Garmin's duplicate-activity rejection, confirmed live: manufacturer
    does NOT need to change (exercise names render correctly on upload even
    with the original watch's real manufacturer intact -- the name-dropping
    behavior is specific to the live exerciseSets PUT endpoint, not FIT
    uploads) -- and (b) Hevy's exercise_title/set records inserted right after
    FileId, replacing the original's own (unreliable) exercise data rather
    than sitting alongside it. Every other byte, including fields neither
    fitparse nor fit_tool have names for (Recovery HR, Est. Sweat Loss, Body
    Battery, etc.), survives unchanged and renders correctly in the Garmin
    Connect app -- confirmed live, not just byte-identical locally -- because
    those bytes are never parsed into an object and re-encoded, just carried
    through verbatim.

    See fit_binary.py's own module docstring for why this operates at the raw
    byte level instead of through fit_tool's object model."""
    hevy_fit_path = os.path.join(tmp_dir, "hevy_isolated.fit")
    h2g_fit.generate_fit(workout, None, hevy_fit_path)
    with open(hevy_fit_path, "rb") as f:
        hevy_bytes = f.read()

    orig_hdr = fit_binary.parse_header(original_fit_bytes)
    orig_records = fit_binary.walk_records(original_fit_bytes, orig_hdr["data_start"], orig_hdr["data_end"])
    hevy_hdr = fit_binary.parse_header(hevy_bytes)
    hevy_records = fit_binary.walk_records(hevy_bytes, hevy_hdr["data_start"], hevy_hdr["data_end"])

    ex_span = fit_binary.group_byte_span(hevy_records, fit_binary.EXERCISE_TITLE_MESG_NUM)
    set_span = fit_binary.group_byte_span(hevy_records, fit_binary.SET_MESG_NUM)
    if not ex_span or not set_span:
        raise RuntimeError("hevy2garmin's generated FIT had no exercise_title/set records to splice")
    extracted = hevy_bytes[ex_span[0]:ex_span[1]] + hevy_bytes[set_span[0]:set_span[1]]

    file_id_recs = [r for r in orig_records if r["global_mesg_num"] == fit_binary.FILE_ID_MESG_NUM]
    file_id_def = next(r for r in file_id_recs if r["is_definition"])
    file_id_data = next(r for r in file_id_recs if not r["is_definition"])

    mutable = bytearray(original_fit_bytes)

    def _patch_field(field_num: int, value: int):
        field_info = fit_binary.find_field_offset(original_fit_bytes, file_id_def, file_id_data, field_num)
        if not field_info:
            return
        offset, size, architecture = field_info
        endian = ">" if architecture == 1 else "<"
        fmt = endian + {1: "B", 2: "H", 4: "I"}.get(size, "I")
        struct.pack_into(fmt, mutable, offset, value)

    # Garmin's upload endpoint rejects the spliced file as a "Duplicate
    # Activity" (409) if it still carries the original watch's real
    # serial_number alongside the original's real start_time/session content
    # -- matches hevy2garmin's own generate_fit() convention of always using
    # this same fixed placeholder. Manufacturer is deliberately left alone
    # (see this function's own docstring).
    _patch_field(fit_binary.FILE_ID_SERIAL_NUMBER_FIELD_NUM, fit_binary.PLACEHOLDER_SERIAL_NUMBER)

    # Rebuild the data section record-by-record rather than as one slice point,
    # DROPPING the original's own pre-existing exercise_title/set records (a
    # watch's own unreliable auto-detection -- e.g. this activity's entire
    # 67-minute session as a single "1 set, 4 reps" blob) instead of leaving
    # them in the file alongside Hevy's real ones. Confirmed live: skipping this
    # filter double-counts (Total Reps/Sets included both the original's and
    # Hevy's data, and "Work Time" summed the original's one giant bogus
    # "active" set duration together with Hevy's real per-set durations).
    insertion_point = file_id_data["offset"] + file_id_data["length"]
    excluded_mesg_nums = {fit_binary.EXERCISE_TITLE_MESG_NUM, fit_binary.SET_MESG_NUM}
    parts = [bytes(mutable[orig_hdr["data_start"]:insertion_point]), extracted]
    for rec in orig_records:
        if rec["offset"] < insertion_point:
            continue  # already covered by the initial slice up to insertion_point
        if rec["global_mesg_num"] in excluded_mesg_nums:
            continue
        parts.append(bytes(mutable[rec["offset"]:rec["offset"] + rec["length"]]))
    new_data_section = b"".join(parts)

    new_header = bytearray(mutable[:orig_hdr["data_start"]])
    struct.pack_into("<I", new_header, 4, len(new_data_section))
    if orig_hdr["header_size"] == 14:
        struct.pack_into("<H", new_header, 12, crc16(bytes(new_header[:12]), crc=0))

    body = bytes(new_header) + new_data_section
    return body + struct.pack("<H", crc16(body, crc=0))


def _upload_fit_bytes(client, fit_bytes: bytes, workout: dict, exclude_activity_ids=None, progress_cb=None) -> dict:
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    _p("Uploading to Garmin Connect…")
    with tempfile.TemporaryDirectory() as tmp:
        fit_path = os.path.join(tmp, "workout.fit")
        with open(fit_path, "wb") as f:
            f.write(fit_bytes)
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


def _build_and_upload(client, workout: dict, exclude_activity_ids=None, progress_cb=None) -> dict:
    """No-original-to-splice-into path (create_new): builds a fresh FIT purely
    from Hevy data. No HR fused in -- there's no watch recording to pull it
    from for this workout."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    _p(f"Generating FIT file for \"{workout.get('title') or 'Workout'}\"…")
    with tempfile.TemporaryDirectory() as tmp:
        fit_path = os.path.join(tmp, "workout.fit")
        h2g_fit.generate_fit(workout, None, fit_path)
        with open(fit_path, "rb") as f:
            fit_bytes = f.read()
    return _upload_fit_bytes(client, fit_bytes, workout, exclude_activity_ids, progress_cb)


def _garmin_link(activity_id) -> str | None:
    return f"https://connect.garmin.com/modern/activity/{activity_id}" if activity_id else None


def _backup_dir() -> str:
    d = os.path.join(os.path.dirname(DB_PATH), "garmin_enrich_backups")
    os.makedirs(d, exist_ok=True)
    return d


def _backup_path(activity_id: int) -> str:
    return os.path.join(_backup_dir(), f"{activity_id}.fit")


def backup_original_fit(activity_id: int, fit_bytes: bytes) -> str:
    path = _backup_path(activity_id)
    with open(path, "wb") as f:
        f.write(fit_bytes)
    return path


def _relink_garmin_run(db, original_activity_id: int, new_activity_id: int, hevy_run: Run) -> bool:
    """Updates our own Run table to reflect a just-completed replacement
    WITHOUT a live re-sync -- we already pushed the enriched data ourselves,
    so there's nothing left to learn from Garmin's rate-limited API here.
    Copies the pre-replace row's own already-correct fields (HR, timing,
    splits, weather -- none of that changed; the splice only touched exercise
    data) onto a new row keyed by the new activity id, taking
    exercise_sets_json/name from the Hevy row's own already-correct values
    instead of Garmin's unreliable auto-detection. Returns False (a no-op,
    not an error) if the pre-replace row is already gone for some reason."""
    old_run = db.get(Run, f"garmin_{original_activity_id}")
    if not old_run:
        return False
    new_run = Run(id=f"garmin_{new_activity_id}")
    for column in Run.__table__.columns:
        if column.name == "id":
            continue
        setattr(new_run, column.name, getattr(old_run, column.name))
    new_run.exercise_sets_json = hevy_run.exercise_sets_json
    new_run.name = hevy_run.name
    db.delete(old_run)
    db.add(new_run)
    return True


def replace_with_enriched(user_id: str, hevy_run_id: str, progress_cb=None) -> dict:
    """Match exists: backs up the original watch activity's real FIT bytes,
    then deletes it and uploads a spliced version (original data + Hevy's
    exercise structure) in its place. Garmin's own duplicate-detection rejects
    the spliced upload while the original still exists (confirmed live: 409
    "Duplicate Activity"), so the delete has to happen before the upload --
    unlike a pure preview/confirm flow, there's no way to let the user see the
    result before anything changes on Garmin. The backup exists specifically
    for this: if the upload fails after the delete, it's used to automatically
    restore the original rather than leaving the workout with nothing on
    Garmin. revert_to_backup() covers the other case -- upload succeeded, but
    the user doesn't like the result after seeing it live."""
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
    _p("Downloading the original activity's real FIT recording…")
    original_fit_bytes = garmin_sync._download_fit_bytes(client, original_activity_id)
    if not original_fit_bytes:
        raise RuntimeError("Could not download the original activity's FIT file")

    with tempfile.TemporaryDirectory() as tmp:
        _p("Splicing exercise data into the original recording (HR/Training Effect/etc. preserved)…")
        spliced_bytes = _splice_exercise_data(original_fit_bytes, workout, tmp)

    backup_path = backup_original_fit(original_activity_id, original_fit_bytes)
    _p(f"Backed up the original to {backup_path}")

    _p(f"Deleting original Garmin activity {original_activity_id}…")
    h2g_garmin.delete_activity(client, original_activity_id)

    try:
        result = _upload_fit_bytes(client, spliced_bytes, workout, progress_cb=progress_cb)
    except Exception as e:
        _p(f"Upload failed after deleting the original ({e}) — restoring from backup…")
        restore_result = _upload_fit_bytes(client, original_fit_bytes, workout, progress_cb=progress_cb)
        _p(f"Restored original as activity {restore_result.get('activity_id')}")
        raise RuntimeError(
            f"Enriched upload failed, but the original was restored as activity "
            f"{restore_result.get('activity_id')}: {e}"
        ) from e

    new_activity_id = result.get("activity_id")
    if new_activity_id:
        db = SessionLocal()
        try:
            hevy_run = db.get(Run, hevy_run_id)
            if _relink_garmin_run(db, original_activity_id, new_activity_id, hevy_run):
                db.commit()
                _p("Updated our own records for the new activity (no re-sync needed)")
        finally:
            db.close()

    _p("Done — original replaced with the enriched upload")
    return {
        "newActivityId": new_activity_id,
        "originalActivityId": original_activity_id,
        "backupPath": backup_path,
        "garminLink": _garmin_link(new_activity_id),
    }


def revert_to_backup(user_id: str, original_activity_id: int, new_activity_id: int, progress_cb=None) -> dict:
    """Manual undo after replace_with_enriched(): the user saw the new upload
    live and didn't like it. Deletes the new upload and re-uploads the backup
    saved at replace time."""
    def _p(msg):
        if progress_cb:
            progress_cb(msg)
    path = _backup_path(original_activity_id)
    if not os.path.exists(path):
        raise RuntimeError(f"No backup found for activity {original_activity_id}")
    with open(path, "rb") as f:
        backup_bytes = f.read()

    client = garmin_sync._login(user_id)
    _p(f"Deleting the enriched upload (activity {new_activity_id})…")
    h2g_garmin.delete_activity(client, new_activity_id)
    _p("Restoring the original from backup…")
    result = _upload_fit_bytes(client, backup_bytes, {"title": "Strength Training"}, progress_cb=progress_cb)
    _p(f"Done — original restored as activity {result.get('activity_id')}")
    return {"restoredActivityId": result.get("activity_id"), "garminLink": _garmin_link(result.get("activity_id"))}


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
        raise ValueError("A matching Garmin activity exists -- use replace_with_enriched instead")

    workout = _fetch_raw_hevy_workout(user_id, hevy_run_id)
    _p("Logging into Garmin…")
    client = garmin_sync._login(user_id)
    result = _build_and_upload(client, workout, progress_cb=progress_cb)
    _p("Done — created on Garmin (no watch HR data to fuse in for this workout)")
    return {
        "activityId": result.get("activity_id"),
        "garminLink": _garmin_link(result.get("activity_id")),
    }


def process_pending_enrichments(user_id: str, progress_cb=None) -> int:
    """Automates replace_with_enriched() for every not-yet-processed Hevy
    strength workout that already has a matching Garmin activity -- called
    after both the scheduled auto-sync tick and a manual Hevy "Sync Now"
    (routes/sync.py), not just from a UI button. Deliberately does NOT fall
    back to create_new() when no match is found: Hevy syncs are typically
    a few minutes ahead of the watch's own upload, so "no match yet" usually
    just means the Garmin side hasn't arrived -- creating a fresh activity
    now would risk a real duplicate once the actual watch recording shows up.
    Left pending, it's picked up automatically on the next pass instead.

    Garmin has no auto-sync schedule of its own (manual-only, rate-limit-
    sensitive -- see registry.py) so the *original* watch-recorded activity a
    Hevy workout should match may not be in our own Run table yet even though
    it already exists on Garmin's servers. A single extra
    `garmin_sync.sync_garmin_activities` call is made -- once per call to this
    function, not once per pending workout -- to give a same-day upload a
    chance to appear before this pass gives up on finding it; the
    already-established rate-limit cooldown/backoff in garmin_sync makes this
    safe to call opportunistically like this. This is the only live Garmin
    sync in this whole function -- once a replacement is actually uploaded,
    there's nothing left to learn from Garmin's API that we didn't just push
    ourselves, so `replace_with_enriched()`/the id-resolution pass below both
    update our own Run table directly instead of re-syncing to "discover" it.

    Manual revert stays exactly as before (routes/sync.py's /revert endpoint,
    garmin_enrich.revert_to_backup) -- automating the push doesn't touch that.
    """
    import logging
    log = logging.getLogger("runlog")

    def _p(msg):
        if progress_cb:
            progress_cb(msg)
        log.info(f"garmin_enrich auto: {msg}")

    db = SessionLocal()
    try:
        pending_ids = [
            r.id for r in db.query(Run).filter(
                Run.source == "hevy",
                Run.garmin_enriched_activity_id.is_(None),
                owned_by(Run.user_id, user_id),
            ).all()
        ]
    finally:
        db.close()
    if not pending_ids:
        return 0

    _p(f"Checking {len(pending_ids)} not-yet-enriched Hevy workout(s) for a Garmin match…")
    synced_garmin_this_pass = False
    enriched_count = 0
    unresolved = []  # [(hevy_run_id, original_activity_id)] -- replace succeeded but upload_fit couldn't confirm the new activity id yet

    for hevy_run_id in pending_ids:
        db = SessionLocal()
        try:
            hevy_run = db.get(Run, hevy_run_id)
            match = find_matching_garmin_run(db, hevy_run, user_id) if hevy_run else None
        finally:
            db.close()

        if not match and not synced_garmin_this_pass:
            synced_garmin_this_pass = True
            try:
                _p("No Garmin match yet — syncing Garmin to check for a same-day upload…")
                garmin_sync.sync_garmin_activities(user_id)
            except Exception as e:
                _p(f"Garmin sync attempt failed (will retry next pass): {e}")
            db = SessionLocal()
            try:
                hevy_run = db.get(Run, hevy_run_id)
                match = find_matching_garmin_run(db, hevy_run, user_id) if hevy_run else None
            finally:
                db.close()

        if not match:
            continue  # still nothing to match -- leave pending for the next pass

        try:
            result = replace_with_enriched(user_id, hevy_run_id, progress_cb=progress_cb)
        except Exception as e:
            _p(f"Auto-enrich failed for {hevy_run_id}: {e}")
            continue

        # replace_with_enriched() already updated the Garmin-side row itself
        # (renamed garmin_{old} -> garmin_{new}, exercise_sets_json taken from
        # Hevy's own already-correct data) when it could confirm the new
        # activity id -- no live re-sync needed to "learn" data we just pushed
        # ourselves. Only this Hevy row's own done-marker is this function's
        # job. Left unset (still NULL) when the id couldn't be confirmed yet --
        # resolved in the follow-up pass below via a single targeted lookup,
        # not a full generic sync.
        if result.get("newActivityId"):
            db = SessionLocal()
            try:
                hevy_run = db.get(Run, hevy_run_id)
                hevy_run.garmin_enriched_activity_id = result["newActivityId"]
                db.commit()
            finally:
                db.close()
        else:
            unresolved.append((hevy_run_id, result["originalActivityId"]))
        enriched_count += 1

    if unresolved:
        _p(f"Resolving {len(unresolved)} activity id(s) Garmin's upload response didn't confirm…")
        client = garmin_sync._login(user_id)
        for hevy_run_id, original_activity_id in unresolved:
            workout = _fetch_raw_hevy_workout(user_id, hevy_run_id)
            # A single targeted lookup by the workout's own start time -- not a
            # full generic sync -- matching the same "find by start time, never
            # just 'most recent'" discipline hevy2garmin's own upload_fit()
            # already uses right after uploading.
            new_activity_id = h2g_garmin.find_activity_by_start_time(
                client, workout.get("start_time"), exclude_activity_ids=[original_activity_id],
            )
            if not new_activity_id:
                continue  # still not indexed -- leave pending, retry next pass
            db = SessionLocal()
            try:
                hevy_run = db.get(Run, hevy_run_id)
                if _relink_garmin_run(db, original_activity_id, new_activity_id, hevy_run):
                    hevy_run.garmin_enriched_activity_id = new_activity_id
                    db.commit()
                    _p(f"Resolved replacement activity id for {hevy_run_id}: {new_activity_id}")
            finally:
                db.close()

    return enriched_count
