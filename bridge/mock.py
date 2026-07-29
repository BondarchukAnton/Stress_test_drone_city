#!/usr/bin/env python3
"""Mock ROS2 bridge sidecar (spec/brief §6).

One HTTP service per robot. Same contract as the real rclpy bridge -- only the
implementation differs. Pure stdlib (http.server).
"""
from __future__ import annotations

import heapq
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCENARIO = os.environ.get("SCENARIO", "scenario-1")
FIXTURES = Path(os.environ.get("FIXTURES", "./test_fixtures"))
BLACKBOARD = Path(os.environ.get("BLACKBOARD", "./blackboard"))
AGENT = os.environ.get("AGENT_ID", "robot")
ARTIFACTS = BLACKBOARD / "artifacts"
SCEN_DIR = FIXTURES / SCENARIO

_pose = {"xy": [0, 0], "heading": 0.0}
_landed = {"v": False}
_led = {"effect": "off", "r": 0, "g": 0, "b": 0}
_strokes = []
_cfg = {}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_map() -> dict:
    try:
        mp = json.loads((SCEN_DIR / "map.json").read_text())
    except FileNotFoundError:
        mp = {}
    mp.update(_cfg)
    return mp


def write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    import struct
    import zlib
    h = len(pixels)
    w = len(pixels[0]) if h else 0
    raw = b"".join(
        b"\x00" + b"".join(bytes(px) for px in row) for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render_cell_photo(path: Path, kind: str) -> None:
    base = (86, 125, 70)
    img = [[base for _ in range(64)] for _ in range(64)]
    if kind == "cargo":
        for y in range(22, 42):
            for x in range(22, 42):
                edge = y in (22, 41) or x in (22, 41)
                img[y][x] = (92, 64, 20) if edge else (153, 102, 51)
    elif kind == "decoy":
        for y in range(26, 38):
            for x in range(24, 40):
                if (x + y) % 7:
                    img[y][x] = (128, 128, 122)
    write_png(path, img)


def cell_truth(mp: dict, cell) -> str:
    cx, cy = int(cell[0]), int(cell[1])
    cargo = mp.get("cargo")
    if isinstance(cargo, list) and [int(cargo[0]), int(cargo[1])] == [cx, cy]:
        return "cargo"
    for d in mp.get("decoys") or []:
        if isinstance(d, list) and [int(d[0]), int(d[1])] == [cx, cy]:
            return "decoy"
    return "empty"


def astar(grid, start, goal):
    if not grid:
        return None
    h = len(grid)
    w = len(grid[0])
    sx, sy = start
    gx, gy = goal

    def ok(x, y):
        return 0 <= x < w and 0 <= y < h and grid[y][x] == 0

    if not ok(sx, sy) or not ok(gx, gy):
        return None

    def hcost(x, y):
        return abs(x - gx) + abs(y - gy)

    openq = [(hcost(sx, sy), 0, (sx, sy))]
    came = {}
    gscore = {(sx, sy): 0}
    while openq:
        _, g, (x, y) = heapq.heappop(openq)
        if (x, y) == (gx, gy):
            path = [[x, y]]
            while (x, y) in came:
                x, y = came[(x, y)]
                path.append([x, y])
            return path[::-1]
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not ok(nx, ny):
                continue
            ng = g + 1
            if ng < gscore.get((nx, ny), 1e9):
                gscore[(nx, ny)] = ng
                came[(nx, ny)] = (x, y)
                heapq.heappush(openq, (ng + hcost(nx, ny), ng, (nx, ny)))
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("content-length", 0))
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "agent": AGENT, "scenario": SCENARIO})
        if self.path == "/pose":
            return self._json(200, _pose)
        if self.path == "/config":
            mp = load_map()
            return self._json(200, {"ok": True,
                                    "grid_size": mp.get("grid_size") or [5, 5],
                                    "cell_size": mp.get("cell_size", 0.6),
                                    "override": dict(_cfg)})
        if self.path == "/canvas":
            mp = load_map()
            cv = mp.get("canvas", {})
            return self._json(200, {"strokes": _strokes,
                                    "w": cv.get("w", 100), "h": cv.get("h", 100)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._read_body()
        except Exception as exc:
            return self._json(400, {"error": f"bad json: {exc}"})

        if self.path == "/config":
            if "grid_size" in body:
                gs = body["grid_size"]
                if not (isinstance(gs, list) and len(gs) == 2
                        and all(isinstance(v, int) and 1 <= v <= 32 for v in gs)):
                    return self._json(400, {"error": "grid_size must be [1..32, 1..32]"})
                _cfg["grid_size"] = gs
            if "cell_size" in body:
                try:
                    cs = float(body["cell_size"])
                except (TypeError, ValueError):
                    return self._json(400, {"error": "cell_size must be a number"})
                if not 0.05 <= cs <= 10.0:
                    return self._json(400, {"error": "cell_size out of range"})
                _cfg["cell_size"] = cs
            if "start" in body:
                st = body["start"]
                if not (isinstance(st, list) and len(st) == 2
                        and all(isinstance(v, (int, float)) for v in st)):
                    return self._json(400, {"error": "start must be [x,y]"})
                _pose["xy"] = [int(st[0]), int(st[1])]
            mp = load_map()
            return self._json(200, {"ok": True,
                                    "grid_size": mp.get("grid_size") or [5, 5],
                                    "cell_size": mp.get("cell_size", 0.6),
                                    "pose": _pose["xy"]})
        if self.path == "/led":
            effect = str(body.get("effect") or "fill")[:24]
            _led.update({"effect": effect, "r": int(body.get("r", 0)) & 255,
                         "g": int(body.get("g", 0)) & 255,
                         "b": int(body.get("b", 0)) & 255})
            return self._json(200, {"ok": True, **_led})
        if self.path == "/land":
            _landed["v"] = True
            return self._json(200, {"ok": True, "landed": True, "pose": _pose["xy"]})
        if self.path == "/takeoff":
            _landed["v"] = False
            return self._json(200, {"ok": True, "landed": False, "pose": _pose["xy"]})
        if _landed["v"] and self.path in ("/photograph", "/move", "/spray"):
            return self._json(409, {"error": "landed (paused for battery swap)"})
        if self.path == "/dwell":
            secs = min(max(float(body.get("seconds", 0) or 0), 0.0), 30.0)
            led = str(body.get("led") or _led["effect"] or "off").lower()
            _led["effect"] = led
            time.sleep(secs)
            return self._json(200, {"ok": True, "cell": list(_pose["xy"]),
                                    "stationary_seconds": secs, "moved": False,
                                    "led": led in ("on", "blink")})
        if self.path == "/stop":
            return self._json(200, {"ok": True, "stopped": True})
        if self.path == "/photograph":
            return self._photograph(body)
        if self.path == "/detect_obstacle":
            return self._detect(body)
        if self.path == "/analyze":
            return self._analyze(body)
        if self.path == "/navigate":
            return self._navigate(body)
        if self.path == "/move":
            return self._move(body)
        if self.path == "/spray":
            return self._spray(body)
        return self._json(404, {"error": "not found"})

    def _valid_cell(self, cell, mp) -> bool:
        if not (isinstance(cell, list) and len(cell) == 2
                and all(isinstance(c, (int, float)) for c in cell)):
            return False
        gs = mp.get("grid_size") or [5, 5]
        return 0 <= int(cell[0]) < int(gs[0]) and 0 <= int(cell[1]) < int(gs[1])

    def _photograph(self, body):
        if "cell" in body:
            mp = load_map()
            cell = body.get("cell")
            if not self._valid_cell(cell, mp):
                return self._json(400, {"error": "invalid cell"})
            cx, cy = int(cell[0]), int(cell[1])
            _pose["xy"] = [cx, cy]
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            dst = ARTIFACTS / f"{AGENT}-cell-{cx}-{cy}.png"
            try:
                render_cell_photo(dst, cell_truth(mp, [cx, cy]))
            except OSError:
                dst.write_bytes(b"")
            rel = (str(dst.relative_to(BLACKBOARD))
                   if str(dst).startswith(str(BLACKBOARD)) else str(dst))
            return self._json(200, {"image_path": rel, "ts": now_iso(),
                                    "cell": [cx, cy]})
        sector = body.get("sector")
        if not isinstance(sector, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", sector):
            return self._json(400, {"error": "invalid sector"})
        mp = load_map()
        meta = mp.get("sectors", {}).get(sector, {})
        src_name = meta.get("image", f"sector-{sector}.png")
        src = SCEN_DIR / src_name
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        dst = ARTIFACTS / f"{AGENT}-sector-{sector}{src.suffix or '.png'}"
        if src.exists():
            shutil.copyfile(src, dst)
        else:
            dst.write_bytes(b"")
        rel = str(dst.relative_to(BLACKBOARD)) if str(dst).startswith(str(BLACKBOARD)) else str(dst)
        return self._json(200, {"image_path": rel, "ts": now_iso(), "sector": sector})

    def _detect(self, body):
        image_path = body.get("image_path", "")
        if not isinstance(image_path, str):
            return self._json(400, {"error": "invalid image_path"})
        m = re.search(r"sector-([A-Za-z0-9]+)", image_path)
        sector = m.group(1) if m else None
        labels = None
        if sector:
            lp = SCEN_DIR / f"sector-{sector}.labels.json"
            if lp.exists():
                labels = json.loads(lp.read_text())
        if labels is not None:
            return self._json(200, {
                "obstacles": labels.get("obstacles", []),
                "coverage": labels.get("coverage", 0.95),
            })
        return self._json(200, {"obstacles": [], "coverage": 0.92})

    def _analyze(self, body):
        mp = load_map()
        cell = body.get("cell")
        if not self._valid_cell(cell, mp):
            return self._json(400, {"error": "invalid cell"})
        close = bool(body.get("close_look"))
        truth = cell_truth(mp, cell)
        if truth == "cargo":
            out = {"cargo": True, "confidence": 0.97 if close else 0.93,
                   "label": "cargo_box"}
        elif truth == "decoy":
            out = ({"cargo": False, "confidence": 0.88, "label": "debris"}
                   if close else
                   {"cargo": True, "confidence": 0.57, "label": "possible_cargo"})
        else:
            out = {"cargo": False, "confidence": 0.96, "label": "clear"}
        out["cell"] = [int(cell[0]), int(cell[1])]
        out["close_look"] = close
        return self._json(200, out)

    def _navigate(self, body):
        frm = body.get("from")
        to = body.get("to")
        grid = body.get("grid")
        if not (isinstance(frm, list) and isinstance(to, list) and isinstance(grid, list)):
            return self._json(400, {"error": "from/to/grid required"})
        path = astar(grid, frm, to)
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        if not path:
            emit({"status": "blocked", "reason": "no path"})
            return
        n = len(path)
        for i, cell in enumerate(path):
            _pose["xy"] = cell
            emit({"pose": cell, "progress": round((i + 1) / n, 3)})
            time.sleep(float(os.environ.get("NAV_STEP_SEC", "0.15")))
        emit({"status": "arrived", "pose": path[-1]})

    def _valid_point(self, p):
        return (isinstance(p, list) and len(p) == 2
                and all(isinstance(c, (int, float)) for c in p))

    def _move(self, body):
        to = body.get("to")
        if not self._valid_point(to):
            return self._json(400, {"error": "to must be [x,y]"})
        _pose["xy"] = to
        return self._json(200, {"pose": to, "ok": True})

    def _spray(self, body):
        points = body.get("points")
        color = body.get("color", "#ffffff")
        width = body.get("width", 2)
        if not isinstance(points, list) or len(points) < 1 or not all(self._valid_point(p) for p in points):
            return self._json(400, {"error": "points must be a list of [x,y]"})
        if not isinstance(color, str) or not _HEX.match(color):
            return self._json(400, {"error": "color must be #rrggbb"})
        if not isinstance(width, (int, float)) or not (0 < width <= 12):
            return self._json(400, {"error": "width out of range"})
        full = [list(_pose["xy"])] + [list(p) for p in points]
        _pose["xy"] = full[-1]
        stroke = {"points": full, "color": color, "width": width, "by": AGENT}
        _strokes.append(stroke)
        return self._json(200, {"ok": True, "points": full, "color": color,
                                "length": len(full)})


def main():
    port = int(os.environ.get("PORT", "9000"))
    init = load_map().get("start")
    if init:
        _pose["xy"] = init
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[bridge {AGENT}] mock on :{port} scenario={SCENARIO}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
