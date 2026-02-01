from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Any, Literal
from uuid import UUID, uuid4

from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

class CommandDB(SQLModel, table=True):
    __tablename__ = "commands"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)

    type: str = Field(index=True)  # "switch_channel"
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    status: str = Field(default="pending", index=True)  # pending | done | failed
    created_at: datetime = Field(default_factory=utcnow)
    processed_at: Optional[datetime] = None
    result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class SessionDB(SQLModel, table=True):
    __tablename__ = "sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    pairing_code: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)

    last_seen_mobile_at: Optional[datetime] = None
    last_seen_pi_at: Optional[datetime] = None


class SessionStateDB(SQLModel, table=True):
    __tablename__ = "session_state"

    session_id: UUID = Field(primary_key=True, foreign_key="sessions.id")
    ad_active: bool = False
    ad_since: Optional[datetime] = None
    last_event_id: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class EventDB(SQLModel, table=True):
    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: UUID = Field(foreign_key="sessions.id", index=True)

    type: str = Field(index=True)  # "ad_started" | "ad_ended" | ...
    confidence: Optional[float] = None
    created_at: datetime = Field(default_factory=utcnow)

    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# -----------------------------
# New simplified (device-based) tables
# -----------------------------

class DeviceCommandDB(SQLModel, table=True):
    """
    Commands addressed to a single logical device (today: 1 mobile app).
    This avoids the session_id/pairing flow while still supporting command polling.
    """

    __tablename__ = "device_commands"

    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)

    type: str = Field(index=True)  # "switch_channel"
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    status: str = Field(default="pending", index=True)  # pending | done | failed
    created_at: datetime = Field(default_factory=utcnow)
    processed_at: Optional[datetime] = None
    result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class AdResultDB(SQLModel, table=True):
    """
    High-frequency ad detection results from Raspberry/AI.
    We keep only the last N (default 100) per device_id.
    """

    __tablename__ = "ad_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)

    is_ad: bool = Field(index=True)
    confidence: Optional[float] = Field(default=None)

    # When the frame was captured on-device (optional) vs when server received it (created_at).
    captured_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)

    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class AdStateDB(SQLModel, table=True):
    """
    Current ad/non-ad state derived from the latest AdResultDB for a device.
    """

    __tablename__ = "ad_state"

    device_id: str = Field(primary_key=True)
    ad_active: bool = False
    ad_since: Optional[datetime] = None
    last_result_id: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class DeviceConfigDB(SQLModel, table=True):
    """
    Device configuration for automatic channel switching.
    - fallback_channel: Channel to switch to when ad is detected
    - original_channel: Channel to return to when ad ends (set by mobile app)
    - auto_switch_enabled: Whether automatic switching is enabled
    """

    __tablename__ = "device_config"

    device_id: str = Field(primary_key=True)
    fallback_channel: Optional[int] = None  # Channel to switch to during ads
    original_channel: Optional[int] = None  # Channel to return to after ads
    auto_switch_enabled: bool = True  # Enable/disable auto-switching
    updated_at: datetime = Field(default_factory=utcnow)


# -----------------------------
# Raspberry Pi Control Tables
# -----------------------------

class RpiStatusDB(SQLModel, table=True):
    """
    Track Raspberry Pi status - online/offline, running components.
    Updated via heartbeat from RPi.
    """

    __tablename__ = "rpi_status"

    device_id: str = Field(primary_key=True)

    # Online status (based on heartbeat)
    is_online: bool = False
    last_heartbeat: Optional[datetime] = None

    # Component status (reported by RPi)
    capture_running: bool = False  # FFmpeg running
    detect_running: bool = False   # CLIP detection running

    # Stats
    frames_captured: int = 0
    frames_processed: int = 0
    ads_detected: int = 0

    # System info
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    disk_percent: Optional[float] = None

    updated_at: datetime = Field(default_factory=utcnow)


class RpiCommandDB(SQLModel, table=True):
    """
    Commands for Raspberry Pi control.
    Types: start_capture, stop_capture, start_detect, stop_detect,
           restart_all, stop_all, set_channel, set_config
    """

    __tablename__ = "rpi_commands"

    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)

    type: str = Field(index=True)
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    status: str = Field(default="pending", index=True)  # pending | done | failed
    created_at: datetime = Field(default_factory=utcnow)
    processed_at: Optional[datetime] = None
    result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class RpiDaemonCommandDB(SQLModel, table=True):
    """
    Commands for Raspberry Pi daemon (controller lifecycle).
    Types: start_controller, stop_controller
    """

    __tablename__ = "rpi_daemon_commands"

    id: Optional[int] = Field(default=None, primary_key=True)
    device_id: str = Field(index=True)

    type: str = Field(index=True)  # start_controller | stop_controller
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    status: str = Field(default="pending", index=True)  # pending | done | failed
    created_at: datetime = Field(default_factory=utcnow)
    processed_at: Optional[datetime] = None
    result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class RpiDaemonStatusDB(SQLModel, table=True):
    """
    Status of Raspberry Pi daemon and controller.
    """

    __tablename__ = "rpi_daemon_status"

    device_id: str = Field(primary_key=True)
    
    daemon_running: bool = False
    controller_running: bool = False
    controller_pid: Optional[int] = None
    
    updated_at: datetime = Field(default_factory=utcnow)
