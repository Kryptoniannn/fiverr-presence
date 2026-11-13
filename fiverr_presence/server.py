"""
fiverr_presence.server — self-hosted HTTP API + mini dashboard (stdlib only).

Endpoints
---------
  GET  /                              -> HTML dashboard (search box, live status)
  GET  /api/v1/status/<username>      -> single result JSON
  GET  /api/v1/status?user=a,b,c      -> list of results JSON
  GET  /healthz                       -> {"ok": true}

Run
---
  python -m fiverr_presence serve --host 127.0.0.1 --port 8399
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import FiverrPresenceChecker, CheckerConfig, profile_url, parse_target

_DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fiverr Presence Checker</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;background:#0f1214;color:#e6e6e6;margin:0;
      display:flex;flex-direction:column;align-items:center;padding:2rem 1rem}
 h1{font-weight:600}
 form{display:flex;gap:.5rem;width:min(560px,95vw)}
 input{flex:1;padding:.7rem 1rem;border-radius:8px;border:1px solid #333;background:#171a1d;color:#eee}
 button{padding:.7rem 1.2rem;border:0;border-radius:8px;background:#1dbf73;color:#04120b;font-weight:700;cursor:pointer}
 #res{width:min(560px,95vw);margin-top:1.2rem}
 .card{display:flex;justify-content:space-between;align-items:center;padding:.9rem 1.1rem;
       border-radius:10px;background:#171a1d;border:1px solid #26292c;margin-bottom:.5rem}
 .dot{width:12px;height:12px;border-radius:50%;display:inline-block;margin-right:.5rem}
 .online{background:#1dbf73}.offline{background:#555}.unknown{background:#e6a23c}
 .small{color:#8a8f98;font-size:.8rem}
</style></head><body>
<h1>Fiverr Presence Checker</h1>
<form onsubmit="return check(event)">
 <input id="q" placeholder="https://www.fiverr.com/username  — or just username" autofocus>
 <button>Check</button>
</form>
<div id="res"><p class="small">Public status only, straight from fiverr.com — cached for {ttl}s per user.</p></div>
<script>
async function check(ev){ev.preventDefault();
 const q=document.getElementById('q').value.trim(); if(!q)return false;
 const res=document.getElementById('res');
 res.innerHTML='<p class="small">checking…</p>';
 try{
  const r=await fetch('/api/v1/status?user='+encodeURIComponent(q));
  const data=await r.json();
  const items=Array.isArray(data)?data:[data];
  res.innerHTML=items.map(d=>`<div class="card"><span><span class="dot ${d.status}"></span><b>${d.username}</b>
    <span class="small">${d.status.toUpperCase()}</span></span>
    <span class="small">${d.error?d.error:d.checked_at}</span></div>`).join('');
 }catch(e){res.innerHTML='<p class="small">request failed: '+e+'</p>';}
 return false;}
</script></body></html>
"""


def serve(host: str = "127.0.0.1", port: int = 8399) -> None:
    checker = FiverrPresenceChecker(
        CheckerConfig(cache_ttl=60.0, rate_limit=1.0)  # polite defaults
    )
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == "/healthz":
                self._json({"ok": True})
                return

            if path == "/" or path == "/index.html":
                body = _DASHBOARD.replace("{ttl}", str(int(checker.config.cache_ttl))).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return

            # /api/v1/status/<username>  (path form — checked before query form)
            if path.startswith("/api/v1/status/"):
                raw = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                try:
                    self._json(checker.check(raw).to_dict())
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                return

            if path == "/api/v1/status":
                qs = urllib.parse.parse_qs(parsed.query)
                raw_users = qs.get("user", [""])[0]
                if not raw_users:
                    self._json({"error": "provide ?user=<username[,username2]> or /api/v1/status/<username>"}, 400)
                    return
                targets = [t.strip() for t in raw_users.split(",") if t.strip()][:50]

                with lock:  # one outbound request at a time — be polite to Fiverr
                    results = []
                    for target in targets:
                        try:
                            results.append(checker.check(target).to_dict())
                        except ValueError as exc:
                            results.append({
                                "username": target[:60], "status": "unknown",
                                "is_online": None, "profile_url": target[:200],
                                "checked_at": "", "http_code": None,
                                "error": str(exc), "source": "", "cached": False,
                            })
                self._json(results if len(results) > 1 else results[0])
                return

            self._json({"error": "not found"}, 404)

        def log_message(self, fmt, *args):  # quieter logs
            print(f"[serve] {self.address_string()} {fmt % args}")

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"fiverr-presence serving on http://{host}:{port}  (dashboard: /  ·  api: /api/v1/status/<username>)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
