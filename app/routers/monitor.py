from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlmodel import Session, select, desc

from app.db.engine import get_session
from app.models import AdResultDB, AdStateDB, DeviceCommandDB, DeviceConfigDB, RpiStatusDB, RpiCommandDB, RpiDaemonStatusDB, utcnow
from app.settings import settings

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
        return HTMLResponse(
            content="""
            <html>
            <head><title>TV Bridge - Auth Required</title></head>
            <body style="font-family: sans-serif; background: #1a1a2e; color: #eee; padding: 50px; text-align: center;">
                <h1 style="color: #ff6b6b;">API Key Required</h1>
                <p>Add <code>?api_key=YOUR_KEY</code> to the URL to access the dashboard.</p>
            </body>
            </html>
            """,
            status_code=401
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>TV Bridge Monitor - {device_id}</title>
    <meta charset="utf-8">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f0f1a;
            color: #eee;
            min-height: 100vh;
        }}
        .header {{
            background: #1a1a2e;
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #2a2a4e;
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .header h1 {{ font-size: 20px; color: #FFD33D; }}
        .header-info {{ display: flex; gap: 20px; align-items: center; font-size: 12px; color: #888; }}
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
    <div class="header">
        <h1>TV Bridge Monitor</h1>
        <div class="header-info">
            <span>Device: <strong>{device_id}</strong></span>
            <span id="update-time">Connecting...</span>
        </div>
    </div>

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
</body>
</html>
"""
    return HTMLResponse(content=html)
