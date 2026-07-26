"""
OPTIONAL secondary source. Garmin has no official public API for this kind of data —
this uses the unofficial `garminconnect` library, which logs in with your real
Garmin credentials and can break whenever Garmin changes their internal endpoints.
Use Strava as your primary source; treat this as a bonus.
"""

import os
import io
import json
import time
import logging
import zipfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from ..models import (
    SessionLocal,
    Run,
    DailySteps,
    ProviderCredential,
    UserTrainingConfig,
    get_sync_meta,
    set_sync_meta,
    run_needs_detail_sync,
    day_needs_wellness_sync,
    resolve_run_id,
    user_key,
)
from .weather import get_historical_weather
from ..util import classify_run_type, detect_intervals, local_today, compute_tss, compute_efficiency_factor
from .. import stats
from ..stats import _normalize_activity_type, activity_family

log = logging.getLogger("runlog")

GARMIN_TOKEN_STORE_DIR = os.environ.get("GARMIN_TOKEN_STORE_DIR", "/data")

# Proactive throttle + retry/backoff for Garmin's unofficial, undocumented rate limits —
# see GarminMidSyncRateLimitError below for why these are deliberately conservative
# (a real sustained block lasts far longer than any short backoff could absorb; this is
# insurance against brief bursts, not an attempt to outlast a genuine lockout).
GARMIN_INTER_ACTIVITY_DELAY_SEC = float(
    os.environ.get("GARMIN_INTER_ACTIVITY_DELAY_SEC", "1.5")
)
GARMIN_MAX_RETRIES = int(os.environ.get("GARMIN_MAX_RETRIES", "3"))
GARMIN_RETRY_BASE_BACKOFF_SEC = float(
    os.environ.get("GARMIN_RETRY_BASE_BACKOFF_SEC", "5.0")
)
# Separate from the per-activity retry ladder above: after every GARMIN_DETAIL_BATCH_SIZE
# activities that actually needed a real detail fetch (skips via dedup don't count —
# they cost no API calls), pause much longer before continuing. The per-activity 1.5s
# delay and 5s/10s/20s retry ladder only insure against brief bursts; real Garmin
# lockouts observed this session outlast that easily, so the fix isn't a longer retry —
# it's fewer detail-fetch calls per unit time in the first place.
GARMIN_DETAIL_BATCH_SIZE = int(os.environ.get("GARMIN_DETAIL_BATCH_SIZE", "5"))
GARMIN_BATCH_PAUSE_SEC = float(os.environ.get("GARMIN_BATCH_PAUSE_SEC", "300"))
# Once a day's step total is stored, it's "settled" and won't be re-requested — except
# a small trailing window that can still change after being first synced (today's total
# keeps accumulating all day, and yesterday's can arrive late if the watch didn't sync
# to Garmin's servers until after midnight).
GARMIN_STEPS_VOLATILE_DAYS = int(os.environ.get("GARMIN_STEPS_VOLATILE_DAYS", "2"))
# Same volatile-window idea, for the per-day wellness metrics below (resting HR/VO2max/
# sleep) — a settled day's row isn't re-fetched. Deliberately much smaller than the
# activity backlog's full-history walk: quick sync only ever covers the volatile window
# itself (cheap, always attempted); the deeper historical window is backlog sync's job,
# capped at GARMIN_WELLNESS_BACKFILL_DAYS rather than the full account history, since
# each day needs 3 separate live API calls (get_stats/get_max_metrics/get_sleep_data),
# not the single cheap list call activities get.
#
# Trimmed from 3 to 2: resting HR/VO2max genuinely only matter for today, but sleep is
# the exception — "today's" sleep is actually last night's, and Garmin can still finish
# processing it the morning after (see day_needs_wellness_sync's docstring: once a day
# is marked synced even once, it's never re-checked again outside this window), so
# yesterday needs to stay in the volatile window too. 1 day would drop that safety net
# entirely; 3 was more margin than the actual data ever needed.
GARMIN_WELLNESS_VOLATILE_DAYS = int(os.environ.get("GARMIN_WELLNESS_VOLATILE_DAYS", "2"))
GARMIN_WELLNESS_BACKFILL_DAYS = int(os.environ.get("GARMIN_WELLNESS_BACKFILL_DAYS", "90"))
# The volatile window used to be re-fetched in full on *every* sync invocation with no
# throttle at all — two manual syncs a minute apart cost the same calls twice for data
# that couldn't plausibly have changed. Skipping a re-check within this many seconds of
# the last one keeps the "recent data can still change" guarantee (still re-checked at
# least this often) while cutting the fixed tax on repeated syncs close together.
GARMIN_WELLNESS_RECHECK_MIN_SEC = int(os.environ.get("GARMIN_WELLNESS_RECHECK_MIN_SEC", "1800"))
# Originally 15 minutes to catch Garmin revising a suggested workout right before you
# start it, but under real rate-limit pressure that freshness isn't worth 2 calls every
# sync — once/day (86400s) still catches same-day revisions on any sync after the day's
# first one, just not ones made in the last few minutes before a workout specifically.
GARMIN_ADAPTIVE_PLAN_RECHECK_MIN_SEC = int(os.environ.get("GARMIN_ADAPTIVE_PLAN_RECHECK_MIN_SEC", "86400"))
GARMIN_ADAPTIVE_PLAN_LAST_CHECKED_KEY = "garmin_adaptive_plan_last_checked_at"
# Cross-invocation cooldown: separate from every per-call retry/pause above, which all
# operate *within* one sync attempt. This instead gates *starting a new attempt at all*
# once a real rate limit has been hit — repeated manual clicks (or any other trigger)
# right after a failure were burning another full login + retry-ladder burst against an
# account that looks like it's under a real IP-level lockout (see item 30's finding),
# not a brief burst, which just makes that lockout worse instead of better. Grows
# exponentially with consecutive failures, resets to 0 on any clean success.
GARMIN_COOLDOWN_BASE_SEC = float(os.environ.get("GARMIN_COOLDOWN_BASE_SEC", "300"))  # 5 min
GARMIN_COOLDOWN_MAX_SEC = float(os.environ.get("GARMIN_COOLDOWN_MAX_SEC", "14400"))  # 4h cap
GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY = "garmin_rate_limit_cooldown_until"
GARMIN_RATE_LIMIT_FAILURES_KEY = "garmin_rate_limit_consecutive_failures"
# The truncation problem (see _parse_fit_streams) isn't running-specific, so FIT download
# is attempted for any activity with GPS by default — set false to restrict back to
# running-only if this measurably worsens rate-limit pressure.
GARMIN_FIT_ROUTE_ALL_GPS = (
    os.environ.get("GARMIN_FIT_ROUTE_ALL_GPS", "true").lower() == "true"
)

METERS_PER_MILE = 1609.34
SEMICIRCLE_TO_DEGREES = 180 / (2**31)
GRAMS_PER_LB = 453.592


class GarminLoginRateLimitError(RuntimeError):
    """Garmin rejected the login endpoint itself (429 or session-conflict error)."""


class GarminMidSyncRateLimitError(RuntimeError):
    """A per-activity API call (laps/route/FIT download) got rate-limited after a
    successful login — distinct from a login failure so the UI doesn't mislabel it."""

    def __init__(self, synced_count: int):
        self.synced_count = synced_count
        super().__init__(
            f"Garmin's API rate-limited a request mid-sync (login succeeded; "
            f"{synced_count} activities were synced first). Progress was saved — "
            f"wait a while before retrying."
        )


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "not supported between instances" in msg


def _seconds_since(iso_timestamp: str) -> float:
    """Distinct from coach.py's _hours_since (seconds resolution, needed for the much
    shorter recheck windows used here) — same tz-naive-timestamp tolerance."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


def is_rate_limit_related(e: Exception) -> bool:
    """True for any exception representing a Garmin rate-limit condition — mid-sync,
    login, or this module's own cross-invocation cooldown gate (_raise_if_garmin_cooling_down,
    a plain RuntimeError since it's raised proactively rather than caught from the API).
    Used by callers (e.g. main.py's backlog runner) that want to distinguish 'wait and
    retry automatically' from a genuine, non-rate-limit failure that should surface
    immediately instead."""
    return isinstance(e, (GarminMidSyncRateLimitError, GarminLoginRateLimitError)) or (
        "cooldown active" in str(e)
    )


def _harden_tokenstore_permissions(token_store: str):
    """Mitigates GHSA-wjhr-76vg-2hvc (CWE-732, High) — garminconnect's Client.dump()
    writes garmin_tokens.json under the process umask, which defaults to world-readable
    (0o644) in a file inside a world-readable/listable (0o755) directory. The file holds
    the DI refresh token: anyone who can read it gets persistent account access,
    bypassing password/MFA. Confirmed exploitable on this exact deployment (checked real
    file perms on /data before writing this). Fixed upstream in garminconnect 0.3.5,
    but that requires Python 3.12 and this project is pinned to 0.3.2 on python:3.11-slim
    (see Dockerfile) — so this replicates the fix (0o600 file inside 0o700 directory)
    independently of which garminconnect version is installed, rather than waiting on
    an upgrade path that isn't available yet. Best-effort: a permission error here
    (e.g. read-only filesystem) shouldn't break a login that otherwise succeeded, so
    failures are logged, not raised."""
    try:
        path = Path(token_store)
        if path.is_dir():
            os.chmod(path, 0o700)
            for f in path.iterdir():
                if f.is_file():
                    os.chmod(f, 0o600)
        elif path.is_file():
            os.chmod(path, 0o600)
    except Exception as e:
        log.debug(f"garmin login: tokenstore permission hardening failed (non-fatal): {e}")


def _get_garmin_credential(user_id: str):
    db = SessionLocal()
    try:
        return (
            db.query(ProviderCredential)
            .filter_by(user_id=user_id, provider="garmin")
            .first()
        )
    finally:
        db.close()


_cached_client = None
_cached_client_user_id = None
_cached_client_logged_in_at = None
# Even with a valid tokenstore session, garminconnect's own login() unconditionally
# makes 2+ more calls every time ("ensure profile is loaded": socialProfile + user
# settings, each internally retried up to 3x on failure) — that cost was invisible
# from the outside since it happens *inside* a call this app always treated as "did
# login succeed", not "how many requests did that take". Caching the already-
# authenticated client object in this process's memory means repeat syncs within this
# window skip calling client.login() at all, not just skip the credential fallback.
GARMIN_CLIENT_CACHE_MAX_AGE_SEC = int(os.environ.get("GARMIN_CLIENT_CACHE_MAX_AGE_SEC", "1800"))


def _login(user_id: str):
    """Three layers, cheapest first:
    1. In-process cached client, if within GARMIN_CLIENT_CACHE_MAX_AGE_SEC — zero calls,
       memory-only, resets on container restart.
    2. _lightweight_session_check — loads the on-disk tokenstore and validates/refreshes
       it via garminconnect's internal client directly, bypassing garminconnect.Garmin.
       login()'s wrapper (which unconditionally makes 2+ more calls even when nothing
       needs refreshing). Zero calls unless a token is genuinely about to expire.
    3. client.login(tokenstore=...) then client.login() (fresh credentials) — the
       original, fully-supported-API fallback, used if either private-internals shortcut
       above fails for any reason (a garminconnect update, corrupted tokenstore, etc).
    The token store is per-user (/data/.garmin_tokens_{user_id}) — /data is the same
    persistent volume runlog.db already lives on, so this survives container recreates.
    Credentials come from ProviderCredential (populated either by the startup migration
    from GARMIN_EMAIL/GARMIN_PASSWORD, or entered later through the Connections UI)."""
    global _cached_client, _cached_client_user_id, _cached_client_logged_in_at
    if (
        _cached_client is not None
        and _cached_client_user_id == user_id
        and _cached_client_logged_in_at is not None
        and _seconds_since(_cached_client_logged_in_at) < GARMIN_CLIENT_CACHE_MAX_AGE_SEC
    ):
        log.debug("garmin login: reusing in-process cached client, no network call")
        return _cached_client

    cred = _get_garmin_credential(user_id)
    if not cred or not cred.username or not cred.password:
        raise RuntimeError(
            "No Garmin credentials on file for this user — add them in Settings → Connections"
        )
    try:
        import garminconnect
    except ImportError:
        raise RuntimeError("garminconnect package not installed")

    token_store = f"{GARMIN_TOKEN_STORE_DIR}/.garmin_tokens_{user_id}"
    client = garminconnect.Garmin(cred.username, cred.password)

    if _lightweight_session_check(client, token_store):
        log.debug("garmin login: lightweight session check succeeded — skipped garminconnect's forced profile/settings calls")
        # ...except display_name/full_name are ONLY ever set as a side effect of
        # those skipped calls (garminconnect._load_profile_and_settings) -- never
        # populated by the lightweight path. A real, confirmed-live bug this
        # caused: get_stats()/get_user_summary() and several other endpoints
        # 403 without a display_name, so resting_hr_bpm silently went to None for
        # every sync since this lightweight path started being taken, not because
        # the account itself lacks a display name (it doesn't -- confirmed via a
        # forced full login). Fetching it once per fresh client (bounded by
        # GARMIN_CLIENT_CACHE_MAX_AGE_SEC's in-process cache above, so this is at
        # most one extra pair of calls per cache window, not per sync) closes the
        # gap without giving up the lightweight path's savings on every other call.
        if not client.display_name:
            try:
                client._load_profile_and_settings()
                log.debug("garmin login: populated display_name after lightweight session check")
            except Exception as e:
                log.debug(f"garmin login: could not populate display_name ({e}) — display-name-dependent calls will fail this session")
    else:
        try:
            client.login(tokenstore=token_store)
            log.debug(f"garmin login: used cached session (full login()) from {token_store}")
        except Exception as e:
            log.debug(f"garmin login: cached session failed ({e}), falling back to fresh credential login")
            try:
                client.login()
                log.debug("garmin login: fresh credential login succeeded")
            except Exception as e:
                if _is_rate_limit_error(e):
                    log.debug(f"garmin login: rate-limited ({e})")
                    raise GarminLoginRateLimitError(
                        "Garmin is rate-limiting login attempts from this network right now "
                        "(this is common with the unofficial API). Wait a while before retrying."
                    ) from e
                raise

    try:
        # BUG FIX: this was `client.garth.dump(...)` — garminconnect.Garmin has no
        # `garth` attribute at all (confirmed: hasattr() is False), so this has been
        # silently failing every single call, swallowed by the bare except below.
        # `client.client` (garminconnect's own internal Client) is the real object with
        # a working dump(). Persisting here matters most after _refresh_session() inside
        # _lightweight_session_check, whose own internals don't necessarily write back
        # to disk — without this, a refreshed-but-unpersisted token would force a real
        # network round-trip again on every subsequent process restart.
        client.client.dump(token_store)
        _harden_tokenstore_permissions(token_store)
    except Exception as e:
        log.debug(f"garmin login: token persist failed (non-fatal): {e}")

    _cached_client = client
    _cached_client_user_id = user_id
    _cached_client_logged_in_at = datetime.now(timezone.utc).isoformat()
    return client


def _lightweight_session_check(client, token_store: str) -> bool:
    """Attempts to validate/refresh the saved session using garminconnect's internal
    client directly (client.client, a garminconnect.client.Client instance) instead of
    going through garminconnect.Garmin.login()'s wrapper, which unconditionally makes
    2+ more calls every time (GET /userprofile-service/socialProfile and user-settings,
    "to ensure profile is loaded" — each internally retried up to 3x on failure) even
    when the saved session needs zero network at all. is_authenticated is a pure local
    property (bool(di_token or jwt_web)); _token_expires_soon() just decodes the JWT's
    own exp claim locally — neither makes a network call. Only _refresh_session(), and
    only when a token is genuinely about to expire, does.

    _token_expires_soon/_refresh_session are private (underscore-prefixed) methods, not
    part of garminconnect's stable public API — deliberately wrapped in a broad except
    and used only as a fast path, with the existing client.login(tokenstore=...) chain
    as the unconditional fallback on any failure, so a future garminconnect version
    changing these internals degrades to today's already-correct (if less efficient)
    behavior instead of breaking sync."""
    try:
        client.client.load(token_store)
        if not client.client.is_authenticated:
            return False
        if client.client.di_refresh_token and client.client._token_expires_soon():
            log.debug("garmin login: token expiring soon, refreshing (1 call instead of 2+)")
            client.client._refresh_session()
        return True
    except Exception as e:
        log.debug(f"garmin login: lightweight session check unavailable ({e})")
        return False


def _maybe_populate_threshold_hr(client, user_id: str):
    """Phase 6.1 — auto-fills UserTrainingConfig.threshold_hr from Garmin's own
    lactate-threshold estimate (get_lactate_threshold(), already available in the
    pinned garminconnect version, no extra OAuth scope needed) the first time a
    sync runs after it's been left unset. Never overwrites a value already there
    (auto-populated before, or entered manually in Settings) — only ever sets it
    from None. Best-effort: any failure (network, unexpected response shape, no
    LTHR estimate yet from Garmin) is swallowed, matching steps/adaptive-plan's
    own degrade-to-no-op pattern, since this is a nice-to-have, not required for
    the sync itself to succeed."""
    db = SessionLocal()
    try:
        config = db.get(UserTrainingConfig, user_id)
        if config and config.threshold_hr:
            return
        data = client.get_lactate_threshold(latest=True)
        hr = (data.get("speed_and_heart_rate") or {}).get("heartRate")
        if not hr:
            return
        if not config:
            config = UserTrainingConfig(
                user_id=user_id, weekly_ramp_pct=3.0, mesocycle_pattern="3:1", distribution="pyramidal",
                strength_days_per_week=2, strength_template="full_body_ab",
            )
            db.add(config)
        config.threshold_hr = round(hr)
        db.commit()
    except Exception as e:
        log.debug(f"garmin threshold HR auto-populate skipped/failed: {e}")
    finally:
        db.close()


def _sync_daily_steps(client, user_id: str, days: int = 30) -> int:
    """Best-effort — the exact response shape of client.get_daily_steps() hasn't been
    verified against the live API (unverified/unexercised as of this writing, see
    STATUS.md), so this degrades to skipping malformed entries rather than raising,
    and is never allowed to fail an activity sync that's otherwise working.
    KNOWN LIMITATION: DailySteps' primary key is still just `date` (see models.py) — two
    real users syncing steps for the same calendar date will overwrite each other's
    count. Safe today since there's only one real user; needs a composite-PK migration
    before true multi-user step tracking works.

    Skips re-requesting days already stored, except the trailing GARMIN_STEPS_VOLATILE_DAYS
    window (see its definition) — mirrors the same don't-re-fetch-settled-data principle
    as Run.detail_synced_at/run_needs_detail_sync, applied here to per-day step totals
    instead of activities. get_daily_steps() chunks into one real API call per 28 days
    of range requested, so re-requesting a full 365-day backlog window every single sync
    (as this used to) cost ~14 API calls for data that's almost entirely already stored
    and never changes. Assumes no historical gaps in what's already stored, which holds
    by construction — every DailySteps row only ever comes from this same function."""
    end = local_today(user_id)
    volatile_start = end - timedelta(days=GARMIN_STEPS_VOLATILE_DAYS - 1)
    requested_start = end - timedelta(days=days - 1)

    db = SessionLocal()
    try:
        newest_settled = (
            db.query(func.max(DailySteps.date))
            .filter(DailySteps.date < volatile_start.isoformat())
            .scalar()
        )
    finally:
        db.close()

    if newest_settled:
        next_needed = datetime.strptime(newest_settled, "%Y-%m-%d").date() + timedelta(days=1)
        start = max(requested_start, next_needed)
    else:
        start = requested_start

    if start > end:
        return 0

    try:
        entries = client.get_daily_steps(start.isoformat(), end.isoformat()) or []
    except Exception:
        return 0

    db = SessionLocal()
    count = 0
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            date_str = entry.get("calendarDate") or entry.get("date")
            steps = (
                entry.get("totalSteps") or entry.get("steps") or entry.get("stepCount")
            )
            if not date_str or steps is None:
                continue
            existing = db.get(DailySteps, (date_str, user_id))
            row = existing or DailySteps(date=date_str, user_id=user_id)
            row.steps = int(steps)
            db.merge(row)
            count += 1
        db.commit()
    finally:
        db.close()

    return count


def _extract_wellness_resting_hr(stats: dict):
    return (
        stats.get("restingHeartRate")
        or stats.get("restingHeartRateInBeatsPerMinute")
        or stats.get("wellnessRestingHeartRate")
    )


def _extract_vo2max(max_metrics) -> float:
    """garminconnect's get_max_metrics() return shape is unverified — tried both as a
    bare dict and as a list of per-device/period dicts, each checked for a few
    plausible nesting paths (a "generic" sub-object is the most commonly documented
    shape for running VO2max specifically)."""
    candidates = max_metrics if isinstance(max_metrics, list) else [max_metrics]
    for c in candidates:
        if not isinstance(c, dict):
            continue
        generic = c.get("generic") or {}
        value = (
            generic.get("vo2MaxPreciseValue")
            or generic.get("vo2MaxValue")
            or c.get("vo2MaxValue")
            or c.get("vo2Max")
        )
        if value:
            return value
    return None


def _extract_hrv(hrv_data) -> tuple:
    """garminconnect's get_hrv_data() return shape is unverified against this exact
    0.3.6 pin (no vendored source in this repo to check against) — same situation
    _extract_vo2max already documents, so the same mitigation applies: try a few
    plausible nesting paths, and log the actual raw keys once on the first real sync
    (see _sync_daily_wellness) so any wrong guess here is a quick follow-up fix, not
    a silent permanent miss. Returns (avg_ms, status) — either may be None."""
    if not isinstance(hrv_data, dict):
        return None, None
    summary = hrv_data.get("hrvSummary") or hrv_data.get("hrvSummaries") or hrv_data
    if isinstance(summary, list):
        summary = summary[0] if summary else {}
    if not isinstance(summary, dict):
        return None, None
    avg_ms = (
        summary.get("lastNightAvg")
        or summary.get("lastNight5MinHigh")
        or summary.get("weeklyAvg")
    )
    status = summary.get("status") or summary.get("hrvStatus")
    return avg_ms, status


def _extract_training_status(data) -> dict:
    """garmin_sync.get_training_status()'s real shape (confirmed live): device-keyed
    JSON under mostRecentTrainingStatus.latestTrainingStatusData, each device carrying
    acuteTrainingLoadDTO plus a plain-language trainingStatusFeedbackPhrase. Picks the
    device flagged primaryTrainingDevice; falls back to the first (only) device if none
    is flagged, since a single-device account may not always set that flag. Returns an
    empty dict (never raises) if the shape doesn't match — same degrade-quietly
    discipline as _extract_hrv/_extract_vo2max."""
    try:
        devices = data.get("mostRecentTrainingStatus", {}).get("latestTrainingStatusData", {})
        if not devices:
            return {}
        device = next((d for d in devices.values() if d.get("primaryTrainingDevice")), None)
        device = device or next(iter(devices.values()))
        acute = device.get("acuteTrainingLoadDTO") or {}
        return {
            "acute": acute.get("dailyTrainingLoadAcute"),
            "chronic": acute.get("dailyTrainingLoadChronic"),
            "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": acute.get("acwrStatus"),
            "status_phrase": device.get("trainingStatusFeedbackPhrase"),
        }
    except (AttributeError, TypeError):
        return {}


def _first_not_none(*values):
    """Like `a or b or c`, but doesn't treat a legitimate 0 as missing — real fields
    here (awake seconds especially) can genuinely be 0 for a solid night's sleep, and
    `or`-chaining would incorrectly fall through to the next (usually-absent) candidate."""
    for v in values:
        if v is not None:
            return v
    return None


def _extract_sleep_fields(sleep_data: dict) -> dict:
    """Confirmed against a real response (2026-07-13): dailySleepDTO's keys are exactly
    as guessed — sleepTimeSeconds/deepSleepSeconds/lightSleepSeconds/remSleepSeconds/
    awakeSleepSeconds are direct top-level keys, sleepScores.overall.value holds the
    score. Falls back to the top level in case a given account/version returns it
    unwrapped (not a "dailySleepDTO" wrapper)."""
    daily = sleep_data.get("dailySleepDTO") or sleep_data or {}
    scores = daily.get("sleepScores") or {}
    overall = scores.get("overall") if isinstance(scores, dict) else None
    sleep_score = _first_not_none(
        overall.get("value") if isinstance(overall, dict) else None,
        daily.get("sleepScore"),
        scores.get("value") if isinstance(scores, dict) else None,
    )
    return {
        "sleep_score": round(sleep_score) if sleep_score is not None else None,
        "sleep_seconds": daily.get("sleepTimeSeconds"),
        "deep_sleep_seconds": daily.get("deepSleepSeconds"),
        "light_sleep_seconds": daily.get("lightSleepSeconds"),
        "rem_sleep_seconds": daily.get("remSleepSeconds"),
        "awake_sleep_seconds": _first_not_none(daily.get("awakeSleepSeconds"), daily.get("awakeDurationSeconds")),
    }


_SLEEP_STAGE_LABELS = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def _extract_sleep_stages(raw_sleep: dict) -> list:
    """The per-segment stage timeline (a real hypnogram), not just daily totals —
    confirmed against real data: get_sleep_data()'s top-level "sleepLevels" array holds
    {startGMT, endGMT, activityLevel} segments; summing each activityLevel's total
    duration and comparing against dailySleepDTO's known deep/light/rem/awakeSleepSeconds
    for the same night confirmed the mapping is 0=deep, 1=light, 2=rem, 3=awake exactly.
    ("sleepMovement", a much finer-grained ~500-entry actigraphy signal, is a different
    array and not what's used here.)"""
    segments = []
    for seg in raw_sleep.get("sleepLevels") or []:
        stage = _SLEEP_STAGE_LABELS.get(seg.get("activityLevel"))
        start, end = seg.get("startGMT"), seg.get("endGMT")
        if stage and start and end:
            segments.append({"start": start, "end": end, "stage": stage})
    return segments


def _bulk_sync_race_predictions(client, user_id: str, start_date: str, end_date: str, progress_cb=None) -> None:
    """Race predictions (Phase 24.3) support a real date-range bulk fetch (up to
    366 days per call, confirmed live) unlike every other wellness metric here, so
    this is called once per _sync_daily_wellness invocation covering the whole
    dates_to_sync span -- one call for N days, not N calls -- rather than folded
    into the per-day loop below. Chunks into <=365-day windows for safety even
    though normal usage (a volatile-window catch-up or the historical backfill)
    should rarely if ever need more than one chunk."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=365), end)
        try:
            data = client.get_race_predictions(
                startdate=cursor.isoformat(), enddate=chunk_end.isoformat(), _type="daily",
            )
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            log.debug(f"garmin wellness sync: get_race_predictions failed for {cursor}..{chunk_end}: {e}")
            cursor = chunk_end + timedelta(days=1)
            continue

        entries = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        db = SessionLocal()
        try:
            updated = 0
            for entry in entries:
                date_str = entry.get("calendarDate")
                if not date_str:
                    continue
                row = db.get(DailySteps, (date_str, user_id)) or DailySteps(date=date_str, user_id=user_id)
                if entry.get("time5K") is not None:
                    row.race_predict_5k_sec = entry["time5K"]
                if entry.get("time10K") is not None:
                    row.race_predict_10k_sec = entry["time10K"]
                if entry.get("timeHalfMarathon") is not None:
                    row.race_predict_half_marathon_sec = entry["timeHalfMarathon"]
                if entry.get("timeMarathon") is not None:
                    row.race_predict_marathon_sec = entry["timeMarathon"]
                db.merge(row)
                updated += 1
            db.commit()
            if progress_cb and updated:
                progress_cb(f"Synced race predictions for {updated} day(s) ({cursor}..{chunk_end})")
        finally:
            db.close()
        cursor = chunk_end + timedelta(days=1)


def _sync_daily_wellness(client, user_id: str, days: int, progress_cb=None) -> int:
    """Resting HR / VO2max / sleep / HRV / respiration (respiration added Phase 24,
    for stats.readiness()), one row per day in DailySteps — deliberately scoped to
    just these metrics (not the full 9-field wellness set from the original plan)
    since each additional metric is another live API call per day, and this
    account's rate-limit sensitivity argued for keeping that cost down. Dedups via
    day_needs_wellness_sync (same principle as run_needs_detail_sync) for settled days;
    the trailing GARMIN_WELLNESS_VOLATILE_DAYS window (today's/yesterday's data can
    still change) is re-checked on a GARMIN_WELLNESS_RECHECK_MIN_SEC throttle rather
    than unconditionally every call — two syncs a minute apart shouldn't both pay the
    full multi-day x multi-metric cost for data that couldn't plausibly have changed
    in between. Each metric is its own independently-wrapped API call (get_stats/
    get_max_metrics/get_hrv_data/get_respiration_data/get_sleep_data) so one metric's
    absence/failure never blocks the others — same discipline as
    _fetch_running_dynamics. A genuine rate-limit hit propagates up unchanged so the
    caller's existing GarminMidSyncRateLimitError/cooldown handling applies uniformly."""
    end = local_today(user_id)
    volatile_start = end - timedelta(days=GARMIN_WELLNESS_VOLATILE_DAYS - 1)
    start = end - timedelta(days=days - 1)

    db = SessionLocal()
    try:
        dates_to_sync = []
        cursor = start
        while cursor <= end:
            date_str = cursor.isoformat()
            if cursor >= volatile_start:
                row = db.get(DailySteps, (date_str, user_id))
                last_synced = row.wellness_synced_at if row else None
                if not last_synced or _seconds_since(last_synced) >= GARMIN_WELLNESS_RECHECK_MIN_SEC:
                    dates_to_sync.append(date_str)
            elif day_needs_wellness_sync(db, date_str, user_id):
                dates_to_sync.append(date_str)
            cursor += timedelta(days=1)
    finally:
        db.close()

    log.debug(f"garmin wellness sync: {len(dates_to_sync)} day(s) need syncing (window {start}..{end})")

    if dates_to_sync:
        _bulk_sync_race_predictions(client, user_id, min(dates_to_sync), max(dates_to_sync), progress_cb)

    synced = 0
    for i, date_str in enumerate(dates_to_sync):
        _maybe_batch_pause(i, progress_cb)

        db = SessionLocal()
        try:
            row = db.get(DailySteps, (date_str, user_id)) or DailySteps(date=date_str, user_id=user_id)

            try:
                stats = client.get_stats(date_str) or {}
                rhr = _extract_wellness_resting_hr(stats)
                if rhr:
                    row.resting_hr_bpm = round(rhr)
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_stats failed for {date_str}: {e}")

            try:
                vo2 = _extract_vo2max(client.get_max_metrics(date_str))
                if vo2:
                    row.vo2max = round(vo2, 1)
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_max_metrics failed for {date_str}: {e}")

            try:
                raw_hrv = client.get_hrv_data(date_str) or {}
                avg_ms, status = _extract_hrv(raw_hrv)
                if avg_ms:
                    row.hrv_last_night_avg_ms = round(avg_ms)
                if status:
                    row.hrv_status = status
                if raw_hrv and not avg_ms:
                    # Extraction came up empty against real data — log the actual raw
                    # keys once so a wrong guess in _extract_hrv is a quick fix, not a
                    # silent permanent miss (same mitigation as _extract_vo2max's own
                    # unverified-shape situation).
                    log.debug(f"garmin wellness sync: get_hrv_data for {date_str} returned no "
                              f"recognized field — raw keys: {list(raw_hrv.keys())}")
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_hrv_data failed for {date_str}: {e}")

            try:
                raw_resp = client.get_respiration_data(date_str) or {}
                waking = raw_resp.get("avgWakingRespirationValue")
                sleep_resp = raw_resp.get("avgSleepRespirationValue")
                lowest = raw_resp.get("lowestRespirationValue")
                highest = raw_resp.get("highestRespirationValue")
                if waking is not None:
                    row.avg_waking_respiration_rate = round(waking, 1)
                if sleep_resp is not None:
                    row.avg_sleep_respiration_rate = round(sleep_resp, 1)
                if lowest is not None:
                    row.lowest_respiration_rate = round(lowest, 1)
                if highest is not None:
                    row.highest_respiration_rate = round(highest, 1)
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_respiration_data failed for {date_str}: {e}")

            try:
                raw_status = client.get_training_status(date_str) or {}
                ts = _extract_training_status(raw_status)
                if ts.get("acute") is not None:
                    row.garmin_training_load_acute = round(ts["acute"], 1)
                if ts.get("chronic") is not None:
                    row.garmin_training_load_chronic = round(ts["chronic"], 1)
                if ts.get("acwr") is not None:
                    row.garmin_acwr = round(ts["acwr"], 2)
                if ts.get("acwr_status"):
                    row.garmin_acwr_status = ts["acwr_status"]
                if ts.get("status_phrase"):
                    row.garmin_training_status_phrase = ts["status_phrase"]
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_training_status failed for {date_str}: {e}")

            try:
                # Real shape (confirmed live): a list with one entry for the
                # requested date, not a bare dict.
                raw_readiness = client.get_training_readiness(date_str) or []
                readiness = raw_readiness[0] if raw_readiness else {}
                if readiness.get("score") is not None:
                    row.training_readiness_score = round(readiness["score"])
                if readiness.get("level"):
                    row.training_readiness_level = readiness["level"]
                if readiness.get("feedbackShort"):
                    row.training_readiness_feedback = readiness["feedbackShort"]
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_training_readiness failed for {date_str}: {e}")

            try:
                # Real shape (confirmed live): a list with one entry for the
                # requested date, not a bare dict.
                raw_bb = client.get_body_battery(date_str) or []
                bb = raw_bb[0] if raw_bb else {}
                if bb.get("charged") is not None:
                    row.body_battery_charged = round(bb["charged"])
                if bb.get("drained") is not None:
                    row.body_battery_drained = round(bb["drained"])
                bb_values = bb.get("bodyBatteryValuesArray")
                if bb_values:
                    row.body_battery_json = json.dumps(bb_values)
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_body_battery failed for {date_str}: {e}")

            try:
                raw_stress = client.get_all_day_stress(date_str) or {}
                if raw_stress.get("avgStressLevel") is not None:
                    row.avg_stress_level = round(raw_stress["avgStressLevel"])
                if raw_stress.get("maxStressLevel") is not None:
                    row.max_stress_level = round(raw_stress["maxStressLevel"])
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_all_day_stress failed for {date_str}: {e}")

            try:
                raw_sleep = client.get_sleep_data(date_str) or {}
                sleep_fields = _extract_sleep_fields(raw_sleep)
                for k, v in sleep_fields.items():
                    if v is not None:
                        setattr(row, k, v)
                stages = _extract_sleep_stages(raw_sleep)
                if stages:
                    row.sleep_stages_json = json.dumps(stages)
                    log.debug(f"garmin wellness sync: {len(stages)} sleep stage segments for {date_str}")
            except Exception as e:
                if _is_rate_limit_error(e):
                    raise
                log.debug(f"garmin wellness sync: get_sleep_data failed for {date_str}: {e}")

            row.wellness_synced_at = datetime.now(timezone.utc).isoformat()
            db.merge(row)
            db.commit()
            synced += 1
            if progress_cb:
                progress_cb(f"Synced wellness for {date_str}")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return synced


def _download_fit_bytes(client, activity_id):
    """One Garmin API call: downloads+unzips the raw original .FIT file — the unprocessed
    device recording, not a Garmin Connect API reprocessing. Degrades to None on any
    failure (no original file for a manually-entered activity, no .FIT member in the zip,
    network error) EXCEPT a genuine rate-limit error, which re-raises so
    _process_activity_with_retry can back off and retry instead of this call silently
    eating the signal a rate limit even happened."""
    try:
        raw = client.download_activity(
            str(activity_id), dl_fmt=client.ActivityDownloadFormat.ORIGINAL
        )
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            fit_names = [n for n in z.namelist() if n.lower().endswith(".fit")]
            return z.read(fit_names[0]) if fit_names else None
    except Exception as e:
        if _is_rate_limit_error(e):
            raise
        return None


def _semicircles_to_degrees(value):
    """FIT position_lat/position_long are encoded in semicircles, not degrees — a UNIT
    difference, not a scale/offset one, so fitparse does NOT auto-convert it (it only
    auto-applies the FIT profile's scale/offset to other fields). Getting this wrong
    produces huge nonsense lat/lon values, not a subtle bug: the first real converted
    point should land within -90..90 / -180..180 — worth checking once real data flows."""
    return None if value is None else value * SEMICIRCLE_TO_DEGREES


def _parse_fit_records(fit) -> list:
    """One pass over every `record` message — the raw ~1Hz device stream. Each point
    keeps every field together (lat/lon/hr/cadence/speed/altitude/distance/time), unlike
    Strava's parallel-array streams, so decimation needs no index-alignment bookkeeping.
    A missing individual field degrades to None on that point, not a dropped point — an
    HR-strap dropout mid-run shouldn't also cost the GPS point."""
    points = []
    for msg in fit.get_messages("record"):
        f = {field.name: field.value for field in msg}
        points.append(
            {
                "lat": _semicircles_to_degrees(f.get("position_lat")),
                "lon": _semicircles_to_degrees(f.get("position_long")),
                "hr": f.get("heart_rate"),
                "cadence": f.get("cadence"),
                "speed_mps": f.get("enhanced_speed")
                if f.get("enhanced_speed") is not None
                else f.get("speed"),
                "alt_m": f.get("enhanced_altitude")
                if f.get("enhanced_altitude") is not None
                else f.get("altitude"),
                "dist_m": f.get("distance"),
                "t": f.get("timestamp"),
            }
        )
    return points


def _calibrate_cadence_scale(raw_cadence_values: list, known_avg_total_spm) -> float:
    """FIT record cadence is a known ambiguity — some devices/firmwares report per-leg
    cadence (needs x2, like Strava's raw stream), others report total steps/min already.
    Self-calibrates against this activity's own trusted summary field
    (averageRunningCadenceInStepsPerMinute, already fetched elsewhere and already used
    unmultiplied) as ground truth, instead of guessing — avoids needing a live test
    against real hardware to resolve which convention this account's device uses."""
    if not raw_cadence_values or not known_avg_total_spm:
        return 1.0
    raw_mean = sum(raw_cadence_values) / len(raw_cadence_values)
    if raw_mean <= 0:
        return 1.0
    ratio = known_avg_total_spm / raw_mean
    return 2.0 if 1.6 <= ratio <= 2.4 else 1.0


def _decimate_fit_route_metrics(
    geo_points: list, known_avg_cadence, max_points: int = 300
) -> list:
    """Mirrors strava._build_route_metrics()'s exact output shape
    ({lat, lon, paceSecPerMi, hr, cadence, gradePct}) and decimation strategy
    (step = n // max_points) so app.js's existing heatmap code needs zero frontend
    changes to light up for Garmin runs too."""
    n = len(geo_points)
    if n == 0:
        return []
    step = max(1, n // max_points)
    cadence_scale = _calibrate_cadence_scale(
        [p["cadence"] for p in geo_points if p.get("cadence") is not None],
        known_avg_cadence,
    )
    if cadence_scale != 1.0:
        log.info(
            f"garmin FIT cadence calibration: scaling x{cadence_scale} (device reports per-leg cadence)"
        )

    result = []
    prev_dist = prev_alt = prev_t = None
    for i in range(0, n, step):
        p = geo_points[i]
        speed, dist_m, alt_m, t = (
            p.get("speed_mps"),
            p.get("dist_m"),
            p.get("alt_m"),
            p.get("t"),
        )

        pace = round(METERS_PER_MILE / speed, 1) if speed and speed > 0.3 else None
        if (
            pace is None
            and dist_m is not None
            and prev_dist is not None
            and t
            and prev_t
        ):
            d_delta, t_delta = dist_m - prev_dist, (t - prev_t).total_seconds()
            if d_delta > 2 and t_delta > 0:
                pace = round((t_delta / d_delta) * METERS_PER_MILE, 1)

        grade = None
        if dist_m is not None and alt_m is not None and prev_dist is not None:
            d_delta = dist_m - prev_dist
            if d_delta > 2:
                grade = round((alt_m - prev_alt) / d_delta * 100, 1)
        if dist_m is not None and alt_m is not None:
            prev_dist, prev_alt = dist_m, alt_m
        if t is not None:
            prev_t = t

        result.append(
            {
                "lat": p["lat"],
                "lon": p["lon"],
                "paceSecPerMi": pace,
                "hr": p.get("hr"),
                "cadence": round(p["cadence"] * cadence_scale)
                if p.get("cadence") is not None
                else None,
                "gradePct": grade,
            }
        )
    return result


def _parse_fit_streams(
    fit_bytes: bytes, known_avg_cadence=None, max_route_points: int = 300
) -> dict:
    """Single parse of one already-downloaded FIT file, returning everything
    _process_activity needs in one call: running dynamics (session message, same fields
    as before) plus the true device-recorded route/routeMetrics (record messages) —
    which, unlike Garmin Connect's geoPolylineDTO summary API, isn't subject to whatever
    privacy-zone masking clips the start/end of routes returned by the summary API (see
    STATUS.md for the ~500m discrepancy that surfaced this). UNVERIFIED against a live
    download as of this writing (Garmin's been rate-limited all session) — field names
    follow the public FIT SDK profile but degrade message-by-message and field-by-field
    rather than raising; an empty/partial result is the worst case, never a crash."""
    result = {"dynamics": {}, "route": [], "routeMetrics": []}
    try:
        import fitparse

        fit = fitparse.FitFile(io.BytesIO(fit_bytes))
    except Exception:
        return result

    try:
        for msg in fit.get_messages("session"):
            f = {field.name: field.value for field in msg}
            step_length_mm = f.get("avg_step_length")
            result["dynamics"] = {
                "verticalOscillationMm": f.get("avg_vertical_oscillation"),
                "groundContactTimeMs": f.get("avg_stance_time"),
                "verticalRatioPct": f.get("avg_vertical_ratio"),
                "strideLengthM": (step_length_mm / 1000) if step_length_mm else None,
                "avgPowerWatts": f.get("avg_power"),
            }
            break
    except Exception:
        pass

    try:
        raw_points = _parse_fit_records(fit)
        geo_points = [
            p
            for p in raw_points
            if p.get("lat") is not None and p.get("lon") is not None
        ]
        route_metrics = _decimate_fit_route_metrics(
            geo_points, known_avg_cadence, max_route_points
        )
        result["routeMetrics"] = route_metrics
        result["route"] = [[p["lat"], p["lon"]] for p in route_metrics]
        log.info(
            f"garmin FIT record parse: {len(raw_points)} raw records, {len(geo_points)} with GPS, "
            f"{len(route_metrics)} decimated route points"
        )
    except Exception:
        pass

    return result


def _fetch_exercise_sets(client, activity_id) -> list:
    """Set-by-set breakdown for a strength_training activity: each ACTIVE set becomes
    one entry, REST gaps are dropped (the Run's own moving_time_sec/duration already
    account for total elapsed time, so rest doesn't need its own row in what's meant to
    be a compact per-set table). Garmin's exercise auto-detection is genuinely
    unreliable — often "UNKNOWN" at <50% confidence — until the user manually corrects
    it in the Garmin Connect app, so this captures whatever's on record at sync time,
    good or bad, rather than trying to second-guess it here."""
    try:
        data = client.get_activity_exercise_sets(activity_id)
    except Exception as e:
        if _is_rate_limit_error(e):
            raise
        return []
    sets = []
    for raw in data.get("exerciseSets", []):
        if raw.get("setType") != "ACTIVE":
            continue
        exercises = raw.get("exercises") or []
        best = max(exercises, key=lambda e: e.get("probability") or 0, default={})
        name = best.get("name") or best.get("category")
        exercise = name.replace("_", " ").title() if name else "Unknown exercise"
        weight_g = raw.get("weight")
        sets.append({
            "exercise": exercise,
            "reps": raw.get("repetitionCount"),
            "weightLb": round(weight_g / GRAMS_PER_LB, 1) if weight_g else None,
            "durationSec": round(raw["duration"]) if raw.get("duration") else None,
        })
    return sets


def _fetch_adaptive_plan_workouts(client, start_date: str, end_date: str) -> list:
    """Garmin's adaptive-coach suggested workouts for any active running plan, if one
    exists — raw fetch + unit conversion only; coach.py owns the semantic workout_type
    mapping and the upsert-with-change-tracking logic (see
    coach.sync_garmin_suggested_workouts). Non-fatal on any failure: not every account
    has an active adaptive plan, and this should never block the rest of a sync over it.
    """
    from ..coach import core as coach
    try:
        plans = client.get_training_plans()
    except Exception as e:
        if _is_rate_limit_error(e):
            raise
        return []
    active_plans = [
        p for p in plans.get("trainingPlanList", [])
        if p.get("trainingStatus", {}).get("statusKey") == "Scheduled"
        and p.get("trainingPlanCategory") == "FBT_ADAPTIVE"
    ]
    entries = []
    for plan in active_plans:
        try:
            full_plan = client.get_adaptive_training_plan_by_id(plan["trainingPlanId"])
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            continue
        for task in full_plan.get("taskList", []):
            date = task.get("calendarDate")
            if not date or not (start_date <= date <= end_date):
                continue
            tw = task.get("taskWorkout") or {}
            if tw.get("restDay"):
                entries.append({
                    "scheduledDate": date, "workoutType": "rest", "activityType": "Other",
                    "notes": "Rest day (Garmin adaptive plan)",
                    "garminWorkoutUuid": tw.get("workoutUuid") or f"restday_{date}",
                })
                continue
            if not tw.get("workoutUuid"):
                continue
            phrase = tw.get("workoutPhrase") or tw.get("trainingEffectLabel") or ""
            sport_key = (tw.get("sportType") or {}).get("sportTypeKey", "running")
            distance_m = tw.get("estimatedDistanceInMeters")
            desc = tw.get("workoutDescription")
            entries.append({
                "scheduledDate": date,
                "workoutType": coach.GARMIN_WORKOUT_PHRASE_MAP.get(phrase, "easy"),
                "activityType": coach.GARMIN_SPORT_TYPE_MAP.get(sport_key, "Run"),
                "targetDistanceMi": round(distance_m / METERS_PER_MILE, 2) if distance_m else None,
                "targetDurationSec": tw.get("estimatedDurationInSecs"),
                "notes": f"Garmin adaptive plan: {tw.get('workoutName') or phrase}" + (f" — {desc}" if desc else ""),
                "garminWorkoutUuid": tw.get("workoutUuid"),
            })
    return entries


def _process_activity(act: dict, client, db, user_id: str) -> bool:
    """Fetch splits/weather for one Garmin activity of any type and upsert it as a Run row.
    Always returns True — every activity type is captured, not just running; only running
    activities get the running-specific classification/interval-detection heuristics."""
    activity_type = act.get("activityType", {}).get("typeKey") or "unknown"
    is_run = "running" in activity_type

    run_id = resolve_run_id(db, "garmin", act['activityId'], user_id)
    distance_mi = (act.get("distance") or 0) / METERS_PER_MILE
    # movingDuration is accelerometer-detected active movement — meaningful for a run
    # (distinguishes real pauses from actual pace) but wrong for strength training,
    # where standing still resting between sets is part of the workout, not a pause.
    # A real session synced at movingDuration=1012s/17min when the actual elapsed
    # session (duration) was 3737s/62min is exactly this: use total elapsed time for
    # strength_training instead.
    moving_time = act.get("duration") if activity_type == "strength_training" \
        else (act.get("movingDuration") or act.get("duration") or 0)
    avg_pace = moving_time / distance_mi if distance_mi else None
    is_treadmill = "treadmill" in (act.get("activityType", {}).get("typeKey") or "")

    # strength_training never has GPS/mile-splits (confirmed against real activities:
    # distance is always 0 for these) — get_activity_splits/get_activity_details would
    # just return empty structures at the cost of 2 real API calls, so skip them
    # entirely for this activity type instead of discovering the same "nothing here"
    # result the expensive way. Not extended to other non-running types (e.g. yoga can
    # carry incidental GPS/movement data) — only where empty is a certainty.
    skip_gps_calls = activity_type == "strength_training"

    splits = []
    if not skip_gps_calls:
        try:
            laps = client.get_activity_splits(act["activityId"])
            lap_dtos = laps.get("lapDTOs", [])
            for i, lap in enumerate(lap_dtos, 1):
                lap_dist_mi = (lap.get("distance") or 0) / METERS_PER_MILE
                lap_time = lap.get("movingDuration") or lap.get("duration") or 0
                if lap_dist_mi > 0:
                    splits.append(
                        {
                            "mile": i,
                            "paceSecPerMi": round(lap_time / lap_dist_mi, 1),
                            "elevGainFt": round(
                                (lap.get("elevationGain") or 0) * 3.28084, 1
                            ),
                            "avgHR": round(lap["averageHR"])
                            if lap.get("averageHR")
                            else None,
                            "maxHR": round(lap["maxHR"]) if lap.get("maxHR") else None,
                            # Garmin's runningCadenceInStepsPerMinute is already total spm — no doubling needed
                            "avgCadence": lap.get("averageRunCadence"),
                        }
                    )
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            # else: fall through, splits stays []

    polyline_route = []
    if not skip_gps_calls:
        try:
            details = client.get_activity_details(act["activityId"], maxpoly=500)
            polyline_route = [
                [p["lat"], p["lon"]]
                for p in details.get("geoPolylineDTO", {}).get("polyline", [])
                if p.get("lat") is not None and p.get("lon") is not None
            ]
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            # else: fall through, polyline_route stays []

    # The geoPolylineDTO fetch above doubles as a free "does this even have GPS" signal
    # before spending a FIT download on it — see GARMIN_FIT_ROUTE_ALL_GPS.
    should_try_fit = is_run or (GARMIN_FIT_ROUTE_ALL_GPS and len(polyline_route) >= 2)
    fit_data = {"dynamics": {}, "route": [], "routeMetrics": []}
    if should_try_fit:
        fit_bytes = _download_fit_bytes(client, act["activityId"])
        if fit_bytes:
            fit_data = _parse_fit_streams(
                fit_bytes,
                known_avg_cadence=act.get("averageRunningCadenceInStepsPerMinute"),
            )

    if len(fit_data["route"]) >= 2:
        route, route_metrics, route_source = (
            fit_data["route"],
            fit_data["routeMetrics"],
            "fit_record_stream",
        )
    elif polyline_route:
        route, route_metrics, route_source = polyline_route, [], "geopolyline_summary"
    else:
        route, route_metrics, route_source = [], [], "none"
    log.info(
        f"garmin activity {act.get('activityId')}: route_source={route_source} ({len(route)} points)"
    )

    dynamics = fit_data["dynamics"]

    run_type = (
        classify_run_type(distance_mi, avg_pace, splits, act.get("averageHR"))
        if is_run
        else activity_type
    )

    exercise_sets = (
        _fetch_exercise_sets(client, act["activityId"])
        if activity_type == "strength_training"
        else []
    )

    intervals_json = "[]"
    if is_run and run_type == "Interval" and splits:
        raw_laps = [
            {
                "durationSec": None,
                "distanceMi": None,
                "paceSecPerMi": s["paceSecPerMi"],
                "elevGainFt": s["elevGainFt"],
                "avgHR": s["avgHR"],
                "maxHR": s["maxHR"],
                "avgCadence": s["avgCadence"],
            }
            for s in splits
        ]
        intervals_json = json.dumps(detect_intervals(raw_laps))

    start_local = act.get("startTimeLocal", "")
    try:
        start_dt = datetime.fromisoformat(start_local)
    except Exception:
        start_dt = datetime.now()

    temp_f, condition, heat_index_f, wet_bulb_f = None, None, None, None
    if not is_treadmill:
        lat, lon = act.get("startLatitude"), act.get("startLongitude")
        if lat and lon:
            temp_f, condition, heat_index_f, wet_bulb_f = get_historical_weather(
                lat, lon, start_dt.strftime("%Y-%m-%d"), start_dt.hour
            )

    existing = db.get(Run, run_id)
    run = existing or Run(id=run_id)
    run.user_id = user_id
    run.source = "garmin"
    run.activity_type = _normalize_activity_type(activity_type)
    run.date = start_dt.strftime("%Y-%m-%d")
    run.start_time = start_dt.strftime("%H:%M")
    run.name = act.get("activityName", "Run")
    run.distance_mi = round(distance_mi, 3)
    run.moving_time_sec = int(moving_time)
    run.elev_gain_ft = round((act.get("elevationGain") or 0) * 3.28084, 1)
    run.avg_hr = round(act["averageHR"]) if act.get("averageHR") else None
    run.max_hr = round(act["maxHR"]) if act.get("maxHR") else None
    run.avg_cadence = act.get("averageRunningCadenceInStepsPerMinute")
    run.avg_pace_sec_per_mi = round(avg_pace, 1) if avg_pace else None
    run.is_treadmill = is_treadmill
    run.temp_f = temp_f
    run.weather_condition = condition
    run.heat_index_f = heat_index_f
    run.wet_bulb_f = wet_bulb_f
    run.suggested_type = run_type
    run.splits_json = json.dumps(splits)
    run.intervals_json = intervals_json
    run.recovery_json = "[]"  # interval recovery-time is Strava-only for now (needs lap start/end stream indices)
    run.route_json = json.dumps(route)
    run.route_metrics_json = json.dumps(route_metrics)
    run.route_source = route_source
    run.vertical_oscillation_mm = dynamics.get("verticalOscillationMm")
    run.ground_contact_time_ms = dynamics.get("groundContactTimeMs")
    run.vertical_ratio_pct = dynamics.get("verticalRatioPct")
    run.stride_length_m = dynamics.get("strideLengthM")
    run.avg_power_watts = dynamics.get("avgPowerWatts")
    run.exercise_sets_json = json.dumps(exercise_sets) if exercise_sets else None
    run.detail_synced_at = datetime.now(timezone.utc).isoformat()

    # Phase 6.1 — computed once at sync time, same discipline as GAP/run-type above.
    training_config = db.get(UserTrainingConfig, user_id)
    threshold_hr = training_config.threshold_hr if training_config else None
    run.tss = compute_tss(run.moving_time_sec, run.avg_hr, threshold_hr, run.suggested_type, activity_family(run.activity_type))
    run.efficiency_factor = compute_efficiency_factor(run.avg_pace_sec_per_mi, run.avg_hr)

    # Phase 6.3 — only fills in gear_id if unset, never overwrites a manual reassignment.
    stats.assign_default_gear(db, run, user_id)

    db.merge(run)
    return True


def _process_activity_with_retry(act, client, db, user_id, progress_cb=None) -> bool:
    """Wraps _process_activity with rate-limit-aware retry/backoff plus a proactive
    inter-activity delay. Non-rate-limit errors keep the original skip-and-continue
    behavior. A rate-limit error that survives GARMIN_MAX_RETRIES attempts re-raises
    unchanged, so callers' existing except-block still turns it into
    GarminMidSyncRateLimitError with an accurate partial-progress message — this only
    changes *when* that happens, not the contract callers rely on. Retrying re-runs
    _process_activity from scratch (redoes splits+route too, not just whichever call
    429'd) — an accepted simplification since every write is an idempotent db.merge()."""
    for attempt in range(GARMIN_MAX_RETRIES + 1):
        try:
            processed = _process_activity(act, client, db, user_id)
            time.sleep(GARMIN_INTER_ACTIVITY_DELAY_SEC)
            return processed
        except Exception as e:
            if not _is_rate_limit_error(e):
                if progress_cb:
                    progress_cb(f"Skipped {act.get('activityName', 'activity')}: {e}")
                time.sleep(GARMIN_INTER_ACTIVITY_DELAY_SEC)
                return False
            if attempt >= GARMIN_MAX_RETRIES:
                raise
            wait = GARMIN_RETRY_BASE_BACKOFF_SEC * (2**attempt)
            if progress_cb:
                progress_cb(
                    f"Rate-limited, retrying in {wait:.0f}s (attempt {attempt + 1}/{GARMIN_MAX_RETRIES})…"
                )
            time.sleep(wait)
    return False


def _maybe_batch_pause(detail_fetch_count: int, progress_cb=None):
    """Called right before the (detail_fetch_count+1)-th genuine detail fetch in a run
    — dedup skips don't count, since they cost no API calls. Every GARMIN_DETAIL_BATCH_SIZE-th
    one, pause for GARMIN_BATCH_PAUSE_SEC first, spreading real API load out over time
    instead of bursting through activities back-to-back."""
    if detail_fetch_count > 0 and detail_fetch_count % GARMIN_DETAIL_BATCH_SIZE == 0:
        log.debug(f"garmin batch pause: {GARMIN_BATCH_PAUSE_SEC:.0f}s after {detail_fetch_count} detail fetches")
        if progress_cb:
            progress_cb(
                f"Pausing {GARMIN_BATCH_PAUSE_SEC:.0f}s after {detail_fetch_count} activities to ease off Garmin's rate limit…"
            )
        time.sleep(GARMIN_BATCH_PAUSE_SEC)


def _garmin_cooldown_remaining_sec(user_id: str) -> float:
    """Seconds left in an active cross-invocation rate-limit cooldown, or 0 if none/expired.
    Namespaced per-user (Phase 1.4) — each user's Garmin credential gets independent
    cooldown tracking, even though Garmin's actual rate limit is arguably account-level
    (a fine tradeoff vs. a cross-user leak; two RunLog users sharing one real Garmin
    account would just get slightly redundant cooldowns, never a wrong one)."""
    until = get_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY))
    if not until:
        return 0.0
    try:
        until_dt = datetime.fromisoformat(until)
    except Exception:
        return 0.0
    return max(0.0, (until_dt - datetime.now(timezone.utc)).total_seconds())


def _raise_if_garmin_cooling_down(user_id: str):
    remaining = _garmin_cooldown_remaining_sec(user_id)
    log.debug(f"garmin cooldown check: {remaining:.0f}s remaining")
    if remaining > 0:
        mins = int(remaining // 60) + 1
        raise RuntimeError(
            f"Garmin rate-limit cooldown active — wait ~{mins} more minute{'s' if mins != 1 else ''} before retrying"
        )


def _record_garmin_rate_limit_hit(user_id: str):
    failures = int(get_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_FAILURES_KEY)) or "0") + 1
    cooldown = min(GARMIN_COOLDOWN_BASE_SEC * (2 ** (failures - 1)), GARMIN_COOLDOWN_MAX_SEC)
    until = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
    set_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_FAILURES_KEY), str(failures))
    set_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY), until.isoformat())
    log.debug(f"garmin rate limit hit #{failures} — cooldown set for {cooldown:.0f}s (until {until.isoformat()})")


def _clear_garmin_rate_limit_cooldown(user_id: str):
    set_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_FAILURES_KEY), "0")
    set_sync_meta(user_key(user_id, GARMIN_RATE_LIMIT_COOLDOWN_UNTIL_KEY), "")
    log.debug("garmin rate limit cooldown cleared")


def sync_garmin_activities(user_id: str, limit: int = 10, progress_cb=None):
    _raise_if_garmin_cooling_down(user_id)
    try:
        client = _login(user_id)
    except GarminLoginRateLimitError:
        _record_garmin_rate_limit_hit(user_id)
        raise

    # Run these BEFORE the activity loop, not after — every real sync so far has hit
    # the rate limit partway through activities, and a GarminMidSyncRateLimitError
    # raised from that loop exits the function before ever reaching code placed after
    # it. Steps is a cheap, independent, self-contained call (degrades to a no-op on
    # failure — see its own docstring) so there's no cost to giving it first crack at
    # the API budget. Resting HR used to have its own separate get_rhr_day() call here
    # too, but _sync_daily_wellness's get_stats() already covers it (and more) per day
    # — a second live call for the same conceptual value was pure redundant API load.
    steps = _sync_daily_steps(client, user_id)
    if progress_cb and steps:
        progress_cb(f"Synced {steps} days of step data")

    # Same "cheap, independent, before the activity loop" reasoning as steps above —
    # runs (and no-ops) on every quick sync until threshold_hr is actually set, so
    # this activity batch's own TSS calc below can benefit from it immediately
    # rather than waiting for a second sync.
    _maybe_populate_threshold_hr(client, user_id)

    # Adaptive-plan suggested workouts, if any — cheap (2 calls total, not per-day) and
    # independent, same reasoning as steps above. Garmin can revise a suggestion right
    # up until the workout starts, so capturing it is the whole point — but throttled
    # (GARMIN_ADAPTIVE_PLAN_RECHECK_MIN_SEC) rather than unconditional every sync, so
    # repeated manual syncs close together don't each burn 2 calls on a plan that
    # hasn't had time to change. See coach.sync_garmin_suggested_workouts for the
    # change-preserving upsert logic.
    last_plan_check = get_sync_meta(user_key(user_id, GARMIN_ADAPTIVE_PLAN_LAST_CHECKED_KEY))
    if not last_plan_check or _seconds_since(last_plan_check) >= GARMIN_ADAPTIVE_PLAN_RECHECK_MIN_SEC:
        try:
            today = local_today(user_id)
            window_end = (today + timedelta(days=6)).isoformat()
            plan_entries = _fetch_adaptive_plan_workouts(client, today.isoformat(), window_end)
            set_sync_meta(user_key(user_id, GARMIN_ADAPTIVE_PLAN_LAST_CHECKED_KEY), datetime.now(timezone.utc).isoformat())
            if plan_entries:
                from ..coach import core as coach
                db = SessionLocal()
                try:
                    n = coach.sync_garmin_suggested_workouts(db, plan_entries, user_id)
                finally:
                    db.close()
                if progress_cb and n:
                    progress_cb(f"Synced {n} suggested workout(s) from Garmin's adaptive plan")
        except Exception as e:
            if _is_rate_limit_error(e):
                raise
            log.debug(f"garmin adaptive plan sync skipped/failed: {e}")

    # Quick sync only ever covers the volatile window (today's/yesterday's wellness data
    # can still change) — the deeper historical backfill is sync_all_garmin_activities'
    # job, since it needs a much bigger day-by-day API budget (see GARMIN_WELLNESS_BACKFILL_DAYS).
    try:
        _sync_daily_wellness(
            client, user_id, days=GARMIN_WELLNESS_VOLATILE_DAYS, progress_cb=progress_cb
        )
    except Exception as e:
        if progress_cb:
            progress_cb("Stopped — Garmin API rate limit hit during wellness sync")
        _record_garmin_rate_limit_hit(user_id)
        raise GarminMidSyncRateLimitError(0) from e

    activities = client.get_activities(0, limit)
    log.debug(f"garmin quick sync: fetched {len(activities)} activities (limit={limit})")

    db = SessionLocal()
    count = 0
    detail_fetches = 0
    try:
        for act in activities:
            # Newest-first list: the first already-synced activity means everything
            # after it is too — stop instead of re-fetching full details (splits,
            # route, FIT download) for activities already stored. This is what makes
            # "quick sync" naturally mean "today's/recent new data only", and it's
            # the single highest-leverage fix for hitting Garmin's rate limit, since
            # the per-activity detail calls (not the cheap activity-list call) are
            # what actually cost API budget.
            if not run_needs_detail_sync(db, resolve_run_id(db, "garmin", act['activityId'], user_id)):
                log.debug(f"garmin quick sync: activity {act.get('activityId')} already synced — stopping")
                break
            log.debug(f"garmin quick sync: activity {act.get('activityId')} needs detail fetch")
            _maybe_batch_pause(detail_fetches, progress_cb)
            detail_fetches += 1
            try:
                processed = _process_activity_with_retry(
                    act, client, db, user_id, progress_cb
                )
            except Exception as e:
                db.commit()
                if progress_cb:
                    progress_cb(
                        f"Stopped — Garmin API rate limit hit mid-sync ({count} runs saved)"
                    )
                _record_garmin_rate_limit_hit(user_id)
                raise GarminMidSyncRateLimitError(count) from e
            if processed:
                count += 1
                if progress_cb:
                    progress_cb(f"Synced {act.get('activityName', 'run')}", count)
        db.commit()
    finally:
        db.close()

    _clear_garmin_rate_limit_cooldown(user_id)
    return count


GARMIN_ACTIVITIES_BACKLOG_OFFSET_KEY = "garmin_activities_backlog_offset"
GARMIN_ACTIVITIES_BACKLOG_COMPLETE_KEY = "garmin_activities_backlog_complete"


def sync_all_garmin_activities(user_id: str, progress_cb=None):
    """Backlog sync — pages through the athlete's entire Garmin activity history.
    Commits incrementally so progress survives if the run is interrupted.

    Resumes from a persisted cursor (sync_meta's GARMIN_ACTIVITIES_BACKLOG_OFFSET_KEY)
    instead of restarting at offset 0 every single run — once a page of history has
    been fully walked (every activity in it individually confirmed via
    run_needs_detail_sync, not just assumed from the first item), there's no reason to
    keep re-listing/skip-checking through it again on the next run. Once the true end
    of history is reached, GARMIN_ACTIVITIES_BACKLOG_COMPLETE_KEY is set and future
    calls return immediately — new activities always arrive at the *front* of Garmin's
    list, which sync_garmin_activities (quick sync) already checks every time via its
    own break-on-first-known logic, so a completed historical walk doesn't need to be
    repeated just because time passed. (No UI to force a fresh full walk yet — clearing
    these two sync_meta keys directly is the manual escape hatch if ever needed.)"""
    _raise_if_garmin_cooling_down(user_id)
    try:
        client = _login(user_id)
    except GarminLoginRateLimitError:
        _record_garmin_rate_limit_hit(user_id)
        raise

    # See sync_garmin_activities for why steps runs first, not last. Resting HR no longer
    # has its own separate sync call here — see the same note in sync_garmin_activities.
    steps = _sync_daily_steps(client, user_id, days=365)
    if progress_cb and steps:
        progress_cb(f"Synced {steps} days of step data")

    # Deeper historical wellness backfill than quick sync's volatile-window-only check —
    # capped at GARMIN_WELLNESS_BACKFILL_DAYS rather than full history (see its own
    # docstring for why). Independent of the activity backlog's completion flag below —
    # wellness has its own per-day dedup, so it keeps making progress across repeated
    # backlog runs even after activities are fully walked.
    try:
        _sync_daily_wellness(
            client, user_id, days=GARMIN_WELLNESS_BACKFILL_DAYS, progress_cb=progress_cb
        )
    except Exception as e:
        if progress_cb:
            progress_cb("Stopped — Garmin API rate limit hit during wellness sync")
        _record_garmin_rate_limit_hit(user_id)
        raise GarminMidSyncRateLimitError(0) from e

    if get_sync_meta(user_key(user_id, GARMIN_ACTIVITIES_BACKLOG_COMPLETE_KEY)) == "1":
        log.debug("garmin backlog sync: already marked complete, returning early")
        if progress_cb:
            progress_cb("Activity history already fully walked — nothing left to confirm")
        return 0

    batch = 5
    start = int(get_sync_meta(user_key(user_id, GARMIN_ACTIVITIES_BACKLOG_OFFSET_KEY)) or "0")
    log.debug(f"garmin backlog sync: starting at offset {start}, batch size {batch}")
    if start and progress_cb:
        progress_cb(f"Resuming backlog walk at offset {start}")

    db = SessionLocal()
    count = 0
    detail_fetches = 0
    try:
        while True:
            activities = client.get_activities(start, batch)
            log.debug(f"garmin backlog sync: page at offset {start} returned {len(activities)} activities")
            if not activities:
                set_sync_meta(user_key(user_id, GARMIN_ACTIVITIES_BACKLOG_COMPLETE_KEY), "1")
                log.debug("garmin backlog sync: empty page — marking complete")
                if progress_cb:
                    progress_cb("Reached the end of activity history — backlog walk complete")
                break
            if progress_cb:
                progress_cb(f"Fetched {len(activities)} activities (offset {start})")

            skipped = 0
            for act in activities:
                # Backlog sync still walks full history (confirming nothing's
                # missing via this cheap paginated list call) but skips the
                # expensive detail fetch entirely for anything already stored.
                if not run_needs_detail_sync(db, resolve_run_id(db, "garmin", act['activityId'], user_id)):
                    skipped += 1
                    continue
                log.debug(f"garmin backlog sync: activity {act.get('activityId')} needs detail fetch")
                _maybe_batch_pause(detail_fetches, progress_cb)
                detail_fetches += 1
                try:
                    processed = _process_activity_with_retry(
                        act, client, db, user_id, progress_cb
                    )
                except Exception as e:
                    db.commit()
                    if progress_cb:
                        progress_cb(
                            f"Stopped — Garmin API rate limit hit mid-sync ({count} runs saved)"
                        )
                    _record_garmin_rate_limit_hit(user_id)
                    raise GarminMidSyncRateLimitError(count) from e
                if processed:
                    count += 1
                    db.commit()
                    if progress_cb:
                        progress_cb(f"Synced {act.get('activityName', 'run')}", count)

            if progress_cb and skipped:
                progress_cb(
                    f"Skipped {skipped} already-synced activities (offset {start})"
                )

            # This page is now fully resolved (every item either synced or already
            # known) — safe to persist the cursor past it. If a rate limit struck
            # mid-page, the exception above already exited the function before this
            # line runs, so the cursor stays put and the next run re-attempts this
            # same page instead of skipping over unfinished work.
            start += len(activities)
            set_sync_meta(user_key(user_id, GARMIN_ACTIVITIES_BACKLOG_OFFSET_KEY), str(start))
            log.debug(f"garmin backlog sync: page done ({skipped} skipped), cursor advanced to {start}")

            if len(activities) < batch:
                set_sync_meta(user_key(user_id, GARMIN_ACTIVITIES_BACKLOG_COMPLETE_KEY), "1")
                log.debug("garmin backlog sync: short page — marking complete")
                if progress_cb:
                    progress_cb("Reached the end of activity history — backlog walk complete")
                break
        db.commit()
    finally:
        db.close()

    _clear_garmin_rate_limit_cooldown(user_id)
    return count
