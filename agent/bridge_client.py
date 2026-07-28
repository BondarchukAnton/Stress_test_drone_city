"""HTTP client for a robot's bridge sidecar (§6).

Same contract for the mock (dev) and the real rclpy bridge (hardware) -- only
BRIDGE_URL changes. navigate() returns an iterator of streamed pose/progress
frames so the rover can animate its path in the visualizer.
"""
from __future__ import annotations

import json
import urllib.request


class BridgeClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def healthz(self) -> bool:
        try:
            return self._get("/healthz").get("ok", False)
        except Exception:
            return False

    def photograph(self, sector: str) -> dict:
        return self._post("/photograph", {"sector": sector})

    def detect_obstacle(self, image_path: str) -> dict:
        return self._post("/detect_obstacle", {"image_path": image_path})

    # ---- survey actions (cargo hunt over a cell grid) ----
    def photograph_cell(self, cell) -> dict:
        return self._post("/photograph", {"cell": list(cell)})

    def analyze(self, cell, close_look: bool = False) -> dict:
        return self._post("/analyze", {"cell": list(cell), "close_look": close_look})

    # ---- pause / battery swap ----
    def land(self) -> dict:
        return self._post("/land", {})

    def takeoff(self) -> dict:
        return self._post("/takeoff", {})

    # ---- LED-лента (регистрация «мигни, чтобы я тебя узнал» и статусы) ----
    def led(self, effect: str = "fill", color: str | None = None,
            r: int = 0, g: int = 0, b: int = 0, seconds: float = 0) -> dict:
        if color:
            h = color.lstrip("#")
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            try:
                r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
        return self._post("/led", {"effect": effect, "r": int(r) & 255,
                                   "g": int(g) & 255, "b": int(b) & 255,
                                   "seconds": seconds})

    def pose(self) -> dict:
        return self._get("/pose")

    # ---- painter actions (spray-can drone) ----
    def move(self, to, grid=None) -> dict:
        body = {"to": to}
        if grid is not None:
            body["grid"] = grid
        return self._post("/move", body)

    # ---- city_missions rover: dwell with LED + evidence (регламент 3с/5с) ----
    def dwell(self, seconds: float, led: str = "blink") -> dict:
        return self._post("/dwell", {"seconds": seconds, "led": led})

    def spray(self, points, color: str, width: int = 2) -> dict:
        return self._post("/spray", {"points": points, "color": color, "width": width})

    def navigate(self, frm, to, grid):
        """Yield streamed frames {pose,progress} ... then {status:'arrived'|'blocked'}."""
        req = urllib.request.Request(
            self.base + "/navigate",
            data=json.dumps({"from": frm, "to": to, "grid": grid}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            for line in resp:
                line = line.strip()
                if line:
                    yield json.loads(line.decode())
