"""Phase 27 — homepage daily coach report: a short trailing-7-day check-in (training
volume/load, readiness trend, one concrete observation), generated at most once per
calendar day and cached in sync_meta. Generation itself never runs in a request's
response path (see Phase 27.1): a nightly cron job (main.py, mirroring the existing
04:00/04:15/04:30 generator/PMC/self-review jobs) pre-generates each user's report,
and routes/chat.py's GET /api/coach/daily-report is a pure cache read that falls back
to a fire-and-forget background task (never awaited in the response) only if the
cron missed that day — e.g. the container wasn't up at its scheduled time. Uses
core.run_one_shot (the same ephemeral no-tools LLM call self_review.py's review/merge
calls use) since all the numbers it needs are cheap to pre-fetch directly from
stats.py; no tool access is needed or given."""
import asyncio
import json
import logging
from datetime import timedelta

from .. import stats
from . import assistant
from . import core
from ..models import SessionLocal, User, DEFAULT_USER_ID, get_sync_meta, set_sync_meta, user_key
from ..util import local_today

log = logging.getLogger("runlog")

_REPORT_INSTRUCTIONS = (
    "Write a short (3-6 sentence) daily check-in covering the trailing 7 days: "
    "training volume/load, readiness/recovery trend, and one concrete observation "
    "or suggestion. Ground every claim in the numbers given below — never invent a "
    "stat you weren't given, and never restate a count/number differently than how "
    "it's given here. No greeting or sign-off boilerplate — get straight to the "
    "content."
)


def _build_report_prompt(db, user_id: str) -> str:
    end = local_today(user_id)
    # 6, not 7: a true trailing-7-CALENDAR-DAY window inclusive of today is
    # [end - 6, end] (7 dates total) — using timedelta(days=7) here was a real bug
    # caught in testing (produced an 8-day window, one extra day inflating run counts
    # and other totals versus what the user actually did in the real last 7 days).
    start = end - timedelta(days=6)
    run_stats = stats.run_summary(db, start.isoformat(), end.isoformat(), "Run", user_id=user_id)
    load = stats.training_load_trend(db, user_id=user_id)
    ready = stats.readiness(db, user_id=user_id)
    wellness = stats.wellness_trend(db, 7, user_id=user_id)
    goals = stats.list_active_goals_with_progress(db, user_id=user_id)

    # Never hand a raw sec/mi number to the model and trust it to reformat/convert
    # units itself — real bug caught from actual use: it once echoed "596.9 sec/mi"
    # verbatim instead of "9:57/mi". Give it the already-formatted string and drop
    # the raw field from what it sees, so there's nothing ambiguous left to (mis)quote.
    pace_display = stats.format_pace_or_speed(run_stats.get("avgPaceSecPerMi"), "Run")
    run_stats_for_prompt = {k: v for k, v in run_stats.items() if k != "avgPaceSecPerMi"}
    run_stats_for_prompt["avgPaceDisplay"] = pace_display or "n/a"

    return (
        f"Trailing 7 days ({start.isoformat()} to {end.isoformat()}), exactly "
        f"{run_stats['runCount']} runs — state this exact count if you mention how "
        f"many runs, do not recount or approximate. avgPaceDisplay is already in "
        f"the correct human format (MM:SS/mi) — quote it exactly, never convert it "
        f"to a raw number of seconds:\n"
        f"Run summary: {json.dumps(run_stats_for_prompt)}\n"
        f"Training load trend: {json.dumps(load)}\n"
        f"Today's readiness: {json.dumps(ready)}\n"
        f"7-day wellness/readiness trend: {json.dumps(wellness)}\n"
        f"Active goals: {json.dumps(goals)}\n"
    )


async def generate_report(db, user_id: str) -> str | None:
    """Returns None on any failure (not configured, LLM error, suspicious reply) so
    the caller can cache "no report today" without ever caching a broken/
    hallucinated one."""
    if not assistant.is_configured():
        return None
    personality, sex = assistant._current_persona_inputs(user_id)
    persona_text = core.PERSONA_PROMPTS.get(personality, core.PERSONA_PROMPTS["normal"])
    sex_block = f"\n\n{core.WOMENS_STRENGTH_PROMPT}" if sex == "female" else ""
    system_prompt = f"{core.BASE_PROMPT}\n\n{persona_text}{sex_block}\n\n{_REPORT_INSTRUCTIONS}"
    prompt = _build_report_prompt(db, user_id)
    reply = await core.run_one_shot(system_prompt, prompt)
    if reply is None or not core.looks_like_real_content(reply, None):
        return None
    return reply


async def generate_and_cache(user_id: str = DEFAULT_USER_ID) -> str | None:
    """Computes today's report and writes it to the sync_meta cache — the single
    place caching happens, shared by the nightly cron job (run_for_all_users below)
    and routes/chat.py's background-task fallback, so there's exactly one code path
    that decides what "today's cached report" means."""
    today = local_today(user_id).isoformat()
    cache_key = user_key(user_id, f"coach_report_{today}")
    db = SessionLocal()
    try:
        report = await generate_report(db, user_id)
    finally:
        db.close()
    set_sync_meta(cache_key, report or "")
    return report


def run_for_all_users() -> None:
    """Cron entry point (main.py, scheduled daily at 04:45 APP_TIMEZONE) — pre-
    generates every non-demo user's report so a Home load never has to wait on the
    LLM call. Exception-isolated per user, mirroring self_review.run_for_all_users'
    identical pattern. P12: sends push notification after report generates."""
    db = SessionLocal()
    try:
        user_ids = [u.id for u in db.query(User).filter(User.is_demo.isnot(True)).all()]
    finally:
        db.close()
    for uid in user_ids:
        try:
            asyncio.run(generate_and_cache(uid))
            # P12 — push notification after report generates (best-effort, no-op if unsubscribed)
            db = SessionLocal()
            try:
                from ..push import send_push
                send_push(db, uid, "Daily Report Ready", "Your daily coach report is ready to read.", "/")
            except Exception:
                log.exception(f"push notification failed for daily_report user {uid}")
            finally:
                db.close()
        except Exception:
            log.exception(f"daily_report.run_for_all_users failed for {uid}")
