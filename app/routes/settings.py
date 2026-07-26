"""API tokens (device/headless-client auth), Web Push subscriptions, app config, and
reverse-geocoding for the Map tab's location labels."""
import time
import uuid
import hashlib
import secrets
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import SessionLocal, ApiToken, User, get_sync_meta, set_sync_meta
from ..accounts import auth
from .. import stats
from ..util import APP_TIMEZONE
from .sync import SYNC_INTERVAL_HOURS, SYNC_LIMIT

router = APIRouter()


# ---------- API tokens (Phase 1.5 — device/headless-client auth, e.g. the Android
# client planned for Phase 3, or any script hitting the ingest endpoint planned for
# Phase 2.2). Meaningful even with AUTH_MODE=disabled: the tokens themselves are
# created and listed now, ready to actually authenticate the moment AUTH_MODE is
# turned on and a caller starts sending X-Api-Token — see app/accounts/auth.py. ----------
@router.get("/api/tokens")
def list_tokens(user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        rows = db.query(ApiToken).filter(ApiToken.user_id == user_id).order_by(ApiToken.created_at.desc()).all()
        return [{"id": t.id, "name": t.name, "createdAt": t.created_at, "lastUsedAt": t.last_used_at} for t in rows]
    finally:
        db.close()


@router.post("/api/tokens")
async def create_token(request: Request, user_id: str = Depends(auth.current_user_id)):
    """Returns the raw token exactly once — only its SHA-256 hash is ever persisted
    (see ApiToken/auth._verify_api_token), so this response is the only chance to see it."""
    body = await request.json()
    name = (body.get("name") or "").strip() or None
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db = SessionLocal()
    try:
        row = ApiToken(
            id=f"tok_{uuid.uuid4().hex[:12]}", user_id=user_id, token_hash=token_hash,
            name=name, created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(row)
        db.commit()
        return {"id": row.id, "name": row.name, "createdAt": row.created_at, "token": raw_token}
    finally:
        db.close()


@router.delete("/api/tokens/{token_id}")
def delete_token(token_id: str, user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        row = db.query(ApiToken).filter(ApiToken.id == token_id, ApiToken.user_id == user_id).first()
        if row:
            db.delete(row)
            db.commit()
        return {"deleted": True}
    finally:
        db.close()


# ---------- Web Push (Phase 0.11, PWA push notifications) ----------
@router.get("/api/push/vapid-public-key")
def get_vapid_public_key():
    """Unauthenticated on purpose — this is a public key by definition (it's handed to
    PushManager.subscribe() client-side), same non-secret status as an OAuth client id."""
    from .. import push
    return {"configured": push.is_configured(), "publicKey": push.VAPID_PUBLIC_KEY}


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, user_id: str = Depends(auth.current_user_id)):
    from .. import push
    body = await request.json()
    endpoint = body.get("endpoint")
    keys = body.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "endpoint and keys.p256dh/keys.auth are required")
    db = SessionLocal()
    try:
        push.subscribe(db, user_id, endpoint, keys["p256dh"], keys["auth"])
        return {"subscribed": True}
    finally:
        db.close()


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user_id: str = Depends(auth.current_user_id)):
    from .. import push
    body = await request.json()
    endpoint = body.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "endpoint is required")
    db = SessionLocal()
    try:
        push.unsubscribe(db, user_id, endpoint)
        return {"unsubscribed": True}
    finally:
        db.close()


@router.post("/api/push/test")
def push_test(user_id: str = Depends(auth.current_user_id)):
    """Sends a real notification to every device this user has subscribed — the one
    genuinely useful verification hook, independent of any feature (daily insight,
    generated workout) that would otherwise trigger a push, since neither exists yet."""
    from .. import push
    if not push.is_configured():
        raise HTTPException(400, "Push not configured — set VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY")
    db = SessionLocal()
    try:
        sent = push.send_push(db, user_id, "HALE", "Push notifications are working.", "/")
        return {"sent": sent}
    finally:
        db.close()


@router.get("/api/config")
def get_config(user_id: str = Depends(auth.current_user_id)):
    from .. import push
    from ..accounts import demo
    db = SessionLocal()
    try:
        resting_hr = stats.latest_resting_hr_bpm(db, user_id)
        is_demo_user = demo.is_demo_user(db, user_id)
        user = db.get(User, user_id)
        tz = (user.timezone if user else None) or APP_TIMEZONE
    finally:
        db.close()
    return {
        "syncIntervalHours": SYNC_INTERVAL_HOURS,
        "syncActivityLimit": SYNC_LIMIT,
        "restingHrBpm": resting_hr,
        "pushConfigured": push.is_configured(),
        "isDemoUser": is_demo_user,
        "timezone": tz,
    }


@router.patch("/api/config")
async def update_config(request: Request, user_id: str = Depends(auth.current_user_id)):
    """Only `timezone` is writable here (Phase 12.2) — everything else GET /api/config
    returns is either a read-only env-derived value (syncIntervalHours/syncActivityLimit/
    pushConfigured) or computed (restingHrBpm/isDemoUser), not user-settable config."""
    body = await request.json()
    if "timezone" not in body:
        raise HTTPException(400, "timezone is required")
    tz_name = body["timezone"]
    import zoneinfo
    if tz_name not in zoneinfo.available_timezones():
        raise HTTPException(400, f"unknown timezone: {tz_name!r}")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user:
            user.timezone = tz_name
            db.commit()
        return {"timezone": tz_name}
    finally:
        db.close()


def _profile_to_dict(user: User | None) -> dict:
    return {
        "heightIn": user.height_in if user else None,
        "weightLb": user.weight_lb if user else None,
        "dateOfBirth": user.date_of_birth if user else None,
        "sex": user.sex if user else None,
    }


_VALID_SEX = ("male", "female", "other")


@router.get("/api/profile")
def get_profile(user_id: str = Depends(auth.current_user_id)):
    """Body-metric profile fields — separate from GET/PATCH /api/config (that's
    app-level settings: sync interval, push, timezone). Currently just the four
    fields Phase 9.5's planned BMR estimate needs (see models.py's User columns);
    nothing reads these yet, but Settings lets the user fill them in ahead of time."""
    db = SessionLocal()
    try:
        return _profile_to_dict(db.get(User, user_id))
    finally:
        db.close()


@router.patch("/api/profile")
async def update_profile(request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(404, "user not found")
        if "heightIn" in body:
            v = body["heightIn"]
            if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                raise HTTPException(400, "heightIn must be a positive number or null")
            user.height_in = v
        if "weightLb" in body:
            v = body["weightLb"]
            if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                raise HTTPException(400, "weightLb must be a positive number or null")
            user.weight_lb = v
        if "dateOfBirth" in body:
            v = body["dateOfBirth"]
            if v is not None:
                try:
                    datetime.strptime(v, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(400, "dateOfBirth must be YYYY-MM-DD or null")
            user.date_of_birth = v
        sex_changed = False
        if "sex" in body:
            v = body["sex"]
            if v is not None and v not in _VALID_SEX:
                raise HTTPException(400, f"sex must be one of {_VALID_SEX} or null")
            sex_changed = user.sex != v
            user.sex = v
        db.commit()
        result = _profile_to_dict(user)
    finally:
        db.close()
    if sex_changed:
        # Sex is baked into the SDK's system prompt at client-construction time (Phase
        # 28's WOMENS_STRENGTH_PROMPT), same as coach_personality — reset so the next
        # chat message rebuilds it, mirroring set_coach_personality's identical pattern.
        from ..coach import assistant
        await assistant.reset_client(user_id)
    return result


# ---------- Geocoding (for the Map tab's location labels) ----------
_last_nominatim_call = 0.0


@router.get("/api/geocode")
def geocode(lat: float, lon: float):
    """Reverse-geocode a lat/lon to a place name, cached in sync_meta (shared across every
    browser/device, unlike a client-side cache) so each real-world location is only ever
    looked up once."""
    key = f"geocode_{lat:.2f}_{lon:.2f}"
    cached = get_sync_meta(key)
    if cached:
        return {"label": cached, "cached": True}

    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_nominatim_call = time.monotonic()

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "json", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
            headers={"User-Agent": "RunLog/1.0 (self-hosted personal running tracker)"},
            timeout=10,
        )
        data = resp.json()
        addr = data.get("address", {})
        place = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") \
            or (data.get("display_name") or "").split(",")[0]
        label = f"{place}, {addr['state']}" if place and addr.get("state") else (place or f"{lat:.2f}, {lon:.2f}")
    except Exception:
        label = f"{lat:.2f}, {lon:.2f}"

    set_sync_meta(key, label)
    return {"label": label, "cached": False}
