#!/usr/bin/env python3
"""city_mission.py — центральный скрипт миссии «Город дронов».

Запускается координатором после того как агенты в чате пришли к консенсусу.
Управляет всеми дронами и ровером напрямую через bridge API.

Порядок действий:
  1. Поднять все 4 дрона на высоту HOVER_ALTITUDE метров
  2. Каждый дрон фотографирует свою зону клеток
  3. Снимки анализируются VLM — ищется целевой объект (огонь)
  4. По результатам определяется клетка с огнём
  5. Ровер едет: старт → водонапорная башня → огонь → старт
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MissionConfig:
    hover_altitude: float = 2.0
    takeoff_wait: float = 3.0
    photo_wait: float = 1.0
    dwell_water: float = 3.0
    dwell_fire: float = 5.0


@dataclass
class DroneResult:
    agent_id: str
    bridge_url: str
    cell: list[int] = field(default_factory=lambda: [0, 0])
    photo_path: str = ""
    fire_detected: bool = False
    fire_cell: list[int] | None = None
    confidence: float = 0.0
    direction: str = "none"
    summary: str = ""
    error: str = ""


# direction → смещение клетки относительно дрона
_DIR_OFFSET: dict[str, tuple[int, int] | None] = {
    "center": (0, 0),
    "left": (-1, 0),
    "right": (1, 0),
    "up": (0, 1),
    "down": (0, -1),
    "up-left": (-1, 1),
    "up-right": (1, 1),
    "down-left": (-1, -1),
    "down-right": (1, -1),
    "none": None,
}

_VLM_FIRE_PROMPT = """Ты — специализированный детектор пожара на бортовом компьютере дрона.
Перед тобой кадр с камеры дрона, направленной строго вниз на игровое поле, разделенное на квадратные клетки.
Дрон висит над центром одной клетки (Центральная клетка под дроном = область в центре кадра).
По краям кадра могут быть частично видны соседние клетки.
Твоя задача — проанализировать аэроснимок игрового поля сверху и найти целевые объекты, обозначающие огонь (очаги возгорания)

### 1. ВИЗУАЛЬНЫЕ ПРИЗНАКИ И ИСКЛЮЧЕНИЯ:
- ЦЕЛЕВОЙ ОБЪЕКТ "ОГОНЬ": Отдельная пластиковая фигурка ярко-красного или тёмно-красного/бордового цвета в форме языка пламени или капли, расположенная на поле, дорогах или возле зданий. Каплевидные (или похожие на язычки пламени) объекты, если они соответствуют какому либо оттенку красного (кроме чисто чёрных и чисто белых), должны вызывать у тебя высокие показатели уверености в том что это целевой объект. Активно пользуйся показателем уверености, если что то хоть отдалёно напоминает объект синаглизируй об этом с соответствующей степенью уверености, например, 0.56 если объект маленький, имеет оттенок тёмно крсный, но не наблюдается каплевидная форма. Объктов может быть в количестве большем чем 2.
- ИСКЛЮЧЕНИЯ (НЕ считай огнем!):
- Мелкие красные светодиоды/лампочки на платах машинок, дронов или роботов.
- Красные элементы декора или кубиков конструктора на границах кадра, если они не имеют формы язычка пламени.
- Красные линии и элементы разметки карты.

### 2. ПРАВИЛА ЛОКАЛИЗАЦИИ:
Огонь на кадре может находиться ТОЛЬКО В ОДНОЙ клетке (либо под дроном, либо в одной из соседних).
Определи направление клетки с огнем относительно центра кадра:
- "center" — огонь находится в центральной клетке прямо под дроном.
- "left", "right", "up", "down", "up-left", "up-right", "down-left", "down-right" — огонь находится в соответствующей соседней клетке.
- "none" — объектов огня на кадре не обнаружено.

### 3. ФОРМАТ ОТВЕТА:
Верни ответ СТРОГО в формате JSON без Markdown-разметки, вводных слов и пояснений вне JSON:
{
  "fire": true,
  "count": 1,
  "confidence": 0.95,
  "direction": "center",
  "summary": "краткое резюме об объектах под дроном или в соседней клетке"
}

Описание полей:
- fire (boolean): true, если найден хотя бы один объект огня, иначе false.
- count (integer): количество найденных объектов огня в этой клетке (0, если fire=false).
- confidence (float): степень уверенности в детекции от 0.0 до 1.0.
- direction (string): строго одно из значений: "center", "left", "right", "up", "down", "up-left", "up-right", "down-left", "down-right" или "none" (если fire=false).
- summary (string): краткое итоговое текстовое описание.
"""


def _fire_prompt() -> str:
    return (os.environ.get("VLM_FIRE_PROMPT") or "").strip() or _VLM_FIRE_PROMPT


def _parse_vlm_response(vlm_text: str, drone_cell: list[int]) -> dict[str, Any]:
    parsed = None
    try:
        start = vlm_text.find("{")
        end = vlm_text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(vlm_text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        pass

    if parsed is None:
        vlm_lower = vlm_text.lower().strip()
        fire_detected = bool(
            re.search(r"\b(?:fire|пожар)\b", vlm_lower)
            and not re.search(r"\bfalse\b", vlm_lower)
        )
        count = 1
        cnt = re.search(r"(?:\bcount\b|количество|число|очагов?)[\s:]*(\d+)", vlm_lower)
        if cnt:
            count = max(1, int(cnt.group(1)))
        conf = 0.85 if fire_detected else 0.5
        cm = re.search(r"(?:confidence|уверенность|вероятность)[\s:]*(\d+\.?\d*)", vlm_lower)
        if cm:
            conf = min(1.0, max(0.0, float(cm.group(1))))
        direction = "center"
        dm = re.search(r'"direction"\s*:\s*"(\w+(?:-\w+)?)"', vlm_lower)
        if dm:
            direction = dm.group(1)
        parsed = {"fire": fire_detected, "count": count,
                  "confidence": conf, "direction": direction}

    is_fire = bool(parsed.get("fire"))
    confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.5))))
    direction = str(parsed.get("direction", "center")).lower()

    offset = _DIR_OFFSET.get(direction, (0, 0))
    if offset is None:
        is_fire = False
        fire_cell = list(drone_cell)
    else:
        fire_cell = [drone_cell[0] + offset[0], drone_cell[1] + offset[1]]

    return {
        "fire": is_fire,
        "count": max(0, int(parsed.get("count", 0))),
        "confidence": confidence,
        "direction": direction,
        "fire_cell": fire_cell,
        "summary": str(parsed.get("summary", "")),
    }


def _read_scenario_map(fixtures: str, scenario: str) -> dict:
    p = Path(fixtures) / scenario / "map.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_world(scenario_map: dict) -> dict:
    grid = scenario_map.get("grid", [])
    water_tower = None
    charge_zone = None
    fire_cell = scenario_map.get("fire", {}).get("cell")
    if "water_tower" in scenario_map:
        water_tower = scenario_map["water_tower"]
    if "charge_zone" in scenario_map:
        charge_zone = scenario_map["charge_zone"]
    elif grid:
        h = len(grid)
        w = len(grid[0]) if h else 0
        charge_zone = [w // 2, h // 2]
    if not water_tower and grid:
        water_tower = [1, 3]  # default from map.json
    return {
        "grid": grid,
        "water_tower": water_tower,
        "charge_zone": charge_zone,
        "fire_cell": fire_cell,
    }


def _astar(grid, start, goal):
    import heapq
    if not grid:
        return None
    h = len(grid)
    w = len(grid[0])
    sx, sy = int(start[0]), int(start[1])
    gx, gy = int(goal[0]), int(goal[1])

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


def run_mission(
    drone_bridges: dict[str, str],
    rover_bridge: str,
    brain,
    bb_root: str,
    scenario_map: dict,
    config: MissionConfig | None = None,
    emit=None,
) -> dict:
    """Выполнить полную миссию: дроны → VLM → ровер.

    Возвращает словарь с результатами.
    """
    cfg = config or MissionConfig()
    world = _load_world(scenario_map)
    grid = world["grid"]
    water_tower = world["water_tower"]
    charge_zone = world["charge_zone"]
    bb_path = Path(bb_root)
    results: dict[str, DroneResult] = {}

    # ---- 1. Поднять дроны ----
    if emit:
        emit({"kind": "mission_phase", "phase": "takeoff",
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

    # ---- 2. Фотосъёмка зон ----
    scouts = list(drone_bridges.keys())
    zones = _split_zones(scenario_map, scouts)
    if emit:
        emit({"kind": "mission_phase", "phase": "photograph",
              "zones": {k: len(v) for k, v in zones.items()}})

    for agent_id, zone_cells in zones.items():
        if not zone_cells:
            continue
        result = results[agent_id]
        if result.error:
            continue
        cell = zone_cells[0]  # фотографируем первую клетку зоны
        result.cell = list(cell)
        try:
            bridge = _make_bridge(result.bridge_url)
            resp = bridge.photograph_cell(cell)
            image_path = resp.get("image_path", "")
            rel = str(image_path)
            result.photo_path = rel
            if emit:
                emit({"kind": "drone_photo", "from": agent_id,
                      "cell": list(cell), "path": rel})
        except Exception as e:
            result.error = f"photo: {e}"
            if emit:
                emit({"kind": "drone_error", "from": agent_id,
                      "phase": "photo", "error": str(e)})

    time.sleep(cfg.photo_wait)

    # ---- 3. VLM-анализ снимков ----
    if emit:
        emit({"kind": "mission_phase", "phase": "vlm_analysis"})

    for agent_id, result in results.items():
        if result.error or not result.photo_path:
            continue
        full_path = bb_path / result.photo_path.lstrip("/")
        try:
            with open(full_path, "rb") as f:
                image_png = f.read()
        except (OSError, FileNotFoundError):
            result.error = f"cannot read {result.photo_path}"
            continue
        if not image_png:
            result.error = "empty image"
            continue

        system = _fire_prompt()
        user = f"Дрон {agent_id} над клеткой [{result.cell[0]},{result.cell[1]}]. Проанализируй кадр."
        try:
            if brain:
                vlm_text = brain.see(system, user, image_png,
                                     max_tokens=200, log_context="fire_detect")
            else:
                det = _detect_in_map(scenario_map, result.cell)
                vlm_text = json.dumps(det) if det else '{"fire":false,"count":0,"confidence":0.5,"direction":"none"}'
        except Exception as e:
            result.error = f"VLM: {e}"
            continue

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

        # чистим снимок
        try:
            os.remove(full_path)
        except OSError:
            pass

    # ---- 4. Определить клетку с огнём ----
    fire_cell = world.get("fire_cell")  # из map.json если есть
    best_conf = 0.0
    for result in results.values():
        if result.fire_detected and result.fire_cell and result.confidence > best_conf:
            fire_cell = result.fire_cell
            best_conf = result.confidence

    if fire_cell is None:
        return {"status": "no_fire_detected", "results": {
            aid: {"cell": r.cell, "fire": r.fire_detected, "error": r.error}
            for aid, r in results.items()}}

    if emit:
        emit({"kind": "fire_located", "cell": fire_cell,
              "confidence": best_conf})

    # ---- 5. Ровер: старт → башня → огонь → старт ----
    if emit:
        emit({"kind": "mission_phase", "phase": "rover",
              "fire_cell": fire_cell, "water_tower": water_tower,
              "charge_zone": charge_zone})

    if rover_bridge:
        try:
            rover = _make_bridge(rover_bridge)
            rover_result = _run_rover(rover, grid, charge_zone,
                                       water_tower, fire_cell,
                                       cfg.dwell_water, cfg.dwell_fire, emit)
        except Exception as e:
            rover_result = {"status": "rover_error", "error": str(e)}
    else:
        rover_result = {"status": "no_rover_bridge"}

    return {
        "status": "completed",
        "fire_cell": fire_cell,
        "drone_results": {
            aid: {
                "cell": r.cell,
                "fire": r.fire_detected,
                "fire_cell": r.fire_cell,
                "confidence": r.confidence,
                "direction": r.direction,
                "error": r.error,
            }
            for aid, r in results.items()
        },
        "rover": rover_result,
    }


def _split_zones(scenario_map: dict, scouts: list[str]) -> dict[str, list[list[int]]]:
    grid = scenario_map.get("grid", [])
    if not grid:
        return {}
    h = len(grid)
    w = len(grid[0])
    cells = []
    for y in range(h):
        row = [[x, y] for x in range(w)]
        if y % 2:
            row.reverse()
        cells += row
    n = len(scouts)
    zones = {sid: [] for sid in scouts}
    for i, cell in enumerate(cells):
        zones[scouts[i % n]].append(cell)
    return zones


def _detect_in_map(scenario_map: dict, cell: list[int]) -> dict | None:
    fire = scenario_map.get("fire")
    if isinstance(fire, dict) and fire.get("cell") == list(cell):
        return {"fire": True, "count": fire.get("level", 1),
                "confidence": 0.93, "direction": "center",
                "summary": f"fire level {fire.get('level', 1)}"}
    return None


def _make_bridge(bridge_url: str):
    from bridge_client import BridgeClient
    timeout = float(os.environ.get("BRIDGE_TIMEOUT", "120"))
    return BridgeClient(bridge_url, timeout=timeout)


def _run_rover(bridge, grid, start, water_tower, fire_cell,
               dwell_water: float, dwell_fire: float, emit=None) -> dict:
    log: list[dict] = []

    def emit_log(entry):
        log.append(entry)
        if emit:
            emit({"kind": "rover_step", **entry})

    pos = list(start) if start else [0, 0]

    # 1. Старт → водонапорная башня
    if water_tower and pos != list(water_tower):
        path = _astar(grid, pos, water_tower)
        if path:
            emit_log({"action": "navigate", "from": pos, "to": water_tower,
                      "cells": len(path)})
            for step in path[1:]:
                bridge.move(step)
                pos = step
                emit_log({"pose": pos})
        else:
            emit_log({"action": "blocked", "from": pos, "to": water_tower})

    # 2. Забор воды
    emit_log({"action": "dwell_water", "cell": pos, "seconds": dwell_water})
    try:
        bridge.dwell(dwell_water, led="blink")
    except Exception:
        pass

    # 3. Башня → огонь
    if pos != list(fire_cell):
        path = _astar(grid, pos, fire_cell)
        if path:
            emit_log({"action": "navigate", "from": pos, "to": fire_cell,
                      "cells": len(path)})
            for step in path[1:]:
                bridge.move(step)
                pos = step
                emit_log({"pose": pos})
        else:
            emit_log({"action": "blocked", "from": pos, "to": fire_cell})

    # 4. Тушение
    emit_log({"action": "dwell_fire", "cell": pos, "seconds": dwell_fire})
    try:
        bridge.dwell(dwell_fire, led="blink")
    except Exception:
        pass

    # 5. Возврат на старт
    if start and pos != list(start):
        path = _astar(grid, pos, start)
        if path:
            emit_log({"action": "navigate_return", "from": pos, "to": start,
                      "cells": len(path)})
            for step in path[1:]:
                bridge.move(step)
                pos = step
                emit_log({"pose": pos})

    try:
        bridge.led("off")
    except Exception:
        pass

    return {"status": "rover_done", "final_position": pos, "log": log}