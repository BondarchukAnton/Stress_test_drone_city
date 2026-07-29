#!/usr/bin/env python3
"""observer.py — режим наблюдателя: взлёт, 20-секундное зависание, фото, посадка, VLM.

Каждый дрон висит над своей стартовой клеткой, делает один снимок, садится.
Затем все снимки анализируются VLM. Файлы НЕ удаляются.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from city_mission import (
    _parse_vlm_response,
    _fire_prompt,
    _detect_in_map,
    _split_zones,
    _make_bridge,
    DroneResult,
    MissionConfig,
)


def _read_scenario_map(fixtures: str, scenario: str) -> dict:
    p = Path(fixtures) / scenario / "map.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _vlm_analyze_one(
    brain,
    bb_path: Path,
    agent_id: str,
    result: DroneResult,
    scenario_map: dict,
    emit,
) -> None:
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
    try:
        if brain:
            vlm_text = brain.see(system, user, image_png,
                                 max_tokens=200, log_context="observer")
        else:
            det = _detect_in_map(scenario_map, result.cell)
            vlm_text = json.dumps(det) if det else '{"fire":false,"count":0,"confidence":0.5,"direction":"none"}'
    except Exception as e:
        result.error = f"VLM: {e}"
        return

    if vlm_text:
        parsed = _parse_vlm_response(vlm_text, result.cell)
        result.fire_detected = parsed["fire"]
        result.fire_cell = parsed["fire_cell"]
        result.confidence = parsed["confidence"]
        result.direction = parsed["direction"]
        result.summary = parsed["summary"]
        if emit:
            emit({"kind": "vlm_result", "from": agent_id,
                  "cell": result.cell, "fire": result.fire_detected,
                  "fire_cell": result.fire_cell,
                  "confidence": result.confidence,
                  "direction": result.direction})


def run_observer(
    drone_bridges: dict[str, str],
    brain,
    bb_root: str,
    scenario_map: dict,
    config: MissionConfig | None = None,
    emit=None,
) -> dict:
    cfg = config or MissionConfig()
    scouts = list(drone_bridges.keys())
    zones = _split_zones(scenario_map, scouts)
    bb_path = Path(bb_root)
    results: dict[str, DroneResult] = {}

    # ---- 1. Поднять дроны ----
    if emit:
        emit({"kind": "observer_phase", "phase": "takeoff",
              "altitude": cfg.hover_altitude})
    for agent_id, bridge_url in drone_bridges.items():
        result = DroneResult(agent_id=agent_id, bridge_url=bridge_url)
        try:
            bridge = _make_bridge(bridge_url)
            bridge.takeoff()
            time.sleep(cfg.takeoff_wait)
            if emit:
                emit({"kind": "drone_takeoff", "from": agent_id,
                      "altitude": cfg.hover_altitude})
        except Exception as e:
            result.error = f"takeoff: {e}"
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "takeoff", "error": str(e)})
        results[agent_id] = result

    # ---- 2. Зависание 20 секунд ----
    hover_secs = float(os.environ.get("OBSERVER_HOVER_SEC", "20"))
    if emit:
        emit({"kind": "observer_phase", "phase": "hover",
              "seconds": hover_secs})
    time.sleep(hover_secs)

    # ---- 3. Фотосъёмка ----
    if emit:
        emit({"kind": "observer_phase", "phase": "photograph"})
    for agent_id, zone_cells in zones.items():
        if not zone_cells:
            continue
        result = results[agent_id]
        if result.error:
            continue
        cell = zone_cells[0]
        result.cell = list(cell)
        try:
            bridge = _make_bridge(result.bridge_url)
            resp = bridge.photograph_cell(cell)
            image_path = resp.get("image_path", "")
            result.photo_path = str(image_path)
            if emit:
                emit({"kind": "drone_photo", "from": agent_id,
                      "cell": list(cell), "path": result.photo_path})
        except Exception as e:
            result.error = f"photo: {e}"
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "photo", "error": str(e)})

    # ---- 4. Посадка ----
    if emit:
        emit({"kind": "observer_phase", "phase": "land"})
    for agent_id, result in results.items():
        if result.error:
            continue
        try:
            bridge = _make_bridge(result.bridge_url)
            bridge.land()
            if emit:
                emit({"kind": "drone_land", "from": agent_id})
        except Exception as e:
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "land", "error": str(e)})

    # ---- 5. VLM-анализ ----
    if emit:
        emit({"kind": "observer_phase", "phase": "vlm_analysis"})
    for agent_id, result in results.items():
        _vlm_analyze_one(brain, bb_path, agent_id, result, scenario_map, emit)

    # ---- 6. Итоги ----
    fire_cell = None
    best_conf = 0.0
    for result in results.values():
        if result.fire_detected and result.fire_cell and result.confidence > best_conf:
            fire_cell = result.fire_cell
            best_conf = result.confidence

    if emit:
        emit({"kind": "observer_done", "fire_cell": fire_cell,
              "confidence": best_conf})

    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "fire_cell": fire_cell,
        "drone_results": {
            aid: {
                "cell": r.cell,
                "photo_path": r.photo_path,
                "fire": r.fire_detected,
                "fire_cell": r.fire_cell,
                "confidence": r.confidence,
                "direction": r.direction,
                "summary": r.summary,
                "error": r.error,
            }
            for aid, r in results.items()
        },
    }