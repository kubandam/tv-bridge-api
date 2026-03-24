"""Shared UI components for HTML dashboards."""


NAV_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f1a;color:#e2e8f0;min-height:100vh}
a{color:inherit;text-decoration:none}
.site-nav{background:#12122a;border-bottom:1px solid #1e1e3a;padding:0 20px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:300;height:52px;gap:16px}
.nav-brand{display:flex;align-items:center;gap:10px;flex-shrink:0}
.nav-logo{font-size:15px;font-weight:700;color:#a78bfa;letter-spacing:-.3px}
.nav-device{background:#1e1e3a;color:#64748b;font-size:11px;padding:2px 9px;border-radius:20px;font-variant-numeric:tabular-nums}
.nav-links{display:flex;gap:2px}
.nav-link{color:#64748b;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:500;transition:all .15s;white-space:nowrap}
.nav-link:hover{color:#a5b4fc;background:#1e1e3a}
.nav-link.active{color:#a78bfa;background:#1e1a3a}
.nav-status{display:flex;align-items:center;gap:6px;font-size:12px;color:#475569;flex-shrink:0}
.nav-dot{width:7px;height:7px;border-radius:50%;background:#475569}
.nav-dot.online{background:#4ade80;box-shadow:0 0 6px #4ade8088}
.nav-dot.ad{background:#f87171;box-shadow:0 0 6px #f8717188;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
"""

UNAUTH_HTML = """<!DOCTYPE html><html><body style="background:#0f0f1a;color:#e2e8f0;
font-family:sans-serif;padding:60px;text-align:center">
<p style="font-size:48px;margin-bottom:16px">TV Bridge</p>
<h1 style="color:#f87171;margin-bottom:12px">API Key Required</h1>
<p style="color:#64748b">Add <code style="background:#1e1e3a;padding:2px 8px;border-radius:4px">?api_key=YOUR_KEY</code> to the URL.</p>
</body></html>"""


def nav_bar(api_key: str, device_id: str, active: str) -> str:
    pages = [
        ("monitor", "Monitor"),
        ("review", "Review"),
        ("labeling", "Live Label"),
        ("admin", "Admin"),
    ]
    links = "".join(
        f'<a href="/{pid}?api_key={api_key}&device_id={device_id}" '
        f'class="nav-link{"  active" if pid == active else ""}">{label}</a>'
        for pid, label in pages
    )
    return (
        f'<nav class="site-nav">'
        f'<div class="nav-brand">'
        f'<span class="nav-logo">TV Bridge</span>'
        f'<span class="nav-device">{device_id}</span>'
        f'</div>'
        f'<div class="nav-links">{links}</div>'
        f'<div class="nav-status" id="navStatus">'
        f'<span class="nav-dot" id="navDot"></span>'
        f'<span id="navStatusText">—</span>'
        f'</div>'
        f'</nav>'
    )


NAV_STATUS_JS = """
(function() {
  const API_KEY = new URLSearchParams(location.search).get('api_key') || '';
  const DEVICE_ID = new URLSearchParams(location.search).get('device_id') || 'tv-1';
  async function pollStatus() {
    try {
      const r = await fetch('/v1/ad-state?device_id=' + DEVICE_ID, {headers:{'X-API-Key': API_KEY}});
      if (!r.ok) return;
      const d = await r.json();
      const dot = document.getElementById('navDot');
      const txt = document.getElementById('navStatusText');
      if (!dot || !txt) return;
      if (d.ad_active) {
        dot.className = 'nav-dot ad';
        txt.textContent = 'REKLAMA';
        txt.style.color = '#f87171';
      } else {
        dot.className = 'nav-dot online';
        txt.textContent = 'Program';
        txt.style.color = '#4ade80';
      }
    } catch(e) {}
  }
  pollStatus();
  setInterval(pollStatus, 3000);
})();
"""
