from __future__ import annotations

import csv
import io
import logging
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select, desc, func

from app.db.engine import get_session
from app.models import FrameHistoryDB, LabeledFrameDB, utcnow
from app.settings import settings
from app.ui import NAV_CSS, UNAUTH_HTML, NAV_STATUS_JS, nav_bar

logger = logging.getLogger(__name__)

router = APIRouter(tags=["review"])


# ── API Endpoints ────────────────────────────────────────────────────


class LabelIn(BaseModel):
    label: str  # ad | program | transition
    device_id: str = "tv-1"


@router.get("/history/channels")
def list_channels(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """List distinct channels seen for this device."""
    rows = db.exec(
        select(FrameHistoryDB.channel)
        .where(FrameHistoryDB.device_id == device_id)
        .where(FrameHistoryDB.channel != None)
        .distinct()
    ).all()
    channels = sorted([r for r in rows if r])
    return {"channels": channels}


@router.get("/history/frames")
def list_frames(
    device_id: str = Query(default="tv-1"),
    channel: Optional[str] = Query(default=None),
    filter: str = Query(default="unlabeled", description="unlabeled | all | ad | program | transition"),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
):
    """Paginated list of historical frames, optionally filtered by channel."""
    stmt = select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)

    if channel:
        stmt = stmt.where(FrameHistoryDB.channel == channel)

    if filter == "unlabeled":
        stmt = stmt.where(FrameHistoryDB.label == None)
    elif filter == "wrong":
        stmt = stmt.where(FrameHistoryDB.is_override == True)
    elif filter in ("ad", "program", "transition"):
        stmt = stmt.where(FrameHistoryDB.label == filter)

    total = db.exec(select(func.count()).select_from(stmt.subquery())).one()
    frames = db.exec(stmt.order_by(desc(FrameHistoryDB.id)).offset(offset).limit(limit)).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": f.id,
                "channel": f.channel,
                "is_ad": f.is_ad,
                "confidence": f.confidence,
                "label": f.label,
                "is_override": f.is_override,
                "captured_at": f.captured_at.isoformat() if f.captured_at else None,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "p_program": f.p_program,
                "detect_time_ms": f.detect_time_ms,
                "top_ad_prompt": f.top_ad_prompt,
                "top_nonad_prompt": f.top_nonad_prompt,
            }
            for f in frames
        ],
    }


@router.post("/history/frames/{frame_id}/label")
def label_frame(
    frame_id: int,
    body: LabelIn,
    db: Session = Depends(get_session),
):
    """Label a frame. Copies labeled frame to labeled_frames table."""
    if body.label not in ("ad", "program", "transition"):
        raise HTTPException(status_code=400, detail="Label must be: ad, program, transition")

    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")

    now = utcnow()
    is_override = (body.label == "ad" and not frame.is_ad) or \
                  (body.label in ("program", "transition") and frame.is_ad)

    frame.label = body.label
    frame.labeled_at = now
    frame.is_override = is_override
    db.add(frame)

    # Copy to labeled_frames (reuse same R2 key)
    labeled = LabeledFrameDB(
        device_id=frame.device_id,
        channel=frame.channel,
        label=body.label,
        image_key=frame.image_key,
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
    """Label multiple frames at once."""
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
            channel=frame.channel,
            label=body.label,
            image_key=frame.image_key,
            ai_was_ad=frame.is_ad,
            ai_confidence=frame.confidence,
            is_override=is_override,
            created_at=frame.captured_at or now,
        ))

    db.commit()
    return {"ok": True, "labeled": len(frames), "label": body.label}


@router.delete("/history/frames/{frame_id}")
def delete_frame(frame_id: int, db: Session = Depends(get_session)):
    """Delete a single frame from DB and R2."""
    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    from app.storage.r2 import delete_frame as r2_delete
    try:
        r2_delete(frame.image_key)
    except Exception:
        pass
    db.delete(frame)
    db.commit()
    return {"ok": True, "deleted_id": frame_id}


@router.delete("/history/frames")
def delete_all_frames(
    device_id: str = Query(default="tv-1"),
    channel: Optional[str] = Query(default=None, description="Delete only this channel, or all if omitted"),
    labeled_only: bool = Query(default=False, description="Delete only labeled frames"),
    db: Session = Depends(get_session),
):
    """Delete frames from DB and R2. Use channel= to limit scope."""
    stmt = select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)
    if channel:
        stmt = stmt.where(FrameHistoryDB.channel == channel)
    if labeled_only:
        stmt = stmt.where(FrameHistoryDB.label != None)

    frames = db.exec(stmt).all()
    keys = [f.image_key for f in frames if f.image_key]

    from app.storage.r2 import delete_frames_batch
    try:
        delete_frames_batch(keys)
    except Exception as e:
        logger.warning(f"R2 batch delete error: {e}")

    for frame in frames:
        db.delete(frame)
    db.commit()

    return {"ok": True, "deleted": len(frames), "device_id": device_id, "channel": channel}


@router.get("/history/stats")
def get_history_stats(
    device_id: str = Query(default="tv-1"),
    channel: Optional[str] = Query(default=None),
    db: Session = Depends(get_session),
):
    """Stats for training readiness, optionally scoped to a channel."""
    stmt = select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)
    if channel:
        stmt = stmt.where(FrameHistoryDB.channel == channel)

    all_history = db.exec(stmt).all()
    total = len(all_history)
    pending = sum(1 for f in all_history if f.label is None)
    labeled = total - pending

    by_label: dict = {"ad": 0, "program": 0, "transition": 0}
    overrides = 0
    for f in all_history:
        if f.label:
            by_label[f.label] = by_label.get(f.label, 0) + 1
        if f.is_override:
            overrides += 1

    target_ad = 500
    target_program = 500
    ad_count = by_label.get("ad", 0)
    program_count = by_label.get("program", 0)
    progress_pct = round(
        (min(ad_count, target_ad) + min(program_count, target_program))
        / (target_ad + target_program) * 100, 1
    )

    return {
        "total": total,
        "pending": pending,
        "labeled": labeled,
        "by_label": by_label,
        "overrides": overrides,
        "target_ad": target_ad,
        "target_program": target_program,
        "progress_pct": progress_pct,
        "training_ready": ad_count >= target_ad and program_count >= target_program,
    }


@router.get("/history/channel-stats")
def get_channel_stats(
    device_id: str = Query(default="tv-1"),
    db: Session = Depends(get_session),
):
    """Per-channel breakdown of frame counts."""
    all_frames = db.exec(
        select(FrameHistoryDB).where(FrameHistoryDB.device_id == device_id)
    ).all()

    channels: dict = {}
    for f in all_frames:
        ch = f.channel or "(unknown)"
        if ch not in channels:
            channels[ch] = {"total": 0, "pending": 0, "ad": 0, "program": 0, "transition": 0}
        channels[ch]["total"] += 1
        if f.label is None:
            channels[ch]["pending"] += 1
        elif f.label in channels[ch]:
            channels[ch][f.label] += 1

    return {"channels": channels}


@router.get("/history/export.zip")
def export_training_dataset(
    device_id: str = Query(default="tv-1"),
    channel: Optional[str] = Query(default=None),
    db: Session = Depends(get_session),
):
    """Download labeled frames as ZIP for ML training."""
    from app.storage.r2 import download_frame

    stmt = (
        select(FrameHistoryDB)
        .where(FrameHistoryDB.device_id == device_id)
        .where(FrameHistoryDB.label != None)
    )
    if channel:
        stmt = stmt.where(FrameHistoryDB.channel == channel)
    labeled_frames = db.exec(stmt.order_by(FrameHistoryDB.id)).all()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_rows = [["filename", "label", "channel", "ai_was_ad", "ai_confidence", "captured_at"]]

        for f in labeled_frames:
            try:
                img_bytes = download_frame(f.image_key)
            except Exception:
                continue
            fname = f"images/{f.id:06d}_{f.label}.jpg"
            zf.writestr(fname, img_bytes)
            csv_rows.append([
                fname,
                f.label,
                f.channel or "",
                str(f.is_ad),
                f"{f.confidence:.4f}" if f.confidence is not None else "",
                f.captured_at.isoformat() if f.captured_at else "",
            ])

        buf = io.StringIO()
        csv.writer(buf).writerows(csv_rows)
        zf.writestr("labels.csv", buf.getvalue())

    zip_buffer.seek(0)
    suffix = f"_{channel}" if channel else ""
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="dataset_{device_id}{suffix}_{len(labeled_frames)}frames.zip"'
        },
    )


# ── Public image proxy (api_key in query param for <img src> use) ────

def serve_frame_image(
    frame_id: int,
    api_key: str = Query(...),
    db: Session = Depends(get_session),
):
    if api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    frame = db.get(FrameHistoryDB, frame_id)
    if not frame:
        raise HTTPException(status_code=404, detail="Frame not found")
    from app.storage.r2 import download_frame
    try:
        data = download_frame(frame.image_key)
    except Exception:
        raise HTTPException(status_code=404, detail="Image not found in storage")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "max-age=86400"},
    )


# ── HTML Dashboards ─────────────────────────────────────────────────

def _auth_check(api_key: str) -> bool:
    return api_key == settings.api_key


def _unauth_html() -> HTMLResponse:
    return HTMLResponse(content=UNAUTH_HTML, status_code=401)


def review_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    if not _auth_check(api_key):
        return _unauth_html()

    _nav = nav_bar(api_key, device_id, "review")
    html = f"""<!DOCTYPE html>
<html lang="sk">
<head>
<title>Review — {device_id}</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
{NAV_CSS}
/* Channels */
.channels{{background:#12122a;border-bottom:1px solid #1e1e3a;padding:0 20px;display:flex;gap:4px;overflow-x:auto}}
.ch-tab{{padding:10px 16px;font-size:13px;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:color .15s}}
.ch-tab:hover{{color:#94a3b8}}
.ch-tab.active{{color:#a78bfa;border-bottom-color:#a78bfa}}
/* Toolbar */
.toolbar{{padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;background:#0f0f1a;border-bottom:1px solid #1e1e3a}}
.stats{{display:flex;gap:16px;font-size:13px;flex-wrap:wrap}}
.stat{{display:flex;flex-direction:column;align-items:center}}
.stat-val{{font-size:20px;font-weight:700;line-height:1}}
.stat-lbl{{font-size:10px;color:#64748b;margin-top:2px;text-transform:uppercase;letter-spacing:.5px}}
.stat-val.pending{{color:#fbbf24}}
.stat-val.ad{{color:#f87171}}
.stat-val.program{{color:#4ade80}}
.stat-val.transition{{color:#60a5fa}}
.filters{{display:flex;gap:4px;flex-wrap:wrap}}
.filter-btn{{padding:5px 12px;border-radius:20px;border:1px solid #2d2d4e;background:transparent;color:#64748b;font-size:12px;cursor:pointer;transition:all .15s}}
.filter-btn:hover{{border-color:#4338ca;color:#a5b4fc}}
.filter-btn.active{{background:#4338ca;border-color:#4338ca;color:#fff}}
/* Grid */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;padding:16px 20px}}
.frame-card{{background:#1a1a2e;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid transparent;transition:all .15s;position:relative}}
.frame-card:hover{{border-color:#4338ca;transform:translateY(-1px)}}
.frame-card img{{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#0f0f1a}}
.frame-meta{{padding:7px 9px}}
.frame-time{{font-size:11px;color:#64748b;font-variant-numeric:tabular-nums}}
.frame-conf{{font-size:11px;color:#475569;margin-top:1px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}}
.badge-pending{{background:#451a03;color:#fbbf24}}
.badge-ad{{background:#450a0a;color:#f87171}}
.badge-program{{background:#052e16;color:#4ade80}}
.badge-transition{{background:#0c1a4e;color:#60a5fa}}
/* Pagination */
.pagination{{display:flex;align-items:center;justify-content:center;gap:12px;padding:20px;color:#64748b;font-size:13px}}
.page-btn{{background:#1a1a2e;border:1px solid #2d2d4e;color:#94a3b8;padding:6px 16px;border-radius:6px;cursor:pointer;transition:all .15s}}
.page-btn:hover:not(:disabled){{border-color:#4338ca;color:#a5b4fc}}
.page-btn:disabled{{opacity:.3;cursor:default}}
/* Empty state */
.empty{{text-align:center;padding:60px 20px;color:#475569}}
.empty h3{{font-size:16px;margin-bottom:8px;color:#64748b}}
/* Modal */
.modal{{display:none;position:fixed;inset:0;z-index:1000;align-items:center;justify-content:center}}
.modal.open{{display:flex}}
.modal-bg{{position:absolute;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(4px)}}
.modal-box{{position:relative;background:#1a1a2e;border:1px solid #2d2d4e;border-radius:12px;max-width:640px;width:calc(100% - 32px);max-height:90vh;overflow-y:auto;z-index:1}}
.modal-img{{width:100%;display:block;border-radius:10px 10px 0 0}}
.modal-body{{padding:16px}}
.modal-info{{font-size:13px;color:#94a3b8;line-height:1.8;margin-bottom:14px}}
.modal-info strong{{color:#e2e8f0}}
.modal-actions{{display:flex;gap:8px;flex-wrap:wrap}}
.modal-actions button{{flex:1;min-width:100px;padding:10px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;transition:all .15s}}
.btn-label-ad{{background:#7f1d1d;color:#fca5a5}}
.btn-label-ad:hover{{background:#991b1b}}
.btn-label-program{{background:#14532d;color:#86efac}}
.btn-label-program:hover{{background:#166534}}
.btn-label-transition{{background:#1e3a5f;color:#93c5fd}}
.btn-label-transition:hover{{background:#1e40af}}
.btn-close{{background:#1e1e3a;color:#64748b}}
.btn-close:hover{{background:#2d2d4e}}
.modal-close-x{{position:absolute;top:10px;right:12px;background:rgba(0,0,0,.5);border:none;color:#94a3b8;font-size:20px;cursor:pointer;border-radius:50%;width:30px;height:30px;display:flex;align-items:center;justify-content:center}}
/* Loading */
.loading{{text-align:center;padding:40px;color:#475569;font-size:14px}}
/* Bulk select */
.frame-card.selected{{border-color:#a78bfa;box-shadow:0 0 0 2px #a78bfa44}}
.select-check{{position:absolute;top:6px;left:6px;width:20px;height:20px;border-radius:50%;border:2px solid #4338ca;background:#0f0f1a;display:none;align-items:center;justify-content:center;color:#a78bfa;font-size:13px;font-weight:700}}
.select-mode .select-check{{display:flex}}
.select-mode .frame-card{{cursor:pointer}}
.frame-card.selected .select-check{{background:#4338ca;border-color:#a78bfa;color:#fff}}
.btn-select{{background:#312e81;color:#a5b4fc;border:1px solid #4338ca;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer}}
.btn-select.active{{background:#4338ca;color:#fff}}
.btn-select-all{{background:transparent;color:#64748b;border:1px solid #2d2d4e;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;display:none}}
.btn-select-all:hover{{border-color:#4338ca;color:#a5b4fc}}
.select-mode .btn-select-all{{display:inline-block}}
.bulk-bar{{position:sticky;bottom:0;background:#1a1a2e;border-top:2px solid #4338ca;padding:12px 20px;display:none;align-items:center;gap:10px;z-index:100;flex-wrap:wrap}}
.bulk-bar.visible{{display:flex}}
.bulk-count{{font-size:14px;font-weight:600;color:#a78bfa;flex:1;min-width:80px}}
.btn-bulk{{border:none;padding:9px 22px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
.btn-bulk-ad{{background:#7f1d1d;color:#fca5a5}}
.btn-bulk-ad:hover{{background:#991b1b}}
.btn-bulk-program{{background:#14532d;color:#86efac}}
.btn-bulk-program:hover{{background:#166534}}
.btn-bulk-transition{{background:#1e3a5f;color:#93c5fd}}
.btn-bulk-transition:hover{{background:#1e40af}}
.btn-bulk-cancel{{background:#2d2d4e;color:#94a3b8;border:none;padding:9px 16px;border-radius:8px;font-size:13px;cursor:pointer}}
.btn-bulk-cancel:hover{{background:#3d3d5e}}
</style>
</head>
<body>
{_nav}

<div class="channels" id="channelsBar">
  <div class="ch-tab active" data-ch="">Vsetky</div>
</div>

<div class="toolbar">
  <div class="stats" id="statsBar">
    <div class="stat"><span class="stat-val" id="sTot">—</span><span class="stat-lbl">Celkom</span></div>
    <div class="stat"><span class="stat-val pending" id="sPend">—</span><span class="stat-lbl">Pending</span></div>
    <div class="stat"><span class="stat-val ad" id="sAd">—</span><span class="stat-lbl">Ad</span></div>
    <div class="stat"><span class="stat-val program" id="sProg">—</span><span class="stat-lbl">Program</span></div>
    <div class="stat"><span class="stat-val transition" id="sTrans">—</span><span class="stat-lbl">Transition</span></div>
  </div>
  <div class="filters">
    <button class="filter-btn active" data-filter="unlabeled">Pending</button>
    <button class="filter-btn" data-filter="all">Vsetky</button>
    <button class="filter-btn" data-filter="ad">Ad</button>
    <button class="filter-btn" data-filter="program">Program</button>
    <button class="filter-btn" data-filter="transition">Transition</button>
    <button class="filter-btn" data-filter="wrong" style="border-color:#f87171;color:#f87171">AI chyby</button>
    <button class="btn-select" id="btnSelectMode" onclick="toggleSelectMode()">Vyber viac</button>
    <button class="btn-select-all" id="btnSelectAll" onclick="selectAll()">Vybrat vsetky</button>
  </div>
</div>

<div id="grid" class="grid"></div>
<div class="pagination" id="pagination"></div>

<!-- Bulk action bar -->
<div class="bulk-bar" id="bulkBar">
  <span class="bulk-count" id="bulkCount">0 vybratych</span>
  <button class="btn-bulk btn-bulk-ad" onclick="doBulkLabel('ad')">AD</button>
  <button class="btn-bulk btn-bulk-program" onclick="doBulkLabel('program')">Program</button>
  <button class="btn-bulk btn-bulk-transition" onclick="doBulkLabel('transition')">Transition</button>
  <button class="btn-bulk-cancel" onclick="toggleSelectMode()">Zrusit</button>
</div>

<!-- Modal -->
<div class="modal" id="modal">
  <div class="modal-bg" onclick="closeModal()"></div>
  <div class="modal-box">
    <button class="modal-close-x" onclick="closeModal()">×</button>
    <img class="modal-img" id="modalImg" src="" alt="">
    <div class="modal-body">
      <div class="modal-info" id="modalInfo"></div>
      <div class="modal-actions">
        <button class="btn-label-ad" onclick="doLabel('ad')">AD</button>
        <button class="btn-label-program" onclick="doLabel('program')">Program</button>
        <button class="btn-label-transition" onclick="doLabel('transition')">Transition</button>
        <button class="btn-close" onclick="closeModal()">Zatvorit</button>
      </div>
    </div>
  </div>
</div>

<script>
const API_KEY = new URLSearchParams(location.search).get('api_key') || '';
const DEVICE_ID = '{device_id}';
const PAGE_SIZE = 24;

let channel = '';
let filter = 'unlabeled';
let offset = 0;
let total = 0;
let selectedId = null;
let selectMode = false;
let selectedIds = new Set();

const hdr = () => ({{headers:{{'X-API-Key': API_KEY}}}});

async function api(path) {{
  const r = await fetch('/v1' + path, hdr());
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}}

async function loadChannels() {{
  try {{
    const d = await api(`/history/channels?device_id=${{DEVICE_ID}}`);
    const bar = document.getElementById('channelsBar');
    bar.innerHTML = '<div class="ch-tab active" data-ch="">Vsetky</div>';
    d.channels.forEach(ch => {{
      const t = document.createElement('div');
      t.className = 'ch-tab';
      t.dataset.ch = ch;
      t.textContent = ch;
      bar.appendChild(t);
    }});
    bar.querySelectorAll('.ch-tab').forEach(t => t.addEventListener('click', () => {{
      bar.querySelectorAll('.ch-tab').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      channel = t.dataset.ch;
      offset = 0;
      loadStats();
      loadFrames();
    }}));
  }} catch(e) {{ console.error('channels', e); }}
}}

async function loadStats() {{
  try {{
    const q = channel ? `&channel=${{encodeURIComponent(channel)}}` : '';
    const d = await api(`/history/stats?device_id=${{DEVICE_ID}}${{q}}`);
    document.getElementById('sTot').textContent = d.total;
    document.getElementById('sPend').textContent = d.pending;
    document.getElementById('sAd').textContent = d.by_label.ad;
    document.getElementById('sProg').textContent = d.by_label.program;
    document.getElementById('sTrans').textContent = d.by_label.transition;
  }} catch(e) {{ console.error('stats', e); }}
}}

async function loadFrames() {{
  const grid = document.getElementById('grid');
  grid.className = 'grid' + (selectMode ? ' select-mode' : '');
  grid.innerHTML = '<div class="loading">Nacitavam...</div>';

  try {{
    let q = `?device_id=${{DEVICE_ID}}&filter=${{filter}}&limit=${{PAGE_SIZE}}&offset=${{offset}}`;
    if (channel) q += `&channel=${{encodeURIComponent(channel)}}`;
    const d = await api('/history/frames' + q);
    total = d.total;

    if (!d.items.length) {{
      grid.innerHTML = '<div class="empty"><h3>Ziadne snimky</h3><p>Zmen filter alebo kanal</p></div>';
      document.getElementById('pagination').innerHTML = '';
      return;
    }}

    grid.innerHTML = d.items.map(f => {{
      const time = f.captured_at
        ? new Date(f.captured_at).toLocaleString('sk-SK', {{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}})
        : '—';
      const conf = f.confidence != null ? (f.confidence * 100).toFixed(0) + '%' : '';
      const badge = f.label
        ? `<span class="badge badge-${{f.label}}">${{f.label}}</span>`
        : `<span class="badge badge-pending">PENDING</span>`;
      const isSel = selectedIds.has(f.id);
      const overrideBadge = f.is_override ? `<span class="badge" style="background:#4a1d1d;color:#f87171;margin-left:4px">AI ✗</span>` : '';
      return `<div class="frame-card${{isSel ? ' selected' : ''}}" id="card-${{f.id}}" onclick="handleCardClick(${{f.id}}, ${{JSON.stringify(f)}})">
        <div class="select-check">${{isSel ? '✓' : ''}}</div>
        <img src="/frames/${{f.id}}.jpg?api_key=${{encodeURIComponent(API_KEY)}}" loading="lazy" onerror="this.style.background='#1e1e3a'">
        <div class="frame-meta">
          <div class="frame-time">${{time}}</div>
          <div class="frame-conf">${{f.channel ? f.channel + ' · ' : ''}}${{conf}}</div>
          ${{badge}}${{overrideBadge}}
        </div>
      </div>`;
    }}).join('');

    renderPagination(total);
  }} catch(e) {{
    grid.innerHTML = `<div class="empty"><h3>Chyba</h3><p>${{e.message}}</p></div>`;
  }}
}}

function renderPagination(tot) {{
  const pages = Math.ceil(tot / PAGE_SIZE);
  const cur = Math.floor(offset / PAGE_SIZE) + 1;
  const el = document.getElementById('pagination');
  if (pages <= 1) {{ el.innerHTML = ''; return; }}
  el.innerHTML = `
    <button class="page-btn" onclick="goPage(${{cur-2}})" ${{cur===1?'disabled':''}}>← Pred</button>
    <span>Strana ${{cur}} / ${{pages}} (${{tot}} snimkov)</span>
    <button class="page-btn" onclick="goPage(${{cur}})" ${{cur===pages?'disabled':''}}>Dalsi →</button>`;
}}

function goPage(idx) {{
  offset = idx * PAGE_SIZE;
  loadFrames();
  window.scrollTo(0, 0);
}}

function openModal(f) {{
  selectedId = f.id;
  document.getElementById('modalImg').src = `/frames/${{f.id}}.jpg?api_key=${{encodeURIComponent(API_KEY)}}`;
  const time = f.captured_at
    ? new Date(f.captured_at).toLocaleString('sk-SK', {{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}})
    : '—';
  const confAd = f.confidence != null ? (f.confidence * 100).toFixed(1) + '%' : '—';
  const confProg = f.p_program != null ? (f.p_program * 100).toFixed(1) + '%' : '—';
  const detectMs = f.detect_time_ms != null ? f.detect_time_ms + ' ms' : '—';
  const override = f.is_override ? ' <span style="color:#f87171">(AI chybovalo)</span>' : '';
  const topAd = f.top_ad_prompt || '—';
  const topProg = f.top_nonad_prompt || '—';
  document.getElementById('modalInfo').innerHTML =
    `<strong>Cas:</strong> ${{time}}<br>
     <strong>Kanal:</strong> ${{f.channel || '—'}}<br>
     <strong>Status:</strong> ${{f.label || 'PENDING'}}${{override}}<br>
     <strong>AI p_ad:</strong> ${{confAd}} &nbsp; <strong>p_program:</strong> ${{confProg}}<br>
     <strong>Inference:</strong> ${{detectMs}}<br>
     <strong>Top ad prompt:</strong> <span style="color:#fca5a5;font-size:11px">${{topAd}}</span><br>
     <strong>Top prog prompt:</strong> <span style="color:#86efac;font-size:11px">${{topProg}}</span>`;
  document.getElementById('modal').classList.add('open');
}}

function closeModal() {{
  document.getElementById('modal').classList.remove('open');
  selectedId = null;
}}

async function doLabel(label) {{
  if (!selectedId) return;
  try {{
    const r = await fetch(`/v1/history/frames/${{selectedId}}/label`, {{
      method: 'POST',
      headers: {{'X-API-Key': API_KEY, 'Content-Type': 'application/json'}},
      body: JSON.stringify({{label, device_id: DEVICE_ID}}),
    }});
    if (!r.ok) throw new Error(await r.text());
    closeModal();
    loadStats();
    loadFrames();
  }} catch(e) {{ alert('Chyba: ' + e.message); }}
}}

function handleCardClick(id, frame) {{
  if (selectMode) {{
    toggleSelect(id);
  }} else {{
    openModal(frame);
  }}
}}

function toggleSelectMode() {{
  selectMode = !selectMode;
  selectedIds.clear();
  const btn = document.getElementById('btnSelectMode');
  const grid = document.getElementById('grid');
  btn.classList.toggle('active', selectMode);
  btn.textContent = selectMode ? 'Zrusit vyber' : 'Vyber viac';
  grid.classList.toggle('select-mode', selectMode);
  updateBulkBar();
  loadFrames();
}}

function toggleSelect(id) {{
  if (selectedIds.has(id)) {{
    selectedIds.delete(id);
  }} else {{
    selectedIds.add(id);
  }}
  const card = document.getElementById('card-' + id);
  if (card) {{
    card.classList.toggle('selected', selectedIds.has(id));
    const check = card.querySelector('.select-check');
    if (check) check.textContent = selectedIds.has(id) ? '✓' : '';
  }}
  updateBulkBar();
}}

function selectAll() {{
  document.querySelectorAll('.frame-card').forEach(card => {{
    const id = parseInt(card.id.replace('card-', ''));
    if (!isNaN(id)) {{
      selectedIds.add(id);
      card.classList.add('selected');
      const check = card.querySelector('.select-check');
      if (check) check.textContent = '✓';
    }}
  }});
  updateBulkBar();
}}

function updateBulkBar() {{
  const bar = document.getElementById('bulkBar');
  const count = document.getElementById('bulkCount');
  const n = selectedIds.size;
  if (selectMode && n > 0) {{
    bar.classList.add('visible');
    count.textContent = n + ' vybratych';
  }} else {{
    bar.classList.remove('visible');
  }}
}}

async function doBulkLabel(label) {{
  if (selectedIds.size === 0) return;
  const ids = Array.from(selectedIds);
  try {{
    const r = await fetch('/v1/history/frames/bulk-label', {{
      method: 'POST',
      headers: {{'X-API-Key': API_KEY, 'Content-Type': 'application/json'}},
      body: JSON.stringify({{ids, label, device_id: DEVICE_ID}}),
    }});
    if (!r.ok) throw new Error(await r.text());
    selectedIds.clear();
    updateBulkBar();
    loadStats();
    loadFrames();
  }} catch(e) {{ alert('Chyba: ' + e.message); }}
}}

// Filter buttons
document.querySelectorAll('.filter-btn').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.filter-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  filter = b.dataset.filter;
  offset = 0;
  loadFrames();
}}));

// Init
loadChannels();
loadStats();
loadFrames();
</script>
<script>{NAV_STATUS_JS}</script>
</body>
</html>"""
    return HTMLResponse(content=html)


def admin_dashboard(
    device_id: str = Query(default="tv-1"),
    api_key: str = Query(default=""),
):
    if not _auth_check(api_key):
        return _unauth_html()

    _nav = nav_bar(api_key, device_id, "admin")
    html = f"""<!DOCTYPE html>
<html lang="sk">
<head>
<title>Admin — {device_id}</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
{NAV_CSS}
.content{{max-width:800px;margin:0 auto;padding:24px 20px}}
.section{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:10px;padding:20px;margin-bottom:20px}}
.section h2{{font-size:14px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.8px;margin-bottom:16px}}
.channel-row{{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #1e1e3a}}
.channel-row:last-child{{border-bottom:none}}
.ch-name{{font-size:14px;font-weight:600;color:#e2e8f0}}
.ch-stats{{font-size:12px;color:#64748b;margin-top:2px}}
.ch-actions{{display:flex;gap:6px}}
.btn-danger{{background:#450a0a;color:#fca5a5;border:1px solid #7f1d1d;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;transition:all .15s}}
.btn-danger:hover{{background:#7f1d1d}}
.btn-export{{background:#0c1a4e;color:#93c5fd;border:1px solid #1e3a5f;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer;text-decoration:none;display:inline-block;transition:all .15s}}
.btn-export:hover{{background:#1e3a5f}}
.btn-big-danger{{width:100%;padding:12px;background:#450a0a;color:#fca5a5;border:1px solid #7f1d1d;border-radius:8px;font-size:14px;cursor:pointer;transition:all .15s;font-weight:600}}
.btn-big-danger:hover{{background:#7f1d1d}}
.progress-bar{{height:8px;background:#1e1e3a;border-radius:4px;overflow:hidden;margin-top:8px}}
.progress-fill{{height:100%;background:#4338ca;border-radius:4px;transition:width .3s}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}}
.stat-card{{background:#12122a;border-radius:8px;padding:14px;text-align:center}}
.stat-card .val{{font-size:22px;font-weight:700}}
.stat-card .lbl{{font-size:11px;color:#64748b;margin-top:4px;text-transform:uppercase;letter-spacing:.5px}}
.alert{{background:#451a03;border:1px solid #92400e;border-radius:8px;padding:12px 16px;font-size:13px;color:#fcd34d;margin-bottom:12px}}
</style>
</head>
<body>
{_nav}
<div class="content">

  <div class="section">
    <h2>Statistiky</h2>
    <div class="stat-grid" id="statGrid">Nacitavam...</div>
    <div style="margin-top:12px">
      <div style="font-size:12px;color:#64748b;margin-bottom:4px">Pokrok ku trenovaniu (ciel: 500 ad + 500 program)</div>
      <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
      <div style="font-size:12px;color:#94a3b8;margin-top:4px" id="progressTxt"></div>
    </div>
  </div>

  <div class="section">
    <h2>Akcie podla kanalu</h2>
    <div id="channelList">Nacitavam...</div>
  </div>

  <div class="section">
    <h2>Export datasetu</h2>
    <div id="exportLinks">Nacitavam...</div>
  </div>

  <div class="section">
    <h2>Nebezpecne akcie</h2>
    <div class="alert">Tieto akcie su nevratne! Vymazu data z DB aj z R2 storage.</div>
    <button class="btn-big-danger" onclick="deleteAll()">Vymazat VSETKY snimky (vsetky kanaly)</button>
  </div>

</div>

<script>
const API_KEY = new URLSearchParams(location.search).get('api_key') || '';
const DEVICE_ID = '{device_id}';
const hdr = () => ({{headers:{{'X-API-Key': API_KEY}}}});

async function api(path) {{
  const r = await fetch('/v1' + path, hdr());
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}}

async function load() {{
  // Stats
  const stats = await api(`/history/stats?device_id=${{DEVICE_ID}}`);
  document.getElementById('statGrid').innerHTML = `
    <div class="stat-card"><div class="val">${{stats.total}}</div><div class="lbl">Celkom</div></div>
    <div class="stat-card"><div class="val" style="color:#fbbf24">${{stats.pending}}</div><div class="lbl">Pending</div></div>
    <div class="stat-card"><div class="val" style="color:#f87171">${{stats.by_label.ad}}</div><div class="lbl">Ad</div></div>
    <div class="stat-card"><div class="val" style="color:#4ade80">${{stats.by_label.program}}</div><div class="lbl">Program</div></div>
    <div class="stat-card"><div class="val" style="color:#60a5fa">${{stats.by_label.transition}}</div><div class="lbl">Transition</div></div>
    <div class="stat-card"><div class="val">${{stats.overrides}}</div><div class="lbl">Overrides</div></div>`;
  document.getElementById('progressFill').style.width = stats.progress_pct + '%';
  document.getElementById('progressTxt').textContent =
    `${{stats.progress_pct}}% — Ad: ${{stats.by_label.ad}}/${{stats.target_ad}}, Program: ${{stats.by_label.program}}/${{stats.target_program}}` +
    (stats.training_ready ? ' — READY!' : '');

  // Channel breakdown
  const chStats = await api(`/history/channel-stats?device_id=${{DEVICE_ID}}`);
  const chs = Object.entries(chStats.channels);
  if (!chs.length) {{
    document.getElementById('channelList').textContent = 'Ziadne kanaly';
    document.getElementById('exportLinks').textContent = 'Ziadne kanaly';
    return;
  }}

  document.getElementById('channelList').innerHTML = chs.map(([ch, s]) => `
    <div class="channel-row">
      <div>
        <div class="ch-name">${{ch}}</div>
        <div class="ch-stats">Celkom: ${{s.total}} | Pending: ${{s.pending}} | Ad: ${{s.ad}} | Program: ${{s.program}}</div>
      </div>
      <div class="ch-actions">
        <button class="btn-danger" onclick="deleteChannel('${{ch}}')">Vymazat</button>
      </div>
    </div>`).join('');

  document.getElementById('exportLinks').innerHTML =
    `<a class="btn-export" href="/v1/history/export.zip?device_id=${{DEVICE_ID}}" onclick="addApiKey(this)">Stiahnut vsetky (ZIP)</a>` +
    '<br><br>' +
    chs.map(([ch]) =>
      `<a class="btn-export" style="margin:4px 4px 4px 0" href="/v1/history/export.zip?device_id=${{DEVICE_ID}}&channel=${{encodeURIComponent(ch)}}" onclick="addApiKey(this)">${{ch}} (ZIP)</a>`
    ).join('');
}}

function addApiKey(el) {{
  el.href = el.href + (el.href.includes('?') ? '&' : '?') + 'x_api_key=' + encodeURIComponent(API_KEY);
}}

// Fix: export needs API key header — use fetch + blob download
document.addEventListener('click', async e => {{
  const a = e.target.closest('a.btn-export');
  if (!a) return;
  e.preventDefault();
  const url = a.getAttribute('href').replace('x_api_key='+encodeURIComponent(API_KEY), '');
  const r = await fetch(url, hdr());
  if (!r.ok) {{ alert('Export zlyhal'); return; }}
  const blob = await r.blob();
  const dl = document.createElement('a');
  dl.href = URL.createObjectURL(blob);
  dl.download = r.headers.get('content-disposition')?.match(/filename="(.+)"/)?.[1] || 'dataset.zip';
  dl.click();
}});

async function deleteChannel(ch) {{
  if (!confirm(`Vymazat vsetky snimky pre kanal "${{ch}}"? Toto je nevratne.`)) return;
  const r = await fetch(`/v1/history/frames?device_id=${{DEVICE_ID}}&channel=${{encodeURIComponent(ch)}}`, {{
    method: 'DELETE', headers: {{'X-API-Key': API_KEY}}
  }});
  const d = await r.json();
  alert(`Vymazanych: ${{d.deleted}} snimkov`);
  load();
}}

async function deleteAll() {{
  if (!confirm('VYMAZAT VSETKY SNIMKY pre vsetky kanaly? Toto je nevratne!')) return;
  if (!confirm('Si si isty? Vsetky data budu zmazane.')) return;
  const r = await fetch(`/v1/history/frames?device_id=${{DEVICE_ID}}`, {{
    method: 'DELETE', headers: {{'X-API-Key': API_KEY}}
  }});
  const d = await r.json();
  alert(`Vymazanych: ${{d.deleted}} snimkov`);
  load();
}}

load();
</script>
<script>{NAV_STATUS_JS}</script>
</body>
</html>"""
    return HTMLResponse(content=html)
