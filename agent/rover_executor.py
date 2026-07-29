#!/usr/bin/env python3
"""rover_executor.py — синхронное управление ровером через HTTP API (:8767).

Использует RoverClient из rover_control_client.py для:
  - initial-cell → привязка к стартовой клетке
  - clear → сброс программного STOP
  - goal-cell → навигация через Nav2 с ожиданием succeeded
  - dwell_water → мигание LED + 3-секундная пауза (забор воды)

Поддерживает два режима:
  - ROVER_API_URL (порт 8767): реальный ровер через rover_control_client.py
  - BRIDGE_URL (порт 9000): мок-ровер через BridgeClient (dwell с LED)
"""
from __future__ import annotations

import time
import uuid
from typing import Any


def request_id() -> str:
    return str(uuid.uuid4())


def _make_rover_client(api_url: str, timeout: float = 300.0):
    from rover_control_client import RoverClient
    return RoverClient(api_url, timeout=timeout)


def _make_bridge_client(bridge_url: str):
    from bridge_client import BridgeClient
    timeout = 120.0
    return BridgeClient(bridge_url, timeout=timeout)


def run_rover_mission(
    target_cell: list[int],
    count: int,
    water_cell: list[int] | None = None,
    init_cell: list[int] | None = None,
    rover_api_url: str = "",
    rover_bridge_url: str = "",
    dwell_water_sec: float = 3.0,
    emit=None,
) -> dict:
    """Выполнить полный цикл ровера: старт → башня → огонь(count раз) → старт.

    Args:
        target_cell: [x, y] — клетка с огнём
        count: количество циклов тушения
        water_cell: [x, y] — клетка водонапорной башни (по умолчанию [1, 3])
        init_cell: [x, y] — стартовая клетка (по умолчанию [1, 1])
        rover_api_url: URL реального ровера (:8767)
        rover_bridge_url: URL моста ровера (:9000)
        dwell_water_sec: длительность забора воды (сек)
        emit: callback для событий {"kind": ..., ...}
    """
    water = list(water_cell) if water_cell else [1, 3]
    init = list(init_cell) if init_cell else [1, 1]
    fire = list(target_cell) if target_cell else [0, 0]

    log: list[dict] = []

    def _emit(kind: str, **kw):
        entry = {"kind": kind, "ts": time.time(), **kw}
        log.append(entry)
        if emit:
            emit(entry)

    use_api = bool(rover_api_url)
    use_bridge = bool(rover_bridge_url)

    _emit("rover_phase", phase="init",
          init_cell=init, water_cell=water, fire_cell=fire, count=count,
          use_api=use_api, use_bridge=use_bridge)

    # ---- 1. Инициализация ровера (только для реального API) ----
    client = None
    lease = None
    bridge_client = None

    if use_api:
        try:
            client = _make_rover_client(rover_api_url)
            from rover_control_client import ControlLease
            cid = f"drone-city-{uuid.uuid4().hex[:8]}"
            lease = ControlLease(client, cid)
            lease.__enter__()
            _emit("rover_lease", acquired=True)

            from rover_control_client import calibrated_cell
            cell_pose = calibrated_cell(client, init[0], init[1], 0.0)
            client.post(
                "/v1/localization/initial-pose",
                {
                    "request_id": request_id(),
                    "map_label": cell_pose["map_label"],
                    "x": cell_pose["x"],
                    "y": cell_pose["y"],
                    "yaw_deg": cell_pose["yaw_deg"],
                },
                lease.lease_id,
            )
            _emit("rover_initial_cell", cell=init)

            client.post("/v1/stop/clear", {}, lease.lease_id)
            _emit("rover_clear", ok=True)
        except Exception as e:
            _emit("rover_error", phase="init", error=str(e))
            result = {"status": "init_error", "error": str(e), "log": log}
            _cleanup(lease)
            return result

    if use_bridge:
        try:
            bridge_client = _make_bridge_client(rover_bridge_url)
            _emit("rover_bridge", ready=True)
        except Exception as e:
            _emit("rover_error", phase="bridge_init", error=str(e))

    # ---- 2. Цикл тушения (count раз) ----
    for cycle in range(1, count + 1):
        _emit("rover_cycle", cycle=cycle, of=count)

        # --- Шаг 2.1: Движение к водонапорной башне ---
        _emit("rover_navigate", phase="to_water", from_cell=init if cycle == 1 else fire,
              to_cell=water)
        nav_ok = _navigate_to(client, lease, bridge_client,
                              water[0], water[1], use_api, _emit)
        if not nav_ok:
            _emit("rover_error", phase="nav_to_water", error="navigation failed")
            _cleanup(lease)
            return {"status": "nav_water_failed", "cycle": cycle, "log": log}

        # --- Шаг 2.2: Протокол заправки водой ---
        _emit("rover_dwell", phase="water_fill", seconds=dwell_water_sec, cell=water)
        _dwell_with_led(bridge_client, use_api, dwell_water_sec, _emit)

        # --- Шаг 2.3: Движение к пожару ---
        _emit("rover_navigate", phase="to_fire", from_cell=water,
              to_cell=fire)
        nav_ok = _navigate_to(client, lease, bridge_client,
                              fire[0], fire[1], use_api, _emit)
        if not nav_ok:
            _emit("rover_error", phase="nav_to_fire", error="navigation failed")
            _cleanup(lease)
            return {"status": "nav_fire_failed", "cycle": cycle, "log": log}

        _emit("rover_fire_extinguished", cycle=cycle)

    # ---- 3. Возврат на старт ----
    _emit("rover_navigate", phase="return_home", from_cell=fire,
          to_cell=init)
    nav_ok = _navigate_to(client, lease, bridge_client,
                          init[0], init[1], use_api, _emit)
    if not nav_ok:
        _emit("rover_error", phase="nav_return", error="navigation failed")

    _cleanup(lease)
    _emit("rover_done", status="completed",
          fire_cell=fire, count=count, init_cell=init)
    return {"status": "completed", "fire_cell": fire, "count": count,
            "init_cell": init, "log": log}


def _navigate_to(
    client,
    lease,
    bridge_client,
    col: int,
    row: int,
    use_api: bool,
    emit,
) -> bool:
    """Отправить ровер в клетку [col, row] и синхронно ждать succeeded."""
    if use_api and client and lease:
        return _navigate_api(client, lease, col, row, emit)
    if bridge_client:
        return _navigate_bridge(bridge_client, [col, row], emit)
    emit({"kind": "rover_skip", "reason": "no_api_no_bridge"})
    return False


def _navigate_api(client, lease, col: int, row: int, emit) -> bool:
    from rover_control_client import calibrated_cell, RoverApiError
    try:
        cell_pose = calibrated_cell(client, col, row, 0.0)
        client.post(
            "/v1/navigation/goal",
            {
                "request_id": request_id(),
                "map_label": cell_pose["map_label"],
                "x": cell_pose["x"],
                "y": cell_pose["y"],
                "yaw_deg": cell_pose["yaw_deg"],
                "replace_active": True,
            },
            lease.lease_id,
        )
    except RoverApiError as e:
        emit({"kind": "rover_error", "phase": "goal", "cell": [col, row],
              "error": str(e)})
        return False
    except Exception as e:
        emit({"kind": "rover_error", "phase": "goal", "cell": [col, row],
              "error": str(e)})
        return False

    terminal = {"succeeded", "aborted", "canceled", "rejected", "error"}
    timeout_sec = 300.0
    deadline = time.monotonic() + timeout_sec
    last_state = ""
    while time.monotonic() < deadline:
        try:
            lease.check()
            status = client.get("/v1/navigation/status")
            state = status.get("state", "")
            if state != last_state:
                emit({"kind": "rover_nav_status", "state": state,
                      "cell": [col, row],
                      "distance": status.get("distance_remaining"),
                      "message": status.get("message", "")})
                last_state = state
            if state in terminal:
                return state == "succeeded"
        except Exception as e:
            emit({"kind": "rover_error", "phase": "nav_poll",
                  "error": str(e)})
            return False
        time.sleep(0.5)

    emit({"kind": "rover_error", "phase": "nav_timeout", "cell": [col, row]})
    return False


def _navigate_bridge(bridge_client, cell: list[int], emit) -> bool:
    try:
        bridge_client.move(cell)
        emit({"kind": "rover_bridge_move", "cell": cell})
        return True
    except Exception as e:
        emit({"kind": "rover_error", "phase": "bridge_move",
              "cell": cell, "error": str(e)})
        return False


def _dwell_with_led(bridge_client, api_mode: bool,
                    seconds: float, emit) -> None:
    """3-секундная пауза с LED-миганием (забор воды).

    Для мок-ровера: используем bridge.dwell() который мигает LED.
    Для реального API: time.sleep(3.0) — LED штатно отображает остановку.
    """
    if bridge_client:
        try:
            emit({"kind": "rover_dwell_led", "action": "blink_start",
                  "seconds": seconds})
            bridge_client.dwell(seconds, led="blink")
            emit({"kind": "rover_dwell_led", "action": "blink_end",
                  "seconds": seconds})
            return
        except Exception as e:
            emit({"kind": "rover_error", "phase": "dwell_led",
                  "error": str(e)})

    # fallback для реального API: простая пауза
    emit({"kind": "rover_dwell", "action": "sleep", "seconds": seconds})
    time.sleep(seconds)
    emit({"kind": "rover_dwell", "action": "sleep_done", "seconds": seconds})


def _cleanup(lease) -> None:
    if lease is not None:
        try:
            lease.__exit__(None, None, None)
        except Exception:
            pass