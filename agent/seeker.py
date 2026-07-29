#!/usr/bin/env python3
"""seeker.py — режим ищейки: пошаговый обход 3×3 клеток вокруг стартовой позиции.

Каждый дрон обходит 9 клеток (стартовая + 8 соседних) по спирали,
фотографирует каждую, садится. Затем все снимки анализируются VLM.
Файлы НЕ удаляются.

Маршрут одного дрона (старт [x, y]):
  [x,   y  ]  старт
  [x,   y+1]  вперёд
  [x-1, y+1]  влево
  [x-1, y  ]  назад
  [x-1, y-1]  назад
  [x,   y-1]  вправо
  [x+1, y-1]  вправо
  [x+1, y  ]  вперёд
  [x+1, y+1]  вперёд
  [x,   y  ]  возврат
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
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


_WALK_PATTERN = [
    (0, 0),    # стартовая клетка
    (0, 1),    # вперёд
    (-1, 1),   # влево
    (-1, 0),   # назад
    (-1, -1),  # назад
    (0, -1),   # вправо
    (1, -1),   # вправо
    (1, 0),    # вперёд
    (1, 1),    # вперёд
    (0, 0),    # возврат в стартовую
]


@dataclass
class CellPhoto:
    cell: list[int]
    photo_path: str
    vlm_result: dict[str, Any] | None = None


def _vlm_analyze_one_photo(
    brain,
    bb_path: Path,
    agent_id: str,
    cell: list[int],
    photo_path: str,
    scenario_map: dict,
    emit,
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
    try:
        if brain:
            vlm_text = brain.see(system, user, image_png,
                                 max_tokens=200, log_context="seeker")
        else:
            det = _detect_in_map(scenario_map, cell)
            vlm_text = json.dumps(det) if det else '{"fire":false,"count":0,"confidence":0.5,"direction":"none"}'
    except Exception:
        return None

    if not vlm_text:
        return None

    parsed = _parse_vlm_response(vlm_text, cell)
    if emit:
        emit({"kind": "vlm_result", "from": agent_id,
              "cell": list(cell), "fire": parsed["fire"],
              "fire_cell": parsed["fire_cell"],
              "confidence": parsed["confidence"],
              "direction": parsed["direction"]})
    return parsed


def run_seeker(
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

    # стартовые клетки дронов
    start_cells: dict[str, list[int]] = {}
    for agent_id, zone_cells in zones.items():
        start_cells[agent_id] = list(zone_cells[0]) if zone_cells else [0, 0]

    # ---- 1. Взлёт ----
    if emit:
        emit({"kind": "seeker_phase", "phase": "takeoff",
              "altitude": cfg.hover_altitude})
    for agent_id, bridge_url in drone_bridges.items():
        try:
            bridge = _make_bridge(bridge_url)
            bridge.takeoff()
            time.sleep(cfg.takeoff_wait)
            if emit:
                emit({"kind": "drone_takeoff", "from": agent_id,
                      "altitude": cfg.hover_altitude})
        except Exception as e:
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "takeoff", "error": str(e)})

    # ---- 2. Пошаговый обход ----
    all_photos: dict[str, list[CellPhoto]] = {aid: [] for aid in scouts}

    for step_idx, (dx, dy) in enumerate(_WALK_PATTERN):
        if emit:
            emit({"kind": "seeker_step", "step": step_idx,
                  "dx": dx, "dy": dy})
        for agent_id in scouts:
            bridge_url = drone_bridges.get(agent_id)
            if not bridge_url:
                continue
            sx, sy = start_cells[agent_id]
            target = [sx + dx, sy + dy]
            try:
                bridge = _make_bridge(bridge_url)
                bridge.move(target)
                resp = bridge.photograph_cell(target)
                photo_path = str(resp.get("image_path", ""))
                photo = CellPhoto(cell=list(target), photo_path=photo_path)
                all_photos[agent_id].append(photo)
                if emit:
                    emit({"kind": "seeker_photo", "from": agent_id,
                          "cell": list(target), "path": photo_path,
                          "step": step_idx})
            except Exception as e:
                if emit:
                    emit({"kind": "drone_error", "from": agent_id,
                          "phase": f"seek_step_{step_idx}",
                          "cell": list(target), "error": str(e)})

    # ---- 3. Посадка ----
    if emit:
        emit({"kind": "seeker_phase", "phase": "land"})
    for agent_id, bridge_url in drone_bridges.items():
        try:
            bridge = _make_bridge(bridge_url)
            bridge.land()
            if emit:
                emit({"kind": "drone_land", "from": agent_id})
        except Exception as e:
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "land", "error": str(e)})

    # ---- 4. VLM-анализ ----
    if emit:
        emit({"kind": "seeker_phase", "phase": "vlm_analysis"})

    fire_cell = None
    best_conf = 0.0

    for agent_id, photos in all_photos.items():
        for photo in photos:
            if not photo.photo_path:
                continue
            parsed = _vlm_analyze_one_photo(
                brain, bb_path, agent_id,
                photo.cell, photo.photo_path,
                scenario_map, emit,
            )
            photo.vlm_result = parsed
            if parsed and parsed["fire"] and parsed["confidence"] > best_conf:
                fire_cell = parsed["fire_cell"]
                best_conf = parsed["confidence"]

    if emit:
        emit({"kind": "seeker_done", "fire_cell": fire_cell,
              "confidence": best_conf,
              "total_photos": sum(len(p) for p in all_photos.values())})

    return {
        "status": "completed" if fire_cell else "no_fire_detected",
        "fire_cell": fire_cell,
        "drone_results": {
            aid: {
                "start_cell": start_cells.get(aid),
                "photos": [
                    {
                        "cell": p.cell,
                        "photo_path": p.photo_path,
                        "fire": p.vlm_result["fire"] if p.vlm_result else False,
                        "fire_cell": p.vlm_result["fire_cell"] if p.vlm_result else None,
                        "confidence": p.vlm_result["confidence"] if p.vlm_result else 0.0,
                        "direction": p.vlm_result["direction"] if p.vlm_result else "none",
                        "summary": p.vlm_result.get("summary", "") if p.vlm_result else "",
                    }
                    for p in photos
                ],
            }
            for aid, photos in all_photos.items()
        },
    }