"""
Server-side ad detection router.

Endpoints:
  POST /v1/detect          -- detect from base64 image in body
  GET  /v1/detect/latest   -- detect latest live frame from RPi
  GET  /v1/detect/model    -- model info (no inference)
  GET  /detect             -- HTML dashboard (api_key query param)
"""

from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.ml.detector import detect_bytes, model_info
from app.routers.device import _latest_images
from app.settings import settings
from app.ui import NAV_CSS, UNAUTH_HTML, NAV_STATUS_JS, nav_bar

router = APIRouter(tags=["detect"])

_history: dict[str, deque] = {}
_HISTORY_MAX = 20


def _get_history(device_id: str) -> deque:
    if device_id not in _history:
        _history[device_id] = deque(maxlen=_HISTORY_MAX)
    return _history[device_id]


class DetectIn(BaseModel):
    image_base64: str
    device_id: str = "tv-1"


@router.post("/detect")
def detect_image(body: DetectIn):
    """Run server-side CLIP detection on a base64 image."""
    try:
        image_bytes = base64.b64decode(body.image_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image")
    try:
        result = detect_bytes(image_bytes)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Detection failed: {e}")
    entry = {**result, "captured_at": datetime.now(timezone.utc).isoformat()}
    _get_history(body.device_id).appendleft(entry)
    return entry


@router.get("/detect/model")
def get_model_info():
    return model_info()


@router.get("/detect/latest")
def detect_latest(device_id: str = Query(default="tv-1")):
    """Run server-side detection on the latest live frame from RPi."""
    if device_id not in _latest_images or not _latest_images[device_id].get("image_base64"):
        raise HTTPException(status_code=404, detail="No live frame available. Is rpi_detect.py running?")
    img_data = _latest_images[device_id]
    try:
        image_bytes = base64.b64decode(img_data["image_base64"])
        result = detect_bytes(image_bytes)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Detection failed: {e}")
    rpi = {
        "is_ad": img_data.get("is_ad"),
        "confidence": img_data.get("confidence"),
        "channel": img_data.get("channel"),
        "timestamp": img_data["timestamp"].isoformat() if img_data.get("timestamp") else None,
    }
    entry = {
        **result,
        "rpi": rpi,
        "captured_at": rpi["timestamp"] or datetime.now(timezone.utc).isoformat(),
    }
    _get_history(device_id).appendleft(entry)
    return entry


@router.get("/detect/history")
def detect_history(device_id: str = Query(default="tv-1")):
    return list(_get_history(device_id))


# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------

def detect_dashboard(
    api_key: str = Query(default=""),
    device_id: str = Query(default="tv-1"),
):
    if api_key != settings.api_key:
        return HTMLResponse(UNAUTH_HTML, status_code=401)

    _nav = nav_bar(api_key, device_id, "detect")

    html = f"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Detect — TV Bridge</title>
<style>
{NAV_CSS}
body{{padding:0;margin:0}}
.page{{max-width:1200px;margin:0 auto;padding:20px 16px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:#12122a;border:1px solid #1e1e3a;border-radius:10px;padding:16px}}
.card-title{{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}}
#liveImg{{width:100%;border-radius:6px;background:#0a0a18;min-height:180px;object-fit:contain;display:none}}
.img-placeholder{{width:100%;min-height:180px;background:#0a0a18;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#334155;font-size:13px}}
.result-badge{{display:flex;align-items:center;justify-content:center;gap:10px;padding:14px;border-radius:8px;margin-bottom:12px;transition:background .3s}}
.result-badge.ad{{background:#3b0a0a;border:1px solid #7f1d1d}}
.result-badge.prog{{background:#052e16;border:1px solid #14532d}}
.result-badge.unk{{background:#1e1e3a;border:1px solid #2e2e5a}}
.badge-text{{font-size:22px;font-weight:700;letter-spacing:1px}}
.badge-text.ad{{color:#f87171}}
.badge-text.prog{{color:#4ade80}}
.badge-text.unk{{color:#64748b}}
.badge-conf{{font-size:13px;color:#94a3b8}}
.conf-bar-wrap{{margin-bottom:12px}}
.conf-label{{font-size:11px;color:#64748b;margin-bottom:4px;display:flex;justify-content:space-between}}
.conf-bar{{height:6px;background:#1e1e3a;border-radius:3px;overflow:hidden}}
.conf-fill{{height:100%;border-radius:3px;transition:width .4s,background .4s}}
.stat-row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1a1a35;font-size:13px}}
.stat-row:last-child{{border-bottom:none}}
.stat-label{{color:#64748b}}
.stat-val{{color:#e2e8f0;font-weight:500;font-variant-numeric:tabular-nums}}
.cmp-row{{display:flex;gap:8px;margin-bottom:8px}}
.cmp-box{{flex:1;background:#0a0a18;border-radius:6px;padding:10px;text-align:center}}
.cmp-title{{font-size:10px;color:#475569;margin-bottom:4px;font-weight:600;text-transform:uppercase}}
.cmp-val{{font-size:14px;font-weight:700}}
.hist-table{{width:100%;border-collapse:collapse;font-size:12px}}
.hist-table th{{color:#475569;font-weight:600;text-align:left;padding:6px 8px;border-bottom:1px solid #1e1e3a;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
.hist-table td{{padding:6px 8px;border-bottom:1px solid #12122a;color:#94a3b8;font-variant-numeric:tabular-nums}}
.hist-table tr:hover td{{background:#1a1a35}}
.status-msg{{font-size:12px;color:#475569;padding:8px 0;display:flex;align-items:center;gap:8px}}
.spin{{display:inline-block;width:10px;height:10px;border:2px solid #334155;border-top-color:#a78bfa;border-radius:50%;animation:spin .7s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.tag-ok{{background:#14532d;color:#4ade80;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-err{{background:#7f1d1d;color:#f87171;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-warn{{background:#78350f;color:#fbbf24;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
</style>
</head>
<body>
{_nav}
<div class="page">
  <div class="grid">

    <!-- LEFT -->
    <div>
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Live záber z RPi</div>
        <div id="imgPlaceholder" class="img-placeholder">Čakám na frame z RPi...</div>
        <img id="liveImg" alt="live frame">
        <div style="margin-top:8px;font-size:11px;color:#334155" id="frameTs"></div>
      </div>

      <div class="card">
        <div class="card-title">Výsledok detekcie (server)</div>
        <div class="result-badge unk" id="resultBadge">
          <span class="badge-text unk" id="badgeText">—</span>
          <span class="badge-conf" id="badgeConf"></span>
        </div>
        <div class="conf-bar-wrap">
          <div class="conf-label">
            <span>Confidence (p_ad)</span>
            <span id="confPct">—</span>
          </div>
          <div class="conf-bar"><div class="conf-fill" id="confFill" style="width:0%;background:#4ade80"></div></div>
        </div>
        <div id="comparisonWrap" style="display:none">
          <div class="cmp-row">
            <div class="cmp-box">
              <div class="cmp-title">Server (nový model)</div>
              <div class="cmp-val" id="cmpServer">—</div>
            </div>
            <div class="cmp-box">
              <div class="cmp-title">RPi (pôvodný)</div>
              <div class="cmp-val" id="cmpRpi">—</div>
            </div>
          </div>
          <div style="font-size:11px;color:#475569;text-align:center;margin-top:4px" id="cmpNote"></div>
        </div>
      </div>
    </div>

    <!-- RIGHT -->
    <div>
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Detaily detekcie</div>
        <div class="stat-row">
          <span class="stat-label">Threshold</span>
          <span class="stat-val" id="dThreshold">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Čas inferencie</span>
          <span class="stat-val" id="dTime">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Kanál (RPi)</span>
          <span class="stat-val" id="dChannel">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">RPi confidence</span>
          <span class="stat-val" id="dRpiConf">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Zhoda Server vs RPi</span>
          <span class="stat-val" id="dAgree">—</span>
        </div>
      </div>

      <div class="card" style="margin-bottom:16px">
        <div class="card-title">Model info</div>
        <div class="stat-row">
          <span class="stat-label">Algoritmus</span>
          <span class="stat-val" id="mName">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">CLIP</span>
          <span class="stat-val" id="mClip">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">CV Precision</span>
          <span class="stat-val" id="mPrec">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">CV Recall</span>
          <span class="stat-val" id="mRec">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Threshold</span>
          <span class="stat-val" id="mThresh">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Tréning</span>
          <span class="stat-val" id="mTrain">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Stav</span>
          <span class="stat-val" id="mStatus">Načítavam...</span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          História
          <span id="histCount" style="color:#334155;font-weight:400;margin-left:8px"></span>
        </div>
        <div class="status-msg" id="statusBar">
          <span class="spin"></span>
          <span>Čakám na prvý výsledok...</span>
        </div>
        <table class="hist-table" id="histTable" style="display:none">
          <thead>
            <tr>
              <th>Čas</th><th>Server</th><th>RPi</th><th>Conf</th><th>ms</th>
            </tr>
          </thead>
          <tbody id="histBody"></tbody>
        </table>
      </div>
    </div>

  </div>
</div>

<script>
(function() {{
  var API_KEY = new URLSearchParams(location.search).get('api_key') || '';
  var DEVICE_ID = new URLSearchParams(location.search).get('device_id') || 'tv-1';
  var lastTs = null;
  var modelLoaded = false;

  function hdr() {{
    return {{ headers: {{ 'X-API-Key': API_KEY }} }};
  }}

  function fmtTime(iso) {{
    if (!iso) return '—';
    var d = new Date(iso);
    return d.toLocaleTimeString('sk-SK', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  }}

  function fmtConf(v) {{
    if (v == null) return '—';
    return (v * 100).toFixed(1) + '%';
  }}

  function setText(id, val) {{
    var el = document.getElementById(id);
    if (el) el.textContent = val != null ? String(val) : '—';
  }}

  function setTag(id, cls, text) {{
    var el = document.getElementById(id);
    if (!el) return;
    var span = document.createElement('span');
    span.className = cls;
    span.textContent = text;
    el.textContent = '';
    el.appendChild(span);
  }}

  function loadModelInfo() {{
    fetch('/v1/detect/model', hdr())
      .then(function(r) {{ return r.ok ? r.json() : null; }})
      .then(function(d) {{
        if (!d) return;
        setText('mName', d.model_name);
        setText('mClip', d.clip_model);
        setText('mPrec', d.cv_precision != null ? (d.cv_precision*100).toFixed(1)+'%' : '—');
        setText('mRec', d.cv_recall != null ? (d.cv_recall*100).toFixed(1)+'%' : '—');
        setText('mThresh', d.threshold != null ? d.threshold.toFixed(4) : '—');
        if (d.train_samples) {{
          setText('mTrain', (d.train_samples.ad||0) + ' ad / ' + (d.train_samples.no_ad||0) + ' no-ad');
        }}
        if (d.loaded) {{
          modelLoaded = true;
          setTag('mStatus', 'tag-ok', 'Načítaný');
        }} else if (d.error) {{
          setTag('mStatus', 'tag-err', d.error);
        }} else {{
          setTag('mStatus', 'tag-warn', 'Nenačítaný (načíta sa pri prvom requeste ~30-60s)');
        }}
      }})
      .catch(function() {{}});
  }}

  function updateResult(d) {{
    var isAd = d.is_ad;
    var conf = d.confidence;

    // Badge
    var badge = document.getElementById('resultBadge');
    var text = document.getElementById('badgeText');
    var confEl = document.getElementById('badgeConf');
    if (badge) {{
      badge.className = 'result-badge ' + (isAd ? 'ad' : 'prog');
    }}
    if (text) {{
      text.className = 'badge-text ' + (isAd ? 'ad' : 'prog');
      text.textContent = isAd ? 'REKLAMA' : 'PROGRAM';
    }}
    if (confEl) confEl.textContent = fmtConf(conf);

    // Confidence bar
    var fill = document.getElementById('confFill');
    if (fill) {{
      fill.style.width = ((conf||0)*100).toFixed(1) + '%';
      fill.style.background = isAd ? '#f87171' : '#4ade80';
    }}
    setText('confPct', fmtConf(conf));
    setText('dThreshold', d.threshold != null ? d.threshold.toFixed(4) : '—');
    setText('dTime', d.detect_ms != null ? d.detect_ms + ' ms' : '—');

    // RPi comparison
    if (d.rpi) {{
      var cmpWrap = document.getElementById('comparisonWrap');
      if (cmpWrap) cmpWrap.style.display = 'block';
      setText('dChannel', d.rpi.channel || '—');
      setText('dRpiConf', fmtConf(d.rpi.confidence));

      var rpiIsAd = d.rpi.is_ad;
      var agree = rpiIsAd != null && isAd === rpiIsAd;

      setText('cmpServer', isAd ? 'REKLAMA' : 'PROGRAM');
      var cmpServer = document.getElementById('cmpServer');
      if (cmpServer) cmpServer.style.color = isAd ? '#f87171' : '#4ade80';

      setText('cmpRpi', rpiIsAd == null ? '—' : rpiIsAd ? 'REKLAMA' : 'PROGRAM');
      var cmpRpi = document.getElementById('cmpRpi');
      if (cmpRpi && rpiIsAd != null) cmpRpi.style.color = rpiIsAd ? '#f87171' : '#4ade80';

      if (rpiIsAd != null) {{
        setTag('dAgree', agree ? 'tag-ok' : 'tag-err', agree ? 'Zhodujú sa' : 'Nezhodujú sa!');
        setText('cmpNote', agree ? '' : 'Server a RPi sa nezhodujú. Jeden z nich sa mýli.');
      }}
    }}
  }}

  function addHistoryRow(d) {{
    var tbody = document.getElementById('histBody');
    var table = document.getElementById('histTable');
    var bar = document.getElementById('statusBar');
    if (!tbody) return;
    if (table) table.style.display = '';
    if (bar) bar.style.display = 'none';

    var isAd = d.is_ad;
    var rpiIsAd = d.rpi ? d.rpi.is_ad : null;
    var agree = rpiIsAd != null && isAd === rpiIsAd;

    var tr = document.createElement('tr');
    if (!agree && rpiIsAd != null) tr.style.background = '#1a0a0a';

    function mkTd(text, color) {{
      var td = document.createElement('td');
      td.textContent = text;
      if (color) td.style.color = color;
      return td;
    }}

    tr.appendChild(mkTd(fmtTime(d.captured_at)));
    tr.appendChild(mkTd(isAd ? 'AD' : 'OK', isAd ? '#f87171' : '#4ade80'));
    tr.appendChild(mkTd(rpiIsAd == null ? '—' : rpiIsAd ? 'AD' : 'OK', rpiIsAd == null ? '#475569' : rpiIsAd ? '#f87171' : '#4ade80'));
    tr.appendChild(mkTd(fmtConf(d.confidence)));
    tr.appendChild(mkTd(d.detect_ms != null ? d.detect_ms : '—'));

    tbody.insertBefore(tr, tbody.firstChild);
    while (tbody.children.length > 20) tbody.removeChild(tbody.lastChild);

    var cnt = document.getElementById('histCount');
    if (cnt) cnt.textContent = '(' + tbody.children.length + ')';
  }}

  function poll() {{
    fetch('/v1/detect/latest?device_id=' + DEVICE_ID, hdr())
      .then(function(r) {{
        if (r.status === 404) {{
          var bar = document.getElementById('statusBar');
          if (bar) {{
            bar.style.display = 'flex';
            bar.querySelector('span:last-child').textContent = 'RPi neposiela frame. Je spustený rpi_detect.py?';
          }}
          return null;
        }}
        if (r.status === 503) {{
          var bar = document.getElementById('statusBar');
          if (bar) {{
            bar.style.display = 'flex';
            bar.querySelector('span:last-child').textContent = 'Načítavam CLIP model na serveri (prvý krát ~30-60s)...';
          }}
          return null;
        }}
        return r.ok ? r.json() : null;
      }})
      .then(function(d) {{
        if (!d) return;
        var ts = d.captured_at || (d.rpi && d.rpi.timestamp);
        if (ts === lastTs) return;
        lastTs = ts;

        // Update image
        var img = document.getElementById('liveImg');
        var ph = document.getElementById('imgPlaceholder');
        if (img) {{
          img.onload = function() {{
            img.style.display = 'block';
            if (ph) ph.style.display = 'none';
          }};
          img.src = '/v1/live-image.jpg?device_id=' + DEVICE_ID + '&t=' + Date.now();
        }}

        var frameTs = document.getElementById('frameTs');
        if (frameTs) frameTs.textContent = ts ? 'Frame: ' + fmtTime(ts) : '';

        updateResult(d);
        addHistoryRow(d);

        if (!modelLoaded) {{
          modelLoaded = true;
          setTag('mStatus', 'tag-ok', 'Načítaný');
        }}
      }})
      .catch(function(e) {{ console.error('poll', e); }});
  }}

  loadModelInfo();
  poll();
  setInterval(poll, 3000);
  setInterval(loadModelInfo, 30000);
}})();
</script>
<script>{NAV_STATUS_JS}</script>
</body>
</html>"""

    return HTMLResponse(html)
