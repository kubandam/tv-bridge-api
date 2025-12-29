from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.db.engine import get_session
from app.models import SessionDB, SessionStateDB, EventDB, utcnow, CommandDB

router = APIRouter(tags=["sessions"])


def generate_pairing_code() -> str:
    # krátky code, ktorý môžeš zobraziť na TV / v mobile
    return secrets.token_urlsafe(6)[:8].upper()
@router.post("/sessions")
def create_session(db: Session = Depends(get_session)):
    pairing_code = generate_pairing_code()

    # ensure uniqueness (simple retry)
    for _ in range(5):
        existing = db.exec(select(SessionDB).where(SessionDB.pairing_code == pairing_code)).first()
        if not existing:
            break
        pairing_code = generate_pairing_code()
    else:
        raise HTTPException(status_code=500, detail="Failed to generate unique pairing code")

    s = SessionDB(pairing_code=pairing_code)
    db.add(s)
    db.commit()
    db.refresh(s)

    # create initial state
    st = SessionStateDB(session_id=s.id, ad_active=False, ad_since=None, last_event_id=0)
    db.add(st)
    db.commit()

    return {"session_id": str(s.id), "pairing_code": s.pairing_code}



@router.post("/sessions/{session_id}/commands/switch-channel")
def command_switch_channel(
    session_id: UUID,
    channel: int = Query(ge=1, le=9999),
    db: Session = Depends(get_session),
):
    s = db.get(SessionDB, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    cmd = CommandDB(
        session_id=session_id,
        type="switch_channel",
        payload={"channel": channel},
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    return {"command_id": cmd.id, "status": cmd.status, "payload": cmd.payload}

@router.get("/sessions/{session_id}/commands/pull")
def pull_commands(
    session_id: UUID,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_session),
):
    s = db.get(SessionDB, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(CommandDB)
        .where(CommandDB.session_id == session_id, CommandDB.id > after_id)
        .order_by(CommandDB.id.asc())
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

@router.post("/sessions/{session_id}/commands/{command_id}/ack")
def ack_command(
    session_id: UUID,
    command_id: int,
    body: Dict[str, Any],
    db: Session = Depends(get_session),
):
    cmd = db.get(CommandDB, command_id)
    if not cmd or cmd.session_id != session_id:
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




@router.get("/sessions/by-code/{pairing_code}")
def get_session_by_code(pairing_code: str, db: Session = Depends(get_session)):
    s = db.exec(select(SessionDB).where(SessionDB.pairing_code == pairing_code)).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": str(s.id), "pairing_code": s.pairing_code, "created_at": s.created_at}


@router.post("/sessions/{session_id}/ai-events")
def post_ai_event(
    session_id: UUID,
    body: Dict[str, Any],
    db: Session = Depends(get_session),
):
    """
    Raspberry posiela:
    {
      "type": "ad_started" | "ad_ended",
      "confidence": 0.93,
      "timestamp": "2025-12-28T14:55:02Z"  (optional)
      ...anything else...
    }
    """
    s = db.get(SessionDB, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    event_type = body.get("type")
    if event_type not in ("ad_started", "ad_ended"):
        raise HTTPException(status_code=400, detail="Invalid event type")

    confidence = body.get("confidence")
    payload = dict(body)

    ev = EventDB(
        session_id=session_id,
        type=event_type,
        confidence=float(confidence) if confidence is not None else None,
        payload=payload,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    # update session last seen
    s.last_seen_pi_at = utcnow()
    db.add(s)

    # update current state
    st = db.get(SessionStateDB, session_id)
    if not st:
        st = SessionStateDB(session_id=session_id)

    if event_type == "ad_started":
        st.ad_active = True
        st.ad_since = utcnow()
    else:  # ad_ended
        st.ad_active = False
        st.ad_since = None

    st.last_event_id = ev.id or st.last_event_id
    st.updated_at = utcnow()

    db.add(st)
    db.commit()

    return {"event_id": ev.id, "state": {"ad_active": st.ad_active, "ad_since": st.ad_since, "last_event_id": st.last_event_id}}


@router.get("/sessions/{session_id}/current-state")
def get_current_state(
    session_id: UUID,
    client: str = Query(default="mobile", pattern="^(mobile|pi)$"),
    db: Session = Depends(get_session),
):
    s = db.get(SessionDB, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    # heartbeat
    now = utcnow()
    if client == "mobile":
        s.last_seen_mobile_at = now
    else:
        s.last_seen_pi_at = now
    db.add(s)
    db.commit()

    st = db.get(SessionStateDB, session_id)
    if not st:
        st = SessionStateDB(session_id=session_id)
        db.add(st)
        db.commit()
        db.refresh(st)

    return {
        "session_id": str(session_id),
        "ad_active": st.ad_active,
        "ad_since": st.ad_since,
        "last_event_id": st.last_event_id,
        "updated_at": st.updated_at,
    }


@router.get("/sessions/{session_id}/events")
def get_events(
    session_id: UUID,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_session),
):
    s = db.get(SessionDB, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(EventDB)
        .where(EventDB.session_id == session_id, EventDB.id > after_id)
        .order_by(EventDB.id.asc())
        .limit(limit)
    )
    events = db.exec(stmt).all()

    return [
        {
            "id": e.id,
            "type": e.type,
            "confidence": e.confidence,
            "created_at": e.created_at,
            "payload": e.payload,
        }
        for e in events
    ]
