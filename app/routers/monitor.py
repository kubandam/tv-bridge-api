from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlmodel import Session, select, desc

from app.db.engine import get_session
from app.models import AdResultDB, AdStateDB, AdEventDB, DeviceCommandDB, DeviceConfigDB, FrameHistoryDB, RpiStatusDB, RpiCommandDB, RpiDaemonStatusDB, utcnow
from app.settings import settings
from app.ui import NAV_CSS, UNAUTH_HTML, NAV_STATUS_JS, nav_bar

router = APIRouter(tags=["monitor"])


@router.get("/monitor/data")
def get_monitor_data(
    device_id: str = Query(default="tv-1"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_session),
):
    """
    Get comprehensive monitoring data for a device.
    Returns recent ad results, commands, current state, config, and RPi status.
    """
    now = utcnow()

    # Get RPi status
    rpi_status = db.get(RpiStatusDB, device_id)
    rpi_data = None
    if rpi_status:
        # Check if offline - handle both timezone-aware and naive datetimes
        timeout = timedelta(seconds=settings.heartbeat_timeout_seconds)
        is_online = False
        if rpi_status.last_heartbeat:
            try:
                # Try direct comparison first
                is_online = (now - rpi_status.last_heartbeat) < timeout
            except TypeError:
                # Handle timezone mismatch by comparing naive datetimes
                now_naive = now.replace(tzinfo=None)
                heartbeat_naive = rpi_status.last_heartbeat.replace(tzinfo=None) if rpi_status.last_heartbeat.tzinfo else rpi_status.last_heartbeat
                is_online = (now_naive - heartbeat_naive) < timeout
        
        rpi_data = {
            "is_online": is_online,
            "last_heartbeat": rpi_status.last_heartbeat.isoformat() if rpi_status.last_heartbeat else None,
            "capture_running": rpi_status.capture_running,
            "detect_running": rpi_status.detect_running,
            "frames_captured": rpi_status.frames_captured,
            "frames_processed": rpi_status.frames_processed,
            "ads_detected": rpi_status.ads_detected,
            "cpu_percent": rpi_status.cpu_percent,
            "memory_percent": rpi_status.memory_percent,
            "disk_percent": rpi_status.disk_percent,
        }

    # Get daemon status
    daemon_status = db.get(RpiDaemonStatusDB, device_id)
    daemon_data = None
    if daemon_status:
        daemon_data = {
            "daemon_running": daemon_status.daemon_running,
            "controller_running": daemon_status.controller_running,
            "controller_pid": daemon_status.controller_pid,
            "updated_at": daemon_status.updated_at.isoformat() if daemon_status.updated_at else None,
        }

    # Get current ad state
    ad_state = db.get(AdStateDB, device_id)
    state_data = {
        "device_id": device_id,
        "ad_active": ad_state.ad_active if ad_state else False,
        "ad_since": ad_state.ad_since.isoformat() if ad_state and ad_state.ad_since else None,
        "last_result_id": ad_state.last_result_id if ad_state else 0,
        "updated_at": ad_state.updated_at.isoformat() if ad_state and ad_state.updated_at else None,
    }

    # Get device config
    config = db.get(DeviceConfigDB, device_id)
    config_data = {
        "device_id": device_id,
        "fallback_channel": config.fallback_channel if config else None,
        "original_channel": config.original_channel if config else None,
        "auto_switch_enabled": config.auto_switch_enabled if config else True,
        "updated_at": config.updated_at.isoformat() if config and config.updated_at else None,
    }

    # Get recent ad results
    results_stmt = (
        select(AdResultDB)
        .where(AdResultDB.device_id == device_id)
        .order_by(desc(AdResultDB.id))
        .limit(limit)
    )
    results = db.exec(results_stmt).all()
    results_data = [
        {
            "id": r.id,
            "is_ad": r.is_ad,
            "confidence": r.confidence,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in results
    ]

    # Get recent device commands
    commands_stmt = (
        select(DeviceCommandDB)
        .where(DeviceCommandDB.device_id == device_id)
        .order_by(desc(DeviceCommandDB.id))
        .limit(limit)
    )
    commands = db.exec(commands_stmt).all()
    commands_data = [
        {
            "id": c.id,
            "type": c.type,
            "payload": c.payload,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "processed_at": c.processed_at.isoformat() if c.processed_at else None,
        }
        for c in commands
    ]

    # Get recent RPi commands
    rpi_commands_stmt = (
        select(RpiCommandDB)
        .where(RpiCommandDB.device_id == device_id)
        .order_by(desc(RpiCommandDB.id))
        .limit(20)
    )
    rpi_commands = db.exec(rpi_commands_stmt).all()
    rpi_commands_data = [
        {
            "id": c.id,
            "type": c.type,
            "payload": c.payload,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "processed_at": c.processed_at.isoformat() if c.processed_at else None,
            "result": c.result,
        }
        for c in rpi_commands
    ]

    # Calculate stats
    one_hour_ago = now - timedelta(hours=1)

    def is_recent(dt):
        if dt is None:
            return False
        try:
            return dt > one_hour_ago
        except TypeError:
            # Handle timezone mismatch
            dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
            one_hour_ago_naive = one_hour_ago.replace(tzinfo=None)
            return dt_naive > one_hour_ago_naive

    results_last_hour = [r for r in results if is_recent(r.created_at)]
    ad_detections_last_hour = len([r for r in results_last_hour if r.is_ad])
    commands_pending = len([c for c in commands if c.status == "pending"])
    commands_done = len([c for c in commands if c.status == "done"])
    commands_failed = len([c for c in commands if c.status == "failed"])

    return {
        "timestamp": now.isoformat(),
        "device_id": device_id,
        "rpi_status": rpi_data,
        "daemon_status": daemon_data,
        "state": state_data,
        "config": config_data,
        "stats": {
            "results_last_hour": len(results_last_hour),
            "ad_detections_last_hour": ad_detections_last_hour,
            "commands_pending": commands_pending,
            "commands_done": commands_done,
            "commands_failed": commands_failed,
        },
        "recent_results": results_data,
        "recent_commands": commands_data,
        "rpi_commands": rpi_commands_data,
    }


@router.get("/monitor/ad-events")
def get_ad_events(
    device_id: str = Query(default="tv-1"),
    limit: int = Query(default=100, ge=1, le=500),
    channel: Optional[str] = Query(default=None),
    db: Session = Depends(get_session),
):
    """
    Permanent log of ad_started / ad_ended transitions.
    Useful for analyzing when ads happen, how long they last, and switch accuracy.
    """
    stmt = (
        select(AdEventDB)
        .where(AdEventDB.device_id == device_id)
        .order_by(desc(AdEventDB.id))
        .limit(limit)
    )
    if channel:
        stmt = stmt.where(AdEventDB.channel == channel)
    events = db.exec(stmt).all()

    ended = [e for e in events if e.event_type == "ad_ended" and e.duration_seconds is not None]
    avg_duration = round(sum(e.duration_seconds for e in ended) / len(ended), 1) if ended else None
    switches = sum(1 for e in events if e.switch_triggered)

    return {
        "device_id": device_id,
        "total": len(events),
        "avg_ad_duration_seconds": avg_duration,
        "switches_triggered": switches,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "channel": e.channel,
                "created_at": e.created_at.isoformat(),
                "duration_seconds": e.duration_seconds,
                "switch_triggered": e.switch_triggered,
            }
            for e in events
        ],
    }


@router.get("/monitor/accuracy")
def get_accuracy(
    device_id: str = Query(default="tv-1"),
    channel: Optional[str] = Query(default=None),
    db: Session = Depends(get_session),
):
    """
    Compute AI accuracy from human-labeled frames.
    Returns overall accuracy and per-channel breakdown.
    """
    stmt = (
        select(FrameHistoryDB)
        .where(FrameHistoryDB.device_id == device_id)
        .where(FrameHistoryDB.label != None)
    )
    if channel:
        stmt = stmt.where(FrameHistoryDB.channel == channel)
    frames = db.exec(stmt).all()

    if not frames:
        return {"device_id": device_id, "total_labeled": 0, "accuracy": None, "by_channel": {}, "by_label": {}}

    def is_correct(f):
        if f.label == "ad":
            return f.is_ad
        return not f.is_ad  # program and transition both mean "not ad"

    correct = sum(1 for f in frames if is_correct(f))
    total = len(frames)
    overrides = sum(1 for f in frames if f.is_override)

    by_channel: dict = {}
    for f in frames:
        ch = f.channel or "(unknown)"
        if ch not in by_channel:
            by_channel[ch] = {"total": 0, "correct": 0, "overrides": 0}
        by_channel[ch]["total"] += 1
        if is_correct(f):
            by_channel[ch]["correct"] += 1
        if f.is_override:
            by_channel[ch]["overrides"] += 1
    for ch in by_channel:
        t = by_channel[ch]["total"]
        by_channel[ch]["accuracy"] = round(by_channel[ch]["correct"] / t * 100, 1) if t else None

    by_label: dict = {}
    for f in frames:
        lbl = f.label
        if lbl not in by_label:
            by_label[lbl] = {"total": 0, "correct": 0}
        by_label[lbl]["total"] += 1
        if is_correct(f):
            by_label[lbl]["correct"] += 1
    for lbl in by_label:
        t = by_label[lbl]["total"]
        by_label[lbl]["accuracy"] = round(by_label[lbl]["correct"] / t * 100, 1) if t else None

    # Confidence analysis: avg confidence on wrong predictions
    wrong_frames = [f for f in frames if not is_correct(f) and f.confidence is not None]
    avg_wrong_conf = round(sum(f.confidence for f in wrong_frames) / len(wrong_frames), 3) if wrong_frames else None

    return {
        "device_id": device_id,
        "total_labeled": total,
        "correct": correct,
        "accuracy": round(correct / total * 100, 1),
        "overrides": overrides,
        "avg_confidence_on_wrong": avg_wrong_conf,
        "by_channel": by_channel,
        "by_label": by_label,
    }


@router.delete("/monitor/commands")
def clear_device_commands(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """
    Delete all device (mobile) commands for the given device_id.
    Use this to clear the command queue and history (pending, done, failed).
    """
    result = db.exec(delete(DeviceCommandDB).where(DeviceCommandDB.device_id == device_id))
    db.commit()
    deleted = result.rowcount if hasattr(result, "rowcount") else 0
    return {"ok": True, "device_id": device_id, "deleted": deleted}


def monitor_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    """
    HTML dashboard for monitoring and controlling the TV Bridge system.
    """
    if api_key != settings.api_key:
        return HTMLResponse(content=UNAUTH_HTML, status_code=401)

    _nav = nav_bar(api_key, device_id, "monitor")
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>TV Bridge Monitor - {device_id}</title>
    <meta charset="utf-8">
    <style>
        {NAV_CSS}
        .main {{ padding: 20px; }}
        .grid {{ display: grid; grid-template-columns: 380px 1fr; gap: 20px; }}
        .card {{
            background: #1a1a2e;
            border-radius: 8px;
            border: 1px solid #2a2a4e;
            overflow: hidden;
        }}
        .card-header {{
            background: #16213e;
            padding: 12px 16px;
            font-weight: 600;
            font-size: 13px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #2a2a4e;
        }}
        .card-body {{ padding: 16px; }}
        .card-title {{ color: #FFD33D; }}

        /* Status Indicator */
        .status-indicator {{
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 12px;
        }}
        .status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}
        .status-dot.online {{ background: #51cf66; box-shadow: 0 0 8px #51cf66; }}
        .status-dot.offline {{ background: #666; }}
        .status-dot.active {{ animation: pulse 1s infinite; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}

        /* RPi Control Panel */
        .rpi-panel {{ margin-bottom: 20px; }}
        .rpi-status {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-bottom: 12px;
        }}
        .rpi-stat {{
            background: #0f0f1a;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
        }}
        .rpi-stat .value {{ font-size: 18px; font-weight: bold; color: #FFD33D; }}
        .rpi-stat .label {{ font-size: 10px; color: #666; margin-top: 2px; }}
        .rpi-components {{
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
        }}
        .component {{
            flex: 1;
            background: #0f0f1a;
            padding: 10px;
            border-radius: 4px;
            text-align: center;
            font-size: 11px;
        }}
        .component.running {{ border: 1px solid #51cf66; color: #51cf66; }}
        .component.stopped {{ border: 1px solid #666; color: #666; }}
        .btn-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
        .btn {{
            background: #2a2a4e;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            color: #eee;
            cursor: pointer;
            font-size: 12px;
            transition: all 0.2s;
        }}
        .btn:hover {{ background: #3a3a5e; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .btn.primary {{ background: #51cf66; color: #000; }}
        .btn.danger {{ background: #ff6b6b; color: #fff; }}
        .btn.warning {{ background: #ffa94d; color: #000; }}
        .btn.small {{ padding: 4px 10px; font-size: 11px; }}

        /* Live Image */
        .live-container {{
            position: relative;
            background: #0f0f1a;
            border-radius: 4px;
            overflow: hidden;
            min-height: 200px;
        }}
        .live-image {{ width: 100%; display: block; }}
        .live-overlay {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 8px 12px;
            background: linear-gradient(transparent, rgba(0,0,0,0.9));
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .live-badge {{
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: bold;
        }}
        .live-badge.ad {{ background: #ff6b6b; color: #fff; }}
        .live-badge.ok {{ background: #51cf66; color: #000; }}
        .no-image {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 200px;
            color: #444;
        }}
        .confidence-bar {{
            height: 3px;
            background: #0f0f1a;
            margin-top: 8px;
            border-radius: 2px;
            overflow: hidden;
        }}
        .confidence-fill {{ height: 100%; transition: width 0.3s; }}
        .confidence-fill.ad {{ background: #ff6b6b; }}
        .confidence-fill.ok {{ background: #51cf66; }}

        /* Image Log Gallery */
        .image-gallery {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 8px;
        }}
        .gallery-item {{
            position: relative;
            aspect-ratio: 16/9;
            background: #0f0f1a;
            border-radius: 4px;
            overflow: hidden;
            cursor: pointer;
            border: 2px solid transparent;
        }}
        .gallery-item:hover {{ border-color: #FFD33D; }}
        .gallery-item.ad {{ border-color: rgba(255, 107, 107, 0.5); }}
        .gallery-item img {{ width: 100%; height: 100%; object-fit: cover; }}
        .gallery-badge {{
            position: absolute;
            top: 4px;
            right: 4px;
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}
        .gallery-badge.ad {{ background: #ff6b6b; }}
        .gallery-badge.ok {{ background: #51cf66; }}

        /* Stats Row */
        .stats-row {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
        }}
        .stat-box {{
            background: #1a1a2e;
            border: 1px solid #2a2a4e;
            border-radius: 8px;
            padding: 12px 20px;
            text-align: center;
            min-width: 100px;
        }}
        .stat-box .number {{ font-size: 24px; font-weight: bold; color: #FFD33D; }}
        .stat-box .label {{ font-size: 10px; color: #666; margin-top: 4px; }}

        /* Logs */
        .log-list {{ max-height: 280px; overflow-y: auto; }}
        .log-entry {{
            padding: 8px 12px;
            border-bottom: 1px solid #2a2a4e;
            font-family: 'Monaco', monospace;
            font-size: 11px;
            display: flex;
            gap: 10px;
        }}
        .log-entry:hover {{ background: #16213e; }}
        .log-entry.ad {{ background: rgba(255, 107, 107, 0.1); }}
        .log-entry.pending {{ color: #ffa94d; }}
        .log-entry.done {{ color: #51cf66; }}
        .log-entry.failed {{ color: #ff6b6b; }}
        .log-time {{ color: #666; min-width: 65px; }}
        .log-icon {{ min-width: 16px; }}

        /* Config */
        .config-row {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #2a2a4e;
            font-size: 13px;
        }}
        .config-row:last-child {{ border: none; }}
        .config-label {{ color: #888; }}
        .config-value {{ font-family: monospace; background: #0f0f1a; padding: 2px 8px; border-radius: 3px; }}

        /* Modal */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.9);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }}
        .modal.active {{ display: flex; }}
        .modal-content {{
            max-width: 90%;
            max-height: 90%;
            position: relative;
        }}
        .modal-content img {{ max-width: 100%; max-height: 80vh; }}
        .modal-info {{
            background: #1a1a2e;
            padding: 12px;
            border-radius: 0 0 8px 8px;
        }}
        .modal-close {{
            position: absolute;
            top: -30px;
            right: 0;
            color: #fff;
            font-size: 24px;
            cursor: pointer;
        }}

        @media (max-width: 1000px) {{
            .grid {{ grid-template-columns: 1fr; }}
            .stats-row {{ flex-wrap: wrap; }}
            .image-gallery {{ grid-template-columns: repeat(3, 1fr); }}
        }}
    </style>
</head>
<body>
{_nav}
    <div class="main">
        <div class="stats-row">
            <div class="stat-box">
                <div class="number" id="stat-results">-</div>
                <div class="label">Results (1h)</div>
            </div>
            <div class="stat-box">
                <div class="number" id="stat-ads">-</div>
                <div class="label">Ads (1h)</div>
            </div>
            <div class="stat-box">
                <div class="number" id="stat-pending">-</div>
                <div class="label">Pending</div>
            </div>
            <div class="stat-box">
                <div class="number" id="stat-done">-</div>
                <div class="label">Done</div>
            </div>
        </div>

        <div class="grid">
            <div class="left-col">
                <!-- Phase 0: Daemon Control (always visible) -->
                <div class="card" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);">
                    <div class="card-header" style="border-bottom: 1px solid rgba(255,255,255,0.2);">
                        <span class="card-title" style="color: #fff;">🤖 System Control</span>
                        <div class="status-indicator" style="display:flex;align-items:center;gap:8px;">
                            <div class="status-dot" id="daemon-status-dot"></div>
                            <span id="daemon-status-text" style="color: #fff;">Unknown</span>
                        </div>
                    </div>
                    <div class="card-body">
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px;">
                            <div style="background:rgba(0,0,0,0.2);padding:12px;border-radius:8px;">
                                <div style="font-size:11px;color:rgba(255,255,255,0.7);text-transform:uppercase;margin-bottom:4px;">Daemon</div>
                                <div style="font-size:18px;font-weight:600;color:#fff;" id="daemon-running">-</div>
                            </div>
                            <div style="background:rgba(0,0,0,0.2);padding:12px;border-radius:8px;">
                                <div style="font-size:11px;color:rgba(255,255,255,0.7);text-transform:uppercase;margin-bottom:4px;">Controller</div>
                                <div style="font-size:18px;font-weight:600;color:#fff;" id="controller-running">-</div>
                            </div>
                        </div>
                        <div style="display:flex;gap:10px;flex-wrap:wrap;">
                            <button type="button" id="start-controller-btn" class="btn" style="flex:1;min-width:140px;background:#51cf66;color:#fff;padding:12px;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;box-shadow:0 2px 8px rgba(0,0,0,0.2);" onclick="startController()">▶ Start Controller</button>
                            <button type="button" id="stop-controller-btn" class="btn" style="flex:1;min-width:140px;background:#ff6b6b;color:#fff;padding:12px;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;box-shadow:0 2px 8px rgba(0,0,0,0.2);" onclick="stopController()">■ Stop Controller</button>
                        </div>
                        <div style="margin-top:15px;padding:10px;background:rgba(0,0,0,0.2);border-radius:6px;font-size:11px;color:rgba(255,255,255,0.8);">
                            <div>Controller PID: <span id="controller-pid" style="color:#fff;font-weight:600;">-</span></div>
                            <div style="margin-top:4px;">Last update: <span id="daemon-last-update" style="color:#fff;font-weight:600;">-</span></div>
                        </div>
                    </div>
                </div>

                <!-- Phase 2: Live TV Feed - HORE vedľa controllera (hidden when capture/detect off) -->
                <div class="card" id="phase2-live" style="margin-top:20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">📺 Live TV Feed</span>
                        <div class="status-indicator">
                            <span class="status-dot active" id="live-dot" style="display:none;"></span>
                            <span id="live-status">No feed</span>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="live-container" id="live-container">
                            <div class="no-image" id="no-image">
                                <div style="font-size: 36px;">📡</div>
                                <div style="margin-top: 8px; font-size: 12px;">Waiting for frames...</div>
                            </div>
                            <img id="live-image" class="live-image" style="display:none;" alt="Live">
                            <div class="live-overlay" id="live-overlay" style="display:none;">
                                <span class="live-badge" id="live-badge">-</span>
                                <span id="live-conf" style="font-size: 11px; color: #ccc;">-</span>
                            </div>
                        </div>
                        <div class="confidence-bar">
                            <div class="confidence-fill" id="conf-fill" style="width: 0;"></div>
                        </div>
                    </div>
                </div>

                <!-- Phase 1: Controller Running - Capture/Detect Control (hidden when controller off) -->
                <div class="card rpi-panel" id="phase1-controls" style="margin-top:20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">📡 Capture & Detection Control</span>
                        <div class="status-indicator">
                            <span class="status-dot" id="rpi-status-dot"></span>
                            <span id="rpi-status-text">Unknown</span>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="rpi-components" style="margin-bottom:15px;">
                            <div class="component" id="capture-status">
                                <div>CAPTURE</div>
                                <div id="capture-label">-</div>
                            </div>
                            <div class="component" id="detect-status">
                                <div>DETECT</div>
                                <div id="detect-label">-</div>
                            </div>
                        </div>

                        <div style="margin-bottom:12px;padding:12px;background:#f8f9fa;border-radius:6px;border-left:4px solid #667eea;">
                            <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:#333;">Quick Actions</div>
                            <div style="display:flex;gap:8px;margin-bottom:8px;">
                                <button class="btn primary" style="flex:1;" onclick="sendRpiCommand('start_capture')">▶ Start Capture</button>
                                <button class="btn primary" style="flex:1;" onclick="sendRpiCommand('start_detect')">▶ Start Detect</button>
                            </div>
                            <div style="display:flex;gap:8px;">
                                <button class="btn warning" style="flex:1;" onclick="sendRpiCommand('restart_all')">🔄 Restart All</button>
                                <button class="btn danger" style="flex:1;" onclick="sendRpiCommand('stop_all')">■ Stop All</button>
                            </div>
                        </div>

                        <div style="font-size:11px;color:#666;">
                            Last heartbeat: <span id="rpi-heartbeat" style="font-weight:600;">-</span>
                        </div>
                    </div>
                </div>

                <!-- Phase 2: System Stats (hidden when controller off) -->
                <div class="card" id="phase2-stats" style="margin-top:20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">📊 System Stats</span>
                    </div>
                    <div class="card-body">
                        <div class="rpi-status">
                            <div class="rpi-stat">
                                <div class="value" id="rpi-frames">-</div>
                                <div class="label">Frames</div>
                            </div>
                            <div class="rpi-stat">
                                <div class="value" id="rpi-processed">-</div>
                                <div class="label">Processed</div>
                            </div>
                            <div class="rpi-stat">
                                <div class="value" id="rpi-cpu">-</div>
                                <div class="label">CPU %</div>
                            </div>
                            <div class="rpi-stat">
                                <div class="value" id="rpi-mem">-</div>
                                <div class="label">Memory %</div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Phase 2: Current State (hidden when controller off) -->
                <div class="card" id="phase2-state" style="margin-top: 20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">📋 Current State</span>
                        <div class="status-indicator">
                            <span class="status-dot" id="ad-dot"></span>
                            <span id="ad-status-text">-</span>
                        </div>
                    </div>
                    <div class="card-body">
                        <div class="config-row">
                            <span class="config-label">Ad Active</span>
                            <span class="config-value" id="ad-active">-</span>
                        </div>
                        <div class="config-row">
                            <span class="config-label">Since</span>
                            <span class="config-value" id="ad-since">-</span>
                        </div>
                        <div class="config-row">
                            <span class="config-label">Fallback CH</span>
                            <span class="config-value" id="fallback-ch">-</span>
                        </div>
                        <div class="config-row">
                            <span class="config-label">Original CH</span>
                            <span class="config-value" id="original-ch">-</span>
                        </div>
                        <div class="config-row">
                            <span class="config-label">Auto-Switch</span>
                            <span class="config-value" id="auto-switch">-</span>
                        </div>
                    </div>
                </div>
            </div>

            <div class="right-col">
                <!-- Phase 2: Image Log - 10 posledných fotiek HORE (hidden when detect off) -->
                <div class="card" id="phase2-images" style="display:none;">
                    <div class="card-header">
                        <span class="card-title">🖼️ Detection Log (Last 10 Images)</span>
                        <button class="btn small" onclick="fetchImageLog()">Refresh</button>
                    </div>
                    <div class="card-body">
                        <div class="image-gallery" id="image-gallery">
                            <div style="grid-column: 1/-1; text-align: center; color: #666; padding: 20px;">
                                Loading images...
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Phase 2: Ad Results Log - Reklama/nie + % (hidden when detect off) -->
                <div class="card" id="phase2-results" style="margin-top: 20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">📊 Ad Detection Results</span>
                    </div>
                    <div class="card-body">
                        <div class="log-list" id="results-log">Loading...</div>
                    </div>
                </div>

                <!-- Phase 1: RPi Commands Log (hidden when controller off) -->
                <div class="card" id="phase1-rpi-commands" style="margin-top: 20px;display:none;">
                    <div class="card-header">
                        <span class="card-title">🔧 RPi Commands</span>
                    </div>
                    <div class="card-body">
                        <div class="log-list" id="rpi-commands-log">Loading...</div>
                    </div>
                </div>

                <!-- Phase 1: Debug Info & Controller Logs -->
                <div class="card" id="phase1-debug" style="margin-top: 20px;display:none;border-left:4px solid #ffa94d;">
                    <div class="card-header" style="background:#fff3cd;">
                        <span class="card-title" style="color:#856404;">🐛 Debug Info</span>
                    </div>
                    <div class="card-body" style="background:#fffef8;">
                        <div style="margin-bottom:15px;padding:12px;background:#fff;border:1px solid #ffc107;border-radius:6px;">
                            <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:#856404;">📋 Controller Logs (Last 50 lines)</div>
                            <div style="font-size:11px;color:#666;margin-bottom:10px;">
                                Logy z <code>rpi_controller.py</code> na Raspberry Pi. Ak detect nefunguje, pozri sa na chyby.
                            </div>
                            <div style="display:flex;gap:8px;margin-bottom:10px;">
                                <button class="btn small" style="background:#ffc107;color:#000;" onclick="alert('SSH to RPi:\\n\\ntail -f ~/CLIP/controller.log\\n\\nOr:\\n\\nsudo journalctl -u rpi-daemon -f')">📖 How to view logs</button>
                            </div>
                            <div style="font-family:monospace;font-size:11px;background:#1a1a2e;color:#51cf66;padding:12px;border-radius:4px;max-height:300px;overflow-y:auto;white-space:pre-wrap;" id="controller-log-output">
                                <div style="color:#666;">Controller logs will be shown here in future version.</div>
                                <div style="color:#666;margin-top:8px;">For now, check logs on RPi:</div>
                                <div style="color:#ffa94d;margin-top:8px;">$ ssh rpi@your-rpi-ip</div>
                                <div style="color:#ffa94d;">$ tail -f ~/CLIP/controller.log</div>
                                <div style="color:#666;margin-top:12px;">Or daemon logs:</div>
                                <div style="color:#ffa94d;">$ sudo journalctl -u rpi-daemon -f</div>
                                <div style="color:#666;margin-top:12px;">Check detect errors:</div>
                                <div style="color:#ffa94d;">$ grep "DETECT" ~/CLIP/controller.log | tail -20</div>
                            </div>
                        </div>
                        
                        <div style="padding:12px;background:#fff;border:1px solid #ffc107;border-radius:6px;">
                            <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:#856404;">🔍 Common Detect Issues</div>
                            <ul style="font-size:11px;color:#666;margin:0;padding-left:20px;">
                                <li><strong>rpi_detect.py not found</strong> - Check if file exists in ~/CLIP/</li>
                                <li><strong>torch/CLIP not installed</strong> - Run: pip3 install torch clip pillow</li>
                                <li><strong>No images in capture dir</strong> - Check if capture is running and creating files</li>
                                <li><strong>Permission denied</strong> - Check file permissions: chmod +x ~/CLIP/rpi_detect.py</li>
                                <li><strong>Python error</strong> - Check detect script: python3 ~/CLIP/rpi_detect.py nova</li>
                            </ul>
                        </div>
                    </div>
                </div>

                <!-- Mobile Commands Log -->
                <div class="card" style="margin-top: 20px;">
                    <div class="card-header">
                        <span class="card-title">📱 Current Mobile Commands (To be executed)</span>
                        <span id="pending-count" style="background:#ffa94d;color:#000;padding:2px 8px;border-radius:3px;font-size:11px;">0 pending</span>
                    </div>
                    <div class="card-body">
                        <div class="log-list" id="current-commands-log">Loading...</div>
                    </div>
                </div>

                <!-- Mobile Commands History -->
                <div class="card" style="margin-top: 20px;">
                    <div class="card-header">
                        <span class="card-title">📱 Mobile Commands History (Last 10)</span>
                        <button type="button" id="clear-commands-btn" class="btn" style="background:#ff6b6b;color:#fff;padding:6px 12px;font-size:12px;border:none;border-radius:4px;cursor:pointer;" title="Vymazať všetky príkazy pre mobil">Premazať všetky</button>
                    </div>
                    <div class="card-body">
                        <div class="log-list" id="commands-log">Loading...</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Image Modal -->
    <div class="modal" id="image-modal" onclick="closeModal()">
        <div class="modal-content" onclick="event.stopPropagation()">
            <span class="modal-close" onclick="closeModal()">&times;</span>
            <img id="modal-image" src="">
            <div class="modal-info">
                <div id="modal-info-text"></div>
            </div>
        </div>
    </div>

    <script>
        const DEVICE_ID = '{device_id}';
        const API_KEY = '{api_key}';
        const BASE_URL = '';

        function formatTime(iso) {{
            if (!iso) return '-';
            return new Date(iso).toLocaleTimeString('sk-SK', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
        }}

        function timeSince(iso) {{
            if (!iso) return '-';
            const s = Math.floor((Date.now() - new Date(iso)) / 1000);
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s/60) + 'm ago';
            return Math.floor(s/3600) + 'h ago';
        }}

        async function fetchData() {{
            try {{
                const res = await fetch('/v1/monitor/data?device_id=' + DEVICE_ID, {{
                    headers: {{ 'X-API-Key': API_KEY }}
                }});
                const data = await res.json();
                updateUI(data);
                document.getElementById('update-time').textContent = 'Updated: ' + formatTime(data.timestamp);
            }} catch (e) {{
                console.error('Fetch error:', e);
                document.getElementById('update-time').textContent = 'Error: ' + e.message;
            }}
        }}

        async function fetchImage() {{
            try {{
                const res = await fetch('/v1/live-image?device_id=' + DEVICE_ID, {{
                    headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }}
                }});
                const data = await res.json();

                const img = document.getElementById('live-image');
                const noImg = document.getElementById('no-image');
                const overlay = document.getElementById('live-overlay');
                const badge = document.getElementById('live-badge');
                const conf = document.getElementById('live-conf');
                const confFill = document.getElementById('conf-fill');
                const liveDot = document.getElementById('live-dot');
                const liveStatus = document.getElementById('live-status');

                if (data.has_image && data.image_base64) {{
                    img.src = 'data:image/jpeg;base64,' + data.image_base64;
                    img.style.display = 'block';
                    noImg.style.display = 'none';
                    overlay.style.display = 'flex';
                    liveDot.style.display = 'block';
                    liveStatus.textContent = timeSince(data.timestamp);

                    if (data.is_ad) {{
                        badge.textContent = 'AD DETECTED';
                        badge.className = 'live-badge ad';
                    }} else {{
                        badge.textContent = 'NO AD';
                        badge.className = 'live-badge ok';
                    }}

                    const c = data.confidence ? (data.confidence * 100).toFixed(0) : 0;
                    conf.textContent = c + '%';
                    confFill.style.width = c + '%';
                    confFill.className = 'confidence-fill ' + (data.is_ad ? 'ad' : 'ok');
                }} else {{
                    img.style.display = 'none';
                    noImg.style.display = 'flex';
                    overlay.style.display = 'none';
                    liveDot.style.display = 'none';
                    liveStatus.textContent = 'No feed';
                }}
            }} catch (e) {{
                console.error('Image error:', e);
            }}
        }}

        async function fetchImageLog() {{
            try {{
                const res = await fetch('/v1/rpi/image-log?device_id=' + DEVICE_ID + '&limit=10&include_images=true', {{
                    headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }}
                }});
                const data = await res.json();

                const gallery = document.getElementById('image-gallery');
                if (!data.items || data.items.length === 0) {{
                    gallery.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#666;padding:20px;">No images in log yet</div>';
                    return;
                }}

                gallery.innerHTML = data.items.map((item, i) => `
                    <div class="gallery-item ${{item.is_ad ? 'ad' : ''}}" onclick="showImage(${{i}})">
                        <img src="data:image/jpeg;base64,${{item.image_base64}}" alt="">
                        <span class="gallery-badge ${{item.is_ad ? 'ad' : 'ok'}}"></span>
                    </div>
                `).join('');

                window.imageLogData = data.items;
            }} catch (e) {{
                console.error('Image log error:', e);
            }}
        }}

        function showImage(index) {{
            if (!window.imageLogData || !window.imageLogData[index]) return;
            const item = window.imageLogData[index];

            document.getElementById('modal-image').src = 'data:image/jpeg;base64,' + item.image_base64;
            document.getElementById('modal-info-text').innerHTML = `
                <strong>${{item.is_ad ? 'AD DETECTED' : 'NO AD'}}</strong> |
                Confidence: ${{item.confidence ? (item.confidence * 100).toFixed(0) + '%' : '-'}} |
                Captured: ${{formatTime(item.captured_at)}}
            `;
            document.getElementById('image-modal').classList.add('active');
        }}

        function closeModal() {{
            document.getElementById('image-modal').classList.remove('active');
        }}

        async function sendRpiCommand(type, payload = {{}}) {{
            try {{
                const res = await fetch('/v1/rpi/commands', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'X-API-Key': API_KEY,
                        'X-Device-Id': DEVICE_ID
                    }},
                    body: JSON.stringify({{ type, payload }})
                }});
                const data = await res.json();
                console.log('Command sent:', data);
                fetchData();
            }} catch (e) {{
                console.error('Command error:', e);
                alert('Failed to send command: ' + e.message);
            }}
        }}

        async function clearDeviceCommands() {{
            if (!confirm('Naozaj vymazať všetky príkazy pre mobil (pending, done, failed)?')) return;
            const btn = document.getElementById('clear-commands-btn');
            btn.disabled = true;
            btn.textContent = 'Mažem...';
            try {{
                const res = await fetch('/v1/monitor/commands?device_id=' + DEVICE_ID, {{
                    method: 'DELETE',
                    headers: {{ 'X-API-Key': API_KEY }}
                }});
                const data = await res.json();
                if (data.ok) {{
                    fetchData();
                }} else {{
                    alert('Chyba: ' + (data.detail || JSON.stringify(data)));
                }}
            }} catch (e) {{
                console.error('Clear commands error:', e);
                alert('Chyba: ' + e.message);
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Premazať všetky';
            }}
        }}

        async function startController() {{
            const btn = document.getElementById('start-controller-btn');
            btn.disabled = true;
            btn.textContent = 'Starting...';
            try {{
                const res = await fetch('/v1/rpi/daemon-commands', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'X-API-Key': API_KEY,
                        'X-Device-Id': DEVICE_ID
                    }},
                    body: JSON.stringify({{ type: 'start_controller', payload: {{}} }})
                }});
                const data = await res.json();
                if (data.ok) {{
                    console.log('Start controller command sent:', data);
                    setTimeout(fetchData, 1000);
                }} else {{
                    alert('Failed: ' + (data.detail || JSON.stringify(data)));
                }}
            }} catch (e) {{
                console.error('Start controller error:', e);
                alert('Failed: ' + e.message);
            }} finally {{
                btn.disabled = false;
                btn.textContent = '▶ Start Controller';
            }}
        }}

        async function stopController() {{
            if (!confirm('Stop controller? This will stop capture and detection.')) return;
            const btn = document.getElementById('stop-controller-btn');
            btn.disabled = true;
            btn.textContent = 'Stopping...';
            try {{
                const res = await fetch('/v1/rpi/daemon-commands', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'X-API-Key': API_KEY,
                        'X-Device-Id': DEVICE_ID
                    }},
                    body: JSON.stringify({{ type: 'stop_controller', payload: {{}} }})
                }});
                const data = await res.json();
                if (data.ok) {{
                    console.log('Stop controller command sent:', data);
                    setTimeout(fetchData, 1000);
                }} else {{
                    alert('Failed: ' + (data.detail || JSON.stringify(data)));
                }}
            }} catch (e) {{
                console.error('Stop controller error:', e);
                alert('Failed: ' + e.message);
            }} finally {{
                btn.disabled = false;
                btn.textContent = '■ Stop Controller';
            }}
        }}

        function updateUI(data) {{
            // Stats
            document.getElementById('stat-results').textContent = data.stats.results_last_hour;
            document.getElementById('stat-ads').textContent = data.stats.ad_detections_last_hour;
            document.getElementById('stat-pending').textContent = data.stats.commands_pending;
            document.getElementById('stat-done').textContent = data.stats.commands_done;

            // RPi Status
            const rpi = data.rpi_status;
            const rpiDot = document.getElementById('rpi-status-dot');
            const rpiText = document.getElementById('rpi-status-text');

            if (rpi && rpi.is_online) {{
                rpiDot.className = 'status-dot online active';
                rpiText.textContent = 'Online';
                document.getElementById('rpi-frames').textContent = rpi.frames_captured || 0;
                document.getElementById('rpi-processed').textContent = rpi.frames_processed || 0;
                document.getElementById('rpi-cpu').textContent = rpi.cpu_percent ? rpi.cpu_percent.toFixed(0) : '-';
                document.getElementById('rpi-mem').textContent = rpi.memory_percent ? rpi.memory_percent.toFixed(0) : '-';
                document.getElementById('rpi-heartbeat').textContent = timeSince(rpi.last_heartbeat);

                const capStatus = document.getElementById('capture-status');
                const detStatus = document.getElementById('detect-status');
                capStatus.className = 'component ' + (rpi.capture_running ? 'running' : 'stopped');
                detStatus.className = 'component ' + (rpi.detect_running ? 'running' : 'stopped');
                document.getElementById('capture-label').textContent = rpi.capture_running ? 'Running' : 'Stopped';
                document.getElementById('detect-label').textContent = rpi.detect_running ? 'Running' : 'Stopped';
            }} else {{
                rpiDot.className = 'status-dot offline';
                rpiText.textContent = 'Offline';
                document.getElementById('rpi-frames').textContent = '-';
                document.getElementById('rpi-processed').textContent = '-';
                document.getElementById('rpi-cpu').textContent = '-';
                document.getElementById('rpi-mem').textContent = '-';
                document.getElementById('rpi-heartbeat').textContent = rpi && rpi.last_heartbeat ? timeSince(rpi.last_heartbeat) : 'Never';
                document.getElementById('capture-status').className = 'component stopped';
                document.getElementById('detect-status').className = 'component stopped';
                document.getElementById('capture-label').textContent = '-';
                document.getElementById('detect-label').textContent = '-';
            }}

            // Ad State
            const adActive = data.state.ad_active;
            const adDot = document.getElementById('ad-dot');
            adDot.className = 'status-dot ' + (adActive ? 'online active' : '');
            adDot.style.background = adActive ? '#ff6b6b' : '#51cf66';
            document.getElementById('ad-status-text').textContent = adActive ? 'AD ACTIVE' : 'No Ad';
            document.getElementById('ad-active').textContent = adActive ? 'YES' : 'NO';
            document.getElementById('ad-since').textContent = adActive ? timeSince(data.state.ad_since) : '-';

            // Config
            document.getElementById('fallback-ch').textContent = data.config.fallback_channel || 'Not set';
            document.getElementById('original-ch').textContent = data.config.original_channel || 'Not set';
            document.getElementById('auto-switch').textContent = data.config.auto_switch_enabled ? 'Enabled' : 'Disabled';

            // Daemon Status
            const daemon = data.daemon_status;
            const daemonDot = document.getElementById('daemon-status-dot');
            const daemonText = document.getElementById('daemon-status-text');
            
            let controllerRunning = false;
            let captureRunning = false;
            let detectRunning = false;
            
            if (daemon && daemon.daemon_running) {{
                daemonDot.className = 'status-dot online active';
                daemonText.textContent = 'Running';
                document.getElementById('daemon-running').textContent = 'YES';
                document.getElementById('daemon-running').style.color = '#fff';
                
                if (daemon.controller_running) {{
                    controllerRunning = true;
                    document.getElementById('controller-running').textContent = 'RUNNING';
                    document.getElementById('controller-running').style.color = '#51cf66';
                }} else {{
                    document.getElementById('controller-running').textContent = 'STOPPED';
                    document.getElementById('controller-running').style.color = 'rgba(255,255,255,0.6)';
                }}
                
                document.getElementById('controller-pid').textContent = daemon.controller_pid || '-';
                document.getElementById('daemon-last-update').textContent = timeSince(daemon.updated_at);
            }} else {{
                daemonDot.className = 'status-dot offline';
                daemonText.textContent = daemon ? 'Stopped' : 'Unknown';
                document.getElementById('daemon-running').textContent = 'NO';
                document.getElementById('daemon-running').style.color = '#ff6b6b';
                document.getElementById('controller-running').textContent = '-';
                document.getElementById('controller-running').style.color = 'rgba(255,255,255,0.6)';
                document.getElementById('controller-pid').textContent = '-';
                document.getElementById('daemon-last-update').textContent = daemon && daemon.updated_at ? timeSince(daemon.updated_at) : 'Never';
            }}

            // Check if capture/detect are running (from rpi_status)
            if (rpi && rpi.is_online) {{
                captureRunning = rpi.capture_running;
                detectRunning = rpi.detect_running;
            }}

            // PHASE VISIBILITY LOGIC
            // Phase 0: Always visible (daemon controls)
            
            // Phase 1: Show when controller is running
            document.getElementById('phase1-controls').style.display = controllerRunning ? 'block' : 'none';
            document.getElementById('phase1-rpi-commands').style.display = controllerRunning ? 'block' : 'none';
            document.getElementById('phase1-debug').style.display = controllerRunning ? 'block' : 'none';
            
            // Phase 2: Show when controller is running
            document.getElementById('phase2-stats').style.display = controllerRunning ? 'block' : 'none';
            document.getElementById('phase2-state').style.display = controllerRunning ? 'block' : 'none';
            
            // Phase 2: Show live/images/results only when capture/detect running
            document.getElementById('phase2-live').style.display = (captureRunning || detectRunning) ? 'block' : 'none';
            document.getElementById('phase2-images').style.display = detectRunning ? 'block' : 'none';
            document.getElementById('phase2-results').style.display = detectRunning ? 'block' : 'none';

            // Results Log  - show last 20, most recent first
            document.getElementById('results-log').innerHTML = data.recent_results.slice(0, 20).map(r => `
                <div class="log-entry ${{r.is_ad ? 'ad' : ''}}">
                    <span class="log-time">${{formatTime(r.created_at)}}</span>
                    <span class="log-icon">${{r.is_ad ? '🚨' : '✓'}}</span>
                    <span>${{r.is_ad ? '<strong>AD DETECTED</strong>' : 'Normal'}} ${{r.confidence ? ' - Conf: ' + (r.confidence*100).toFixed(0) + '%' : ''}}</span>
                </div>
            `).join('') || '<div class="log-entry" style="color:#666;padding:20px;text-align:center;">No detection results yet</div>';

            // RPi Commands Log - show last 10, most recent first
            document.getElementById('rpi-commands-log').innerHTML = data.rpi_commands.slice(0, 10).map(c => `
                <div class="log-entry ${{c.status}}">
                    <span class="log-time">${{formatTime(c.created_at)}}</span>
                    <span class="log-icon">${{c.status === 'done' ? '✓' : (c.status === 'failed' ? '✗' : '⏳')}}</span>
                    <span><strong>${{c.type}}</strong> [${{c.status}}]${{c.processed_at ? ' - ' + timeSince(c.processed_at) : ''}}</span>
                </div>
            `).join('') || '<div class="log-entry" style="color:#666;padding:20px;text-align:center;">No RPi commands yet</div>';

            // Mobile Commands Log - show last 10, most recent first with MORE DETAILS
            document.getElementById('commands-log').innerHTML = data.recent_commands.slice(0, 10).map(c => {{
                let desc = c.type;
                let extraInfo = '';
                
                if (c.type === 'switch_channel' && c.payload && c.payload.channel) {{
                    desc = `📺 Switch to Channel ${{c.payload.channel}}`;
                    if (c.payload.reason) {{
                        extraInfo = ` (${{c.payload.reason}})`;
                    }}
                }}
                
                let statusBadge = '';
                if (c.status === 'pending') statusBadge = '<span style="color:#ffa94d">⏳ Pending</span>';
                else if (c.status === 'done') statusBadge = '<span style="color:#51cf66">✓ Done</span>';
                else if (c.status === 'failed') statusBadge = '<span style="color:#ff6b6b">✗ Failed</span>';
                
                let timing = '';
                if (c.processed_at) {{
                    timing = ` - Processed ${{timeSince(c.processed_at)}}`;
                }} else if (c.status === 'pending') {{
                    timing = ` - Waiting ${{timeSince(c.created_at)}}`;
                }}
                
                return `
                    <div class="log-entry ${{c.status}}">
                        <span class="log-time">${{formatTime(c.created_at)}}</span>
                        <span class="log-icon">${{c.status === 'done' ? '✓' : c.status === 'failed' ? '✗' : '⏳'}}</span>
                        <div style="flex:1;">
                            <div><strong>${{desc}}</strong>${{extraInfo}}</div>
                            <div style="font-size:10px;color:#888;margin-top:2px;">${{statusBadge}}${{timing}}</div>
                        </div>
                    </div>
                `;
            }}).join('') || '<div class="log-entry" style="color:#666;padding:20px;text-align:center;">No mobile app commands yet. Commands will appear here when the mobile app receives switch instructions.</div>';
            
            // Current Pending Commands (most important for monitoring!)
            const pendingCommands = data.recent_commands.filter(c => c.status === 'pending');
            document.getElementById('pending-count').textContent = `${{pendingCommands.length}} pending`;
            document.getElementById('pending-count').style.background = pendingCommands.length > 0 ? '#ffa94d' : '#666';
            
            document.getElementById('current-commands-log').innerHTML = pendingCommands.map(c => {{
                let desc = c.type;
                let extraInfo = '';
                
                if (c.type === 'switch_channel' && c.payload && c.payload.channel) {{
                    desc = `📺 Switch to Channel ${{c.payload.channel}}`;
                    if (c.payload.reason) {{
                        extraInfo = ` <span style="color:#888;">(${{c.payload.reason}})</span>`;
                    }}
                }}
                
                const ageSeconds = Math.floor((Date.now() - new Date(c.created_at)) / 1000);
                let ageColor = '#51cf66';
                if (ageSeconds > 10) ageColor = '#ffa94d';
                if (ageSeconds > 30) ageColor = '#ff6b6b';
                
                return `
                    <div class="log-entry pending" style="border-left:4px solid #ffa94d;">
                        <span class="log-time">${{formatTime(c.created_at)}}</span>
                        <span class="log-icon">⏳</span>
                        <div style="flex:1;">
                            <div style="font-size:14px;"><strong>${{desc}}</strong>${{extraInfo}}</div>
                            <div style="font-size:10px;color:${{ageColor}};margin-top:4px;">
                                ⏱️ Waiting for ${{ageSeconds}}s - Command ID: ${{c.id}}
                            </div>
                        </div>
                    </div>
                `;
            }}).join('') || '<div class="log-entry" style="color:#51cf66;padding:20px;text-align:center;">✓ No pending commands - all clear!</div>';
        }}

        // Initial fetch
        fetchData();
        fetchImage();
        fetchImageLog();

        document.getElementById('clear-commands-btn').addEventListener('click', clearDeviceCommands);

        // Refresh intervals
        setInterval(fetchData, 2000);
        setInterval(fetchImage, 2000);
        setInterval(fetchImageLog, 10000);

        // Keyboard shortcut to close modal
        document.addEventListener('keydown', e => {{
            if (e.key === 'Escape') closeModal();
        }});
    </script>
    <script>{NAV_STATUS_JS}</script>
</body>
</html>
"""
    return HTMLResponse(content=html)


def live_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    if api_key != settings.api_key:
        return HTMLResponse(content=UNAUTH_HTML, status_code=401)

    _nav = nav_bar(api_key, device_id, "live")
    return HTMLResponse(content=_build_live_html(api_key, device_id, _nav))


def _build_live_html(api_key: str, device_id: str, nav_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="sk">
<head>
<title>Live Detection</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
{NAV_CSS}
:root{{--ad:#ef4444;--prog:#22c55e;--dim:#0a0a18;--card:#1a1a2e;--border:#1e1e3a;--mu:#64748b;--tx:#e2e8f0}}
.banner{{display:flex;align-items:center;justify-content:space-between;padding:20px 28px;transition:background .4s,border-color .4s;border-bottom:2px solid var(--border);gap:20px;flex-wrap:wrap}}
.banner.is-ad{{background:linear-gradient(135deg,#450a0a 0%,#7f1d1d 100%);border-color:#ef4444}}
.banner.is-prog{{background:linear-gradient(135deg,#052e16 0%,#14532d 100%);border-color:#22c55e}}
.banner.is-uk{{background:linear-gradient(135deg,#0f0f1a 0%,#1a1a2e 100%);border-color:var(--border)}}
.s-pill{{display:flex;align-items:center;gap:14px}}
.s-ico{{font-size:42px;line-height:1}}
.s-lbl{{font-size:32px;font-weight:800;letter-spacing:-.5px;line-height:1}}
.s-sub{{font-size:13px;color:rgba(255,255,255,.6);margin-top:3px}}
.chips{{display:flex;gap:16px;flex-wrap:wrap}}
.chip{{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:12px 20px;text-align:center;min-width:90px}}
.chip .v{{font-size:22px;font-weight:700;line-height:1}}
.chip .l{{font-size:10px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:.6px;margin-top:3px}}
.pulse{{animation:pulse 1.2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.main{{padding:16px 20px;display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:900px){{.main{{grid-template-columns:1fr}}}}
.col{{display:flex;flex-direction:column;gap:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
.card-hd{{padding:11px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}}
.card-ti{{font-size:12px;font-weight:600;color:var(--mu);text-transform:uppercase;letter-spacing:.6px}}
.cb{{padding:16px}}
.frame-wrap{{position:relative;background:#080810;border-radius:6px;overflow:hidden;aspect-ratio:16/9;cursor:pointer}}
.frame-wrap img{{width:100%;height:100%;object-fit:cover;display:block}}
.frame-empty{{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:8px;color:var(--mu)}}
.f-ov{{position:absolute;bottom:0;left:0;right:0;padding:10px 12px;background:linear-gradient(transparent,rgba(0,0,0,.85));display:flex;align-items:center;justify-content:space-between}}
.fbadge{{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:.5px}}
.fbadge.ad{{background:#ef4444;color:#fff}}
.fbadge.prog{{background:#22c55e;color:#000}}
.cbar{{height:4px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden;margin-top:8px}}
.cfill{{height:100%;border-radius:2px;transition:width .4s,background .4s}}
.cfill.ad{{background:var(--ad)}}
.cfill.prog{{background:var(--prog)}}
.chart-wrap{{position:relative;height:160px}}
.thumbs{{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:4px}}
.thumb{{position:relative;aspect-ratio:16/9;border-radius:5px;overflow:hidden;cursor:pointer;background:#0a0a18}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .15s}}
.thumb:hover img{{transform:scale(1.05)}}
.tdot{{position:absolute;top:5px;right:5px;width:8px;height:8px;border-radius:50%;border:1.5px solid rgba(0,0,0,.5)}}
.tdot.ad{{background:var(--ad)}}
.tdot.prog{{background:var(--prog)}}
.tconf{{position:absolute;bottom:4px;left:5px;font-size:9px;color:#fff;font-weight:600;text-shadow:0 1px 3px rgba(0,0,0,.8)}}
.rpi-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.rtile{{background:var(--dim);border-radius:7px;padding:12px;text-align:center}}
.rtile .v{{font-size:20px;font-weight:700;line-height:1}}
.rtile .l{{font-size:10px;color:var(--mu);margin-top:3px;text-transform:uppercase;letter-spacing:.5px}}
.rtile .bar{{height:3px;border-radius:2px;margin-top:8px;background:#2d2d4e;overflow:hidden}}
.rtile .bf{{height:100%;border-radius:2px;transition:width .4s}}
.odot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.odot.on{{background:#22c55e;box-shadow:0 0 6px #22c55e88;animation:pulse 2s infinite}}
.odot.off{{background:#475569}}
.ev-list{{max-height:220px;overflow-y:auto}}
.ev-row{{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:12px}}
.ev-row:last-child{{border:none}}
.ev-ico{{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0}}
.ev-ico.started{{background:#450a0a;color:#f87171}}
.ev-ico.ended{{background:#052e16;color:#4ade80}}
.ev-dur{{margin-left:auto;color:var(--mu);font-size:11px;white-space:nowrap}}
.ev-time{{color:var(--mu);font-size:10px;min-width:45px;flex-shrink:0}}
.acc-row{{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}}
.acc-row:last-child{{border:none}}
.acc-lbl{{color:var(--mu)}}
.tag{{font-size:10px;padding:2px 7px;border-radius:20px;font-weight:600;text-transform:uppercase}}
.tg{{background:#052e16;color:#4ade80}}
.tr{{background:#450a0a;color:#f87171}}
.ty{{background:#1e1e3a;color:#64748b}}
.modal{{display:none;position:fixed;inset:0;z-index:1000;align-items:center;justify-content:center;background:rgba(0,0,0,.85);backdrop-filter:blur(4px)}}
.modal.open{{display:flex}}
.mi{{max-width:80vw;max-height:90vh;border-radius:10px;overflow:hidden;background:var(--card);border:1px solid var(--border)}}
.mi img{{display:block;max-width:100%;max-height:80vh}}
.mfoot{{padding:10px 14px;font-size:12px;color:var(--mu);display:flex;justify-content:space-between;align-items:center}}
</style>
</head>
<body>
{nav_html}

<div class="banner is-uk" id="banner">
  <div class="s-pill">
    <div class="s-ico" id="sIco">📡</div>
    <div>
      <div class="s-lbl" id="sLbl">Načítavam...</div>
      <div class="s-sub" id="sSub">Čakám na dáta</div>
    </div>
  </div>
  <div class="chips">
    <div class="chip"><div class="v" id="cConf">—</div><div class="l">Confidence</div></div>
    <div class="chip"><div class="v" id="cRate">—</div><div class="l">Ad rate / 1h</div></div>
    <div class="chip"><div class="v" id="cDur">—</div><div class="l">Avg ad dĺžka</div></div>
    <div class="chip"><div class="v" id="cProc">—</div><div class="l">Spracované</div></div>
  </div>
</div>

<div class="main">
  <div class="col">

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">Posledný frame</span>
        <span style="font-size:11px;color:var(--mu)" id="fTs">—</span>
      </div>
      <div class="cb">
        <div class="frame-wrap" id="fWrap" onclick="openModal(this.dataset.src, this.dataset.meta)">
          <div class="frame-empty" id="fEmpty"><div style="font-size:40px">📡</div><div style="font-size:12px">Čakám na frame...</div></div>
          <img id="fImg" src="" alt="" style="display:none">
          <div class="f-ov" id="fOv" style="display:none">
            <span class="fbadge" id="fBadge">—</span>
            <span style="font-size:11px;color:rgba(255,255,255,.7)" id="fConf">—</span>
          </div>
        </div>
        <div class="cbar"><div class="cfill prog" id="cFill" style="width:0"></div></div>
        <div style="font-size:10px;color:var(--mu);margin-top:5px;text-align:right" id="fMeta">—</div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">RPi štatistiky</span>
        <div style="display:flex;align-items:center;gap:6px;font-size:12px">
          <span class="odot off" id="rpiDot"></span>
          <span id="rpiTxt" style="color:var(--mu)">—</span>
        </div>
      </div>
      <div class="cb">
        <div class="rpi-grid">
          <div class="rtile"><div class="v" id="rCpu">—</div><div class="l">CPU %</div><div class="bar"><div class="bf" id="rCpuB" style="width:0;background:#a78bfa"></div></div></div>
          <div class="rtile"><div class="v" id="rMem">—</div><div class="l">Pamäť %</div><div class="bar"><div class="bf" id="rMemB" style="width:0;background:#60a5fa"></div></div></div>
          <div class="rtile"><div class="v" id="rTemp">—</div><div class="l">Teplota °C</div><div class="bar"><div class="bf" id="rTempB" style="width:0;background:#fb923c"></div></div></div>
          <div class="rtile"><div class="v" id="rAds">—</div><div class="l">Reklamy celkom</div><div class="bar"><div class="bf" id="rAdsB" style="width:0;background:#f87171"></div></div></div>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <div style="flex:1;background:var(--dim);border-radius:6px;padding:8px;text-align:center;font-size:11px"><div style="color:var(--mu);margin-bottom:2px">Capture</div><div id="rCap" style="font-weight:600">—</div></div>
          <div style="flex:1;background:var(--dim);border-radius:6px;padding:8px;text-align:center;font-size:11px"><div style="color:var(--mu);margin-bottom:2px">Detect</div><div id="rDet" style="font-weight:600">—</div></div>
          <div style="flex:1;background:var(--dim);border-radius:6px;padding:8px;text-align:center;font-size:11px"><div style="color:var(--mu);margin-bottom:2px">Heartbeat</div><div id="rHb" style="font-weight:600;font-size:10px">—</div></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">Presnosť AI (labelované framy)</span>
        <span id="accTot" style="font-size:11px;color:var(--mu)">—</span>
      </div>
      <div class="cb" id="accBody"><div style="text-align:center;color:var(--mu);font-size:12px;padding:20px">Načítavam...</div></div>
    </div>

  </div>
  <div class="col">

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">Confidence timeline (posledných 50)</span>
        <span style="font-size:11px;color:var(--mu)" id="chTs">—</span>
      </div>
      <div class="cb"><div class="chart-wrap"><canvas id="confChart"></canvas></div></div>
    </div>

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">Posledné framy (10)</span>
        <span style="font-size:11px;color:var(--mu)" id="thTs">—</span>
      </div>
      <div class="cb"><div class="thumbs" id="thumbs"><div style="grid-column:1/-1;text-align:center;color:var(--mu);padding:20px;font-size:12px">Načítavam...</div></div></div>
    </div>

    <div class="card">
      <div class="card-hd">
        <span class="card-ti">Ad udalosti (posledných 20)</span>
        <div style="display:flex;gap:12px">
          <span id="evDur" style="font-size:11px;color:var(--mu)">Priem.: —</span>
          <span id="evSw" style="font-size:11px;color:var(--mu)">Prepnutí: —</span>
        </div>
      </div>
      <div class="cb"><div class="ev-list" id="evList"><div style="text-align:center;color:var(--mu);padding:20px;font-size:12px">Načítavam...</div></div></div>
    </div>

  </div>
</div>

<div class="modal" id="modal" onclick="closeModal()">
  <div class="mi" onclick="event.stopPropagation()">
    <img id="mImg" src="" alt="">
    <div class="mfoot">
      <span id="mMeta" style="font-size:11px"></span>
      <button onclick="closeModal()" style="background:none;border:none;color:var(--mu);cursor:pointer;font-size:13px">Zatvoriť ×</button>
    </div>
  </div>
</div>

<script>
const AK = '{api_key}', DID = '{device_id}';
const hdr = () => ({{headers:{{'X-API-Key': AK}}}});

function fmt(iso) {{
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('sk-SK', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
}}
function ago(iso) {{
  if (!iso) return '—';
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm';
  return Math.floor(s/3600) + 'h';
}}
function dur(s) {{
  if (s == null) return '—';
  return s < 60 ? s.toFixed(0) + 's' : (s/60).toFixed(1) + 'min';
}}
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function txt(id, val) {{ document.getElementById(id).textContent = val; }}

// Chart
const ctx = document.getElementById('confChart').getContext('2d');
const chart = new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: [],
    datasets: [
      {{ label: 'Confidence', data: [], borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,.08)',
         borderWidth: 1.5, pointRadius: 2.5, pointHoverRadius: 5, tension: 0.3, fill: true,
         pointBackgroundColor: [] }},
      {{ label: 'Threshold', data: [], borderColor: 'rgba(248,113,113,.5)', borderWidth: 1,
         pointRadius: 0, borderDash: [4,3], fill: false }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false, animation: {{ duration: 300 }},
    plugins: {{ legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: c => (c.parsed.y*100).toFixed(1)+'%' }} }} }},
    scales: {{
      x: {{ ticks: {{ color:'#475569', maxTicksLimit:8, maxRotation:0, font:{{size:9}} }}, grid: {{ color:'#1e1e3a' }} }},
      y: {{ min:0, max:1,
        ticks: {{ color:'#475569', callback: v => (v*100).toFixed(0)+'%', font:{{size:9}} }},
        grid: {{ color:'#1e1e3a' }} }},
    }},
  }},
}});

function updateChart(results) {{
  const rev = [...results].reverse();
  chart.data.labels = rev.map(r => fmt(r.captured_at || r.created_at));
  chart.data.datasets[0].data = rev.map(r => r.confidence ?? 0);
  chart.data.datasets[0].pointBackgroundColor = rev.map(r => r.is_ad ? 'rgba(239,68,68,.9)' : 'rgba(34,197,94,.6)');
  chart.data.datasets[1].data = rev.map(() => 0.55);
  chart.update('none');
  txt('chTs', 'Akt. ' + new Date().toLocaleTimeString('sk-SK'));
}}

async function pollMonitor() {{
  try {{
    const r = await fetch('/v1/monitor/data?device_id=' + DID + '&limit=50', hdr());
    if (!r.ok) return;
    const d = await r.json();
    const adA = d.state.ad_active;
    const banner = document.getElementById('banner');
    banner.className = 'banner ' + (adA ? 'is-ad' : 'is-prog');
    txt('sIco', adA ? '🚨' : '📺');
    txt('sLbl', adA ? 'REKLAMA' : 'PROGRAM');
    if (adA) {{
      txt('sSub', d.state.ad_since ? 'Trvá ' + ago(d.state.ad_since) + ' · od ' + fmt(d.state.ad_since) : 'Aktívna reklama');
    }} else {{
      const last = d.recent_results.find(x => x.confidence != null);
      txt('sSub', last ? 'Posledná det. ' + ago(last.captured_at || last.created_at) : 'Žiadna reklama');
    }}
    const lc = d.recent_results[0]?.confidence;
    txt('cConf', lc != null ? (lc*100).toFixed(0)+'%' : '—');
    const t1h = d.stats.results_last_hour, a1h = d.stats.ad_detections_last_hour;
    txt('cRate', t1h ? Math.round(a1h/t1h*100)+'%' : '—');
    txt('cProc', d.rpi_status?.frames_processed ?? '—');
    if (d.recent_results.length) updateChart(d.recent_results);
    const rpi = d.rpi_status;
    if (rpi) {{
      const on = rpi.is_online;
      document.getElementById('rpiDot').className = 'odot ' + (on ? 'on' : 'off');
      txt('rpiTxt', on ? 'Online' : 'Offline');
      txt('rCpu', rpi.cpu_percent != null ? rpi.cpu_percent.toFixed(0)+'%' : '—');
      document.getElementById('rCpuB').style.width = (rpi.cpu_percent ?? 0) + '%';
      txt('rMem', rpi.memory_percent != null ? rpi.memory_percent.toFixed(0)+'%' : '—');
      document.getElementById('rMemB').style.width = (rpi.memory_percent ?? 0) + '%';
      txt('rTemp', rpi.temperature_celsius != null ? rpi.temperature_celsius.toFixed(1) : '—');
      document.getElementById('rTempB').style.width = Math.min(rpi.temperature_celsius ?? 0, 85)/85*100 + '%';
      txt('rAds', rpi.ads_detected ?? '—');
      document.getElementById('rAdsB').style.width = Math.min((rpi.ads_detected ?? 0)/200*100, 100) + '%';
      txt('rCap', rpi.capture_running ? '✅ Beží' : '⬜ Stop');
      txt('rDet', rpi.detect_running ? '✅ Beží' : '⬜ Stop');
      txt('rHb', rpi.last_heartbeat ? ago(rpi.last_heartbeat) + ' ago' : '—');
    }}
  }} catch(e) {{ console.error('monitor', e); }}
}}

async function pollFrames() {{
  try {{
    const r = await fetch('/v1/history/frames?device_id=' + DID + '&filter=all&limit=10', hdr());
    if (!r.ok) return;
    const d = await r.json();
    if (!d.items.length) return;
    const f = d.items[0];
    const src = '/frames/' + f.id + '.jpg?api_key=' + encodeURIComponent(AK);
    const wrap = document.getElementById('fWrap');
    const img = document.getElementById('fImg');
    if (img.dataset.fid !== String(f.id)) {{
      img.dataset.fid = String(f.id);
      img.src = src;
      img.style.display = 'block';
      document.getElementById('fEmpty').style.display = 'none';
      document.getElementById('fOv').style.display = 'flex';
    }}
    const cp = f.confidence != null ? (f.confidence*100).toFixed(1) : null;
    const badge = document.getElementById('fBadge');
    badge.textContent = f.is_ad ? 'REKLAMA' : 'PROGRAM';
    badge.className = 'fbadge ' + (f.is_ad ? 'ad' : 'prog');
    txt('fConf', cp ? cp + '%' : '');
    document.getElementById('cFill').style.width = (f.confidence ?? 0)*100 + '%';
    document.getElementById('cFill').className = 'cfill ' + (f.is_ad ? 'ad' : 'prog');
    txt('fTs', fmt(f.captured_at));
    const meta = (f.channel ? f.channel + ' · ' : '') + (cp ? 'p_ad=' + cp + '%' : '') + (f.detect_time_ms ? ' · ' + f.detect_time_ms + 'ms' : '');
    txt('fMeta', meta);
    wrap.dataset.src = src;
    wrap.dataset.meta = meta;

    // Thumbnails — build safely
    const thumbs = document.getElementById('thumbs');
    thumbs.textContent = '';
    d.items.forEach(fr => {{
      const fsrc = '/frames/' + fr.id + '.jpg?api_key=' + encodeURIComponent(AK);
      const fcp = fr.confidence != null ? (fr.confidence*100).toFixed(0) + '%' : '';
      const fmeta = (fr.channel || '') + ' · ' + fcp + ' · ' + fmt(fr.captured_at);
      const div = document.createElement('div');
      div.className = 'thumb';
      div.onclick = () => openModal(fsrc, fmeta);
      const im = document.createElement('img');
      im.src = fsrc;
      im.loading = 'lazy';
      im.onerror = () => {{ im.style.display = 'none'; }};
      const dot = document.createElement('span');
      dot.className = 'tdot ' + (fr.is_ad ? 'ad' : 'prog');
      const cl = document.createElement('span');
      cl.className = 'tconf';
      cl.textContent = fcp;
      div.appendChild(im);
      div.appendChild(dot);
      div.appendChild(cl);
      thumbs.appendChild(div);
    }});
    txt('thTs', 'Akt. ' + new Date().toLocaleTimeString('sk-SK'));
  }} catch(e) {{ console.error('frames', e); }}
}}

async function pollEvents() {{
  try {{
    const r = await fetch('/v1/monitor/ad-events?device_id=' + DID + '&limit=20', hdr());
    if (!r.ok) return;
    const d = await r.json();
    txt('cDur', d.avg_ad_duration_seconds != null ? dur(d.avg_ad_duration_seconds) : '—');
    txt('evDur', 'Priem.: ' + (d.avg_ad_duration_seconds != null ? dur(d.avg_ad_duration_seconds) : '—'));
    txt('evSw', 'Prepnutí: ' + d.switches_triggered);
    const list = document.getElementById('evList');
    list.textContent = '';
    if (!d.events.length) {{
      const p = document.createElement('div');
      p.style.cssText = 'text-align:center;color:var(--mu);padding:20px;font-size:12px';
      p.textContent = 'Žiadne udalosti';
      list.appendChild(p);
      return;
    }}
    d.events.forEach(e => {{
      const row = document.createElement('div');
      row.className = 'ev-row';
      const isStart = e.event_type === 'ad_started';
      const tEl = document.createElement('span');
      tEl.className = 'ev-time';
      tEl.textContent = fmt(e.created_at);
      const ico = document.createElement('span');
      ico.className = 'ev-ico ' + (isStart ? 'started' : 'ended');
      ico.textContent = isStart ? '▶' : '■';
      const info = document.createElement('div');
      const title = document.createElement('div');
      title.style.cssText = 'font-weight:600;font-size:12px';
      title.textContent = isStart ? 'Reklama začala' : 'Reklama skončila';
      const sub = document.createElement('div');
      sub.style.cssText = 'color:var(--mu);font-size:11px';
      sub.textContent = (e.channel || '') + (e.avg_confidence != null ? ' · conf=' + (e.avg_confidence*100).toFixed(0)+'%' : '');
      info.appendChild(title);
      info.appendChild(sub);
      const durEl = document.createElement('span');
      durEl.className = 'ev-dur';
      durEl.textContent = e.duration_seconds != null ? dur(e.duration_seconds) : (isStart ? '▶ živá' : '');
      if (isStart && !e.duration_seconds) durEl.style.color = '#f87171';
      row.appendChild(tEl);
      row.appendChild(ico);
      row.appendChild(info);
      row.appendChild(durEl);
      list.appendChild(row);
    }});
  }} catch(e) {{ console.error('events', e); }}
}}

async function pollAccuracy() {{
  try {{
    const r = await fetch('/v1/monitor/accuracy?device_id=' + DID, hdr());
    if (!r.ok) return;
    const d = await r.json();
    txt('accTot', d.total_labeled + ' labelovaných');
    const body = document.getElementById('accBody');
    body.textContent = '';
    if (!d.total_labeled) {{
      const p = document.createElement('div');
      p.style.cssText = 'text-align:center;color:var(--mu);padding:12px;font-size:12px';
      p.textContent = 'Žiadne labelované framy';
      body.appendChild(p);
      return;
    }}
    const rows = [
      ['Celková presnosť', `<span class="tag ${{d.accuracy >= 90 ? 'tg' : d.accuracy >= 70 ? 'ty' : 'tr'}}">${{d.accuracy}}%</span>`],
      ['Správne / Celkom', d.correct + ' / ' + d.total_labeled],
      ['AI chyby (overrides)', `<span style="color:${{d.overrides > 0 ? '#f87171' : '#4ade80'}}">${{d.overrides}}</span>`],
    ];
    if (d.avg_confidence_on_wrong != null)
      rows.push(['Conf pri chybách', (d.avg_confidence_on_wrong*100).toFixed(0) + '%']);
    Object.entries(d.by_channel || {{}}).forEach(([ch, s]) => {{
      rows.push([esc(ch), `<span class="tag ${{s.accuracy >= 90 ? 'tg' : 'ty'}}">${{s.accuracy}}%</span> (${{s.total}} fr.)`]);
    }});
    rows.forEach(([lbl, val]) => {{
      const row = document.createElement('div');
      row.className = 'acc-row';
      const l = document.createElement('span');
      l.className = 'acc-lbl';
      l.textContent = lbl;
      const v = document.createElement('span');
      v.style.fontWeight = '600';
      v.style.fontSize = '13px';
      v.innerHTML = val;
      row.appendChild(l);
      row.appendChild(v);
      body.appendChild(row);
    }});
  }} catch(e) {{ console.error('accuracy', e); }}
}}

function openModal(src, meta) {{
  document.getElementById('mImg').src = src;
  document.getElementById('mMeta').textContent = meta || '';
  document.getElementById('modal').classList.add('open');
}}
function closeModal() {{ document.getElementById('modal').classList.remove('open'); }}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

pollMonitor();
pollFrames();
pollEvents();
pollAccuracy();
setInterval(pollMonitor, 2000);
setInterval(pollFrames, 3000);
setInterval(pollEvents, 10000);
setInterval(pollAccuracy, 30000);
</script>
<script>{NAV_STATUS_JS}</script>
</body>
</html>"""
