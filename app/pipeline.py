"""Phase 6.2 — PMC (Performance Management Chart) pipeline: CTL/ATL/TSB from real
per-run training load (Phase 6.1's Run.tss). Always fully recomputed from Run
history, never incrementally adjusted — same discipline as stats.backfill_run_metrics
— cheap enough (a few hundred days of simple arithmetic) that there's no reason to
risk incremental-computation drift for a marginal cost saving."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from .models import SessionLocal, Run, DailyMetrics, User, DEFAULT_USER_ID, owned_by

log = logging.getLogger("runlog")

CTL_DAYS = 42  # "fitness" — long rolling average, changes slowly
ATL_DAYS = 7   # "fatigue" — short rolling average, changes quickly


def compute_daily_metrics(db, user_id: str = DEFAULT_USER_ID) -> int:
    """Recomputes the full DailyMetrics history for `user_id` from every Run with a
    real tss value (Phase 6.1 — hrTSS or its fallback estimate). Returns the number
    of days written. TSB for a given day is *yesterday's* ctl minus atl — the
    standard convention of representing freshness at the start of a day, before
    that day's own training is absorbed into ctl/atl."""
    rows = (
        db.query(Run.date, Run.tss)
        .filter(owned_by(Run.user_id, user_id), Run.tss.isnot(None))
        .all()
    )
    if not rows:
        return 0

    daily_load = defaultdict(float)
    for date_str, tss in rows:
        daily_load[date_str] += tss

    start = datetime.strptime(min(daily_load), "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    end = max(datetime.strptime(max(daily_load), "%Y-%m-%d").date(), today)

    computed_at = datetime.now(timezone.utc).isoformat()
    ctl = atl = 0.0
    out = []
    d = start
    while d <= end:
        d_str = d.isoformat()
        load = daily_load.get(d_str, 0.0)
        tsb = ctl - atl
        ctl = ctl + (load - ctl) / CTL_DAYS
        atl = atl + (load - atl) / ATL_DAYS
        out.append(DailyMetrics(
            user_id=user_id, date=d_str, computed_at=computed_at,
            daily_load=round(load, 1), ctl=round(ctl, 2), atl=round(atl, 2), tsb=round(tsb, 2),
        ))
        d += timedelta(days=1)

    db.query(DailyMetrics).filter(owned_by(DailyMetrics.user_id, user_id)).delete()
    db.add_all(out)
    db.commit()
    return len(out)


def run_for_all_users():
    """Nightly job entry point (see main.py's scheduler wiring) — same real
    multi-tenant iteration as coach.generator.run_for_all_users (every non-demo
    user, not just DEFAULT_USER_ID)."""
    db = SessionLocal()
    try:
        users = db.query(User).filter(or_(User.is_demo == False, User.is_demo.is_(None))).all()  # noqa: E712
        for user in users:
            try:
                count = compute_daily_metrics(db, user.id)
                log.debug(f"pipeline: recomputed {count} days of metrics for {user.id}")
            except Exception as e:
                log.warning(f"pipeline: run failed for {user.id}: {e}")
    finally:
        db.close()
