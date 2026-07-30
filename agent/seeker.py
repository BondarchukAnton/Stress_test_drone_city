#!/usr/bin/env python3
"""seeker.py — ищейка: отказоустойчивый обход 3×3 всех дронов через ThreadPoolExecutor.

Поток на дрон — изоляция ошибок.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drone_api import (
    DroneHandle, DroneTelemetry,
    _cell_to_aruco, _find_home_cell, init_drone,
)
from city_mission import (
    _parse_vlm_response, _fire_prompt, _detect_in_map, MissionConfig,
)
from mission_journal import journal_record as _jr

_ARUCO_LOCK_SEC = float(os.environ.get("ARUCO_LOCK_SEC", "18"))
_BODY_TAKEOFF = float(os.environ.get("BODY_TAKEOFF", "2.0"))
_GRID_W = int(os.environ.get("GRID_W", "6"))
_GRID_H = int(os.environ.get("GRID_H", "6"))
_CELL_SIZE_M = float(os.environ.get("CELL_SIZE_M", "0.8"))
_ORIGIN_X = float(os.environ.get("FIELD_ORIGIN_X", "-2.0"))
_ORIGIN_Y = float(os.environ.get("FIELD_ORIGIN_Y", "-2.0"))
_TAKEOFF_RETRIES = int(os.environ.get("TAKEOFF_RETRIES", "2"))

_WALK_PATTERN = [
    (0, 0), (0, 1), (-1, 1), (-1, 0), (-1, -1),
    (0, -1), (1, -1), (1, 0), (1, 1), (0, 0),
]


@dataclass
class CellPhoto:
    cell: list[int]
    photo_path: str
    vlm_result: dict[str, Any] | None = None


def _vlm_analyze_photo(brain, bb_path: Path, agent_id: str,
                       cell: list[int], photo_path: str,
                       scenario_map: dict, emit, errors_log: list[str],
                       ) -> dict[str, Any] | None:
    full_path = bb_path / photo_path.lstrip("/")
    try:
        with open(full_path, "rb") as f:
            image_png = f.read()
    except (OSError, FileNotFoundError):
        return None
    if not image_png:
        return None

    system = _fire_prompt()
    user = f"Дрон {agent_id} над клеткой [{cell[0]},{cell[1]}]. Проанализируй кадр."
    vlm_ok = False

    try:
        if brain and not getattr(brain, "is_mock", False):
            vlm_text = brain.see(system, user, image_png,
                                 max_tokens=200, log_context="seeker")
            vlm_ok = True
        else:
            det = _detect_in_map(scenario_map, cell)
            vlm_text = json.dumps(det) if det else (
                '{"fire":false,"count":0,"confidence":0.5,"direction":"none"}'
            )
    except Exception as e:
        msg = f"[VLM Fallback] {agent_id} cell={cell}: VLM failed ({e}), switching to CV"
        errors_log.append(msg)
        if emit:
            emit({"kind": "vlm_fallback", "from": agent_id,
                  "cell": cell, "error": str(e)})

    if not vlm_ok:
        from fallback_cv import fallback_vlm_result
        vlm_text = json.dumps(fallback_vlm_result(str(full_path), cell))

    if not vlm_text:
        return None

    parsed = _parse_vlm_response(vlm_text, cell)
    _jr("vlm_detection", agent=agent_id,
        cell=cell, fire=parsed["fire"],
        fire_cell=parsed["fire_cell"], confidence=parsed["confidence"],
        direction=parsed["direction"], fallback=not vlm_ok)
    if emit:
        emit({"kind": "vlm_result", "from": agent_id,
              "cell": list(cell), "fire": parsed["fire"],
              "fire_cell": parsed["fire_cell"],
              "confidence": parsed["confidence"],
              "direction": parsed["direction"],
              "fallback": not vlm_ok})
    return parsed


def _fly_one_drone(agent_id: str, brain, bb_root: str,
                   scenario_map: dict, emit, errors_log: list[str],
                   diagnostics: dict) -> dict:
    bb_path = Path(bb_root)
    diagnostics[agent_id] = "unknown"
    in_air = False

    grid = scenario_map.get("grid", [])
    grid_w = len(grid[0]) if grid else _GRID_W
    grid_h = len(grid) if grid else _GRID_H
    cell_size_m = float(scenario_map.get("cell_size_m", _CELL_SIZE_M))

    drone = init_drone(agent_id)

    if not drone.is_available:
        errors_log.append(f"[WARNING] {agent_id}: sverk_interfaces not available")
        diagnostics[agent_id] = "unreachable"
        return _empty_result(agent_id)

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
            errors_log.append(
                f"[WARNING] {agent_id}: takeoff attempt {attempt}/{_TAKEOFF_RETRIES} failed: {e}")
            if attempt >= _TAKEOFF_RETRIES:
                diagnostics[agent_id] = "takeoff_failed"
                return _empty_result(agent_id)
            time.sleep(2)

    try:
        time.sleep(_ARUCO_LOCK_SEC)

        telemetry = drone.get_telemetry()
        home_cell = _find_home_cell(telemetry.x, telemetry.y,
                                    grid_w, grid_h, cell_size_m,
                                    _ORIGIN_X, _ORIGIN_Y)
        if emit:
            emit({"kind": "home_cell", "from": agent_id,
                  "cell": home_cell,
                  "aruco_x": telemetry.x, "aruco_y": telemetry.y})

        all_photos: list[CellPhoto] = []
        for step_idx, (dx, dy) in enumerate(_WALK_PATTERN):
            target_cell = [max(0, min(grid_w - 1, home_cell[0] + dx)),
                           max(0, min(grid_h - 1, home_cell[1] + dy))]
            if emit:
                emit({"kind": "seeker_step", "from": agent_id,
                      "step": step_idx, "cell": target_cell})
            try:
                ax, ay = _cell_to_aruco(target_cell[0], target_cell[1],
                                        _ORIGIN_X, _ORIGIN_Y, cell_size_m)
                drone.navigate_wait(x=ax, y=ay, z=_BODY_TAKEOFF,
                                    yaw=0.0, speed=0.5, timeout=30.0, tolerance=0.3)
            except Exception as e:
                if emit:
                    emit({"kind": "drone_error", "from": agent_id,
                          "phase": f"seek_step_{step_idx}",
                          "cell": target_cell, "error": str(e)})
                continue

            try:
                photo_path = drone.take_picture()
            except Exception as e:
                errors_log.append(f"[WARNING] {agent_id}: photo save failed at {target_cell}: {e}")
                photo_path = ""
            all_photos.append(CellPhoto(cell=list(target_cell), photo_path=photo_path))

            if emit and photo_path:
                emit({"kind": "seeker_photo", "from": agent_id,
                      "cell": target_cell, "path": photo_path, "step": step_idx})

    except Exception as e:
        errors_log.append(f"[ERROR] {agent_id}: flight failed: {e}")
        diagnostics[agent_id] = "flight_error"
    finally:
        if in_air:
            try:
                drone.land()
                if emit:
                    emit({"kind": "drone_land", "from": agent_id})
            except Exception as e:
                errors_log.append(f"[ERROR] {agent_id}: emergency land failed: {e}")

    # --- VLM ---
    if emit:
        emit({"kind": "seeker_phase", "from": agent_id,
              "phase": "vlm_analysis", "photos": len(all_photos)})

    fire_cell = None
    best_conf = 0.0
    for photo in all_photos:
        if not photo.photo_path:
            continue
        parsed = _vlm_analyze_photo(brain, bb_path, agent_id,
                                    photo.cell, photo.photo_path,
                                    scenario_map, emit, errors_log)
        photo.vlm_result = parsed
        if parsed and parsed["fire"] and parsed["confidence"] > best_conf:
            fire_cell = parsed["fire_cell"]
            best_conf = parsed["confidence"]

    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "agent_id": agent_id,
        "drone_results": {
            agent_id: {
                "home_cell": home_cell,
                "photos": [
                    {"cell": p.cell, "photo_path": p.photo_path,
                     "fire": p.vlm_result["fire"] if p.vlm_result else False,
                     "fire_cell": p.vlm_result["fire_cell"] if p.vlm_result else None,
                     "confidence": p.vlm_result["confidence"] if p.vlm_result else 0.0,
                     "direction": p.vlm_result["direction"] if p.vlm_result else "none",
                     "summary": p.vlm_result.get("summary", "") if p.vlm_result else ""}
                    for p in all_photos
                ],
            }
        },
    }


def run_seeker_parallel(
    agent_ids: list[str],
    brain, bb_root: str, scenario_map: dict,
    config: MissionConfig | None = None, emit=None,
) -> dict:
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
        for p in dr.get("photos", []):
            if p.get("fire") and p.get("confidence", 0) > best_conf:
                fire_cell = p.get("fire_cell")
                best_conf = p.get("confidence", 0)

    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "fire_cell": fire_cell,
        "drone_results": all_results,
        "diagnostics": diagnostics,
        "errors_log": errors_log,
    }


def run_seeker(drone: DroneHandle, brain, bb_root: str,
               scenario_map: dict,
               config: MissionConfig | None = None, emit=None) -> dict:
    errors_log: list[str] = []
    diagnostics: dict[str, str] = {}
    r = _fly_one_drone(drone.agent_id, brain, bb_root, scenario_map,
                       emit, errors_log, diagnostics)
    dr = r.get("drone_results", {}).get(drone.agent_id, {})
    fire_cell = None
    for p in dr.get("photos", []):
        if p.get("fire"):
            fire_cell = p.get("fire_cell")
    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "agent_id": drone.agent_id,
        "drone_results": {drone.agent_id: dr},
    }


def _empty_result(agent_id: str) -> dict:
    return {
        "status": "error", "agent_id": agent_id,
        "drone_results": {
            agent_id: {"cell": [0, 0], "photo_path": "", "fire": False,
                       "fire_cell": None, "confidence": 0.0,
                       "direction": "none", "summary": "", "error": "unreachable"}
        },
    }