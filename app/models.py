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
