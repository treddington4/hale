"""Sync endpoints: quick "Sync Now" (background job w/ live status), full backlog sync
(same job shape, much longer-running), Garmin export import, sync_meta history, and
per-user provider credentials (Settings → Connections).

Per-source behavior (credential checks, quick sync, backlog sync, the scheduled
auto-sync loop) dispatches through app/sync/registry.py's INTEGRATIONS table instead
of an if/elif chain per source name -- see that module's own docstring for why, and
for why Garmin's backlog sync keeps its rate-limit retry loop here as a deliberate
special case rather than being folded into the generic path."""
import os
import time
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Depends

from ..models import SessionLocal, ProviderCredential, get_sync_meta, set_sync_meta, user_key
from ..accounts import auth
from ..sync import garmin_sync, hevy_sync
from ..sync.registry import INTEGRATIONS, SYNC_LIMIT
from .. import stats

router = APIRouter()

SYNC_INTERVAL_HOURS = int(os.environ.get("SYNC_INTERVAL_HOURS", "6"))


def _next_auto_sync_time():
    """Fire immediately on a genuinely fresh start (no recent sync on record), but
    don't re-trigger a real Strava sync on every container restart if one already ran
    recently — e.g. a burst of redeploys during active development previously fired a
    real auto-sync on every single one of them, since next_run_time=datetime.now() was
    unconditional. Only delays the *first* scheduled run; the recurring interval after
    that is unaffected. Checks DEFAULT_USER_ID's own last-synced-at specifically (a
    deliberate simplification, Phase 1.4) — this is a one-time scheduler-startup
    heuristic to avoid hammering Strava right after a redeploy, not per-user data, and
    _auto_sync() below already re-syncs every credentialed user on every tick regardless."""
    from ..models import DEFAULT_USER_ID
    from datetime import timedelta
    last = get_sync_meta(user_key(DEFAULT_USER_ID, "strava_last_synced_at"))
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            remaining = timedelta(hours=SYNC_INTERVAL_HOURS) - (datetime.now(timezone.utc) - last_dt)
            if remaining > timedelta(0):
                return datetime.now() + remaining
        except Exception:
            pass
    return datetime.now()


def _users_with_credential(provider: str):
    db = SessionLocal()
    try:
        return [c.user_id for c in db.query(ProviderCredential).filter_by(provider=provider).all()]
    finally:
        db.close()


def _auto_sync():
    """Runs every auto-sync-eligible integration (official/documented APIs — Strava,
    Hevy; Garmin is manual-only, see registry.py's own auto_sync_eligible flags) for
    every user with a credential on file, not just one hardcoded account — today
    that's still just DEFAULT_USER_ID in practice (no real login exists yet), but the
    loop is already correct for whenever there's more than one."""
    import logging
    log = logging.getLogger("runlog")
    for source, integration in INTEGRATIONS.items():
        if not integration.auto_sync_eligible:
            continue
        for user_id in _users_with_credential(source):
            set_sync_meta(user_key(user_id, f"{source}_last_error"), "")
            try:
                n = integration.sync_recent(user_id)
                stats.record_sync(source, user_id, count=n)
                log.info(f"Auto-sync: upserted {n} from {source} for {user_id}")
            except Exception as e:
                stats.record_sync(source, user_id, error=str(e))
                log.warning(f"Auto-sync skipped for {user_id}/{source}: {e}")


def _has_credential(user_id: str, provider: str) -> bool:
    integration = INTEGRATIONS.get(provider)
    return bool(integration and integration.has_credential(user_id))


def _reject_if_demo(user_id: str):
    """Blocks a handful of endpoints that are either meaningless for a demo account
    (fake credentials sync-mocking already prevents from ever being used) or a real
    parsing operation not worth exposing publicly — see Phase 11.3."""
    from ..accounts import demo
    db = SessionLocal()
    try:
        if demo.is_demo_user(db, user_id):
            raise HTTPException(403, "Not available in the demo")
    finally:
        db.close()


# ---------- Quick sync ("Sync Now" — runs as a background job with live status, same
# shape as backlog sync below, since a Garmin quick sync alone (login + wellness +
# adaptive plan + per-activity detail fetches) can genuinely take long enough that a
# blocking request with a static "Syncing…" label isn't good enough feedback) ----------
_QUICK_SYNC_LOG_LIMIT = 50
_quick_sync_lock = threading.Lock()
# Keyed by (user_id, source) rather than a source-only dict (Phase 1.4) — lazily
# created on first use via _get_quick_sync_job, since the set of real users isn't
# known ahead of time the way the fixed sources were.
_quick_sync_jobs: dict = {}


def _new_job_state() -> dict:
    return {"status": "idle", "count": 0, "log": [], "startedAt": None, "finishedAt": None, "error": None}


def _get_quick_sync_job(user_id: str, source: str) -> dict:
    return _quick_sync_jobs.setdefault((user_id, source), _new_job_state())


def _quick_sync_progress(user_id: str, source: str, msg: str, count: int = None):
    with _quick_sync_lock:
        job = _get_quick_sync_job(user_id, source)
        job["log"].append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
        job["log"] = job["log"][-_QUICK_SYNC_LOG_LIMIT:]
        if count is not None:
            job["count"] = count


def _run_quick_sync(user_id: str, source: str):
    try:
        cb = lambda msg, count=None: _quick_sync_progress(user_id, source, msg, count)
        n = INTEGRATIONS[source].sync_recent(user_id, cb)
        with _quick_sync_lock:
            job = _get_quick_sync_job(user_id, source)
            job["status"] = "done"
            job["count"] = n
            job["finishedAt"] = datetime.now(timezone.utc).isoformat()
        stats.record_sync(source, user_id, count=n)
        _quick_sync_progress(user_id, source, f"Done — {n} activities upserted")
    except Exception as e:
        msg = str(e)
        with _quick_sync_lock:
            job = _get_quick_sync_job(user_id, source)
            job["status"] = "error"
            job["error"] = msg
            job["finishedAt"] = datetime.now(timezone.utc).isoformat()
        # A rate-limit error mid-sync still carries real progress (see
        # GarminMidSyncRateLimitError.synced_count) — record it as a partial success,
        # not just an error, so "Last synced" doesn't misleadingly say "Never".
        stats.record_sync(source, user_id, count=getattr(e, "synced_count", None), error=msg)
        _quick_sync_progress(user_id, source, f"Error: {msg}")


@router.post("/api/sync/{source}")
def manual_sync(source: str, user_id: str = Depends(auth.current_user_id)):
    if source not in INTEGRATIONS:
        raise HTTPException(404, "Unknown source")

    from ..accounts import demo
    db = SessionLocal()
    try:
        is_demo = demo.is_demo_user(db, user_id)
    finally:
        db.close()
    if is_demo:
        # A demo user never has a real credential — skip straight past those checks
        # and fake a completed sync (no thread, no outbound call) rather than 400ing
        # with a confusing "not authenticated" error.
        with _quick_sync_lock:
            job = _get_quick_sync_job(user_id, source)
            now_iso = datetime.now(timezone.utc).isoformat()
            job.update({"status": "done", "count": 0,
                        "log": ["Demo mode — sync is simulated, no external calls made."],
                        "startedAt": now_iso, "finishedAt": now_iso, "error": None})
        return {"status": "started"}

    integration = INTEGRATIONS[source]
    if not integration.has_credential(user_id):
        raise HTTPException(400, integration.missing_credential_message)

    with _quick_sync_lock:
        job = _get_quick_sync_job(user_id, source)
        if job["status"] == "running":
            raise HTTPException(409, "Sync already running")
        job.update({"status": "running", "count": 0, "log": [], "startedAt": datetime.now(timezone.utc).isoformat(),
                    "finishedAt": None, "error": None})

    set_sync_meta(user_key(user_id, f"{source}_last_error"), "")  # clear any stale error the moment a new attempt starts, not just on success
    threading.Thread(target=_run_quick_sync, args=(user_id, source), daemon=True).start()
    return {"status": "started"}


@router.get("/api/sync/{source}/status")
def quick_sync_status(source: str, user_id: str = Depends(auth.current_user_id)):
    if source not in INTEGRATIONS:
        raise HTTPException(404, "Unknown source")
    with _quick_sync_lock:
        return dict(_get_quick_sync_job(user_id, source))


@router.post("/api/garmin/import")
async def import_garmin_export(file: UploadFile = File(...), user_id: str = Depends(auth.current_user_id)):
    """One-time bulk import from Garmin Connect's official data-export ZIP (requested via
    account.garmin.com, separate from the live unofficial-API sync above). Lets historical
    activities/steps come from a file instead of the rate-limited live API — the live
    sync's own dedup (run_needs_detail_sync) means anything this import already covers
    is automatically skipped on future syncs, so the live API only has to handle activities
    genuinely newer than the export. Safe to re-upload the same or an overlapping export."""
    _reject_if_demo(user_id)
    from ..sync import garmin_import
    import logging
    log = logging.getLogger("runlog")
    data = await file.read()
    log.info(f"garmin import: received {file.filename} ({len(data)} bytes)")
    summary = garmin_import.import_garmin_export(data, user_id)
    return summary


@router.get("/api/sync/meta")
def sync_meta(user_id: str = Depends(auth.current_user_id)):
    def info(source: str):
        count = get_sync_meta(user_key(user_id, f"{source}_last_count"))
        return {
            "lastSyncedAt": get_sync_meta(user_key(user_id, f"{source}_last_synced_at")),
            "lastCount": int(count) if count is not None else None,
            "lastError": get_sync_meta(user_key(user_id, f"{source}_last_error")) or None,
        }
    return {source: info(source) for source in INTEGRATIONS}


# ---------- Backlog sync (full history, runs as a background job) ----------
_BACKLOG_LOG_LIMIT = 200
_backlog_lock = threading.Lock()
# Keyed by (user_id, source) — see the quick-sync job dict above for why.
_backlog_jobs: dict = {}


def _get_backlog_job(user_id: str, source: str) -> dict:
    return _backlog_jobs.setdefault((user_id, source), _new_job_state())


def _backlog_progress(user_id: str, source: str, msg: str, count: int = None):
    with _backlog_lock:
        job = _get_backlog_job(user_id, source)
        job["log"].append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
        job["log"] = job["log"][-_BACKLOG_LOG_LIMIT:]
        if count is not None:
            job["count"] = count


def _run_backlog_sync(user_id: str, source: str):
    try:
        if source == "garmin":
            # Auto-continue through rate limits instead of stopping and requiring a
            # manual re-click: this thread is already backgrounded, so waiting out the
            # cooldown (garmin_sync's own exponential backoff, 5min base, capped at 4h)
            # and retrying in place is strictly better than surfacing an error the user
            # has to notice and act on. Only a genuinely non-rate-limit failure (bad
            # credentials, a real bug) still propagates immediately below. Deliberately
            # not dispatched through INTEGRATIONS[source].sync_all — see registry.py's
            # own docstring on why this stays a special case here.
            total = 0
            while True:
                base = total

                def _progress(msg, count=None, base=base):
                    _backlog_progress(user_id, source, msg, (base + count) if count is not None else None)

                try:
                    total += garmin_sync.sync_all_garmin_activities(user_id, progress_cb=_progress)
                    break
                except Exception as e:
                    if not garmin_sync.is_rate_limit_related(e):
                        raise
                    wait = garmin_sync._garmin_cooldown_remaining_sec(user_id) + 5
                    _backlog_progress(
                        user_id, source,
                        f"Rate-limited — auto-retrying in {wait / 60:.1f} min, no need to click again…",
                    )
                    time.sleep(wait)
            n = total
        else:
            n = INTEGRATIONS[source].sync_all(
                user_id, lambda msg, count=None: _backlog_progress(user_id, source, msg, count),
            )
        with _backlog_lock:
            job = _get_backlog_job(user_id, source)
            job["status"] = "done"
            job["count"] = n
            job["finishedAt"] = datetime.now(timezone.utc).isoformat()
        stats.record_sync(f"{source}_backlog", user_id, count=n)
        _backlog_progress(user_id, source, f"Done — {n} activities upserted")
    except Exception as e:
        msg = str(e)
        with _backlog_lock:
            job = _get_backlog_job(user_id, source)
            job["status"] = "error"
            job["error"] = msg
            job["finishedAt"] = datetime.now(timezone.utc).isoformat()
        # A rate-limit error mid-backlog still carries real progress (see
        # GarminMidSyncRateLimitError.synced_count) — record it as a partial success,
        # not just an error, so "Last synced" doesn't misleadingly say "Never" even
        # though real activities were committed before the failure.
        stats.record_sync(f"{source}_backlog", user_id, count=getattr(e, "synced_count", None), error=msg)
        _backlog_progress(user_id, source, f"Error: {msg}")


@router.post("/api/sync/{source}/backlog")
def start_backlog_sync(source: str, user_id: str = Depends(auth.current_user_id)):
    if source not in INTEGRATIONS:
        raise HTTPException(404, "Unknown source")

    from ..accounts import demo
    db = SessionLocal()
    try:
        is_demo = demo.is_demo_user(db, user_id)
    finally:
        db.close()
    if is_demo:
        with _backlog_lock:
            job = _get_backlog_job(user_id, source)
            now_iso = datetime.now(timezone.utc).isoformat()
            job.update({"status": "done", "count": 0,
                        "log": ["Demo mode — sync is simulated, no external calls made."],
                        "startedAt": now_iso, "finishedAt": now_iso, "error": None})
        return {"status": "started"}

    integration = INTEGRATIONS[source]
    if not integration.has_credential(user_id):
        raise HTTPException(400, integration.missing_credential_message)

    with _backlog_lock:
        job = _get_backlog_job(user_id, source)
        if job["status"] == "running":
            raise HTTPException(409, "Backlog sync already running")
        job.update({"status": "running", "count": 0, "log": [], "startedAt": datetime.now(timezone.utc).isoformat(),
                    "finishedAt": None, "error": None})

    # Clear the *persisted* last-error too, not just the in-memory job state above —
    # otherwise the old error stays visible in "last completed" for the whole duration
    # of this new run, since sync_meta only gets overwritten when _run_backlog_sync
    # finishes.
    set_sync_meta(user_key(user_id, f"{source}_backlog_last_error"), "")

    threading.Thread(target=_run_backlog_sync, args=(user_id, source), daemon=True).start()
    return {"status": "started"}


@router.get("/api/sync/{source}/backlog/status")
def backlog_status(source: str, user_id: str = Depends(auth.current_user_id)):
    if source not in INTEGRATIONS:
        raise HTTPException(404, "Unknown source")
    with _backlog_lock:
        job = dict(_get_backlog_job(user_id, source))

    last_count = get_sync_meta(user_key(user_id, f"{source}_backlog_last_count"))
    job["lastCompleted"] = {
        "syncedAt": get_sync_meta(user_key(user_id, f"{source}_backlog_last_synced_at")),
        "count": int(last_count) if last_count is not None else None,
        "error": get_sync_meta(user_key(user_id, f"{source}_backlog_last_error")) or None,
    }
    return job


# ---------- Garmin status/diagnostics ----------
@router.get("/api/garmin/status")
def garmin_status(user_id: str = Depends(auth.current_user_id)):
    return {"configured": _has_credential(user_id, "garmin")}


@router.get("/api/garmin/route-diagnostics")
def garmin_route_diagnostics(user_id: str = Depends(auth.current_user_id)):
    """How Garmin runs' routes were actually sourced — surfaces whether the FIT-derived
    unmasked path is really firing (vs. falling back to Garmin Connect's summary API,
    which independent testing found clips ~500m near a privacy-zone-protected location)
    without digging through container logs."""
    from ..models import Run, owned_by
    db = SessionLocal()
    try:
        rows = db.query(Run.route_source).filter(Run.source == "garmin", owned_by(Run.user_id, user_id)).all()
        counts = {"fit_record_stream": 0, "geopolyline_summary": 0, "none": 0, "unknown": 0}
        for (source,) in rows:
            counts[source if source in counts else "unknown"] += 1
        return counts
    finally:
        db.close()


# ---------- Hevy status ----------
@router.get("/api/hevy/status")
def hevy_status(user_id: str = Depends(auth.current_user_id)):
    return {"configured": _has_credential(user_id, "hevy")}


# ---------- Connections (per-user provider credentials — Settings tab) ----------
@router.get("/api/connections")
def get_connections(user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        creds = db.query(ProviderCredential).filter_by(user_id=user_id).all()
        return [{"provider": c.provider, "username": c.username} for c in creds]
    finally:
        db.close()


@router.post("/api/connections/garmin")
async def set_garmin_connection(request: Request, user_id: str = Depends(auth.current_user_id)):
    _reject_if_demo(user_id)
    body = await request.json()
    username, password = body.get("username"), body.get("password")
    if not username or not password:
        raise HTTPException(400, "username and password are required")
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="garmin").first()
        if not cred:
            cred = ProviderCredential(user_id=user_id, provider="garmin",
                                       created_at=datetime.now(timezone.utc).isoformat())
            db.add(cred)
        cred.username = username
        cred.password = password
        db.commit()
        return {"status": "saved"}
    finally:
        db.close()


@router.post("/api/connections/hevy")
async def set_hevy_connection(request: Request, user_id: str = Depends(auth.current_user_id)):
    _reject_if_demo(user_id)
    body = await request.json()
    api_key = (body.get("apiKey") or "").strip()
    if not api_key:
        raise HTTPException(400, "apiKey is required")
    try:
        hevy_sync.validate_api_key(api_key)
    except hevy_sync.HevyAuthError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Couldn't reach Hevy to validate this key: {e}")
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider="hevy").first()
        if not cred:
            cred = ProviderCredential(user_id=user_id, provider="hevy",
                                       created_at=datetime.now(timezone.utc).isoformat())
            db.add(cred)
        cred.access_token = api_key
        db.commit()
        return {"status": "saved"}
    finally:
        db.close()


@router.delete("/api/connections/{provider}")
def delete_connection(provider: str, user_id: str = Depends(auth.current_user_id)):
    _reject_if_demo(user_id)
    db = SessionLocal()
    try:
        cred = db.query(ProviderCredential).filter_by(user_id=user_id, provider=provider).first()
        if cred:
            db.delete(cred)
            db.commit()
        return {"deleted": True}
    finally:
        db.close()
