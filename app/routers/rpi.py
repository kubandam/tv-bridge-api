from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db.engine import get_session
from app.models import RpiStatusDB, RpiCommandDB, RpiDaemonCommandDB, RpiDaemonStatusDB, utcnow
from app.settings import settings

router = APIRouter(tags=["rpi"])


def require_device_id(x_device_id: str | None = Header(default=None)) -> str:
    if x_device_id:
        return x_device_id
    if settings.default_device_id:
        return settings.default_device_id
    raise HTTPException(status_code=400, detail="Missing X-Device-Id header")


# -----------------------------
# In-memory image log storage
# Format: { "device_id": deque([{image_base64, timestamp, is_ad, confidence, filename}, ...]) }
# Max size: settings.max_image_log_size per device
# -----------------------------
_image_logs: Dict[str, deque] = {}


def get_image_log(device_id: str) -> deque:
    if device_id not in _image_logs:
        _image_logs[device_id] = deque(maxlen=settings.max_image_log_size)
    return _image_logs[device_id]


# -----------------------------
# Pydantic Models
# -----------------------------

class HeartbeatIn(BaseModel):
    capture_running: bool = False
    detect_running: bool = False
    frames_captured: int = 0
    frames_processed: int = 0
    ads_detected: int = 0
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    disk_percent: Optional[float] = None


class CommandIn(BaseModel):
    type: str = Field(..., description="Command type: start_capture, stop_capture, start_detect, stop_detect, restart_all, stop_all, set_channel, set_config")
    payload: Dict[str, Any] = Field(default_factory=dict)


class CommandAckIn(BaseModel):
    status: str = Field(..., description="done or failed")
    result: Dict[str, Any] = Field(default_factory=dict)


class ImageLogIn(BaseModel):
    image_base64: str
    is_ad: bool
    confidence: Optional[float] = None
    filename: Optional[str] = None
    captured_at: Optional[datetime] = None


# -----------------------------
# Heartbeat Endpoint
# -----------------------------

@router.post("/heartbeat")
def post_heartbeat(
    body: HeartbeatIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    RPi sends heartbeat every 10 seconds with current status.
    """
    now = utcnow()

    status = db.get(RpiStatusDB, device_id)
    if not status:
        status = RpiStatusDB(device_id=device_id)

    status.is_online = True
    status.last_heartbeat = now
    status.capture_running = body.capture_running
    status.detect_running = body.detect_running
    status.frames_captured = body.frames_captured
    status.frames_processed = body.frames_processed
    status.ads_detected = body.ads_detected
    status.cpu_percent = body.cpu_percent
    status.memory_percent = body.memory_percent
    status.disk_percent = body.disk_percent
    status.updated_at = now

    db.add(status)
    db.commit()
    db.refresh(status)

    return {
        "ok": True,
        "device_id": device_id,
        "timestamp": now.isoformat(),
    }


@router.get("/status")
def get_status(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Get current RPi status including online/offline.
    """
    status = db.get(RpiStatusDB, device_id)
    now = utcnow()

    if not status:
        return {
            "device_id": device_id,
            "is_online": False,
            "last_heartbeat": None,
            "capture_running": False,
            "detect_running": False,
            "frames_captured": 0,
            "frames_processed": 0,
            "ads_detected": 0,
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
        }

    # Check if offline (no heartbeat in threshold seconds)
    is_online = False
    if status.last_heartbeat:
        timeout = timedelta(seconds=settings.heartbeat_timeout_seconds)
        is_online = (now - status.last_heartbeat) < timeout

    # Update online status if changed
    if status.is_online != is_online:
        status.is_online = is_online
        db.add(status)
        db.commit()

    return {
        "device_id": device_id,
        "is_online": is_online,
        "last_heartbeat": status.last_heartbeat.isoformat() if status.last_heartbeat else None,
        "capture_running": status.capture_running,
        "detect_running": status.detect_running,
        "frames_captured": status.frames_captured,
        "frames_processed": status.frames_processed,
        "ads_detected": status.ads_detected,
        "cpu_percent": status.cpu_percent,
        "memory_percent": status.memory_percent,
        "disk_percent": status.disk_percent,
        "updated_at": status.updated_at.isoformat() if status.updated_at else None,
    }


# -----------------------------
# Command Endpoints (Dashboard -> RPi)
# -----------------------------

@router.post("/commands")
def create_command(
    body: CommandIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Dashboard creates command for RPi to execute.

    Command types:
    - start_capture: Start FFmpeg capture
    - stop_capture: Stop FFmpeg capture
    - start_detect: Start CLIP detection
    - stop_detect: Stop CLIP detection
    - restart_all: Restart everything
    - stop_all: Stop everything
    - set_channel: Change TV channel (payload: {channel: int})
    - set_config: Update config (payload: {key: value, ...})
    """
    valid_types = [
        "start_capture", "stop_capture",
        "start_detect", "stop_detect",
        "restart_all", "stop_all",
        "set_channel", "set_config",
    ]

    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid command type. Valid types: {valid_types}"
        )

    cmd = RpiCommandDB(
        device_id=device_id,
        type=body.type,
        payload=body.payload,
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    return {
        "command_id": cmd.id,
        "type": cmd.type,
        "payload": cmd.payload,
        "status": cmd.status,
        "created_at": cmd.created_at.isoformat(),
    }


@router.get("/commands/pull")
def pull_commands(
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    RPi polls for pending commands.
    """
    stmt = (
        select(RpiCommandDB)
        .where(
            RpiCommandDB.device_id == device_id,
            RpiCommandDB.id > after_id,
            RpiCommandDB.status == "pending",
        )
        .order_by(RpiCommandDB.id.asc())
        .limit(limit)
    )
    cmds = db.exec(stmt).all()

    return [
        {
            "id": c.id,
            "type": c.type,
            "payload": c.payload,
            "created_at": c.created_at.isoformat(),
        }
        for c in cmds
    ]


@router.post("/commands/{command_id}/ack")
def ack_command(
    command_id: int,
    body: CommandAckIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    RPi acknowledges command completion.
    """
    cmd = db.get(RpiCommandDB, command_id)
    if not cmd or cmd.device_id != device_id:
        raise HTTPException(status_code=404, detail="Command not found")

    if body.status not in ("done", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'done' or 'failed'")

    cmd.status = body.status
    cmd.processed_at = utcnow()
    cmd.result = body.result

    db.add(cmd)
    db.commit()

    return {"ok": True, "command_id": command_id, "status": body.status}


@router.get("/commands/history")
def get_command_history(
    limit: int = Query(default=50, ge=1, le=200),
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Get recent commands for this device.
    """
    stmt = (
        select(RpiCommandDB)
        .where(RpiCommandDB.device_id == device_id)
        .order_by(RpiCommandDB.id.desc())
        .limit(limit)
    )
    cmds = db.exec(stmt).all()

    return [
        {
            "id": c.id,
            "type": c.type,
            "payload": c.payload,
            "status": c.status,
            "created_at": c.created_at.isoformat(),
            "processed_at": c.processed_at.isoformat() if c.processed_at else None,
            "result": c.result,
        }
        for c in cmds
    ]


# -----------------------------
# Image Log Endpoints
# -----------------------------

@router.post("/image-log")
def add_image_to_log(
    body: ImageLogIn,
    device_id: str = Depends(require_device_id),
):
    """
    RPi uploads detection image to log.
    Images are stored in memory (not DB) with max size limit.
    """
    now = utcnow()
    log = get_image_log(device_id)

    log.append({
        "image_base64": body.image_base64,
        "is_ad": body.is_ad,
        "confidence": body.confidence,
        "filename": body.filename,
        "captured_at": (body.captured_at.isoformat() if body.captured_at else now.isoformat()),
        "uploaded_at": now.isoformat(),
    })

    return {
        "ok": True,
        "log_size": len(log),
        "max_size": settings.max_image_log_size,
    }


@router.get("/image-log")
def get_image_log_list(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_images: bool = Query(default=False, description="Include base64 images in response"),
    device_id: str = Depends(require_device_id),
):
    """
    Get detection image log metadata.
    Set include_images=true to include base64 data (larger response).
    """
    log = get_image_log(device_id)

    # Convert deque to list (newest first)
    items = list(reversed(log))

    # Apply pagination
    paginated = items[offset:offset + limit]

    result = []
    for i, item in enumerate(paginated):
        entry = {
            "index": offset + i,
            "is_ad": item["is_ad"],
            "confidence": item["confidence"],
            "filename": item["filename"],
            "captured_at": item["captured_at"],
            "uploaded_at": item["uploaded_at"],
        }
        if include_images:
            entry["image_base64"] = item["image_base64"]
        result.append(entry)

    return {
        "device_id": device_id,
        "total": len(log),
        "limit": limit,
        "offset": offset,
        "items": result,
    }


@router.get("/image-log/{index}")
def get_image_log_item(
    index: int,
    device_id: str = Depends(require_device_id),
):
    """
    Get specific image from log by index.
    Returns JSON with image_base64.
    """
    log = get_image_log(device_id)
    items = list(reversed(log))  # newest first

    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Image not found")

    item = items[index]
    return {
        "device_id": device_id,
        "index": index,
        **item,
    }


@router.get("/image-log/{index}.jpg")
def get_image_log_raw(
    index: int,
    device_id: str = Query(default="tv-1"),
):
    """
    Get specific image from log as raw JPEG.
    Use in <img src="..."> tags.
    """
    log = get_image_log(device_id)
    items = list(reversed(log))  # newest first

    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Image not found")

    item = items[index]
    image_bytes = base64.b64decode(item["image_base64"])

    return Response(
        content=image_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@router.delete("/image-log")
def clear_image_log(
    device_id: str = Depends(require_device_id),
):
    """
    Clear image log for this device.
    """
    if device_id in _image_logs:
        _image_logs[device_id].clear()

    return {"ok": True, "device_id": device_id}


# -----------------------------
# Quick Actions (convenience endpoints)
# -----------------------------

@router.post("/start")
def quick_start(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Quick start: Start both capture and detection.
    """
    cmds = []
    for cmd_type in ["start_capture", "start_detect"]:
        cmd = RpiCommandDB(
            device_id=device_id,
            type=cmd_type,
            payload={},
            status="pending",
        )
        db.add(cmd)
        cmds.append(cmd)

    db.commit()

    return {
        "ok": True,
        "commands": [
            {"id": c.id, "type": c.type}
            for c in cmds
        ],
    }


@router.post("/stop")
def quick_stop(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Quick stop: Stop everything.
    """
    cmd = RpiCommandDB(
        device_id=device_id,
        type="stop_all",
        payload={},
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    return {
        "ok": True,
        "command_id": cmd.id,
    }


@router.post("/restart")
def quick_restart(
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Quick restart: Stop and start everything.
    """
    cmd = RpiCommandDB(
        device_id=device_id,
        type="restart_all",
        payload={},
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    return {
        "ok": True,
        "command_id": cmd.id,
    }


# -----------------------------
# Daemon Lifecycle Management
# -----------------------------

class DaemonCommandIn(BaseModel):
    type: str = Field(..., description="Command type: start_controller | stop_controller")
    payload: Dict[str, Any] = Field(default_factory=dict)


class DaemonStatusIn(BaseModel):
    daemon_running: bool
    controller_running: bool
    controller_pid: Optional[int] = None


@router.post("/daemon-commands")
def create_daemon_command(
    body: DaemonCommandIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Create a daemon command (start_controller, stop_controller).
    Used by monitor dashboard to control controller lifecycle.
    """
    if body.type not in ["start_controller", "stop_controller"]:
        raise HTTPException(status_code=400, detail=f"Invalid daemon command type: {body.type}")
    
    cmd = RpiDaemonCommandDB(
        device_id=device_id,
        type=body.type,
        payload=body.payload,
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    
    return {
        "ok": True,
        "command_id": cmd.id,
        "type": cmd.type,
        "device_id": device_id,
    }


@router.get("/daemon-commands")
def poll_daemon_commands(
    device_id: str = Query(...),
    since: int = Query(default=0, description="Last command ID seen"),
    db: Session = Depends(get_session),
):
    """
    Poll for pending daemon commands (used by rpi_daemon.py).
    Returns commands with ID > since and status=pending.
    """
    stmt = (
        select(RpiDaemonCommandDB)
        .where(RpiDaemonCommandDB.device_id == device_id)
        .where(RpiDaemonCommandDB.id > since)
        .where(RpiDaemonCommandDB.status == "pending")
        .order_by(RpiDaemonCommandDB.id)
        .limit(20)
    )
    
    commands = db.exec(stmt).all()
    
    return {
        "commands": [
            {
                "id": c.id,
                "type": c.type,
                "payload": c.payload,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in commands
        ]
    }


class DaemonCommandUpdate(BaseModel):
    status: str = Field(..., description="done | failed")
    result: Dict[str, Any] = Field(default_factory=dict)


@router.put("/daemon-commands/{command_id}")
def update_daemon_command(
    command_id: int,
    body: DaemonCommandUpdate,
    db: Session = Depends(get_session),
):
    """
    Update daemon command status (used by rpi_daemon.py to report results).
    """
    cmd = db.get(RpiDaemonCommandDB, command_id)
    if not cmd:
        raise HTTPException(status_code=404, detail="Command not found")
    
    if body.status not in ["done", "failed"]:
        raise HTTPException(status_code=400, detail="Status must be 'done' or 'failed'")
    
    cmd.status = body.status
    cmd.result = body.result
    cmd.processed_at = utcnow()
    
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    
    return {
        "ok": True,
        "command_id": cmd.id,
        "status": cmd.status,
    }


@router.post("/daemon-status")
def update_daemon_status(
    body: DaemonStatusIn,
    device_id: str = Depends(require_device_id),
    db: Session = Depends(get_session),
):
    """
    Update daemon status (used by rpi_daemon.py to report current state).
    """
    status = db.get(RpiDaemonStatusDB, device_id)
    
    if not status:
        status = RpiDaemonStatusDB(device_id=device_id)
    
    status.daemon_running = body.daemon_running
    status.controller_running = body.controller_running
    status.controller_pid = body.controller_pid
    status.updated_at = utcnow()
    
    db.add(status)
    db.commit()
    db.refresh(status)
    
    return {
        "ok": True,
        "device_id": device_id,
        "daemon_running": status.daemon_running,
        "controller_running": status.controller_running,
    }


@router.get("/daemon-status")
def get_daemon_status(
    device_id: str = Query(...),
    db: Session = Depends(get_session),
):
    """
    Get current daemon status.
    """
    status = db.get(RpiDaemonStatusDB, device_id)
    
    if not status:
        return {
            "device_id": device_id,
            "daemon_running": False,
            "controller_running": False,
            "controller_pid": None,
            "updated_at": None,
        }
    
    return {
        "device_id": device_id,
        "daemon_running": status.daemon_running,
        "controller_running": status.controller_running,
        "controller_pid": status.controller_pid,
        "updated_at": status.updated_at.isoformat() if status.updated_at else None,
    }


@router.get("/controller-log")
def get_controller_log(
    device_id: str = Query(...),
    lines: int = Query(default=100, ge=1, le=1000, description="Number of lines to return"),
):
    """
    Get controller.log from Raspberry Pi controller.
    Returns last N lines of the log file.
    
    This endpoint reads the log file that rpi_daemon.py writes when starting rpi_controller.py.
    """
    # For now, return a placeholder - in production, RPi would need to send logs via API
    # or we'd need to implement log streaming/upload mechanism
    return {
        "device_id": device_id,
        "lines": [],
        "message": "Controller logs need to be streamed from RPi. Check RPi with: tail -f ~/CLIP/controller.log or journalctl -u rpi-daemon -f",
        "instructions": [
            "On RPi: tail -f ~/CLIP/controller.log",
            "Or: sudo journalctl -u rpi-daemon -f",
            "To see detect errors: grep DETECT ~/CLIP/controller.log",
        ]
    }
