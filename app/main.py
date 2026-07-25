import os
import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from .models import init_db
from .routes import auth, sync, settings, wellness, chat, health, workouts, goals, dashboard, gear

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("runlog")
log.setLevel(LOG_LEVEL)

app = FastAPI(title="HALE")
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(auth.router)
app.include_router(sync.router)
app.include_router(settings.router)
app.include_router(wellness.router)
app.include_router(chat.router)
app.include_router(health.router)
app.include_router(workouts.router)
app.include_router(goals.router)
app.include_router(dashboard.router)
app.include_router(gear.router)


@app.get("/health")
@app.get("/healthz")
def healthz():
    """Container/orchestrator liveness check — deliberately no auth dependency and no
    DB round-trip (a transient DB hiccup shouldn't look like a dead container and
    trigger a restart loop). Exists because the app previously had no real health
    signal: '/' only 200s via the SPA catch-all below, which only gets registered
    when web-dist was actually built, so any host whose health check hits a
    different path (or hits during a broken frontend build) saw a 404 with no way
    to tell startup succeeded. /health is SnapDeploy's documented convention
    (docs/troubleshooting: "Add a /health endpoint that returns 200 OK"); /healthz
    kept as an alias since it's the more common convention elsewhere."""
    return {"status": "ok"}


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Force browsers to revalidate (not blindly reuse a heuristically-cached copy) on
    every request — StaticFiles already sends ETag/Last-Modified, so revalidation is a
    cheap 304 when nothing changed, but without this header a plain reload after a
    deploy can silently keep serving pre-deploy JS/CSS. Applies to everything (low-
    traffic personal LAN app, no real caching win to give up)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.on_event("startup")
def startup():
    init_db()
    scheduler = BackgroundScheduler()
    from .accounts import demo
    if demo.is_enabled():
        # Demo users never have real Strava/Garmin credentials — auto-sync would
        # just iterate over zero credentialed users every cycle. Expiry cleanup is
        # lazy (see demo.create_demo_session's opportunistic sweep), not a periodic
        # job, so this deployment registers no background jobs at all and has no
        # dependency on the process staying alive between requests — it runs fine
        # on a sleep-on-idle free host, not just an always-on one.
        log.info(f"Demo login enabled — capacity {demo.DEMO_CAPACITY}, sessions {demo.DEMO_SESSION_HOURS}h "
                 "(no background scheduler — sync/expiry are both handled inline per request)")
    else:
        scheduler.add_job(sync._auto_sync, "interval", hours=sync.SYNC_INTERVAL_HOURS,
                           next_run_time=sync._next_auto_sync_time())
        log.info(f"Auto-sync scheduled every {sync.SYNC_INTERVAL_HOURS}h")

        def _run_generator():
            from .coach import generator
            generator.run_for_all_users()

        from .util import APP_TIMEZONE
        scheduler.add_job(_run_generator, "cron", hour=4, minute=0, timezone=APP_TIMEZONE)
        log.info(f"Workout generator scheduled daily at 04:00 {APP_TIMEZONE}")

        def _run_pmc_pipeline():
            from . import pipeline
            pipeline.run_for_all_users()

        scheduler.add_job(_run_pmc_pipeline, "cron", hour=4, minute=15, timezone=APP_TIMEZONE)
        log.info(f"PMC pipeline (CTL/ATL/TSB) scheduled daily at 04:15 {APP_TIMEZONE}")

        def _run_self_review():
            from .coach import self_review
            self_review.run_for_all_users()

        scheduler.add_job(_run_self_review, "cron", hour=4, minute=30, timezone=APP_TIMEZONE)
        log.info(f"Coach self-review scheduled daily at 04:30 {APP_TIMEZONE}")
    scheduler.start()


# Both directories are resolved relative to this file, not the process's CWD — the
# app is now an installed package (see pyproject.toml), so nothing here can assume
# CWD lines up with the package directory the way it happened to when this ran as a
# flat, uninstalled top-level module.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WEB_DIST_DIR = os.path.join(os.path.dirname(__file__), "web-dist")

# Legacy vanilla-JS frontend (Phase 0 predecessor) — kept reachable at /legacy for
# one release during the parity window (see PLAN.md 0.10), then deleted along with
# app/static/ once the new frontend below is confirmed stable.
app.mount("/legacy", StaticFiles(directory=STATIC_DIR, html=True), name="legacy-static")

# New built frontend (web/dist, copied in by the Dockerfile's web-builder stage).
# Vite content-hashes everything under assets/, so that directory alone can be
# served as plain static files; every other path (every React Router route, a
# hard reload on /insights, /map, etc.) needs to fall through to index.html so
# client-side routing can take over — StaticFiles(html=True) only auto-serves
# index.html for the mount's own root, not for arbitrary unmatched sub-paths, so
# this is a explicit catch-all rather than a second bare StaticFiles mount.
if os.path.isdir(WEB_DIST_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEB_DIST_DIR, "assets")), name="web-assets")

    @app.get("/{full_path:path}")
    async def serve_web_app(full_path: str):
        candidate = os.path.join(WEB_DIST_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(WEB_DIST_DIR, "index.html"))
else:
    # Local dev without a built web-dist/ (e.g. running main.py directly against
    # the Vite dev server on :5173 instead) — fall back to legacy at the root so
    # the app is never left with literally nothing at "/".
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
