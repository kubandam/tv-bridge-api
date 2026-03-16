from __future__ import annotations

import base64
import csv
import io
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select, desc, func

from app.db.engine import get_session
from app.models import FrameHistoryDB, LabeledFrameDB, utcnow
from app.settings import settings

router = APIRouter(tags=["review"])


# ── API Endpoints ──────────────────────────────────────────────────

class LabelIn(BaseModel):
    label: str  # ad | program | transition
    device_id: str = "tv-1"


@router.get("/history/frames")
def list_frames(
    device_id: str = Query(default="tv-1"),
    filter: str = Query(default="unlabeled", description="unlabeled | all | ad | program | transition"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
):
    """Paginated list of historical frames."""
    base_stmt = select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)

    if filter == "unlabeled":
        base_stmt = base_stmt.where(FrameHistoryDB.label == None)
    elif filter in ("ad", "program", "transition"):
        base_stmt = base_stmt.where(FrameHistoryDB.label == filter)

    total = db.exec(select(func.count()).select_from(base_stmt.subquery())).one()
    frames = db.exec(base_stmt.order_by(desc(FrameHistoryDB.id)).offset(offset).limit(limit)).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": f.id,
                "is_ad": f.is_ad,
                "confidence": f.confidence,
                "label": f.label,
                "captured_at": f.captured_at.isoformat() if f.captured_at else None,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in frames
        ],
    }


@router.get("/history/frames/{frame_id}.jpg")
def get_frame_image(frame_id: int, db: Session = Depends(get_session)):
    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    return Response(content=base64.b64decode(frame.image_base64), media_type="image/jpeg")


@router.post("/history/frames/{frame_id}/label")
def label_frame(
    frame_id: int,
    body: LabelIn,
    db: Session = Depends(get_session),
):
    """Label a historical frame. Saves to frame_history and copies to labeled_frames."""
    if body.label not in ("ad", "program", "transition"):
        raise HTTPException(status_code=400, detail="Label must be: ad, program, transition")

    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")

    now = utcnow()
    is_override = False
    if body.label == "ad" and not frame.is_ad:
        is_override = True
    elif body.label in ("program", "transition") and frame.is_ad:
        is_override = True

    frame.label = body.label
    frame.labeled_at = now
    frame.is_override = is_override
    db.add(frame)

    # Copy to labeled_frames so existing training pipeline sees it
    labeled = LabeledFrameDB(
        device_id=frame.device_id,
        label=body.label,
        image_base64=frame.image_base64,
        ai_was_ad=frame.is_ad,
        ai_confidence=frame.confidence,
        is_override=is_override,
        created_at=frame.captured_at or now,
    )
    db.add(labeled)
    db.commit()
    db.refresh(labeled)

    return {
        "ok": True,
        "frame_id": frame_id,
        "labeled_frame_id": labeled.id,
        "label": body.label,
        "is_override": is_override,
    }


class BulkLabelIn(BaseModel):
    ids: list[int]
    label: str
    device_id: str = "tv-1"


@router.post("/history/frames/bulk-label")
def bulk_label_frames(body: BulkLabelIn, db: Session = Depends(get_session)):
    """Label multiple historical frames at once."""
    if body.label not in ("ad", "program", "transition"):
        raise HTTPException(status_code=400, detail="Label must be: ad, program, transition")
    if not body.ids:
        raise HTTPException(status_code=400, detail="No frame IDs provided")
    if len(body.ids) > 500:
        raise HTTPException(status_code=400, detail="Max 500 frames per bulk operation")

    now = utcnow()
    frames = db.exec(select(FrameHistoryDB).where(FrameHistoryDB.id.in_(body.ids))).all()

    for frame in frames:
        is_override = (body.label == "ad" and not frame.is_ad) or \
                      (body.label in ("program", "transition") and frame.is_ad)
        frame.label = body.label
        frame.labeled_at = now
        frame.is_override = is_override
        db.add(frame)
        db.add(LabeledFrameDB(
            device_id=frame.device_id,
            label=body.label,
            image_base64=frame.image_base64,
            ai_was_ad=frame.is_ad,
            ai_confidence=frame.confidence,
            is_override=is_override,
            created_at=frame.captured_at or now,
        ))

    db.commit()
    return {"ok": True, "labeled": len(frames), "label": body.label}


@router.delete("/history/frames/{frame_id}")
def delete_frame(frame_id: int, db: Session = Depends(get_session)):
    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    db.delete(frame)
    db.commit()
    return {"ok": True, "deleted_id": frame_id}


@router.get("/history/stats")
def get_history_stats(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """Training readiness stats combining history labels + live labels."""
    all_history = db.exec(
        select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)
    ).all()

    total_history = len(all_history)
    unlabeled = sum(1 for f in all_history if f.label is None)
    labeled_from_history = total_history - unlabeled

    by_label = {"ad": 0, "program": 0, "transition": 0}
    overrides = 0
    for f in all_history:
        if f.label:
            by_label[f.label] = by_label.get(f.label, 0) + 1
        if f.is_override:
            overrides += 1

    labeled_from_live = db.exec(
        select(func.count()).where(LabeledFrameDB.device_id == device_id)
    ).one()

    # labeled_frames includes copies from history, so use frame_history as source of truth
    # to avoid double-counting: total labeled = labeled_from_history + live-only labels
    # Live-only = LabeledFrameDB rows that have no corresponding FrameHistoryDB label
    # Simplest: just report both numbers and their sum
    total_labeled = labeled_from_history + labeled_from_live

    # Per-class targets: 500 ad, 500 program, transition = bonus (rare, collect what we can)
    target_ad = 500
    target_program = 500
    ad_count = by_label.get("ad", 0)
    program_count = by_label.get("program", 0)
    transition_count = by_label.get("transition", 0)
    progress_pct = round((min(ad_count, target_ad) + min(program_count, target_program)) / (target_ad + target_program) * 100, 1)
    training_ready = ad_count >= target_ad and program_count >= target_program

    return {
        "total_history": total_history,
        "unlabeled": unlabeled,
        "labeled_from_history": labeled_from_history,
        "labeled_from_live": labeled_from_live,
        "total_labeled": total_labeled,
        "by_label": by_label,
        "overrides": overrides,
        "target_ad": target_ad,
        "target_program": target_program,
        "progress_pct": progress_pct,
        "training_ready": training_ready,
    }


@router.get("/history/export.zip")
def export_training_dataset(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """
    Download all labeled frames as a ZIP for ML training.
    Contains: images/XXXXXX_label.jpg and labels.csv
    """
    history_labeled = db.exec(
        select(FrameHistoryDB)
        .where(FrameHistoryDB.device_id == device_id)
        .where(FrameHistoryDB.label != None)
        .order_by(FrameHistoryDB.id)
    ).all()

    live_labeled = db.exec(
        select(LabeledFrameDB)
        .where(LabeledFrameDB.device_id == device_id)
        .order_by(LabeledFrameDB.id)
    ).all()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_rows = [["filename", "label", "ai_was_ad", "ai_confidence", "captured_at", "source"]]

        for f in history_labeled:
            fname = f"images/{f.id:06d}_{f.label}.jpg"
            zf.writestr(fname, base64.b64decode(f.image_base64))
            csv_rows.append([
                fname, f.label, str(f.is_ad),
                f"{f.confidence:.4f}" if f.confidence is not None else "",
                f.captured_at.isoformat() if f.captured_at else "",
                "history",
            ])

        for f in live_labeled:
            fname = f"images/live_{f.id:06d}_{f.label}.jpg"
            zf.writestr(fname, base64.b64decode(f.image_base64))
            csv_rows.append([
                fname, f.label, str(f.ai_was_ad),
                f"{f.ai_confidence:.4f}" if f.ai_confidence is not None else "",
                f.created_at.isoformat() if f.created_at else "",
                "live",
            ])

        csv_buf = io.StringIO()
        csv.writer(csv_buf).writerows(csv_rows)
        zf.writestr("labels.csv", csv_buf.getvalue())

    zip_buffer.seek(0)
    total = len(history_labeled) + len(live_labeled)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dataset_{device_id}_{total}frames.zip"'
        },
    )


# ── HTML Dashboard ──────────────────────────────────────────────────

def review_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    if api_key != settings.api_key:
        return HTMLResponse(
            content="""<html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;
            padding:50px;text-align:center"><h1 style="color:#ff6b6b">API Key Required</h1>
            <p>Add <code>?api_key=YOUR_KEY</code> to the URL.</p></body></html>""",
            status_code=401,
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Review History - {device_id}</title>
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
            padding: 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #2a2a4e;
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .header h1 {{ font-size: 18px; color: #a78bfa; }}
        .header-right {{ display: flex; gap: 16px; align-items: center; font-size: 12px; color: #888; }}

        .main {{ display: grid; grid-template-columns: 1fr 280px; gap: 20px; padding: 20px; max-width: 1200px; margin: 0 auto; }}

        /* Left: review area */
        .review-area {{ display: flex; flex-direction: column; gap: 16px; }}

        /* Filter + mode tabs */
        .tabs-row {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
        .filter-tabs {{ display: flex; gap: 6px; flex: 1; flex-wrap: wrap; }}
        .filter-tab {{
            padding: 6px 14px;
            border-radius: 20px;
            border: 1px solid #2a2a4e;
            background: #1a1a2e;
            color: #888;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
            transition: all 0.15s;
        }}
        .filter-tab.active {{ background: #a78bfa; border-color: #a78bfa; color: #111; }}
        .filter-tab:hover:not(.active) {{ border-color: #a78bfa; color: #a78bfa; }}

        /* Mode toggle */
        .mode-toggle {{ display: flex; background: #1a1a2e; border-radius: 6px; border: 1px solid #2a2a4e; overflow: hidden; }}
        .mode-btn {{ padding: 6px 14px; border: none; background: transparent; color: #888; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.15s; }}
        .mode-btn.active {{ background: #2a2a4e; color: #eee; }}

        /* Grid thumbnails */
        .thumb-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }}
        .thumb-item {{
            position: relative;
            border-radius: 6px;
            overflow: hidden;
            cursor: pointer;
            border: 2px solid transparent;
            background: #1a1a2e;
            aspect-ratio: 16/9;
            transition: border-color 0.1s, transform 0.1s;
        }}
        .thumb-item:hover {{ border-color: #a78bfa; transform: scale(1.02); }}
        .thumb-item.selected {{ border-color: #a78bfa; }}
        .thumb-item.selected::after {{
            content: '';
            position: absolute;
            inset: 0;
            background: rgba(167,139,250,0.2);
            pointer-events: none;
        }}
        .thumb-item img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
        .thumb-placeholder {{ width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #333; font-size: 11px; }}
        .thumb-badge {{ position: absolute; top: 4px; left: 4px; display: flex; gap: 3px; }}
        .thumb-check {{
            position: absolute;
            top: 4px;
            right: 4px;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            background: #a78bfa;
            color: #111;
            font-size: 13px;
            font-weight: 900;
            display: none;
            align-items: center;
            justify-content: center;
        }}
        .thumb-item.selected .thumb-check {{ display: flex; }}
        .thumb-label-tag {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 2px 6px;
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            text-align: center;
        }}
        .tl-ad {{ background: rgba(255,68,68,0.85); color: #fff; }}
        .tl-program {{ background: rgba(34,197,94,0.85); color: #fff; }}
        .tl-transition {{ background: rgba(245,158,11,0.85); color: #111; }}

        /* Selection action bar */
        .sel-bar {{
            position: sticky;
            bottom: 0;
            background: #1a1a2e;
            border-top: 2px solid #a78bfa;
            padding: 12px 16px;
            display: none;
            gap: 10px;
            align-items: center;
            z-index: 50;
            flex-wrap: wrap;
        }}
        .sel-bar.visible {{ display: flex; }}
        .sel-count {{ font-size: 14px; font-weight: 700; color: #a78bfa; min-width: 90px; }}
        .sel-label-btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 7px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            text-transform: uppercase;
            transition: all 0.12s;
        }}
        .sel-label-btn:hover {{ transform: translateY(-1px); }}
        .sel-label-btn:disabled {{ opacity: 0.4; cursor: not-allowed; transform: none; }}
        .sel-helpers {{ display: flex; gap: 6px; margin-left: auto; }}
        .sel-helper {{ padding: 6px 12px; border: 1px solid #2a2a4e; border-radius: 5px; background: transparent; color: #888; font-size: 12px; cursor: pointer; }}
        .sel-helper:hover {{ border-color: #a78bfa; color: #a78bfa; }}

        /* Load more */
        .load-more-btn {{ display: block; width: 100%; padding: 10px; background: #1a1a2e; border: 1px solid #2a2a4e; border-radius: 6px; color: #888; font-size: 13px; cursor: pointer; text-align: center; margin-top: 8px; }}
        .load-more-btn:hover {{ border-color: #a78bfa; color: #a78bfa; }}

        /* Frame card */
        .frame-card {{
            background: #1a1a2e;
            border-radius: 10px;
            border: 1px solid #2a2a4e;
            overflow: hidden;
        }}
        .frame-header {{
            padding: 10px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #2a2a4e;
            font-size: 12px;
            color: #888;
        }}
        .frame-counter {{ font-weight: 700; color: #a78bfa; font-size: 14px; }}
        .frame-img-wrap {{
            position: relative;
            background: #0a0a14;
            min-height: 200px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        #frame-img {{
            max-width: 100%;
            max-height: 420px;
            display: block;
        }}
        .frame-img-loading {{
            color: #444;
            font-size: 14px;
            padding: 60px;
            text-align: center;
        }}
        .frame-meta {{
            padding: 10px 16px;
            display: flex;
            gap: 20px;
            align-items: center;
            font-size: 13px;
            border-top: 1px solid #2a2a4e;
            background: #141420;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 10px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .badge-ad {{ background: #ff4444; color: #fff; }}
        .badge-ok {{ background: #22c55e; color: #fff; }}

        /* Label buttons */
        .label-area {{
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            align-items: center;
        }}
        .label-buttons {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }}
        .label-btn {{
            padding: 13px 26px;
            border: none;
            border-radius: 8px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.12s;
            min-width: 130px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .label-btn:hover {{ transform: translateY(-2px); box-shadow: 0 4px 14px rgba(0,0,0,0.35); }}
        .label-btn:active {{ transform: translateY(0); }}
        .label-btn:disabled {{ opacity: 0.4; cursor: not-allowed; transform: none; }}
        .btn-ad {{ background: #ff4444; color: #fff; }}
        .btn-program {{ background: #22c55e; color: #fff; }}
        .btn-transition {{ background: #f59e0b; color: #111; }}
        .btn-skip {{
            background: #2a2a4e;
            color: #aaa;
            padding: 8px 22px;
            border: none;
            border-radius: 6px;
            font-size: 13px;
            cursor: pointer;
            transition: background 0.15s;
        }}
        .btn-skip:hover {{ background: #3a3a6e; color: #ccc; }}
        .label-feedback {{
            font-size: 13px;
            min-height: 20px;
            text-align: center;
        }}
        .shortcuts {{ font-size: 11px; color: #555; text-align: center; }}
        .kbd {{
            display: inline-block;
            background: #2a2a4e;
            border-radius: 3px;
            padding: 1px 6px;
            font-family: monospace;
            font-size: 11px;
            color: #aaa;
            border: 1px solid #3a3a5e;
        }}

        /* Empty state */
        .empty-state {{
            text-align: center;
            padding: 60px 20px;
            color: #555;
        }}
        .empty-state h2 {{ color: #22c55e; margin-bottom: 10px; }}

        /* Right sidebar */
        .sidebar {{ display: flex; flex-direction: column; gap: 16px; }}
        .card {{
            background: #1a1a2e;
            border-radius: 8px;
            border: 1px solid #2a2a4e;
            overflow: hidden;
        }}
        .card-title {{
            padding: 10px 14px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #666;
            border-bottom: 1px solid #2a2a4e;
        }}
        .card-body {{ padding: 14px; }}

        /* Progress */
        .progress-bar-wrap {{
            background: #2a2a4e;
            border-radius: 6px;
            height: 20px;
            position: relative;
            overflow: hidden;
            margin-bottom: 12px;
        }}
        .progress-bar-fill {{
            height: 100%;
            border-radius: 6px;
            background: linear-gradient(90deg, #a78bfa, #7c3aed);
            transition: width 0.5s;
        }}
        .progress-bar-text {{
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 700;
            color: #fff;
        }}
        .stat-row {{
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            font-size: 13px;
            border-bottom: 1px solid #1f1f35;
        }}
        .stat-row:last-child {{ border-bottom: none; }}
        .stat-val {{ font-weight: 700; }}

        /* Recent labels */
        .recent-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 0;
            border-bottom: 1px solid #1f1f35;
            font-size: 12px;
        }}
        .recent-item:last-child {{ border-bottom: none; }}
        .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
        .dot-ad {{ background: #ff4444; }}
        .dot-program {{ background: #22c55e; }}
        .dot-transition {{ background: #f59e0b; }}
        .recent-time {{ color: #555; margin-left: auto; font-size: 11px; }}
        .override-tag {{ color: #f59e0b; font-size: 10px; }}

        /* Export button */
        .export-btn {{
            display: block;
            width: 100%;
            padding: 10px;
            background: #1f2d1f;
            border: 1px solid #22c55e;
            border-radius: 6px;
            color: #22c55e;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            transition: background 0.15s;
        }}
        .export-btn:hover {{ background: #2a3d2a; }}
        .export-ready {{ border-color: #a78bfa; color: #a78bfa; background: #1c1a2e; }}
        .export-ready:hover {{ background: #231f3d; }}

        @media (max-width: 768px) {{
            .main {{ grid-template-columns: 1fr; }}
            .sidebar {{ order: -1; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>History Review &amp; Labeling</h1>
        <div class="header-right">
            <span id="unlabeled-count">-- unlabeled</span>
            <span>Device: {device_id}</span>
            <span id="update-time">--</span>
        </div>
    </div>

    <div class="main">
        <!-- Review area -->
        <div class="review-area">
            <!-- Filter + mode row -->
            <div class="tabs-row">
                <div class="filter-tabs">
                    <button class="filter-tab active" onclick="setFilter('unlabeled')" id="tab-unlabeled">Unlabeled</button>
                    <button class="filter-tab" onclick="setFilter('all')" id="tab-all">All</button>
                    <button class="filter-tab" onclick="setFilter('ad')" id="tab-ad">Reklama</button>
                    <button class="filter-tab" onclick="setFilter('program')" id="tab-program">Program</button>
                    <button class="filter-tab" onclick="setFilter('transition')" id="tab-transition">Prechod</button>
                </div>
                <div class="mode-toggle">
                    <button class="mode-btn active" onclick="setMode('queue')" id="mode-queue">&#9654; Queue</button>
                    <button class="mode-btn" onclick="setMode('grid')" id="mode-grid">&#9783; Grid</button>
                </div>
            </div>

            <!-- Queue mode -->
            <div id="queue-section">
                <div class="frame-card" id="frame-card">
                    <div class="frame-header">
                        <span class="frame-counter" id="frame-counter">Loading...</span>
                        <span id="frame-time">--</span>
                    </div>
                    <div class="frame-img-wrap">
                        <img id="frame-img" src="" alt="Frame" style="display:none">
                        <div class="frame-img-loading" id="frame-loading">Loading frames...</div>
                    </div>
                    <div class="frame-meta" id="frame-meta" style="display:none">
                        <span>AI: <span id="ai-pred">--</span></span>
                        <span>Confidence: <span id="ai-conf">--</span></span>
                        <span id="frame-label-badge"></span>
                    </div>
                    <div class="label-area">
                        <div class="label-buttons">
                            <button class="label-btn btn-ad" onclick="doLabel('ad')" id="btn-ad">Reklama</button>
                            <button class="label-btn btn-program" onclick="doLabel('program')" id="btn-program">Program</button>
                            <button class="label-btn btn-transition" onclick="doLabel('transition')" id="btn-transition">Prechod</button>
                        </div>
                        <div class="label-feedback" id="label-fb"></div>
                        <button class="btn-skip" onclick="doSkip()">Skip &rarr; &nbsp;<span class="kbd">Space</span></button>
                        <div class="shortcuts">
                            <span class="kbd">1</span> Reklama &nbsp;
                            <span class="kbd">2</span> Program &nbsp;
                            <span class="kbd">3</span> Prechod &nbsp;
                            <span class="kbd">←</span><span class="kbd">→</span> Navigate
                        </div>
                    </div>
                </div>
                <div id="empty-state" style="display:none" class="frame-card">
                    <div class="empty-state">
                        <h2>&#10003; All done!</h2>
                        <p>No more frames to review in this filter.</p>
                        <p style="margin-top:8px;font-size:13px;">Switch filter or wait for new frames from RPi.</p>
                    </div>
                </div>
            </div>

            <!-- Grid mode -->
            <div id="grid-section" style="display:none">
                <div class="thumb-grid" id="thumb-grid"></div>
                <div id="grid-empty" style="display:none" class="frame-card">
                    <div class="empty-state"><h2>&#10003; All done!</h2><p>No frames in this filter.</p></div>
                </div>
                <button class="load-more-btn" id="load-more-btn" style="display:none" onclick="loadMoreGrid()">Load more frames...</button>
            </div>

            <!-- Selection bar (grid mode) -->
            <div class="sel-bar" id="sel-bar">
                <span class="sel-count" id="sel-count">0 selected</span>
                <button class="sel-label-btn btn-ad" onclick="bulkLabel('ad')" id="sel-btn-ad" disabled>&#128308; Reklama</button>
                <button class="sel-label-btn btn-program" onclick="bulkLabel('program')" id="sel-btn-prog" disabled>&#128994; Program</button>
                <button class="sel-label-btn btn-transition" onclick="bulkLabel('transition')" id="sel-btn-trans" disabled>&#128992; Prechod</button>
                <div class="sel-helpers">
                    <button class="sel-helper" onclick="selectAll()">Select All</button>
                    <button class="sel-helper" onclick="deselectAll()">Clear</button>
                </div>
            </div>
        </div>

        <!-- Sidebar -->
        <div class="sidebar">
            <!-- Training progress -->
            <div class="card">
                <div class="card-title">Training Progress</div>
                <div class="card-body">
                    <div style="font-size:11px;color:#666;margin-bottom:8px">Target: 500 Reklama + 500 Program</div>

                    <div style="margin-bottom:10px">
                        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
                            <span style="color:#ff4444">&#128308; Reklama</span>
                            <span id="stat-ad-txt" style="font-weight:700">0 / 500</span>
                        </div>
                        <div class="progress-bar-wrap" style="height:14px">
                            <div class="progress-bar-fill" id="prog-ad" style="width:0%;background:linear-gradient(90deg,#ff4444,#cc2222)"></div>
                        </div>
                    </div>

                    <div style="margin-bottom:10px">
                        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
                            <span style="color:#22c55e">&#128994; Program</span>
                            <span id="stat-prog-txt" style="font-weight:700">0 / 500</span>
                        </div>
                        <div class="progress-bar-wrap" style="height:14px">
                            <div class="progress-bar-fill" id="prog-prog" style="width:0%;background:linear-gradient(90deg,#22c55e,#16a34a)"></div>
                        </div>
                    </div>

                    <div style="margin-bottom:12px">
                        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
                            <span style="color:#f59e0b">&#128992; Prechod</span>
                            <span id="stat-trans-txt" style="font-weight:700;color:#555">0 (bonus)</span>
                        </div>
                        <div class="progress-bar-wrap" style="height:14px">
                            <div class="progress-bar-fill" id="prog-trans" style="width:0%;background:linear-gradient(90deg,#f59e0b,#d97706)"></div>
                        </div>
                    </div>

                    <div style="border-top:1px solid #2a2a4e;padding-top:10px">
                        <div style="font-size:11px;color:#666;margin-bottom:4px">Overall progress</div>
                        <div class="progress-bar-wrap">
                            <div class="progress-bar-fill" id="prog-bar" style="width:0%"></div>
                            <div class="progress-bar-text" id="prog-text">0%</div>
                        </div>
                    </div>

                    <div class="stat-row" style="margin-top:10px">
                        <span>Total history</span>
                        <span class="stat-val" id="stat-total">0</span>
                    </div>
                    <div class="stat-row">
                        <span>Unlabeled</span>
                        <span class="stat-val" id="stat-unlabeled">0</span>
                    </div>
                    <div class="stat-row">
                        <span>AI overrides</span>
                        <span class="stat-val" id="stat-overrides">0</span>
                    </div>
                </div>
            </div>

            <!-- Training ready indicator -->
            <div class="card" id="training-card">
                <div class="card-title">Dataset Export</div>
                <div class="card-body">
                    <p id="training-msg" style="font-size:12px;color:#888;margin-bottom:10px">
                        Collect 300 labeled frames to train the custom model.
                    </p>
                    <a id="export-btn" class="export-btn" href="#" onclick="downloadDataset(event)">
                        &#8595; Download Dataset (.zip)
                    </a>
                </div>
            </div>

            <!-- Recent labels -->
            <div class="card">
                <div class="card-title">Recent Labels</div>
                <div class="card-body" id="recent-labels" style="max-height:300px;overflow-y:auto;">
                    <div style="color:#555;font-size:12px">No labels yet.</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const DEVICE_ID = '{device_id}';
        const API_KEY = '{api_key}';
        const BASE = '/v1';

        function hdrs() {{
            return {{
                'Content-Type': 'application/json',
                'X-API-Key': API_KEY,
                'X-Device-Id': DEVICE_ID,
            }};
        }}

        // ── State ──────────────────────────────────────────────────
        let queue = [];
        let queueIdx = 0;
        let queueTotal = 0;
        let currentFilter = 'unlabeled';
        let currentMode = 'queue';
        let busy = false;
        let recentLabels = [];
        let imgCache = {{}};

        // Grid state
        let gridFrames = [];
        let gridTotal = 0;
        let gridOffset = 0;
        let selectedIds = new Set();
        let lastClickedIdx = null;
        const thumbObserver = new IntersectionObserver((entries) => {{
            entries.forEach(e => {{
                if (e.isIntersecting) {{
                    const id = parseInt(e.target.dataset.id);
                    if (!imgCache[id]) loadThumbImage(id);
                    thumbObserver.unobserve(e.target);
                }}
            }});
        }}, {{ rootMargin: '200px' }});

        // ── Init ──────────────────────────────────────────────────
        async function init() {{
            await loadQueue();
            await refreshStats();
        }}

        // ── Mode switching ────────────────────────────────────────
        function setMode(mode) {{
            currentMode = mode;
            document.getElementById('mode-queue').classList.toggle('active', mode === 'queue');
            document.getElementById('mode-grid').classList.toggle('active', mode === 'grid');
            document.getElementById('queue-section').style.display = mode === 'queue' ? 'block' : 'none';
            document.getElementById('grid-section').style.display = mode === 'grid' ? 'block' : 'none';
            document.getElementById('sel-bar').classList.toggle('visible', mode === 'grid');
            if (mode === 'grid') loadGrid(true);
        }}

        // ── Grid ──────────────────────────────────────────────────
        async function loadGrid(reset = false) {{
            if (reset) {{
                gridFrames = [];
                gridOffset = 0;
                selectedIds.clear();
                updateSelBar();
                document.getElementById('thumb-grid').innerHTML = '';
            }}
            try {{
                const res = await fetch(BASE + '/history/frames?device_id=' + DEVICE_ID + '&filter=' + currentFilter + '&limit=60&offset=' + gridOffset, {{ headers: hdrs() }});
                const data = await res.json();
                gridTotal = data.total;
                const newItems = data.items;
                gridFrames = gridFrames.concat(newItems);
                gridOffset = gridFrames.length;
                const container = document.getElementById('thumb-grid');
                newItems.forEach((f, i) => container.appendChild(createThumb(f, gridFrames.length - newItems.length + i)));
                document.getElementById('grid-empty').style.display = gridFrames.length === 0 ? 'block' : 'none';
                document.getElementById('load-more-btn').style.display = gridFrames.length < gridTotal ? 'block' : 'none';
                document.getElementById('update-time').textContent = new Date().toLocaleTimeString();
            }} catch(e) {{}}
        }}

        async function loadMoreGrid() {{
            await loadGrid(false);
        }}

        function createThumb(frame, idx) {{
            const div = document.createElement('div');
            div.className = 'thumb-item';
            div.dataset.id = frame.id;
            div.dataset.idx = idx;

            const aiBadge = frame.is_ad
                ? '<span class="badge badge-ad" style="font-size:9px;padding:2px 6px">AD</span>'
                : '<span class="badge badge-ok" style="font-size:9px;padding:2px 6px">OK</span>';
            const labelTag = frame.label
                ? `<div class="thumb-label-tag tl-${{frame.label}}">${{frame.label === 'transition' ? 'Prechod' : frame.label === 'ad' ? 'Reklama' : 'Program'}}</div>`
                : '';

            div.innerHTML = `
                <img id="timg-${{frame.id}}" style="display:none" alt="">
                <div class="thumb-placeholder" id="tload-${{frame.id}}">&#9651;</div>
                <div class="thumb-badge">${{aiBadge}}</div>
                <div class="thumb-check">&#10003;</div>
                ${{labelTag}}
            `;
            div.addEventListener('click', e => onThumbClick(e, frame.id, idx));
            thumbObserver.observe(div);
            return div;
        }}

        async function loadThumbImage(frameId) {{
            try {{
                const res = await fetch(BASE + '/history/frames/' + frameId + '.jpg', {{
                    headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }},
                }});
                if (res.ok) {{
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    imgCache[frameId] = url;
                    const img = document.getElementById('timg-' + frameId);
                    const ph = document.getElementById('tload-' + frameId);
                    if (img) {{ img.src = url; img.style.display = 'block'; }}
                    if (ph) ph.style.display = 'none';
                }}
            }} catch(e) {{}}
        }}

        function onThumbClick(e, frameId, idx) {{
            if (e.shiftKey && lastClickedIdx !== null) {{
                const a = Math.min(lastClickedIdx, idx);
                const b = Math.max(lastClickedIdx, idx);
                for (let i = a; i <= b; i++) {{
                    if (gridFrames[i]) selectedIds.add(gridFrames[i].id);
                }}
            }} else if (e.ctrlKey || e.metaKey) {{
                if (selectedIds.has(frameId)) selectedIds.delete(frameId);
                else selectedIds.add(frameId);
            }} else {{
                if (selectedIds.has(frameId) && selectedIds.size === 1) selectedIds.delete(frameId);
                else {{ selectedIds.clear(); selectedIds.add(frameId); }}
            }}
            lastClickedIdx = idx;
            updateGridSelection();
            updateSelBar();
        }}

        function updateGridSelection() {{
            document.querySelectorAll('.thumb-item').forEach(el => {{
                el.classList.toggle('selected', selectedIds.has(parseInt(el.dataset.id)));
            }});
        }}

        function selectAll() {{
            gridFrames.forEach(f => selectedIds.add(f.id));
            updateGridSelection();
            updateSelBar();
        }}

        function deselectAll() {{
            selectedIds.clear();
            updateGridSelection();
            updateSelBar();
        }}

        function updateSelBar() {{
            const n = selectedIds.size;
            document.getElementById('sel-count').textContent = n + ' selected';
            ['sel-btn-ad', 'sel-btn-prog', 'sel-btn-trans'].forEach(id => {{
                const btn = document.getElementById(id);
                if (btn) btn.disabled = n === 0;
            }});
        }}

        async function bulkLabel(label) {{
            if (selectedIds.size === 0 || busy) return;
            busy = true;
            const ids = Array.from(selectedIds);
            const selCount = document.getElementById('sel-count');
            selCount.textContent = 'Saving ' + ids.length + '...';
            try {{
                const res = await fetch(BASE + '/history/frames/bulk-label', {{
                    method: 'POST',
                    headers: hdrs(),
                    body: JSON.stringify({{ ids, label, device_id: DEVICE_ID }}),
                }});
                const data = await res.json();
                if (data.ok) {{
                    const labelNames = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
                    addRecent(label, false, new Date().toISOString());
                    if (currentFilter === 'unlabeled') {{
                        ids.forEach(id => {{
                            const el = document.querySelector('.thumb-item[data-id="' + id + '"]');
                            if (el) el.remove();
                            const fi = gridFrames.findIndex(f => f.id === id);
                            if (fi !== -1) gridFrames.splice(fi, 1);
                        }});
                        gridOffset -= ids.length;
                        gridTotal = Math.max(0, gridTotal - ids.length);
                        document.getElementById('grid-empty').style.display = gridFrames.length === 0 ? 'block' : 'none';
                    }} else {{
                        // Update label tags in DOM
                        const tagClass = {{ ad: 'tl-ad', program: 'tl-program', transition: 'tl-transition' }};
                        const tagName = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
                        ids.forEach(id => {{
                            const el = document.querySelector('.thumb-item[data-id="' + id + '"]');
                            if (el) {{
                                let tag = el.querySelector('.thumb-label-tag');
                                if (!tag) {{ tag = document.createElement('div'); tag.className = 'thumb-label-tag'; el.appendChild(tag); }}
                                tag.className = 'thumb-label-tag ' + tagClass[label];
                                tag.textContent = tagName[label];
                            }}
                        }});
                    }}
                    selectedIds.clear();
                    updateGridSelection();
                    refreshStats();
                }}
            }} catch(e) {{}}
            updateSelBar();
            busy = false;
        }}

        // ── Queue loading ─────────────────────────────────────────
        async function loadQueue(resetIdx = true) {{
            try {{
                const res = await fetch(BASE + '/history/frames?device_id=' + DEVICE_ID + '&filter=' + currentFilter + '&limit=100&offset=0', {{
                    headers: hdrs(),
                }});
                const data = await res.json();
                queue = data.items;
                queueTotal = data.total;
                if (resetIdx) queueIdx = 0;
                renderFrame();
                document.getElementById('update-time').textContent = new Date().toLocaleTimeString();
            }} catch (e) {{
                document.getElementById('frame-loading').textContent = 'Error loading frames.';
            }}
        }}

        async function loadMoreIfNeeded() {{
            // Preload more when near end of current batch
            if (queue.length - queueIdx < 5 && queue.length < queueTotal) {{
                const res = await fetch(BASE + '/history/frames?device_id=' + DEVICE_ID + '&filter=' + currentFilter + '&limit=50&offset=' + queue.length, {{
                    headers: hdrs(),
                }});
                const data = await res.json();
                queue = queue.concat(data.items);
            }}
        }}

        // ── Render current frame ──────────────────────────────────
        function renderFrame() {{
            const card = document.getElementById('frame-card');
            const empty = document.getElementById('empty-state');

            if (queue.length === 0) {{
                card.style.display = 'none';
                empty.style.display = 'block';
                document.getElementById('unlabeled-count').textContent = '0 ' + currentFilter;
                return;
            }}

            card.style.display = 'block';
            empty.style.display = 'none';

            const frame = queue[queueIdx];
            const pos = queueIdx + 1;
            const total = Math.max(queue.length, queueTotal);

            document.getElementById('frame-counter').textContent = pos + ' / ' + total;
            document.getElementById('unlabeled-count').textContent = queueTotal + ' ' + currentFilter;

            // Time
            const ts = frame.captured_at || frame.created_at;
            document.getElementById('frame-time').textContent = ts ? timeSince(ts) : '--';

            // AI prediction
            const predEl = document.getElementById('ai-pred');
            const confEl = document.getElementById('ai-conf');
            predEl.innerHTML = frame.is_ad
                ? '<span class="badge badge-ad">AD</span>'
                : '<span class="badge badge-ok">OK</span>';
            confEl.textContent = frame.confidence != null ? (frame.confidence * 100).toFixed(1) + '%' : '--';

            // Existing label badge
            const labelBadge = document.getElementById('frame-label-badge');
            const labelColors = {{ ad: '#ff4444', program: '#22c55e', transition: '#f59e0b' }};
            const labelNames = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
            if (frame.label) {{
                labelBadge.innerHTML = `<span style="color:${{labelColors[frame.label]}};font-size:12px;font-weight:700">&#10003; ${{labelNames[frame.label]}}</span>`;
            }} else {{
                labelBadge.innerHTML = '';
            }}

            document.getElementById('frame-meta').style.display = 'flex';

            // Load image
            loadFrameImage(frame.id);
            loadMoreIfNeeded();
        }}

        async function loadFrameImage(frameId) {{
            const imgEl = document.getElementById('frame-img');
            const loadingEl = document.getElementById('frame-loading');

            imgEl.style.display = 'none';
            loadingEl.style.display = 'block';
            loadingEl.textContent = 'Loading...';

            if (imgCache[frameId]) {{
                imgEl.src = imgCache[frameId];
                imgEl.style.display = 'block';
                loadingEl.style.display = 'none';
                return;
            }}

            try {{
                const res = await fetch(BASE + '/history/frames/' + frameId + '.jpg', {{
                    headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }},
                }});
                if (res.ok) {{
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    imgCache[frameId] = url;
                    // Only show if still on this frame
                    if (queue[queueIdx] && queue[queueIdx].id === frameId) {{
                        imgEl.src = url;
                        imgEl.style.display = 'block';
                        loadingEl.style.display = 'none';
                    }}
                }} else {{
                    loadingEl.textContent = 'Image unavailable';
                }}
            }} catch (e) {{
                loadingEl.textContent = 'Failed to load image';
            }}

            // Preload next frame image
            if (queue[queueIdx + 1]) {{
                prefetchImage(queue[queueIdx + 1].id);
            }}
        }}

        function prefetchImage(frameId) {{
            if (imgCache[frameId]) return;
            fetch(BASE + '/history/frames/' + frameId + '.jpg', {{
                headers: {{ 'X-API-Key': API_KEY, 'X-Device-Id': DEVICE_ID }},
            }}).then(r => r.ok ? r.blob() : null).then(blob => {{
                if (blob) imgCache[frameId] = URL.createObjectURL(blob);
            }}).catch(() => {{}});
        }}

        // ── Labeling ──────────────────────────────────────────────
        async function doLabel(label) {{
            if (busy || queue.length === 0) return;
            busy = true;
            setButtonsDisabled(true);

            const frame = queue[queueIdx];
            const fb = document.getElementById('label-fb');
            fb.textContent = 'Saving...';
            fb.style.color = '#FFD33D';

            try {{
                const res = await fetch(BASE + '/history/frames/' + frame.id + '/label', {{
                    method: 'POST',
                    headers: hdrs(),
                    body: JSON.stringify({{ label, device_id: DEVICE_ID }}),
                }});
                const data = await res.json();

                if (data.ok) {{
                    const labelNames = {{ ad: 'REKLAMA', program: 'PROGRAM', transition: 'PRECHOD' }};
                    const override = data.is_override ? ' &#9888; AI Override' : '';
                    fb.innerHTML = '&#10003; ' + labelNames[label] + override;
                    fb.style.color = '#22c55e';

                    // Mark in queue
                    queue[queueIdx].label = label;

                    // Add to recent
                    addRecent(label, data.is_override, frame.captured_at || frame.created_at);

                    // Decrement unlabeled count
                    if (currentFilter === 'unlabeled') queueTotal = Math.max(0, queueTotal - 1);

                    setTimeout(() => {{
                        fb.textContent = '';
                        advance();
                    }}, 400);

                    refreshStats();
                }} else {{
                    fb.textContent = '&#10007; Error: ' + (data.detail || 'Unknown');
                    fb.style.color = '#ff4444';
                    setTimeout(() => {{ fb.textContent = ''; }}, 2000);
                }}
            }} catch (e) {{
                fb.textContent = '&#10007; Network error';
                fb.style.color = '#ff4444';
                setTimeout(() => {{ fb.textContent = ''; }}, 2000);
            }}

            setButtonsDisabled(false);
            busy = false;
        }}

        function doSkip() {{
            if (busy) return;
            advance();
        }}

        function advance() {{
            if (currentFilter === 'unlabeled') {{
                // Remove labeled frame from queue and stay at same index
                queue.splice(queueIdx, 1);
                if (queueIdx >= queue.length && queueIdx > 0) queueIdx = queue.length - 1;
            }} else {{
                queueIdx = Math.min(queueIdx + 1, queue.length - 1);
            }}
            renderFrame();
        }}

        function goBack() {{
            if (queueIdx > 0) {{
                queueIdx--;
                renderFrame();
            }}
        }}

        function setButtonsDisabled(val) {{
            ['btn-ad', 'btn-program', 'btn-transition'].forEach(id => {{
                document.getElementById(id).disabled = val;
            }});
        }}

        // ── Filter ────────────────────────────────────────────────
        function setFilter(f) {{
            currentFilter = f;
            ['unlabeled', 'all', 'ad', 'program', 'transition'].forEach(name => {{
                document.getElementById('tab-' + name).classList.toggle('active', name === f);
            }});
            imgCache = {{}};
            if (currentMode === 'queue') loadQueue(true);
            else loadGrid(true);
        }}

        // ── Recent labels ─────────────────────────────────────────
        function addRecent(label, isOverride, ts) {{
            recentLabels.unshift({{ label, isOverride, ts }});
            if (recentLabels.length > 20) recentLabels.pop();
            renderRecent();
        }}

        function renderRecent() {{
            const el = document.getElementById('recent-labels');
            if (recentLabels.length === 0) {{
                el.innerHTML = '<div style="color:#555;font-size:12px">No labels yet.</div>';
                return;
            }}
            const labelNames = {{ ad: 'Reklama', program: 'Program', transition: 'Prechod' }};
            el.innerHTML = recentLabels.map(r => `
                <div class="recent-item">
                    <span class="dot dot-${{r.label}}"></span>
                    <span>${{labelNames[r.label]}}</span>
                    ${{r.isOverride ? '<span class="override-tag">&#9888;</span>' : ''}}
                    <span class="recent-time">${{timeSince(r.ts)}}</span>
                </div>
            `).join('');
        }}

        // ── Stats ─────────────────────────────────────────────────
        async function refreshStats() {{
            try {{
                const res = await fetch(BASE + '/history/stats?device_id=' + DEVICE_ID, {{ headers: hdrs() }});
                const d = await res.json();

                const ad = d.by_label.ad || 0;
                const prog = d.by_label.program || 0;
                const trans = d.by_label.transition || 0;
                const tAd = d.target_ad || 500;
                const tProg = d.target_program || 500;

                document.getElementById('stat-ad-txt').textContent = ad + ' / ' + tAd;
                document.getElementById('stat-prog-txt').textContent = prog + ' / ' + tProg;
                document.getElementById('stat-trans-txt').textContent = trans + ' (bonus)';
                document.getElementById('stat-trans-txt').style.color = trans > 0 ? '#f59e0b' : '#555';

                document.getElementById('prog-ad').style.width = Math.min(ad / tAd * 100, 100) + '%';
                document.getElementById('prog-prog').style.width = Math.min(prog / tProg * 100, 100) + '%';
                document.getElementById('prog-trans').style.width = Math.min(trans / 100 * 100, 100) + '%';

                document.getElementById('stat-total').textContent = d.total_history || 0;
                document.getElementById('stat-unlabeled').textContent = d.unlabeled || 0;
                document.getElementById('stat-overrides').textContent = d.overrides || 0;

                const pct = d.progress_pct || 0;
                document.getElementById('prog-bar').style.width = pct + '%';
                document.getElementById('prog-text').textContent = pct + '%';

                const exportBtn = document.getElementById('export-btn');
                const trainingMsg = document.getElementById('training-msg');
                if (d.training_ready) {{
                    trainingMsg.innerHTML = '<span style="color:#22c55e">&#10003; Dataset ready! ' + d.total_labeled + ' labels collected.</span>';
                    exportBtn.className = 'export-btn export-ready';
                }} else {{
                    const needAd = Math.max(0, tAd - ad);
                    const needProg = Math.max(0, tProg - prog);
                    let parts = [];
                    if (needAd > 0) parts.push(needAd + ' Reklama');
                    if (needProg > 0) parts.push(needProg + ' Program');
                    trainingMsg.textContent = 'Still need: ' + parts.join(', ') + '.';
                    exportBtn.className = 'export-btn';
                }}
            }} catch (e) {{ }}
        }}

        // ── Export ────────────────────────────────────────────────
        function downloadDataset(e) {{
            e.preventDefault();
            const url = BASE + '/history/export.zip?device_id=' + DEVICE_ID;
            const a = document.createElement('a');
            a.href = url;
            a.setAttribute('download', '');
            // Add API key as header not possible via <a>, use fetch + blob
            fetch(url, {{ headers: hdrs() }})
                .then(r => r.blob())
                .then(blob => {{
                    const burl = URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = burl;
                    link.download = 'dataset_' + DEVICE_ID + '.zip';
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(burl), 5000);
                }});
        }}

        // ── Keyboard ──────────────────────────────────────────────
        document.addEventListener('keydown', e => {{
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (currentMode === 'queue') {{
                if (e.key === '1') doLabel('ad');
                else if (e.key === '2') doLabel('program');
                else if (e.key === '3') doLabel('transition');
                else if (e.key === ' ') {{ e.preventDefault(); doSkip(); }}
                else if (e.key === 'ArrowRight') advance();
                else if (e.key === 'ArrowLeft') goBack();
            }} else {{
                // Grid mode
                if ((e.ctrlKey || e.metaKey) && e.key === 'a') {{ e.preventDefault(); selectAll(); }}
                else if (e.key === 'Escape') deselectAll();
                else if (e.key === '1' && selectedIds.size > 0) bulkLabel('ad');
                else if (e.key === '2' && selectedIds.size > 0) bulkLabel('program');
                else if (e.key === '3' && selectedIds.size > 0) bulkLabel('transition');
            }}
        }});

        // ── Helpers ───────────────────────────────────────────────
        function timeSince(isoStr) {{
            if (!isoStr) return '--';
            const d = new Date(isoStr);
            const s = Math.floor((new Date() - d) / 1000);
            if (s < 5) return 'just now';
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s / 60) + 'm ago';
            if (s < 86400) return Math.floor(s / 3600) + 'h ago';
            return new Date(isoStr).toLocaleDateString();
        }}

        // ── Start ─────────────────────────────────────────────────
        init();
        setInterval(refreshStats, 15000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)
