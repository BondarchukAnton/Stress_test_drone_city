#!/usr/bin/env python3
"""observer.py — наблюдатель: отказоустойчивый облёт всех дронов через ThreadPoolExecutor.

Поток на дрон — изоляция ошибок. Один упавший дрон не валит миссию.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from pathlib import Path
from typing import Any

from drone_api import (
    DroneHandle, DroneTelemetry,
    _aruco_to_cell, _find_home_cell, init_drone,
)
from city_mission import (
    _parse_vlm_response, _fire_prompt, _detect_in_map,
    DroneResult, MissionConfig,
)
from mission_journal import journal_record as _jr

_ARUCO_LOCK_SEC = float(os.environ.get("ARUCO_LOCK_SEC", "18"))
_BODY_TAKEOFF = float(os.environ.get("BODY_TAKEOFF", "2.0"))
_OBSERVER_HOVER_SEC = float(os.environ.get("OBSERVER_HOVER_SEC", "20"))
_GRID_W = int(os.environ.get("GRID_W", "6"))
_GRID_H = int(os.environ.get("GRID_H", "6"))
_CELL_SIZE_M = float(os.environ.get("CELL_SIZE_M", "0.8"))
_ORIGIN_X = float(os.environ.get("FIELD_ORIGIN_X", "-2.0"))
_ORIGIN_Y = float(os.environ.get("FIELD_ORIGIN_Y", "-2.0"))
_PING_TIMEOUT = float(os.environ.get("PING_TIMEOUT", "10"))
_TAKEOFF_RETRIES = int(os.environ.get("TAKEOFF_RETRIES", "2"))


def _vlm_analyze(brain, bb_path: Path, agent_id: str,
                 result: DroneResult, scenario_map: dict,
                 emit, errors_log: list[str]) -> None:
    if result.error or not result.photo_path:
        return
    full_path = bb_path / result.photo_path.lstrip("/")
    try:
        with open(full_path, "rb") as f:
            image_png = f.read()
    except (OSError, FileNotFoundError):
        result.error = f"cannot read {result.photo_path}"
        return
    if not image_png:
        result.error = "empty image"
        return

    system = _fire_prompt()
    user = f"Дрон {agent_id} над клеткой [{result.cell[0]},{result.cell[1]}]. Проанализируй кадр."
    vlm_ok = False

    try:
        if brain and not getattr(brain, "is_mock", False):
            vlm_text = brain.see(system, user, image_png,
                                 max_tokens=200, log_context="observer")
            vlm_ok = True
        else:
            det = _detect_in_map(scenario_map, result.cell)
            vlm_text = json.dumps(det) if det else (
                '{"fire":false,"count":0,"confidence":0.5,"direction":"none"}'
            )
    except Exception as e:
        msg = f"[VLM Fallback] {agent_id}: primary VLM failed ({e}), switching to local CV..."
        errors_log.append(msg)
        if emit:
            emit({"kind": "vlm_fallback", "from": agent_id,
                  "cell": result.cell, "error": str(e)})

    if not vlm_ok:
        from fallback_cv import fallback_vlm_result
        vlm_text = json.dumps(fallback_vlm_result(str(full_path), result.cell))

    if vlm_text:
        parsed = _parse_vlm_response(vlm_text, result.cell)
        result.fire_detected = parsed["fire"]
        result.fire_cell = parsed["fire_cell"]
        result.confidence = parsed["confidence"]
        result.direction = parsed["direction"]
        result.summary = parsed["summary"]
        _jr("vlm_detection", agent=agent_id,
            cell=result.cell, fire=result.fire_detected,
            fire_cell=result.fire_cell, confidence=result.confidence,
            direction=result.direction, fallback=not vlm_ok)
        if emit:
            emit({"kind": "vlm_result", "from": agent_id,
                  "cell": result.cell, "fire": result.fire_detected,
                  "fire_cell": result.fire_cell,
                  "confidence": result.confidence,
                  "direction": result.direction,
                  "fallback": not vlm_ok})


def _fly_one_drone(agent_id: str, brain, bb_root: str,
                   scenario_map: dict, emit, errors_log: list[str],
                   diagnostics: dict) -> dict:
    """Полёт и VLM для одного дрона с полной изоляцией ошибок."""
    bb_path = Path(bb_root)
    result = DroneResult(agent_id=agent_id, bridge_url="sverk")
    diagnostics[agent_id] = "unknown"
    in_air = False

    grid = scenario_map.get("grid", [])
    grid_w = len(grid[0]) if grid else _GRID_W
    grid_h = len(grid) if grid else _GRID_H
    cell_size_m = float(scenario_map.get("cell_size_m", _CELL_SIZE_M))

    drone = init_drone(agent_id)

    # --- проверка связи ---
    if not drone.is_available:
        msg = f"[WARNING] {agent_id}: sverk_interfaces not available"
        errors_log.append(msg)
        diagnostics[agent_id] = "unreachable"
        emit({"kind": "drone_error", "from": agent_id,
              "phase": "ping", "error": "sverk_unavailable"})
        return _diagnostics_for(agent_id, result, errors_log, diagnostics)

    diagnostics[agent_id] = "ok"

    # --- взлёт с повторами ---
    for attempt in range(1, _TAKEOFF_RETRIES + 1):
        try:
            drone.takeoff(altitude=_BODY_TAKEOFF)
            in_air = True
            if emit:
                emit({"kind": "drone_takeoff", "from": agent_id,
                      "altitude": _BODY_TAKEOFF, "attempt": attempt})
            break
        except Exception as e:
            msg = f"[WARNING] {agent_id}: takeoff attempt {attempt}/{_TAKEOFF_RETRIES} failed: {e}"
            errors_log.append(msg)
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "takeoff", "error": str(e), "attempt": attempt})
            if attempt >= _TAKEOFF_RETRIES:
                diagnostics[agent_id] = "takeoff_failed"
                result.error = "takeoff_failed after retries"
                return _diagnostics_for(agent_id, result, errors_log, diagnostics)
            time.sleep(2)

    try:
        # --- ArUco lock ---
        time.sleep(_ARUCO_LOCK_SEC)

        # --- home_cell ---
        telemetry = drone.get_telemetry()
        home_cell = _find_home_cell(telemetry.x, telemetry.y,
                                    grid_w, grid_h, cell_size_m,
                                    _ORIGIN_X, _ORIGIN_Y)
        result.cell = home_cell
        if emit:
            emit({"kind": "home_cell", "from": agent_id,
                  "cell": home_cell,
                  "aruco_x": telemetry.x, "aruco_y": telemetry.y})

        # --- зависание ---
        time.sleep(_OBSERVER_HOVER_SEC)

        # --- снимок ---
        try:
            photo_path = drone.take_picture()
            result.photo_path = photo_path
            if emit:
                emit({"kind": "drone_photo", "from": agent_id,
                      "cell": home_cell, "path": photo_path})
        except Exception as e:
            msg = f"[WARNING] {agent_id}: photo failed: {e}"
            errors_log.append(msg)
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "photo", "error": str(e)})

        # --- VLM ---
        if emit:
            emit({"kind": "observer_phase", "from": agent_id,
                  "phase": "vlm_analysis"})
        _vlm_analyze(brain, bb_path, agent_id, result, scenario_map, emit, errors_log)

    except Exception as e:
        msg = f"[ERROR] {agent_id}: flight failed: {e}"
        errors_log.append(msg)
        diagnostics[agent_id] = "flight_error"
        result.error = str(e)
        if emit:
            emit({"kind": "drone_error", "from": agent_id,
                  "phase": "flight", "error": str(e)})
    finally:
        if in_air:
            try:
                drone.land()
                if emit:
                    emit({"kind": "drone_land", "from": agent_id})
            except Exception as e:
                msg = f"[ERROR] {agent_id}: emergency land failed: {e}"
                errors_log.append(msg)

    return _diagnostics_for(agent_id, result, errors_log, diagnostics)


def run_observer_parallel(
    agent_ids: list[str],
    brain,
    bb_root: str,
    scenario_map: dict,
    config: MissionConfig | None = None,
    emit=None,
) -> dict:
    """Параллельный облёт всех дронов с изоляцией ошибок."""
    diagnostics: dict[str, str] = {}
    errors_log: list[str] = []
    all_results: dict[str, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _fly_one_drone, aid, brain, bb_root, scenario_map,
                emit, errors_log, diagnostics,
            ): aid
            for aid in agent_ids
        }
        for future in concurrent.futures.as_completed(futures):
            aid = futures[future]
            try:
                r = future.result()
                dr = r.get("drone_results", {}).get(aid, {})
                all_results[aid] = dr
            except Exception as e:
                diagnostics[aid] = "thread_crashed"
                errors_log.append(f"[ERROR] {aid}: thread crashed: {e}")

    fire_cell = None
    best_conf = 0.0
    for aid, dr in all_results.items():
        if dr.get("fire") and dr.get("confidence", 0) > best_conf:
            fire_cell = dr.get("fire_cell")
            best_conf = dr.get("confidence", 0)

    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "fire_cell": fire_cell,
        "drone_results": all_results,
        "diagnostics": diagnostics,
        "errors_log": errors_log,
    }


def run_observer(drone: DroneHandle, brain, bb_root: str,
                 scenario_map: dict,
                 config: MissionConfig | None = None,
                 emit=None) -> dict:
    """Одиночный облёт (для обратной совместимости)."""
    errors_log: list[str] = []
    diagnostics: dict[str, str] = {}
    r = _fly_one_drone(drone.agent_id, brain, bb_root, scenario_map,
                       emit, errors_log, diagnostics)
    dr = r.get("drone_results", {}).get(drone.agent_id, {})
    fire_cell = dr.get("fire_cell") if dr.get("fire") else None
    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "agent_id": drone.agent_id,
        "drone_results": {drone.agent_id: dr},
    }


def _result_dict(agent_id: str, result: DroneResult) -> dict:
    return {
        "status": "completed" if result.fire_detected else "no_fire_detected",
        "agent_id": agent_id,
        "drone_results": {
            agent_id: {
                "cell": result.cell,
                "photo_path": result.photo_path,
                "fire": result.fire_detected,
                "fire_cell": result.fire_cell,
                "confidence": result.confidence,
                "direction": result.direction,
                "summary": result.summary,
                "error": result.error,
            }
        },
    }


def _diagnostics_for(agent_id: str, result: DroneResult,
                     errors: list[str], diag: dict) -> dict:
    r = _diagnostics_for(agent_id, result, errors_log, diagnostics)
    r["diagnostics"] = diag
    r["errors_log"] = errors
    return r