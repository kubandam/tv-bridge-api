from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select, desc, func

from app.db.engine import get_session
from app.models import LabeledFrameDB, AdResultDB, AdStateDB, utcnow
from app.settings import settings
from app.ui import NAV_CSS, UNAUTH_HTML, NAV_STATUS_JS, nav_bar

router = APIRouter(tags=["labeling"])


# ── API Endpoints ──────────────────────────────────────────────────

class LabelIn(BaseModel):
    label: str  # ad | program | transition
    device_id: str = "tv-1"


@router.post("/label")
def label_current_frame(
    body: LabelIn,
    db: Session = Depends(get_session),
):
    """
    Label the current live frame. Grabs the latest image from in-memory
    storage and saves it with the user's label.
    """
    if body.label not in ("ad", "program", "transition"):
        raise HTTPException(status_code=400, detail="Label must be: ad, program, transition")

    # Import here to access the in-memory image store from device router
    from app.routers.device import _latest_images

    if body.device_id not in _latest_images:
        raise HTTPException(status_code=404, detail="No live image available to label")

    img_data = _latest_images[body.device_id]

    ai_was_ad = img_data.get("is_ad")
    ai_confidence = img_data.get("confidence")

    is_override = False
    if ai_was_ad is not None:
        if body.label == "ad" and not ai_was_ad:
            is_override = True
        elif body.label in ("program", "transition") and ai_was_ad:
            is_override = True

    # Upload live image to R2
    import uuid as _uuid
    from app.storage.r2 import upload_frame
    image_bytes = base64.b64decode(img_data["image_base64"])
    image_key = f"frames/{body.device_id}/live_{_uuid.uuid4()}.jpg"
    upload_frame(image_bytes, image_key)

    frame = LabeledFrameDB(
        device_id=body.device_id,
        channel=img_data.get("channel"),
        label=body.label,
        image_key=image_key,
        ai_was_ad=ai_was_ad,
        ai_confidence=ai_confidence,
        is_override=is_override,
    )
    db.add(frame)
    db.commit()
    db.refresh(frame)

    return {
        "ok": True,
        "id": frame.id,
        "label": frame.label,
        "is_override": frame.is_override,
    }


@router.get("/labels")
def get_labels(
    device_id: str = Query(default="tv-1"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_session),
):
    """Get recent labeled frames (metadata only, no images)."""
    stmt = (
        select(LabeledFrameDB)
        .where(LabeledFrameDB.device_id == device_id)
        .order_by(desc(LabeledFrameDB.id))
        .limit(limit)
    )
    frames = db.exec(stmt).all()
    return [
        {
            "id": f.id,
            "label": f.label,
            "ai_was_ad": f.ai_was_ad,
            "ai_confidence": f.ai_confidence,
            "is_override": f.is_override,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in frames
    ]


@router.get("/labels/{frame_id}.jpg")
def get_labeled_frame_image(
    frame_id: int,
    db: Session = Depends(get_session),
):
    """Get labeled frame image as JPEG."""
    frame = db.get(LabeledFrameDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    from app.storage.r2 import download_frame
    image_bytes = download_frame(frame.image_key)
    return Response(content=image_bytes, media_type="image/jpeg")


@router.delete("/labels/{frame_id}")
def delete_labeled_frame(
    frame_id: int,
    db: Session = Depends(get_session),
):
    """Delete a labeled frame."""
    frame = db.get(LabeledFrameDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    from app.storage.r2 import delete_frame
    try:
        delete_frame(frame.image_key)
    except Exception:
        pass
    db.delete(frame)
    db.commit()
    return {"ok": True, "deleted_id": frame_id}


@router.get("/labels/stats")
def get_label_stats(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """Get labeling statistics and ad analytics."""
    now = utcnow()

    # Count by label
    all_frames = db.exec(
        select(LabeledFrameDB).where(LabeledFrameDB.device_id == device_id)
    ).all()

    total = len(all_frames)
    by_label = {"ad": 0, "program": 0, "transition": 0}
    overrides = 0
    for f in all_frames:
        by_label[f.label] = by_label.get(f.label, 0) + 1
        if f.is_override:
            overrides += 1

    # AI accuracy (based on overrides)
    ai_accuracy = None
    if total > 0:
        ai_accuracy = round((1 - overrides / total) * 100, 1)

    # Ad block analytics from ad_results (last 24h)
    yesterday = now - timedelta(hours=24)
    results_stmt = (
        select(AdResultDB)
        .where(AdResultDB.device_id == device_id)
        .where(AdResultDB.created_at > yesterday)
        .order_by(AdResultDB.created_at)
    )
    results = db.exec(results_stmt).all()

    # Calculate ad blocks (contiguous sequences of is_ad=True)
    ad_blocks = []
    current_block_start = None
    for r in results:
        if r.is_ad and current_block_start is None:
            current_block_start = r.created_at
        elif not r.is_ad and current_block_start is not None:
            duration = (r.created_at - current_block_start).total_seconds()
            ad_blocks.append({"start": current_block_start.isoformat(), "duration_s": duration})
            current_block_start = None
    # Close open block
    if current_block_start is not None:
        duration = (now - current_block_start).total_seconds()
        ad_blocks.append({"start": current_block_start.isoformat(), "duration_s": duration})

    avg_duration = None
    avg_interval = None
    if ad_blocks:
        avg_duration = round(sum(b["duration_s"] for b in ad_blocks) / len(ad_blocks), 1)
    if len(ad_blocks) >= 2:
        from datetime import datetime as dt
        intervals = []
        for i in range(1, len(ad_blocks)):
            t1 = dt.fromisoformat(ad_blocks[i - 1]["start"])
            t2 = dt.fromisoformat(ad_blocks[i]["start"])
            intervals.append((t2 - t1).total_seconds())
        avg_interval = round(sum(intervals) / len(intervals), 1)

    # Per-class targets: 500 ad, 500 program, transition = bonus
    target_ad = 500
    target_program = 500
    ad_count = by_label.get("ad", 0)
    program_count = by_label.get("program", 0)
    progress_pct = round((min(ad_count, target_ad) + min(program_count, target_program)) / (target_ad + target_program) * 100, 1)

    return {
        "total_labeled": total,
        "by_label": by_label,
        "overrides": overrides,
        "ai_accuracy_pct": ai_accuracy,
        "target_ad": target_ad,
        "target_program": target_program,
        "progress_pct": progress_pct,
        "training_ready": ad_count >= target_ad and program_count >= target_program,
        "ad_blocks_24h": len(ad_blocks),
        "avg_ad_duration_s": avg_duration,
        "avg_interval_between_ads_s": avg_interval,
    }


# ── HTML Dashboard ──────────────────────────────────────────────────

def labeling_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    """
    Web-based labeling and monitoring interface for TV ad detection.
    """
    if api_key != settings.api_key:
        return HTMLResponse(content=UNAUTH_HTML, status_code=401)

    _nav = nav_bar(api_key, device_id, "labeling")
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>TV Ad Labeling - {device_id}</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
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
        .main {{ padding: 20px; max-width: 1400px; margin: 0 auto; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        .grid-full {{ grid-column: 1 / -1; }}
        .card {{
            background: #1a1a2e;
            border-radius: 8px;
            border: 1px solid #2a2a4e;
            overflow: hidden;
        }}
        .card-title {{
            padding: 12px 16px;
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #888;
            border-bottom: 1px solid #2a2a4e;
        }}
        .card-body {{ padding: 16px; }}

        /* Live Feed */
        .live-container {{ text-align: center; }}
        .live-img {{
            max-width: 100%;
            border-radius: 6px;
            border: 2px solid #2a2a4e;
        }}
        .live-meta {{
            margin-top: 10px;
            display: flex;
            justify-content: center;
            gap: 20px;
            font-size: 13px;
            color: #aaa;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .badge-ad {{ background: #ff4444; color: white; }}
        .badge-ok {{ background: #22c55e; color: white; }}
        .badge-unknown {{ background: #666; color: white; }}

        /* Label Buttons */
        .label-buttons {{
            display: flex;
            gap: 12px;
            margin-top: 16px;
            justify-content: center;
        }}
        .label-btn {{
            padding: 14px 28px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.15s;
            min-width: 140px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        .label-btn:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
        .label-btn:active {{ transform: translateY(0); }}
        .label-btn:disabled {{ opacity: 0.5; cursor: not-allowed; transform: none; }}
        .btn-ad {{ background: #ff4444; color: white; }}
        .btn-program {{ background: #22c55e; color: white; }}
        .btn-transition {{ background: #f59e0b; color: #111; }}

        .label-feedback {{
            text-align: center;
            margin-top: 10px;
            font-size: 14px;
            min-height: 24px;
        }}

        /* Progress Bar */
        .progress-container {{
            background: #2a2a4e;
            border-radius: 8px;
            overflow: hidden;
            height: 24px;
            position: relative;
            margin-top: 8px;
        }}
        .progress-bar {{
            height: 100%;
            border-radius: 8px;
            transition: width 0.5s;
            background: linear-gradient(90deg, #FFD33D, #ff9500);
        }}
        .progress-text {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 12px;
            font-weight: 700;
            color: #111;
        }}

        /* Stats Grid */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 12px;
        }}
        .stat-box {{
            background: #0f0f1a;
            border-radius: 6px;
            padding: 12px;
            text-align: center;
        }}
        .stat-value {{ font-size: 24px; font-weight: 700; color: #FFD33D; }}
        .stat-label {{ font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; }}

        /* Recent Labels Table */
        .labels-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .labels-table th {{
            text-align: left;
            padding: 8px 12px;
            color: #888;
            font-weight: 600;
            border-bottom: 1px solid #2a2a4e;
        }}
        .labels-table td {{
            padding: 8px 12px;
            border-bottom: 1px solid #1f1f35;
        }}
        .labels-table tr:hover {{ background: #1f1f35; }}

        /* Gallery */
        .gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 10px;
        }}
        .gallery-item {{
            position: relative;
            border-radius: 6px;
            overflow: hidden;
            cursor: pointer;
            border: 2px solid #2a2a4e;
            transition: border-color 0.2s;
        }}
        .gallery-item:hover {{ border-color: #FFD33D; }}
        .gallery-item img {{ width: 100%; display: block; aspect-ratio: 16/9; object-fit: cover; }}
        .gallery-label {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 4px 8px;
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            text-align: center;
        }}
        .gl-ad {{ background: rgba(255,68,68,0.85); color: white; }}
        .gl-program {{ background: rgba(34,197,94,0.85); color: white; }}
        .gl-transition {{ background: rgba(245,158,11,0.85); color: #111; }}

        /* Delete button */
        .delete-btn {{
            position: absolute;
            top: 4px;
            right: 4px;
            background: rgba(0,0,0,0.6);
            color: #ff6b6b;
            border: none;
            border-radius: 50%;
            width: 22px;
            height: 22px;
            cursor: pointer;
            font-size: 12px;
            display: none;
            align-items: center;
            justify-content: center;
        }}
        .gallery-item:hover .delete-btn {{ display: flex; }}

        /* Analytics */
        .analytics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
        }}
        .analytics-box {{
            background: #0f0f1a;
            border-radius: 6px;
            padding: 16px;
            text-align: center;
        }}
        .analytics-value {{ font-size: 20px; font-weight: 700; }}
        .analytics-label {{ font-size: 11px; color: #888; margin-top: 4px; }}

        /* Status indicators */
        .status-dot {{
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
        }}
        .dot-online {{ background: #22c55e; }}
        .dot-offline {{ background: #ff4444; }}

        /* Modal */
        .modal-overlay {{
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.85);
            z-index: 200;
            align-items: center;
            justify-content: center;
        }}
        .modal-overlay.active {{ display: flex; }}
        .modal-img {{ max-width: 90%; max-height: 90vh; border-radius: 8px; }}
        .modal-close {{
            position: absolute;
            top: 20px;
            right: 20px;
            background: none;
            border: none;
            color: white;
            font-size: 32px;
            cursor: pointer;
        }}

        /* Keyboard hint */
        .kbd {{
            display: inline-block;
            background: #2a2a4e;
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 12px;
            font-family: monospace;
            color: #ccc;
            border: 1px solid #3a3a5e;
        }}
        .shortcuts {{ text-align: center; margin-top: 8px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
{_nav}

    <div class="main">
        <div class="grid">
            <!-- Left: Live Feed + Labeling -->
            <div>
                <div class="card">
                    <div class="card-title">Live Feed &amp; Labeling</div>
                    <div class="card-body live-container">
                        <img id="live-img" class="live-img" src="" alt="Live Feed" style="display:none;">
                        <div id="no-live-img" style="color:#666; padding:40px; text-align:center;">Waiting for live feed...</div>
                        <div class="live-meta">
                            <span>AI: <span id="ai-prediction">--</span></span>
                            <span>Confidence: <span id="ai-confidence">--</span></span>
                            <span id="live-time">--</span>
                        </div>

                        <div class="label-buttons">
                            <button class="label-btn btn-ad" onclick="labelFrame('ad')" id="btn-ad">Reklama</button>
                            <button class="label-btn btn-program" onclick="labelFrame('program')" id="btn-program">Program</button>
                            <button class="label-btn btn-transition" onclick="labelFrame('transition')" id="btn-transition">Prechod</button>
                        </div>
                        <div class="label-feedback" id="label-feedback"></div>
                        <div class="shortcuts">
                            Keyboard: <span class="kbd">1</span> Reklama &nbsp;
                            <span class="kbd">2</span> Program &nbsp;
                            <span class="kbd">3</span> Prechod
                        </div>
                    </div>
                </div>

                <!-- System Status -->
                <div class="card" style="margin-top: 20px;">
                    <div class="card-title">System Status</div>
                    <div class="card-body">
                        <div id="system-status" style="font-size: 13px; line-height: 2;">
                            Loading...
                        </div>
                    </div>
                </div>
            </div>

            <!-- Right: Stats + Progress -->
            <div>
                <!-- Training Progress -->
                <div class="card">
                    <div class="card-title">Training Dataset Progress</div>
                    <div class="card-body">
                        <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px;">
                            <span id="progress-count">0 / 300 samples</span>
                            <span id="progress-pct">0%</span>
                        </div>
                        <div class="progress-container">
                            <div class="progress-bar" id="progress-bar" style="width: 0%;"></div>
                        </div>
                        <div class="stats-grid" style="margin-top: 16px;">
                            <div class="stat-box">
                                <div class="stat-value" id="stat-ad">0</div>
                                <div class="stat-label">Reklama</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-value" id="stat-program">0</div>
                                <div class="stat-label">Program</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-value" id="stat-transition">0</div>
                                <div class="stat-label">Prechod</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-value" id="stat-accuracy" style="color: #22c55e;">--</div>
                                <div class="stat-label">AI Accuracy</div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Ad Analytics -->
                <div class="card" style="margin-top: 20px;">
                    <div class="card-title">Ad Analytics (24h)</div>
                    <div class="card-body">
                        <div class="analytics-grid">
                            <div class="analytics-box">
                                <div class="analytics-value" id="analytics-blocks" style="color: #ff4444;">--</div>
                                <div class="analytics-label">Ad Blocks</div>
                            </div>
                            <div class="analytics-box">
                                <div class="analytics-value" id="analytics-duration" style="color: #f59e0b;">--</div>
                                <div class="analytics-label">Avg Duration</div>
                            </div>
                            <div class="analytics-box">
                                <div class="analytics-value" id="analytics-interval" style="color: #3b82f6;">--</div>
                                <div class="analytics-label">Avg Interval</div>
                            </div>
                            <div class="analytics-box">
                                <div class="analytics-value" id="analytics-overrides" style="color: #ff6b6b;">--</div>
                                <div class="analytics-label">AI Overrides</div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Recent Labels -->
                <div class="card" style="margin-top: 20px;">
                    <div class="card-title">Recent Labels</div>
                    <div class="card-body" style="max-height: 300px; overflow-y: auto;">
                        <table class="labels-table">
                            <thead>
                                <tr>
                                    <th>Time</th>
                                    <th>Label</th>
                                    <th>AI Said</th>
                                    <th>Override</th>
                                </tr>
                            </thead>
                            <tbody id="labels-tbody">
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Gallery (full width) -->
            <div class="grid-full">
                <div class="card">
                    <div class="card-title">Labeled Frames Gallery (Recent)</div>
                    <div class="card-body">
                        <div class="gallery" id="gallery"></div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Image Modal -->
    <div class="modal-overlay" id="modal" onclick="closeModal()">
        <button class="modal-close" onclick="closeModal()">&times;</button>
        <img class="modal-img" id="modal-img" src="" alt="Full size">
    </div>

    <script>
        const DEVICE_ID = '{device_id}';
        const API_KEY = '{api_key}';
        const BASE = '/v1';

        function headers() {{
            return {{
                'Content-Type': 'application/json',
                'X-API-Key': API_KEY,
                'X-Device-Id': DEVICE_ID,
            }};
        }}

        // ── Labeling ──────────────────────────────────────
        let labeling = false;

        async function labelFrame(label) {{
            if (labeling) return;
            labeling = true;
            const btns = document.querySelectorAll('.label-btn');
            btns.forEach(b => b.disabled = true);

            const fb = document.getElementById('label-feedback');
            fb.textContent = 'Saving...';
            fb.style.color = '#FFD33D';

            try {{
                const res = await fetch(BASE + '/label', {{
                    method: 'POST',
                    headers: headers(),
                    body: JSON.stringify({{ label, device_id: DEVICE_ID }}),
                }});
                const data = await res.json();
                if (data.ok) {{
                    const labelNames = {{ ad: 'REKLAMA', program: 'PROGRAM', transition: 'PRECHOD' }};
                    const override = data.is_override ? ' (AI Override!)' : '';
                    fb.textContent = '\\u2713 Saved: ' + labelNames[label] + override;
                    fb.style.color = '#22c55e';
                    refreshStats();
                    refreshLabels();
                    refreshGallery();
                }} else {{
                    fb.textContent = '\\u2717 Error: ' + (data.detail || 'Unknown');
                    fb.style.color = '#ff4444';
                }}
            }} catch (e) {{
                fb.textContent = '\\u2717 Network error';
                fb.style.color = '#ff4444';
            }}

            setTimeout(() => {{ fb.textContent = ''; }}, 3000);
            btns.forEach(b => b.disabled = false);
            labeling = false;
        }}

        // ── Keyboard shortcuts ──────────────────────────
        document.addEventListener('keydown', (e) => {{
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.key === '1') labelFrame('ad');
            else if (e.key === '2') labelFrame('program');
            else if (e.key === '3') labelFrame('transition');
            else if (e.key === 'Escape') closeModal();
        }});

        // ── Live image refresh ──────────────────────────
        async function refreshLiveImage() {{
            try {{
                const res = await fetch(BASE + '/live-image?device_id=' + DEVICE_ID, {{
                    headers: headers(),
                }});
                const data = await res.json();
                const img = document.getElementById('live-img');
                const noImg = document.getElementById('no-live-img');

                if (data.has_image && data.image_base64) {{
                    img.src = 'data:image/jpeg;base64,' + data.image_base64;
                    img.style.display = 'block';
                    noImg.style.display = 'none';

                    const pred = document.getElementById('ai-prediction');
                    const conf = document.getElementById('ai-confidence');
                    if (data.is_ad) {{
                        pred.innerHTML = '<span class="badge badge-ad">AD</span>';
                    }} else {{
                        pred.innerHTML = '<span class="badge badge-ok">OK</span>';
                    }}
                    conf.textContent = data.confidence != null ? (data.confidence * 100).toFixed(1) + '%' : '--';
                    document.getElementById('live-time').textContent = data.timestamp ? timeSince(data.timestamp) : '';
                }} else {{
                    img.style.display = 'none';
                    noImg.style.display = 'block';
                }}
            }} catch (e) {{ }}
        }}

        // ── Stats ──────────────────────────────────────
        async function refreshStats() {{
            try {{
                const res = await fetch(BASE + '/labels/stats?device_id=' + DEVICE_ID, {{
                    headers: headers(),
                }});
                const data = await res.json();

                const ad = data.by_label.ad || 0;
                const prog = data.by_label.program || 0;
                const tAd = data.target_ad || 500;
                const tProg = data.target_program || 500;
                document.getElementById('stat-ad').textContent = ad + ' / ' + tAd;
                document.getElementById('stat-program').textContent = prog + ' / ' + tProg;
                document.getElementById('stat-transition').textContent = (data.by_label.transition || 0) + ' (bonus)';

                document.getElementById('progress-count').textContent = data.progress_pct + '% complete';
                document.getElementById('progress-pct').textContent = data.progress_pct + '%';
                document.getElementById('progress-bar').style.width = data.progress_pct + '%';

                const acc = document.getElementById('stat-accuracy');
                if (data.ai_accuracy_pct != null) {{
                    acc.textContent = data.ai_accuracy_pct + '%';
                    acc.style.color = data.ai_accuracy_pct >= 90 ? '#22c55e' : data.ai_accuracy_pct >= 70 ? '#f59e0b' : '#ff4444';
                }} else {{
                    acc.textContent = '--';
                }}

                // Analytics
                document.getElementById('analytics-blocks').textContent = data.ad_blocks_24h || 0;
                document.getElementById('analytics-duration').textContent = data.avg_ad_duration_s != null ? formatDuration(data.avg_ad_duration_s) : '--';
                document.getElementById('analytics-interval').textContent = data.avg_interval_between_ads_s != null ? formatDuration(data.avg_interval_between_ads_s) : '--';
                document.getElementById('analytics-overrides').textContent = data.overrides || 0;
            }} catch (e) {{ }}
        }}

        // ── Recent labels ──────────────────────────────
        async function refreshLabels() {{
            try {{
                const res = await fetch(BASE + '/labels?device_id=' + DEVICE_ID + '&limit=20', {{
                    headers: headers(),
                }});
                const data = await res.json();
                const tbody = document.getElementById('labels-tbody');
                tbody.innerHTML = '';
                for (const f of data) {{
                    const tr = document.createElement('tr');
                    const labelColors = {{ ad: '#ff4444', program: '#22c55e', transition: '#f59e0b' }};
                    const labelNames = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
                    tr.innerHTML = `
                        <td>${{timeSince(f.created_at)}}</td>
                        <td style="color: ${{labelColors[f.label] || '#eee'}}">${{labelNames[f.label] || f.label}}</td>
                        <td>${{f.ai_was_ad != null ? (f.ai_was_ad ? 'AD' : 'OK') : '--'}}</td>
                        <td>${{f.is_override ? '\\u26a0\\ufe0f' : '\\u2713'}}</td>
                    `;
                    tbody.appendChild(tr);
                }}
            }} catch (e) {{ }}
        }}

        // ── Gallery ──────────────────────────────────
        async function refreshGallery() {{
            try {{
                const res = await fetch(BASE + '/labels?device_id=' + DEVICE_ID + '&limit=30', {{
                    headers: headers(),
                }});
                const data = await res.json();
                const gallery = document.getElementById('gallery');
                gallery.innerHTML = '';
                for (const f of data) {{
                    const labelClass = {{ ad: 'gl-ad', program: 'gl-program', transition: 'gl-transition' }};
                    const labelNames = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
                    const div = document.createElement('div');
                    div.className = 'gallery-item';
                    div.innerHTML = `
                        <img data-frame-id="${{f.id}}" alt="${{f.label}}" style="background:#1a1a2e;" onclick="openModal(this.src)">
                        <span class="gallery-label ${{labelClass[f.label] || ''}}">${{labelNames[f.label] || f.label}}</span>
                        <button class="delete-btn" onclick="deleteFrame(${{f.id}}, event)">&times;</button>
                    `;
                    gallery.appendChild(div);

                    // Fetch image with auth headers
                    fetchGalleryImage(f.id);
                }}
            }} catch (e) {{ }}
        }}

        async function fetchGalleryImage(frameId) {{
            try {{
                const res = await fetch(BASE + '/labels/' + frameId + '.jpg', {{
                    headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }},
                }});
                if (res.ok) {{
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const img = document.querySelector(`img[data-frame-id="${{frameId}}"]`);
                    if (img) {{
                        img.src = url;
                    }}
                }}
            }} catch (e) {{ }}
        }}

        async function deleteFrame(id, event) {{
            event.stopPropagation();
            if (!confirm('Delete this labeled frame?')) return;
            try {{
                await fetch(BASE + '/labels/' + id, {{
                    method: 'DELETE',
                    headers: headers(),
                }});
                refreshGallery();
                refreshStats();
                refreshLabels();
            }} catch (e) {{ }}
        }}

        // ── System Status ──────────────────────────────
        async function refreshSystemStatus() {{
            try {{
                const res = await fetch(BASE + '/monitor/data?device_id=' + DEVICE_ID, {{
                    headers: headers(),
                }});
                const data = await res.json();

                // RPi status in header
                const rpiEl = document.getElementById('rpi-status');
                if (data.rpi_status) {{
                    const online = data.rpi_status.is_online;
                    rpiEl.innerHTML = `<span class="status-dot ${{online ? 'dot-online' : 'dot-offline'}}"></span>RPi: ${{online ? 'Online' : 'Offline'}}`;
                }}

                // AI status in header
                const aiEl = document.getElementById('ai-status');
                if (data.state) {{
                    aiEl.innerHTML = data.state.ad_active
                        ? '<span class="badge badge-ad">AD Active</span>'
                        : '<span class="badge badge-ok">No Ad</span>';
                }}

                // System status card
                const statusEl = document.getElementById('system-status');
                let html = '';
                if (data.rpi_status) {{
                    const r = data.rpi_status;
                    html += `<span class="status-dot ${{r.is_online ? 'dot-online' : 'dot-offline'}}"></span>RPi: ${{r.is_online ? 'Online' : 'Offline'}}`;
                    if (r.last_heartbeat) html += ` (last: ${{timeSince(r.last_heartbeat)}})`;
                    html += '<br>';
                    html += `Capture: ${{r.capture_running ? '\\u2705 Running' : '\\u274c Stopped'}} | Detect: ${{r.detect_running ? '\\u2705 Running' : '\\u274c Stopped'}}<br>`;
                    if (r.cpu_percent != null) html += `CPU: ${{r.cpu_percent}}% | RAM: ${{r.memory_percent}}% | Disk: ${{r.disk_percent}}%<br>`;
                    html += `Frames: ${{r.frames_captured}} captured / ${{r.frames_processed}} processed<br>`;
                }}
                if (data.config) {{
                    html += `<br>Auto-switch: ${{data.config.auto_switch_enabled ? '\\u2705 ON' : '\\u274c OFF'}}`;
                    if (data.config.fallback_channel) html += ` | Fallback CH: ${{data.config.fallback_channel}}`;
                    if (data.config.original_channel) html += ` | Original CH: ${{data.config.original_channel}}`;
                }}
                statusEl.innerHTML = html || 'No status data available.';

                document.getElementById('update-time').textContent = 'Updated: ' + new Date().toLocaleTimeString();
            }} catch (e) {{ }}
        }}

        // ── Modal ──────────────────────────────────
        function openModal(src) {{
            document.getElementById('modal-img').src = src;
            document.getElementById('modal').classList.add('active');
        }}
        function closeModal() {{
            document.getElementById('modal').classList.remove('active');
        }}

        // ── Helpers ──────────────────────────────────
        function timeSince(isoStr) {{
            if (!isoStr) return '--';
            const d = new Date(isoStr);
            const now = new Date();
            const s = Math.floor((now - d) / 1000);
            if (s < 5) return 'just now';
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s / 60) + 'm ago';
            if (s < 86400) return Math.floor(s / 3600) + 'h ago';
            return Math.floor(s / 86400) + 'd ago';
        }}

        function formatDuration(seconds) {{
            if (seconds < 60) return Math.round(seconds) + 's';
            if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + Math.round(seconds % 60) + 's';
            return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
        }}

        // ── Init & Refresh loops ──────────────────────
        refreshLiveImage();
        refreshStats();
        refreshLabels();
        refreshGallery();
        refreshSystemStatus();

        setInterval(refreshLiveImage, 2000);
        setInterval(refreshStats, 10000);
        setInterval(refreshLabels, 10000);
        setInterval(refreshGallery, 15000);
        setInterval(refreshSystemStatus, 5000);
    </script>
    <script>{NAV_STATUS_JS}</script>
</body>
</html>
"""
    return HTMLResponse(content=html)
