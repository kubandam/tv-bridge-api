from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from sqlalchemy import delete

from app.db.engine import get_session
from app.models import AdResultDB, AdStateDB, DeviceCommandDB, DeviceConfigDB, FrameHistoryDB, utcnow
from app.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["device"])

# In-memory storage for latest image per device (base64, not persisted to R2)
# Format: { "device_id": {"image_base64": "...", "timestamp": datetime, "is_ad": bool,
#                          "confidence": float, "channel": str|None} }
_latest_images: Dict[str, Dict[str, Any]] = {}

# History sampling: last saved timestamp per device
_last_history_ts: Dict[str, datetime] = {}

MAX_HISTORY_UNLABELED = 5000


def require_device_id(x_device_id: str | None = Header(default=None)) -> str:
    if x_device_id:
        return x_device_id
    if settings.default_device_id:
        return settings.default_device_id
    raise HTTPException(status_code=400, detail="Missing X-Device-Id header (and DEFAULT_DEVICE_ID not set)")


class AdResultIn(BaseModel):
    is_ad: bool
    confidence: float | None = None
    captured_at: datetime | None = None
    channel: str | None = None  # e.g. "CT:1", "STV:1"
    payload: Dict[str, Any] = Field(default_factory=dict)
    image_base64: str | None = None  # Optional base64 JPEG


@router.post("/ad-results")
def post_ad_result(
    body: AdResultIn,
    device_id: str = Depends(require_device_id),
    keep_last: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_session),
):
    """
    Raspberry sends detection results (every ~3s).
    Image is sampled to frame_history at settings.history_sample_interval_s.
    """
    now = utcnow()

    # Update in-memory latest (for live preview + labeling)
    if body.image_base64:
        _latest_images[device_id] = {
            "image_base64": body.image_base64,
            "timestamp": now,
            "is_ad": body.is_ad,
            "confidence": body.confidence,
            "channel": body.channel,
        }

        # Persist sampled frame to history
        last_ts = _last_history_ts.get(device_id)
        elapsed = (now - last_ts).total_seconds() if last_ts else None
        if elapsed is None or elapsed >= settings.history_sample_interval_s:
            _last_history_ts[device_id] = now
            try:
                from app.storage.r2 import upload_frame
                image_bytes = base64.b64decode(body.image_base64)
                image_key = f"frames/{device_id}/{uuid.uuid4()}.jpg"
                upload_frame(image_bytes, image_key)

                hist = FrameHistoryDB(
                    device_id=device_id,
                    channel=body.channel,
                    image_key=image_key,
                    is_ad=body.is_ad,
                    confidence=body.confidence,
                    captured_at=body.captured_at,
                )
                db.add(hist)
                db.flush()

                # Rotate: delete oldest unlabeled when over cap (never delete labeled)
                old_ids_stmt = (
                    select(FrameHistoryDB.id)
                    .where(FrameHistoryDB.device_id == device_id)
                    .where(FrameHistoryDB.label == None)
                    .order_by(FrameHistoryDB.id.desc())
                    .offset(MAX_HISTORY_UNLABELED)
                )
                old_ids = db.exec(old_ids_stmt).all()
                if old_ids:
                    # Also delete from R2
                    old_frames = db.exec(
                        select(FrameHistoryDB).where(FrameHistoryDB.id.in_(old_ids))
                    ).all()
                    old_keys = [f.image_key for f in old_frames if f.image_key]
                    if old_keys:
                        from app.storage.r2 import delete_frames_batch
                        try:
                            delete_frames_batch(old_keys)
                        except Exception as e:
                            logger.warning(f"R2 batch delete failed: {e}")
                    db.exec(delete(FrameHistoryDB).where(FrameHistoryDB.id.in_(old_ids)))

            except Exception as e:
                logger.error(f"Failed to save frame history: {e}")

    # Save ad result to DB
    row = AdResultDB(
        device_id=device_id,
        is_ad=body.is_ad,
        confidence=body.confidence,
        captured_at=body.captured_at,
        payload=body.payload,
    )
    db.add(row)
    db.flush()

    # Update state
    st = db.get(AdStateDB, device_id)
    if not st:
        st = AdStateDB(device_id=device_id, ad_active=False, ad_since=None, last_result_id=0)

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

    # Auto-switch logic
    config = db.get(DeviceConfigDB, device_id)
    if config and config.auto_switch_enabled:
        if body.is_ad and not was_ad_active and config.fallback_channel:
            pending_cmds = db.exec(
                select(DeviceCommandDB).where(
                    DeviceCommandDB.device_id == device_id,
                    DeviceCommandDB.status == "pending",
                )
            ).all()
            for cmd in pending_cmds:
                cmd.status = "cancelled"
                cmd.processed_at = now
                db.add(cmd)
            switch_command = DeviceCommandDB(
                device_id=device_id,
                type="switch_channel",
                payload={"channel": config.fallback_channel, "reason": "ad_started"},
                status="pending",
            )
            db.add(switch_command)
        elif not body.is_ad and was_ad_active and config.original_channel:
            pending_cmds = db.exec(
                select(DeviceCommandDB).where(
                    DeviceCommandDB.device_id == device_id,
                    DeviceCommandDB.status == "pending",
                )
            ).all()
            for cmd in pending_cmds:
                cmd.status = "cancelled"
                cmd.processed_at = now
                db.add(cmd)
            switch_command = DeviceCommandDB(
                device_id=device_id,
                type="switch_channel",
                payload={"channel": config.original_channel, "reason": "ad_ended"},
                status="pending",
            )
            db.add(switch_command)

    # Rotate ad_results
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
    now = utcnow()
    pending_cmds = db.exec(
        select(DeviceCommandDB).where(
            DeviceCommandDB.device_id == device_id,
            DeviceCommandDB.status == "pending",
        )
    ).all()
    for cmd in pending_cmds:
        cmd.status = "cancelled"
        cmd.processed_at = now
        db.add(cmd)
    cmd = DeviceCommandDB(
        device_id=device_id,
        type="switch_channel",
        payload={"channel": channel, "reason": "manual"},
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
        .where(
            DeviceCommandDB.device_id == device_id,
            DeviceCommandDB.status == "pending",
        )
        .order_by(DeviceCommandDB.id.desc())
        .limit(1)
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
    status = body.get("status")
    if status not in ("done", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'done' or 'failed'")
    cmd.status = status
    cmd.processed_at = utcnow()
    cmd.result = body.get("result", {})
    db.add(cmd)
    db.commit()
    return {"ok": True}


# -----------------------------
# Device Configuration
# -----------------------------

class DeviceConfigIn(BaseModel):
    fallback_channel: Optional[int] = Field(default=None, ge=1, le=9999)
    auto_switch_enabled: Optional[bool] = None


@router.get("/config")
def get_device_config(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
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


# -----------------------------
# Live Image
# -----------------------------

@router.get("/live-image")
def get_live_image_info(device_id: str = Depends(require_device_id)):
    if device_id not in _latest_images:
        return {
            "device_id": device_id,
            "has_image": False,
            "image_base64": None,
            "timestamp": None,
            "is_ad": None,
            "confidence": None,
            "channel": None,
        }
    img_data = _latest_images[device_id]
    return {
        "device_id": device_id,
        "has_image": True,
        "image_base64": img_data["image_base64"],
        "timestamp": img_data["timestamp"].isoformat() if img_data["timestamp"] else None,
        "is_ad": img_data["is_ad"],
        "confidence": img_data["confidence"],
        "channel": img_data.get("channel"),
    }


@router.get("/live-image.jpg")
def get_live_image_raw(device_id: str = Query(default="tv-1")):
    if device_id not in _latest_images:
        raise HTTPException(status_code=404, detail="No image available for this device")
    img_data = _latest_images[device_id]
    image_bytes = base64.b64decode(img_data["image_base64"])
    return Response(
        content=image_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
