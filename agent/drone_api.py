#!/usr/bin/env python3
"""drone_api.py — единый интерфейс управления дроном через sverk_interfaces.

Всегда использует sverk_interfaces (ROS2 + ArUco). Никаких BridgeClient/моков.
Если библиотека недоступна — методы становятся no-op с предупреждением.

Интерфейс DroneHandle:
  - takeoff(altitude=2.0)          → взлёт в frame body
  - get_telemetry()                → {x, y, z, yaw} в aruco_map
  - navigate_wait(x, y, z, ...)    → полёт в точку в aruco_map + ожидание
  - take_picture(timeout=3.0)      → сохранение снимка на диск, возврат пути
  - land(timeout=15.0)             → посадка

ArUco ↔ сетка:
  - _aruco_to_cell(x_m, y_m, ...)  → метры в клетку [col, row]
  - _cell_to_aruco(col, row, ...)  → клетка в метры (центр)
  - _find_home_cell(x_m, y_m, ...) → ближайшая клетка к позиции
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mission_journal import journal_record as _jr

_sverk_available = False
try:
    import sverk_interfaces
    _sverk_available = True
except ImportError:
    sverk_interfaces = None  # type: ignore


@dataclass
class DroneTelemetry:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0


def _aruco_to_cell(x_m: float, y_m: float, grid_w: int, grid_h: int,
                   origin_x: float = 0.0, origin_y: float = 0.0,
                   cell_size_m: float = 1.0) -> list[int]:
    col = int((x_m - origin_x) / cell_size_m)
    row = int((y_m - origin_y) / cell_size_m)
    col = max(0, min(grid_w - 1, col))
    row = max(0, min(grid_h - 1, row))
    return [col, row]


def _cell_to_aruco(col: int, row: int, origin_x: float = 0.0,
                   origin_y: float = 0.0, cell_size_m: float = 1.0) -> tuple[float, float]:
    return (origin_x + (col + 0.5) * cell_size_m,
            origin_y + (row + 0.5) * cell_size_m)


def _find_home_cell(x_m: float, y_m: float, grid_w: int, grid_h: int,
                    cell_size_m: float = 1.0,
                    origin_x: float = 0.0, origin_y: float = 0.0) -> list[int]:
    return _aruco_to_cell(x_m, y_m, grid_w, grid_h, origin_x, origin_y, cell_size_m)


class DroneHandle:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._drone = None
        self._bb_root = Path(os.environ.get("BLACKBOARD", "/blackboard"))
        self._photo_dir = self._bb_root / "artifacts"
        self._photo_dir.mkdir(parents=True, exist_ok=True)

        if _sverk_available:
            try:
                self._drone = sverk_interfaces.init(
                    Nodename=f'mission_node_{agent_id.replace("-", "_")}'
                )
                print(f"[{agent_id}] sverk_interfaces initialized", flush=True)
            except Exception as e:
                print(f"[{agent_id}] sverk_interfaces init failed: {e}", flush=True)

    @property
    def is_available(self) -> bool:
        return self._drone is not None

    def takeoff(self, altitude: float = 2.0):
        _jr("command_sent", agent=self.agent_id, command="takeoff",
            params={"altitude": altitude, "frame": "body"})
        if not self._drone:
            print(f"[{self.agent_id}] takeoff SKIPPED (no sverk_interfaces)", flush=True)
            return
        self._drone.control.navigate(
            x=0.0, y=0.0, z=altitude, yaw=0.0,
            speed=0.5, frame_id='body', auto_arm=True,
        )

    def get_telemetry(self) -> DroneTelemetry:
        if not self._drone:
            print(f"[{self.agent_id}] telemetry SKIPPED (no sverk_interfaces)", flush=True)
            return DroneTelemetry()
        telemetry = self._drone.telemetry.get_telemetry(frame_id='aruco_map')
        t = DroneTelemetry(
            x=float(telemetry.x), y=float(telemetry.y),
            z=float(telemetry.z), yaw=float(getattr(telemetry, 'yaw', 0)),
        )
        _jr("telemetry", agent=self.agent_id,
            x=t.x, y=t.y, z=t.z, yaw=t.yaw)
        return t

    def navigate_wait(self, x: float, y: float, z: float, yaw: float = 0.0,
                      speed: float = 0.5, timeout: float = 30.0,
                      tolerance: float = 0.3):
        _jr("command_sent", agent=self.agent_id, command="navigate_wait",
            params={"x": x, "y": y, "z": z, "yaw": yaw, "frame": "aruco_map"})
        if not self._drone:
            print(f"[{self.agent_id}] navigate SKIPPED (no sverk_interfaces)", flush=True)
            return
        self._drone.control.navigate_wait(
            x=x, y=y, z=z, yaw=yaw, speed=speed,
            frame_id='aruco_map', timeout=timeout, tolerance=tolerance,
        )

    def take_picture(self, timeout: float = 3.0) -> str:
        _jr("command_sent", agent=self.agent_id, command="take_picture")
        if not self._drone:
            print(f"[{self.agent_id}] picture SKIPPED (no sverk_interfaces)", flush=True)
            return ""
        frame = self._drone.image.take_picture(timeout=timeout)
        filename = f"{self.agent_id}-{int(time.time())}.png"
        dst = self._photo_dir / filename
        _save_raw_image(frame, dst)
        return str(dst.relative_to(self._bb_root))

    def land(self, timeout: float = 15.0):
        _jr("command_sent", agent=self.agent_id, command="land")
        if not self._drone:
            print(f"[{self.agent_id}] land SKIPPED (no sverk_interfaces)", flush=True)
            return
        self._drone.control.land(timeout=timeout)


def _save_raw_image(frame_data, path: Path) -> None:
    try:
        if hasattr(frame_data, 'data'):
            path.write_bytes(bytes(frame_data.data))
            return
        path.write_bytes(bytes(frame_data))
    except Exception:
        path.write_bytes(b"")


def init_drone(agent_id: str) -> DroneHandle:
    return DroneHandle(agent_id=agent_id)