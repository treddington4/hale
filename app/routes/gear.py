"""Gear (shoes/bikes/bike-components) CRUD — Phase 6.3. Mileage/wear itself is
computed at read time by stats.gear_summary(), not stored here."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import SessionLocal, Gear, owned_by
from ..accounts import auth
from .. import stats

router = APIRouter()

_VALID_KINDS = ("shoe", "bike", "bike_component")


def _unset_other_defaults(db, user_id: str, kind: str, except_id: str | None = None):
    """At most one default gear item per (user, kind) — mirrors how Garmin/Strava
    auto-assignment (stats.assign_default_gear) expects to find at most one match."""
    q = db.query(Gear).filter(owned_by(Gear.user_id, user_id), Gear.kind == kind, Gear.is_default.is_(True))
    if except_id:
        q = q.filter(Gear.id != except_id)
    for g in q.all():
        g.is_default = False


@router.get("/api/gear")
def get_gear(user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        return stats.gear_summary(db, user_id)
    finally:
        db.close()


@router.post("/api/gear")
async def create_gear(request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    if body.get("kind") not in _VALID_KINDS:
        raise HTTPException(400, f"kind must be one of {_VALID_KINDS}")
    if body.get("kind") == "bike_component" and not body.get("parentGearId"):
        raise HTTPException(400, "bike_component requires parentGearId")
    db = SessionLocal()
    try:
        is_default = bool(body.get("isDefault"))
        if is_default:
            _unset_other_defaults(db, user_id, body["kind"])
        g = Gear(
            id=f"gear_{uuid.uuid4().hex[:12]}", user_id=user_id,
            name=body.get("name") or "Untitled gear", kind=body["kind"],
            parent_gear_id=body.get("parentGearId"), is_default=is_default,
            start_date=body.get("startDate") or datetime.now(timezone.utc).date().isoformat(),
            replace_at_mi=body.get("replaceAtMi"), notes=body.get("notes"),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(g)
        db.commit()
        return stats.gear_summary(db, user_id)
    finally:
        db.close()


@router.patch("/api/gear/{gear_id}")
async def update_gear(gear_id: str, request: Request, user_id: str = Depends(auth.current_user_id)):
    body = await request.json()
    db = SessionLocal()
    try:
        g = db.query(Gear).filter(Gear.id == gear_id, owned_by(Gear.user_id, user_id)).first()
        if not g:
            raise HTTPException(404, "Gear not found")
        if "name" in body:
            g.name = body["name"]
        if "startDate" in body:
            g.start_date = body["startDate"]
        if "retiredDate" in body:
            g.retired_date = body["retiredDate"]
        if "replaceAtMi" in body:
            g.replace_at_mi = body["replaceAtMi"]
        if "notes" in body:
            g.notes = body["notes"]
        if "isDefault" in body:
            g.is_default = bool(body["isDefault"])
            if g.is_default:
                _unset_other_defaults(db, user_id, g.kind, except_id=g.id)
        db.commit()
        return stats.gear_summary(db, user_id)
    finally:
        db.close()


@router.delete("/api/gear/{gear_id}")
def delete_gear(gear_id: str, user_id: str = Depends(auth.current_user_id)):
    db = SessionLocal()
    try:
        g = db.query(Gear).filter(Gear.id == gear_id, owned_by(Gear.user_id, user_id)).first()
        if not g:
            raise HTTPException(404, "Gear not found")
        db.delete(g)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()
