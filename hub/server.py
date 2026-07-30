#!/usr/bin/env python3
"""Hub + Dashboard server.

HUB_MODE=0 (default): read-only dashboard, tails blackboard/events.jsonl and
serves SSE stream + simple dashboard.

HUB_MODE=1: ALSO acts as network rendezvous for distributed drones (HttpBoard).
Drones reach it over HTTP to read shared state and post messages/progress.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from bb import Blackboard, now_iso

HERE = Path(__file__).resolve().parent
SCENARIO = os.environ.get("SCENARIO", "scenario-1")
TASK = os.environ.get("TASK", "")
FIXTURES = Path(os.environ.get("FIXTURES", "./test_fixtures"))
WEB_DIR = Path(os.environ.get("WEB_DIR", str(HERE / "static")))

HUB_MODE = os.environ.get("HUB_MODE", "0") not in ("0", "", "false", "no")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _safe_id(s) -> str | None:
    s = str(s or "")
    return s if _ID_RE.match(s) else None


_CTYPES = {
    ".html": "text/html", ".js": "text/javascript",
    ".css": "text/css", ".json": "application/json",
    ".png": "image/png", ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _ctype(p: Path) -> str:
    return _CTYPES.get(p.suffix.lower(), "application/octet-stream")


BB = Blackboard()
if HUB_MODE:
    BB.ensure_layout()
EVENTS = BB.events_path

_STATE = {"phase", "decision", "assignments", "world", "pause", "critic", "control"}


def agents_summary() -> list:
    reg = BB.read_registry()
    last = {}
    if EVENTS.exists():
        with open(EVENTS, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                f = e.get("from")
                if not f:
                    continue
                if e.get("kind") in ("thought", "thought_end"):
                    d = last.setdefault(f, {})
                    if e.get("phase"):
                        d["phase"] = e["phase"]
    ids = set(reg) | set(last)
    out = []
    for i in sorted(ids):
        r = reg.get(i) or {}
        out.append({"id": i, "role": r.get("role"),
                    "phase": last.get(i, {}).get("phase"),
                    "registered": i in reg})
    return out


def assemble_transcript(agent_id: str) -> dict:
    thoughts, cur = [], None
    if EVENTS.exists():
        with open(EVENTS, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("from") != agent_id:
                    continue
                k = e.get("kind")
                if k == "thought":
                    thoughts.append({"ts": e.get("t"), "phase": e.get("phase"),
                                     "text": e.get("text", "")})
                elif k == "thought_start":
                    cur = {"ts": e.get("t"), "phase": e.get("phase"), "text": ""}
                elif k == "thought_delta" and cur is not None:
                    cur["text"] += e.get("text", "")
                elif k == "thought_end":
                    if cur is None:
                        cur = {"ts": e.get("t"), "phase": e.get("phase"), "text": ""}
                    if not cur["text"]:
                        cur["text"] = e.get("text", "")
                    thoughts.append(cur)
                    cur = None
    if cur is not None:
        cur["streaming"] = True
        thoughts.append(cur)
    messages = [m for m in BB.list_messages()
                if m.get("from") == agent_id or m.get("to") == agent_id]
    reg = BB.read_registry().get(agent_id, {})
    return {"id": agent_id, "role": reg.get("role"), "meta": reg,
            "thoughts": thoughts, "messages": messages}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, data: bytes):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("content-length", 0))
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def _authed(self) -> bool:
        if not HUB_TOKEN:
            return True
        return self.headers.get("authorization", "") == f"Bearer {HUB_TOKEN}"

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json({"ok": True, "hub": HUB_MODE, "scenario": SCENARIO})
        if path == "/rerun":
            return self._json({"ok": False, "error": "use POST /rerun"}, 405)
        if path.startswith("/scenario.json"):
            mp = FIXTURES / SCENARIO / "map.json"
            return self._send(200, "application/json",
                              mp.read_bytes() if mp.exists() else b"{}")
        if path == "/agents":
            return self._json(agents_summary())
        if path.startswith("/agents/") and path.endswith("/transcript"):
            aid = path[len("/agents/"):-len("/transcript")]
            return self._json(assemble_transcript(aid))
        if path == "/events":
            return self._sse()
        if path == "/messages":
            return self._json(BB.list_messages())
        if path == "/progress":
            return self._json(BB.read_all_progress())
        if path.startswith("/state/"):
            name = path.split("/", 2)[2]
            if name in _STATE:
                return self._json(BB.read_json(BB.state / f"{name}.json", {}))
            return self._json({"error": "unknown state"}, 404)
        if path in ("/", "", "/index.html") or path.endswith(".html") or path.endswith(".js") or path.endswith(".css"):
            return self._web(path)
        return self._web(path)

    def _web(self, path: str):
        idx = WEB_DIR / "index.html"
        if not idx.exists():
            return self._send(200, "text/html",
                b"<!doctype html><meta charset=utf-8>"
                b"<body style='font:16px sans-serif;background:#0b0f16;color:#eee;padding:40px'>"
                b"<h2>Stress Test Drone City -- Hub</h2>"
                b"<p>Dashboard not built. Check <code>hub/static/</code>.</p></body>")
        rel = path.lstrip("/")
        if rel and not path.endswith("/"):
            f = (WEB_DIR / rel).resolve()
            try:
                if str(f).startswith(str(WEB_DIR.resolve())) and f.is_file():
                    return self._file(f, _ctype(f))
            except OSError:
                pass
        return self._file(idx, "text/html")

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/rerun":
            if self.headers.get("x-rerun") != "1":
                return self._json({"ok": False, "error": "x-rerun header required"}, 403)
            try:
                if hasattr(BB, "reset_runtime"):
                    BB.reset_runtime()
                    BB.append_event({"kind": "phase", "phase": "INIT", "round": 0})
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "not a FileBoard"}, 400)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if path == "/pause":
            if self.headers.get("x-pause") != "1":
                return self._json({"ok": False, "error": "x-pause header required"}, 403)
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            try:
                body = self._body()
            except Exception as exc:
                return self._json({"error": f"bad json: {exc}"}, 400)
            paused = bool(body.get("paused", True))
            obj = {"paused": paused,
                   "reason": str(body.get("reason") or
                                 ("battery swap" if paused else ""))[:200],
                   "by": "operator", "ts": now_iso()}
            try:
                BB.write_pause(obj)
                BB.append_event({"kind": "pause", **obj})
                return self._json({"ok": True, **obj})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if path == "/step":
            if self.headers.get("x-pause") != "1":
                return self._json({"ok": False, "error": "x-pause header required"}, 403)
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            try:
                secs = max(0.3, min(10.0, float(self._body().get("secs", 2.0))))
            except Exception as exc:
                return self._json({"error": f"bad json: {exc}"}, 400)
            try:
                BB.write_pause({"paused": False, "reason": "step", "by": "operator",
                                "ts": now_iso()})
                BB.append_event({"kind": "pause", "paused": False, "reason": "step"})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)

            def _repause():
                time.sleep(secs)
                try:
                    BB.write_pause({"paused": True, "reason": "step-hold",
                                    "by": "operator", "ts": now_iso()})
                    BB.append_event({"kind": "pause", "paused": True,
                                     "reason": "step-hold"})
                except Exception:
                    pass
            threading.Thread(target=_repause, daemon=True).start()
            return self._json({"ok": True, "stepped_secs": secs})

        # ---- hub write endpoints (HUB_MODE=1 only) ----
        if not HUB_MODE:
            return self._json({"error": "hub disabled"}, 403)
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        try:
            body = self._body()
        except Exception as exc:
            return self._json({"error": f"bad json: {exc}"}, 400)

        if path == "/messages":
            if not _safe_id(body.get("from")) or not _safe_id(body.get("type")):
                return self._json({"error": "bad from/type"}, 400)
            return self._json(BB.write_message(body))
        if path == "/events":
            BB.append_event(body)
            return self._json({"ok": True})
        if path == "/register":
            aid = _safe_id(body.get("id"))
            if not aid:
                return self._json({"error": "id required"}, 400)
            BB.write_registry(aid, body)
            return self._json({"ok": True, "id": aid})
        if path.startswith("/progress/"):
            aid = _safe_id(path.split("/", 2)[2])
            if not aid:
                return self._json({"error": "bad agent id"}, 400)
            BB.write_progress(aid, body)
            return self._json({"ok": True})
        if path.startswith("/state/"):
            name = path.split("/", 2)[2]
            if name not in _STATE:
                return self._json({"error": "unknown state"}, 404)
            BB.write_json(BB.state / f"{name}.json", body)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # ---- static / SSE ----
    def _file(self, p: Path, ctype: str):
        try:
            self._send(200, ctype, p.read_bytes())
        except FileNotFoundError:
            self._send(404, "text/plain", b"missing")

    def _sse(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()

        def send(line: str):
            self.wfile.write(f"data: {line}\n\n".encode())
            self.wfile.flush()

        pos = 0
        try:
            send(json.dumps({"kind": "hello"}))
            while True:
                if EVENTS.exists():
                    try:
                        size = EVENTS.stat().st_size
                    except OSError:
                        size = 0
                    if size < pos:
                        pos = 0
                        send(json.dumps({"kind": "hello"}))
                    with open(EVENTS, "r", encoding="utf-8") as fh:
                        fh.seek(pos)
                        for line in fh:
                            line = line.strip()
                            if line:
                                send(line)
                        pos = fh.tell()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    mode = "HUB+dashboard" if HUB_MODE else "dashboard"
    print(f"[hub] {mode} on :{port} events={EVENTS}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
