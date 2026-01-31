from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, desc

from app.db.engine import get_session
from app.models import AdResultDB, AdStateDB, DeviceCommandDB, DeviceConfigDB, utcnow
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
    Returns recent ad results, commands, current state, and config.
    """
    now = utcnow()

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

    # Get recent ad results (from Raspberry Pi)
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
            "source": "raspberry_pi",
        }
        for r in results
    ]

    # Get recent commands (for mobile app)
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
            "result": c.result,
        }
        for c in commands
    ]

    # Calculate stats
    one_hour_ago = now - timedelta(hours=1)
    # Handle both timezone-aware and naive datetimes
    def is_recent(dt):
        if dt is None:
            return False
        # Make comparison work regardless of timezone awareness
        try:
            return dt > one_hour_ago
        except TypeError:
            # If comparison fails due to tz mismatch, strip tz info
            return dt.replace(tzinfo=None) > one_hour_ago.replace(tzinfo=None)

    results_last_hour = [r for r in results if is_recent(r.created_at)]
    ad_detections_last_hour = len([r for r in results_last_hour if r.is_ad])
    commands_pending = len([c for c in commands if c.status == "pending"])
    commands_done = len([c for c in commands if c.status == "done"])
    commands_failed = len([c for c in commands if c.status == "failed"])

    return {
        "timestamp": now.isoformat(),
        "device_id": device_id,
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
    }


@router.get("/monitor", response_class=HTMLResponse)
def monitor_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    """
    HTML dashboard for monitoring the TV Bridge system.
    Auto-refreshes every 2 seconds.
    Pass api_key as query parameter to authenticate.
    """
    # Validate API key for dashboard access
    if api_key != settings.api_key:
        return HTMLResponse(
            content="""
            <html>
            <head><title>TV Bridge Monitor - Auth Required</title></head>
            <body style="font-family: sans-serif; background: #1a1a2e; color: #eee; padding: 50px; text-align: center;">
                <h1 style="color: #ff6b6b;">🔒 API Key Required</h1>
                <p>Add <code>?api_key=YOUR_KEY</code> to the URL to access the dashboard.</p>
                <p style="color: #888; margin-top: 20px;">Example: /v1/monitor?api_key=xxx</p>
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
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
            min-height: 100vh;
        }}
        h1 {{ color: #FFD33D; margin-bottom: 20px; }}
        h2 {{ color: #888; font-size: 14px; text-transform: uppercase; margin: 20px 0 10px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .card {{
            background: #16213e;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #0f3460;
        }}
        .card-title {{
            color: #FFD33D;
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .status-dot {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
        }}
        .status-dot.active {{ background: #51cf66; animation: pulse 1s infinite; }}
        .status-dot.inactive {{ background: #888; }}
        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
        }}
        .stat-row {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #0f3460;
        }}
        .stat-row:last-child {{ border-bottom: none; }}
        .stat-label {{ color: #888; }}
        .stat-value {{ font-weight: 600; }}
        .stat-value.highlight {{ color: #FFD33D; }}
        .log-list {{
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
        }}
        .log-entry {{
            padding: 8px 10px;
            border-bottom: 1px solid #0f3460;
            display: flex;
            gap: 10px;
        }}
        .log-entry:hover {{ background: #1f2f4f; }}
        .log-time {{ color: #666; min-width: 80px; }}
        .log-icon {{ min-width: 24px; }}
        .log-message {{ flex: 1; word-break: break-word; }}
        .log-entry.ad {{ background: rgba(255, 107, 107, 0.1); }}
        .log-entry.no-ad {{ background: rgba(81, 207, 102, 0.05); }}
        .log-entry.pending {{ color: #FFD33D; }}
        .log-entry.done {{ color: #51cf66; }}
        .log-entry.failed {{ color: #ff6b6b; }}
        .refresh-info {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #16213e;
            padding: 10px 15px;
            border-radius: 8px;
            font-size: 12px;
            color: #888;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 10px;
            margin-bottom: 20px;
        }}
        .stat-box {{
            background: #16213e;
            border-radius: 8px;
            padding: 15px;
            text-align: center;
            border: 1px solid #0f3460;
        }}
        .stat-box .number {{
            font-size: 28px;
            font-weight: bold;
            color: #FFD33D;
        }}
        .stat-box .label {{
            font-size: 11px;
            color: #888;
            margin-top: 5px;
        }}
        .config-value {{
            background: #0f3460;
            padding: 4px 8px;
            border-radius: 4px;
            font-family: monospace;
        }}
        #error-banner {{
            display: none;
            background: #ff6b6b;
            color: white;
            padding: 10px 20px;
            margin-bottom: 20px;
            border-radius: 8px;
        }}
    </style>
</head>
<body>
    <div class="refresh-info">
        <span id="refresh-status">Connecting...</span>
    </div>

    <h1>📺 TV Bridge Monitor</h1>
    <div id="error-banner"></div>

    <div class="stats-grid" id="stats-grid">
        <div class="stat-box">
            <div class="number" id="stat-results">-</div>
            <div class="label">Results (1h)</div>
        </div>
        <div class="stat-box">
            <div class="number" id="stat-ads">-</div>
            <div class="label">Ads Detected (1h)</div>
        </div>
        <div class="stat-box">
            <div class="number" id="stat-pending">-</div>
            <div class="label">Commands Pending</div>
        </div>
        <div class="stat-box">
            <div class="number" id="stat-done">-</div>
            <div class="label">Commands Done</div>
        </div>
        <div class="stat-box">
            <div class="number" id="stat-failed">-</div>
            <div class="label">Commands Failed</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">
                <span class="status-dot" id="ad-status-dot"></span>
                Current State
            </div>
            <div class="stat-row">
                <span class="stat-label">Device ID</span>
                <span class="stat-value">{device_id}</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Ad Active</span>
                <span class="stat-value" id="ad-active">-</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Ad Since</span>
                <span class="stat-value" id="ad-since">-</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Last Update</span>
                <span class="stat-value" id="state-updated">-</span>
            </div>
        </div>

        <div class="card">
            <div class="card-title">⚙️ Configuration</div>
            <div class="stat-row">
                <span class="stat-label">Fallback Channel</span>
                <span class="stat-value config-value" id="fallback-channel">-</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Original Channel</span>
                <span class="stat-value config-value" id="original-channel">-</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Auto-Switch</span>
                <span class="stat-value" id="auto-switch">-</span>
            </div>
        </div>
    </div>

    <div class="grid" style="margin-top: 20px;">
        <div class="card">
            <div class="card-title">📡 Raspberry Pi → API (Ad Results)</div>
            <div class="log-list" id="results-log">
                <div class="log-entry">Loading...</div>
            </div>
        </div>

        <div class="card">
            <div class="card-title">📱 API → Mobile App (Commands)</div>
            <div class="log-list" id="commands-log">
                <div class="log-entry">Loading...</div>
            </div>
        </div>
    </div>

    <script>
        const DEVICE_ID = '{device_id}';
        const API_KEY = '{api_key}';
        let lastResultId = 0;
        let lastCommandId = 0;

        function formatTime(isoString) {{
            if (!isoString) return '-';
            const d = new Date(isoString);
            return d.toLocaleTimeString('sk-SK', {{ hour: '2-digit', minute: '2-digit', second: '2-digit' }});
        }}

        function formatTimeFull(isoString) {{
            if (!isoString) return '-';
            const d = new Date(isoString);
            return d.toLocaleString('sk-SK');
        }}

        function timeSince(isoString) {{
            if (!isoString) return '-';
            const d = new Date(isoString);
            const now = new Date();
            const seconds = Math.floor((now - d) / 1000);
            if (seconds < 60) return seconds + 's ago';
            if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
            return Math.floor(seconds / 3600) + 'h ago';
        }}

        async function fetchData() {{
            try {{
                const res = await fetch('/v1/monitor/data?device_id=' + DEVICE_ID + '&limit=50', {{
                    headers: {{ 'X-API-Key': API_KEY }}
                }});
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                updateUI(data);
                document.getElementById('refresh-status').textContent = 'Updated: ' + formatTime(data.timestamp);
                document.getElementById('error-banner').style.display = 'none';
            }} catch (e) {{
                console.error('Fetch error:', e);
                document.getElementById('refresh-status').textContent = 'Error: ' + e.message;
                document.getElementById('error-banner').textContent = 'Failed to fetch data: ' + e.message;
                document.getElementById('error-banner').style.display = 'block';
            }}
        }}

        function updateUI(data) {{
            // Stats
            document.getElementById('stat-results').textContent = data.stats.results_last_hour;
            document.getElementById('stat-ads').textContent = data.stats.ad_detections_last_hour;
            document.getElementById('stat-pending').textContent = data.stats.commands_pending;
            document.getElementById('stat-done').textContent = data.stats.commands_done;
            document.getElementById('stat-failed').textContent = data.stats.commands_failed;

            // State
            const adActive = data.state.ad_active;
            document.getElementById('ad-active').textContent = adActive ? '🚨 YES' : '✅ NO';
            document.getElementById('ad-active').style.color = adActive ? '#ff6b6b' : '#51cf66';
            document.getElementById('ad-since').textContent = adActive ? timeSince(data.state.ad_since) : '-';
            document.getElementById('state-updated').textContent = timeSince(data.state.updated_at);

            const dot = document.getElementById('ad-status-dot');
            dot.className = 'status-dot ' + (adActive ? 'active' : 'inactive');
            dot.style.background = adActive ? '#ff6b6b' : '#51cf66';

            // Config
            document.getElementById('fallback-channel').textContent = data.config.fallback_channel || 'Not set';
            document.getElementById('original-channel').textContent = data.config.original_channel || 'Not set';
            document.getElementById('auto-switch').textContent = data.config.auto_switch_enabled ? '✅ Enabled' : '❌ Disabled';

            // Results log
            const resultsHtml = data.recent_results.map(r => `
                <div class="log-entry ${{r.is_ad ? 'ad' : 'no-ad'}}">
                    <span class="log-time">${{formatTime(r.created_at)}}</span>
                    <span class="log-icon">${{r.is_ad ? '🚨' : '✅'}}</span>
                    <span class="log-message">
                        ${{r.is_ad ? 'AD DETECTED' : 'No ad'}}
                        ${{r.confidence ? '(' + (r.confidence * 100).toFixed(0) + '%)' : ''}}
                        <span style="color:#666">#${{r.id}}</span>
                    </span>
                </div>
            `).join('') || '<div class="log-entry">No results yet</div>';
            document.getElementById('results-log').innerHTML = resultsHtml;

            // Commands log
            const commandsHtml = data.recent_commands.map(c => `
                <div class="log-entry ${{c.status}}">
                    <span class="log-time">${{formatTime(c.created_at)}}</span>
                    <span class="log-icon">${{
                        c.status === 'done' ? '✅' :
                        c.status === 'failed' ? '❌' :
                        '⏳'
                    }}</span>
                    <span class="log-message">
                        ${{c.type === 'switch_channel' ? 'Switch to CH ' + c.payload.channel : c.type}}
                        ${{c.payload.reason ? '(' + c.payload.reason + ')' : ''}}
                        <span style="color:#666">#${{c.id}} [${{c.status}}]</span>
                        ${{c.processed_at ? '<br><span style="color:#888">Processed: ' + timeSince(c.processed_at) + '</span>' : ''}}
                    </span>
                </div>
            `).join('') || '<div class="log-entry">No commands yet</div>';
            document.getElementById('commands-log').innerHTML = commandsHtml;
        }}

        // Initial fetch
        fetchData();

        // Refresh every 2 seconds
        setInterval(fetchData, 2000);
    </script>
</body>
</html>
"""
    return html
