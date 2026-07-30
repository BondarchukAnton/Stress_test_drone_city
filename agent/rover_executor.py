#!/usr/bin/env python3
"""rover_executor.py — отказоустойчивое управление ровером.

Все ошибки навигации перехватываются, миссия не падает.
Возвращает честный отчёт: сколько циклов выполнено, на каком шаге сбой.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from mission_journal import journal_record as _jr, journal_freeze as _jfreeze


def request_id() -> str:
    return str(uuid.uuid4())


def _make_rover_client(api_url: str, timeout: float = 300.0):
    from rover_control_client import RoverClient
    return RoverClient(api_url, timeout=timeout)


def _make_bridge_client(bridge_url: str):
    from bridge_client import BridgeClient
    return BridgeClient(bridge_url, timeout=120.0)


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
    water = list(water_cell) if water_cell else [1, 3]
    init = list(init_cell) if init_cell else [1, 1]
    fire = list(target_cell) if target_cell else [0, 0]

    log: list[dict] = []
    errors_log: list[str] = []

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

    client = None
    lease = None
    bridge_client = None

    # ---- 1. Инициализация ----
    if use_api:
        try:
            client = _make_rover_client(rover_api_url)
            from rover_control_client import ControlLease, RoverApiError
            cid = f"dc-{uuid.uuid4().hex[:8]}"
            lease = ControlLease(client, cid)
            lease.__enter__()
            _emit("rover_lease", acquired=True)

            from rover_control_client import calibrated_cell
            cell_pose = calibrated_cell(client, init[0], init[1], 0.0)
            client.post(
                "/v1/localization/initial-pose",
                {"request_id": request_id(),
                 "map_label": cell_pose["map_label"],
                 "x": cell_pose["x"], "y": cell_pose["y"],
                 "yaw_deg": cell_pose["yaw_deg"]},
                lease.lease_id,
            )
            _emit("rover_initial_cell", cell=init)
            client.post("/v1/stop/clear", {}, lease.lease_id)
            _emit("rover_clear", ok=True)
        except RoverApiError as e:
            errors_log.append(f"[ERROR] rover init: {e}")
            _emit("rover_error", phase="init", error=str(e))
            _cleanup(lease)
            return {"status": "init_error", "rover_status": "failed",
                    "extinguished_count": 0, "error": str(e),
                    "errors_log": errors_log, "log": log}
        except Exception as e:
            errors_log.append(f"[ERROR] rover init: {e}")
            _cleanup(lease)
            return {"status": "init_error", "rover_status": "failed",
                    "extinguished_count": 0, "error": str(e),
                    "errors_log": errors_log, "log": log}

    if use_bridge:
        try:
            bridge_client = _make_bridge_client(rover_bridge_url)
        except Exception as e:
            errors_log.append(f"[WARNING] rover bridge init failed: {e}")

    # ---- 2. Цикл тушения ----
    extinguished = 0
    failed_step = None

    for cycle in range(1, count + 1):
        _emit("rover_cycle", cycle=cycle, of=count)

        # --- вода ---
        try:
            _jr("rover_navigation", agent="rover", phase="to_water",
                cell=water, cycle=cycle)
            nav_ok = _navigate_to(client, lease, bridge_client,
                                  water[0], water[1], use_api, _emit)
        except Exception as e:
            errors_log.append(f"[ERROR] rover nav to water (cycle {cycle}): {e}")
            failed_step = f"nav_to_water_cycle_{cycle}"
            _try_cancel(client, lease, _emit)
            break
        if not nav_ok:
            failed_step = f"nav_to_water_cycle_{cycle}"
            _try_cancel(client, lease, _emit)
            break

        try:
            _dwell_with_led(bridge_client, use_api, dwell_water_sec, _emit)
        except Exception as e:
            errors_log.append(f"[WARNING] rover dwell water: {e}")

        # --- огонь ---
        try:
            _jr("rover_navigation", agent="rover", phase="to_fire",
                cell=fire, cycle=cycle)
            nav_ok = _navigate_to(client, lease, bridge_client,
                                  fire[0], fire[1], use_api, _emit)
            if nav_ok:
                _jr("rover_reached_fire", agent="rover",
                    cell=fire, cycle=cycle)
                _jfreeze()
        except Exception as e:
            errors_log.append(f"[ERROR] rover nav to fire (cycle {cycle}): {e}")
            failed_step = f"nav_to_fire_cycle_{cycle}"
            _try_cancel(client, lease, _emit)
            break
        if not nav_ok:
            failed_step = f"nav_to_fire_cycle_{cycle}"
            _try_cancel(client, lease, _emit)
            break

        extinguished += 1
        _emit("rover_fire_extinguished", cycle=cycle)

    # ---- 3. Возврат на старт (best-effort) ----
    try:
        _navigate_to(client, lease, bridge_client,
                     init[0], init[1], use_api, _emit)
    except Exception as e:
        errors_log.append(f"[WARNING] rover return to start: {e}")

    _cleanup(lease)
    _emit("rover_done", status="completed" if extinguished == count else "partial",
          fire_cell=fire, extinguished=extinguished, target_count=count)
    return {
        "status": "completed" if extinguished == count else "partial_success",
        "rover_status": "failed" if failed_step else "ok",
        "extinguished_count": extinguished,
        "target_count": count,
        "failed_step": failed_step,
        "errors_log": errors_log,
        "log": log,
    }


def _navigate_to(client, lease, bridge_client,
                 col: int, row: int, use_api: bool, emit) -> bool:
    if use_api and client and lease:
        return _navigate_api(client, lease, col, row, emit)
    if bridge_client:
        return _navigate_bridge(bridge_client, [col, row], emit)
    return False


def _navigate_api(client, lease, col: int, row: int, emit) -> bool:
    from rover_control_client import calibrated_cell, RoverApiError
    try:
        cell_pose = calibrated_cell(client, col, row, 0.0)
        client.post(
            "/v1/navigation/goal",
            {"request_id": request_id(),
             "map_label": cell_pose["map_label"],
             "x": cell_pose["x"], "y": cell_pose["y"],
             "yaw_deg": cell_pose["yaw_deg"],
             "replace_active": True},
            lease.lease_id,
        )
    except RoverApiError as e:
        emit({"kind": "rover_error", "phase": "goal",
              "cell": [col, row], "error": str(e)})
        return False
    except Exception as e:
        emit({"kind": "rover_error", "phase": "goal",
              "cell": [col, row], "error": str(e)})
        return False

    terminal = {"succeeded", "aborted", "canceled", "rejected", "error"}
    deadline = time.monotonic() + 300.0
    while time.monotonic() < deadline:
        try:
            lease.check()
            status = client.get("/v1/navigation/status")
            state = status.get("state", "")
            emit({"kind": "rover_nav_status", "state": state,
                  "cell": [col, row],
                  "distance": status.get("distance_remaining")})
            if state in terminal:
                return state == "succeeded"
        except Exception as e:
            emit({"kind": "rover_error", "phase": "nav_poll", "error": str(e)})
            return False
        time.sleep(0.5)
    return False


def _navigate_bridge(bridge_client, cell: list[int], emit) -> bool:
    try:
        bridge_client.move(cell)
        return True
    except Exception as e:
        emit({"kind": "rover_error", "phase": "bridge_move",
              "cell": cell, "error": str(e)})
        return False


def _try_cancel(client, lease, emit) -> None:
    try:
        if client and lease:
            from rover_control_client import RoverApiError
            client.post("/v1/navigation/cancel",
                        {"request_id": request_id()}, lease.lease_id)
    except Exception:
        pass


def _dwell_with_led(bridge_client, api_mode: bool,
                    seconds: float, emit) -> None:
    if bridge_client:
        try:
            bridge_client.dwell(seconds, led="blink")
            return
        except Exception:
            pass
    time.sleep(seconds)


def _cleanup(lease) -> None:
    if lease is not None:
        try:
            lease.__exit__(None, None, None)
        except Exception:
            pass