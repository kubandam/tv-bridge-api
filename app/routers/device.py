from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sqlalchemy import delete

from app.db.engine import get_session
from app.models import AdResultDB, AdStateDB, DeviceCommandDB, DeviceConfigDB, utcnow
from app.settings import settings

router = APIRouter(tags=["device"])


def require_device_id(x_device_id: str | None = Header(default=None)) -> str:
    """
    Device identity.
    - Prefer explicit X-Device-Id header (clients can set via ENV).
    - Fallback to DEFAULT_DEVICE_ID on server (useful while prototyping with 1 device).
    """
    if x_device_id:
        return x_device_id
    if settings.default_device_id:
        return settings.default_device_id
    raise HTTPException(status_code=400, detail="Missing X-Device-Id header (and DEFAULT_DEVICE_ID not set)")


class AdResultIn(BaseModel):
    is_ad: bool
    confidence: float | None = None
    captured_at: datetime | None = None
    payload: Dict[str, Any] = Field(default_factory=dict)


@router.post("/ad-results")
def post_ad_result(
    body: AdResultIn,
    device_id: str = Depends(require_device_id),
    keep_last: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_session),
):
    """
    Raspberry sends high-frequency detection results (every 1-2s).
    We store them and guarantee max N last rows per device_id (default: 100).

    Auto-switch logic:
    - When ad starts (is_ad=True, was False) → switch to fallback_channel
    - When ad ends (is_ad=False, was True) → switch back to original_channel
    """
    now = utcnow()

    row = AdResultDB(
        device_id=device_id,
        is_ad=body.is_ad,
        confidence=body.confidence,
        captured_at=body.captured_at,
        payload=body.payload,
    )
    db.add(row)
    db.flush()  # assigns row.id without committing

    # Update state (derived from latest result)
    st = db.get(AdStateDB, device_id)
    if not st:
        st = AdStateDB(device_id=device_id, ad_active=False, ad_since=None, last_result_id=0)

    # Track previous state for auto-switch logic
    was_ad_active = st.ad_active
    switch_command = None

    if body.is_ad:
        if not st.ad_active:
            st.ad_active = True
            st.ad_since = now
    else:
        st.ad_active = False
        st.ad_since = None

    st.last_result_id = int(row.id or st.last_result_id)
    st.updated_at = now
    db.add(st)

    # Auto-switch logic: create command when ad state changes
    config = db.get(DeviceConfigDB, device_id)
    if config and config.auto_switch_enabled:
        if body.is_ad and not was_ad_active:
            # Ad just started → switch to fallback channel
            if config.fallback_channel:
                switch_command = DeviceCommandDB(
                    device_id=device_id,
                    type="switch_channel",
                    payload={"channel": config.fallback_channel, "reason": "ad_started"},
                    status="pending",
                )
                db.add(switch_command)
        elif not body.is_ad and was_ad_active:
            # Ad just ended → switch back to original channel
            if config.original_channel:
                switch_command = DeviceCommandDB(
                    device_id=device_id,
                    type="switch_channel",
                    payload={"channel": config.original_channel, "reason": "ad_ended"},
                    status="pending",
                )
                db.add(switch_command)

    # Enforce max N rows in DB (delete older than keep_last)
    old_ids_stmt = (
        select(AdResultDB.id)
        .where(AdResultDB.device_id == device_id)
        .order_by(AdResultDB.id.desc())
        .offset(keep_last)
    )
    old_ids = db.exec(old_ids_stmt).all()
    if old_ids:
        db.exec(delete(AdResultDB).where(AdResultDB.id.in_(old_ids)))

    db.commit()
    db.refresh(row)
    db.refresh(st)

    response = {
        "result_id": row.id,
        "device_id": device_id,
        "state": {
            "ad_active": st.ad_active,
            "ad_since": st.ad_since,
            "last_result_id": st.last_result_id,
            "updated_at": st.updated_at,
        },
    }

    if switch_command:
        db.refresh(switch_command)
        response["auto_switch"] = {
            "command_id": switch_command.id,
            "channel": switch_command.payload.get("channel"),
            "reason": switch_command.payload.get("reason"),
        }

    return response


@router.get("/ad-state")
def get_ad_state(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    st = db.get(AdStateDB, device_id)
    if not st:
        return {
            "device_id": device_id,
            "ad_active": False,
            "ad_since": None,
            "last_result_id": 0,
            "updated_at": None,
        }
    return {
        "device_id": device_id,
        "ad_active": st.ad_active,
        "ad_since": st.ad_since,
        "last_result_id": st.last_result_id,
        "updated_at": st.updated_at,
    }


@router.get("/ad-results")
def list_ad_results(
    device_id: str = Depends(require_device_id),
    limit: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_session),
):
    stmt = (
        select(AdResultDB)
        .where(AdResultDB.device_id == device_id)
        .order_by(AdResultDB.id.desc())
        .limit(limit)
    )
    rows = db.exec(stmt).all()
    return [
        {
            "id": r.id,
            "is_ad": r.is_ad,
            "confidence": r.confidence,
            "captured_at": r.captured_at,
            "created_at": r.created_at,
            "payload": r.payload,
        }
        for r in rows
    ]


@router.post("/commands/switch-channel")
def command_switch_channel(
    channel: int = Query(ge=1, le=9999),
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    cmd = DeviceCommandDB(
        device_id=device_id,
        type="switch_channel",
        payload={"channel": channel},
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    return {"command_id": cmd.id, "status": cmd.status, "payload": cmd.payload}


@router.get("/commands/pull")
def pull_commands(
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    stmt = (
        select(DeviceCommandDB)
        .where(DeviceCommandDB.device_id == device_id, DeviceCommandDB.id > after_id)
        .order_by(DeviceCommandDB.id.asc())
        .limit(limit)
    )
    cmds = db.exec(stmt).all()
    return [
        {
            "id": c.id,
            "type": c.type,
            "payload": c.payload,
            "status": c.status,
            "created_at": c.created_at,
        }
        for c in cmds
    ]


@router.post("/commands/{command_id}/ack")
def ack_command(
    command_id: int,
    body: Dict[str, Any],
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    cmd = db.get(DeviceCommandDB, command_id)
    if not cmd or cmd.device_id != device_id:
        raise HTTPException(status_code=404, detail="Command not found")

    status = body.get("status")  # "done" | "failed"
    if status not in ("done", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'done' or 'failed'")

    cmd.status = status
    cmd.processed_at = utcnow()
    cmd.result = body.get("result", {})

    db.add(cmd)
    db.commit()
    return {"ok": True}


# -----------------------------
# Device Configuration Endpoints
# -----------------------------

class DeviceConfigIn(BaseModel):
    fallback_channel: Optional[int] = Field(default=None, ge=1, le=9999)
    auto_switch_enabled: Optional[bool] = None


@router.get("/config")
def get_device_config(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """Get device configuration for auto-switching."""
    config = db.get(DeviceConfigDB, device_id)
    if not config:
        return {
            "device_id": device_id,
            "fallback_channel": None,
            "original_channel": None,
            "auto_switch_enabled": True,
            "updated_at": None,
        }
    return {
        "device_id": config.device_id,
        "fallback_channel": config.fallback_channel,
        "original_channel": config.original_channel,
        "auto_switch_enabled": config.auto_switch_enabled,
        "updated_at": config.updated_at,
    }


@router.put("/config")
def update_device_config(
    body: DeviceConfigIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Update device configuration.
    - fallback_channel: Channel to switch to when ad is detected
    - auto_switch_enabled: Enable/disable automatic channel switching
    """
    config = db.get(DeviceConfigDB, device_id)
    if not config:
        config = DeviceConfigDB(device_id=device_id)

    if body.fallback_channel is not None:
        config.fallback_channel = body.fallback_channel
    if body.auto_switch_enabled is not None:
        config.auto_switch_enabled = body.auto_switch_enabled
    config.updated_at = utcnow()

    db.add(config)
    db.commit()
    db.refresh(config)

    return {
        "device_id": config.device_id,
        "fallback_channel": config.fallback_channel,
        "original_channel": config.original_channel,
        "auto_switch_enabled": config.auto_switch_enabled,
        "updated_at": config.updated_at,
    }


@router.post("/config/current-channel")
def set_current_channel(
    channel: int = Query(ge=1, le=9999),
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Mobile app reports the current channel.
    This sets original_channel so API knows where to return after ads.
    Call this whenever user manually switches channel.
    """
    config = db.get(DeviceConfigDB, device_id)
    if not config:
        config = DeviceConfigDB(device_id=device_id)

    config.original_channel = channel
    config.updated_at = utcnow()

    db.add(config)
    db.commit()
    db.refresh(config)

    return {
        "device_id": config.device_id,
        "original_channel": config.original_channel,
        "updated_at": config.updated_at,
    }


