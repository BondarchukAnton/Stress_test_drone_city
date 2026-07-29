"""city_missions agent behaviours (Город дронов): coordinator + scout + rover,
on the shared phase machine / blackboard.

Phases:
  INIT     coordinator reads map.json (grid, cell size, charge zone, water tower),
           splits the field into scout zones, opens SURVEY.
  SURVEY   scouts fly their zones, photograph cells, run VLM (brain.see) on the
           host to detect fire. Coordinator consolidates confirmed fire facts.
  EXECUTE  coordinator compiles the fire-extinguishing plan (rover drives to fire
           via roads, dwells at water tower) and runs it.
  DONE     summary on the board.
"""
from __future__ import annotations

import json
import os

from . import make_msg
from .survey_common import cell_key, grid_size, zones_from_map


def _survey_zones(scenario_map, labels):
    if os.environ.get("SURVEY_ROUNDROBIN", "0") in ("0", "", "false", "no"):
        return zones_from_map(scenario_map, labels)
    per = max(1, int(os.environ.get("SURVEY_CELLS_PER_TURN", "2")))
    w, h = grid_size(scenario_map)
    cells = []
    for cy in range(h):
        row = [[cx, cy] for cx in range(w)]
        if cy % 2:
            row.reverse()
        cells += row
    zones = {lab: [] for lab in labels}
    for k in range(0, len(cells), per):
        zones[labels[(k // per) % len(labels)]] += cells[k:k + per]
    return zones
from .city_world import (load_world, rank_missions, fire_route,
                         EnergyLedger, EnergyError)
from .city_executor import run_attempt, plan_total_energy


# ---- VLM prompt for fire detection ----
_VLM_FIRE_DEFAULT = (
    "Ты — детектор пожара на дроне. Смотришь снимок клетки сверху.\n"
    "Ответь СТРОГО в формате JSON (без markdown, без ```):\n"
    '{"fire": true|false, "count": число объектов огня, "confidence": 0.0-1.0}\n'
    "fire=true если видишь яркое свечение, дым или открытое пламя в клетке.\n"
    "count — сколько отдельных очагов/объектов огня ты видишь.\n"
    "confidence — насколько ты уверен (0.0 = не уверен, 1.0 = абсолютно точно)."
)


def _fire_prompt() -> str:
    return os.environ.get("VLM_FIRE_PROMPT", _VLM_FIRE_DEFAULT)


# ---- fact consolidation -----------------------------------------------------
def _observations(ctx) -> list:
    return [m for m in ctx.messages if m.get("type") == "OBSERVATION"]


def _confirmed(ctx, kind: str, min_votes: int = 1):
    votes: dict[str, list] = {}
    for m in _observations(ctx):
        for d in (m.get("payload") or {}).get("detections", []):
            if d.get("type") != kind:
                continue
            key = cell_key(d.get("cell") or (m["payload"].get("cell") or [0, 0]))
            votes.setdefault(key, []).append(d)
    if not votes:
        return None
    best = max(votes.items(), key=lambda kv: len(kv[1]))
    if len(best[1]) < min_votes:
        return None
    cell = [int(x) for x in best[0].split(",")]
    dets = best[1]
    out = {"cell": cell, "confidence": max(d.get("confidence", 0) for d in dets),
           "confirmed_by": len(dets)}
    levels = [int(d["level"]) for d in dets if d.get("level") is not None]
    if levels:
        levels.sort()
        out["level"] = levels[len(levels) // 2]
    return out


def _build_world(ctx):
    """WorldModel from the scenario map, overlaid with confirmed fire facts."""
    world = load_world(ctx.scenario_map)
    fire = _confirmed(ctx, "fire")
    if fire:
        world.fire = {"cell": fire["cell"], "level": fire.get("level", 1),
                       "confidence": fire["confidence"], "confirmed_by": fire["confirmed_by"]}
    return world


def _facts_ready(world) -> bool:
    return bool(world.fire and world.fire.get("cell") is not None)


# ---- coordinator ------------------------------------------------------------
def coordinator_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    world_bb = ctx.world or {}

    if phase == "INIT":
        w, h = grid_size(ctx.scenario_map)
        scouts = ctx.config.get("scouts", [])
        zones = _survey_zones(ctx.scenario_map, [chr(ord("A") + i) for i in range(len(scouts))])
        assign = {sid: list(zones.values())[i] for i, sid in enumerate(scouts)} if zones else {}
        ctx.bb.write_world({"task": "city_missions", "w": w, "h": h,
                            "phase": "SURVEY", "zones": zones, "assign": assign,
                            "facts": {}, "missions": None, "log": []})
        ctx.bb.write_phase("SURVEY", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "SURVEY"})
        split = "; ".join(f"{sid}→зона {chr(65 + i)} [{len(z)} кл.]"
                          for i, (sid, z) in enumerate(assign.items()))
        return {"thought": f"Поле {w}×{h}. Разбил на {len(zones)} зон, распределяю дронам.",
                "messages": [make_msg(ctx, "FACILITATE", "all", "SURVEY",
                             body=f"Поле {w}×{h}, {len(zones)} зоны. Распределение: {split}. "
                                  f"Каждый — подтвердите зону и снимите свои клетки.",
                             payload={"assign": dict(assign)})],
                "idle": False}

    if phase == "SURVEY":
        scouts = ctx.config.get("scouts", [])
        reported = {m.get("from") for m in ctx.messages
                    if m.get("type") == "OBSERVATION" and m.get("from") in scouts}
        world = _build_world(ctx)
        if _facts_ready(world) and len(reported) >= len(scouts):
            world_bb.update(facts={"fire": world.fire}, phase="EXECUTE")
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("EXECUTE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "consensus", "facts": {"fire": world.fire}})
            body = f"Все зоны сняты. Пожар подтверждён: {world.fire['cell']} ур.{world.fire.get('level')}."
            return {"thought": "Наблюдения сошлись — планирую тушение.",
                    "messages": [make_msg(ctx, "DECISION", "all", "EXECUTE", body=body,
                                 payload={"fire": world.fire})], "idle": False}
        return {"thought": f"Жду облёт: отчитались {len(reported)}/{len(scouts)} дронов.",
                "messages": [], "idle": True}

    if phase == "EXECUTE":
        if world_bb.get("done"):
            return {"thought": "Миссия выполнена.", "messages": [], "idle": True}
        world = _build_world(ctx)
        order, reasons = rank_missions(world)

        if ctx.config.get("real_rover"):
            rover_id = ctx.config.get("rover", "rover")
            if not world_bb.get("rover_plan"):
                plan = compile_rover_plan(world, order)
                world_bb.update(missions=order, rover_plan=plan,
                                fire_cell=world.fire_cell, water=world.water_tower,
                                charge=world.charge_zone)
                ctx.bb.write_world(world_bb)
                ctx.emit({"kind": "rover_plan", "order": order, "steps": len(plan)})
                body = f"План ровера ({len(plan)} шагов): {order}. Пожар {world.fire_cell} ур.{world.fire.get('level')}."
                return {"thought": "Скомпилировал план, передаю роверу.",
                        "messages": [make_msg(ctx, "ASSIGNMENT", rover_id, "EXECUTE",
                                     body=body, payload={"rover_plan": plan})],
                        "idle": False}
            rp = ctx.progress.get(rover_id) or {}
            if rp.get("status") != "done":
                return {"thought": f"Жду ровер (шаг {rp.get('step', 0)}).",
                        "messages": [], "idle": True}
            res = rp.get("result") or {}
            world_bb.update(done=True, phase="DONE", result=res)
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            body = f"Пожар {'потушен' if res.get('fire_ok') else '—'}, заряд {res.get('final_energy')}."
            return {"thought": "Ровер выполнил план.",
                    "messages": [make_msg(ctx, "REPORT", "all", "DONE", body=body,
                                 payload=res)], "idle": False}

        res = run_attempt(world)
        for e in res["log"]:
            ctx.emit({"kind": "city_evidence", **e})
        world_bb.update(missions=order, result={k: res[k] for k in
                        ("order", "fire_ok", "within_time", "final_energy", "sim_time_s")},
                        log=res["log"], done=True, phase="DONE")
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
        body = f"Пожар {'потушен' if res['fire_ok'] else '—'}, {res['sim_time_s']}с, заряд {res['final_energy']}."
        return {"thought": "Выполнил план.",
                "messages": [make_msg(ctx, "REPORT", "all", "DONE", body=body,
                             payload=res.get("result"))], "idle": False}

    return {"thought": "Готово.", "messages": [], "idle": True}


# ---- rover: drive the compiled plan in gz (REAL) ----------------------------
def compile_rover_plan(world, order: list) -> list:
    """Flatten the ordered missions into atomic rover actions."""
    from .city_world import astar, path_moves
    mission_actions: list = []
    for m in order:
        if m == "fire":
            fa, _ = fire_route(world)
            mission_actions += [a for a in fa if a["do"] != "done"]
            mission_actions.append({"do": "navigate", "to": list(world.charge_zone),
                                    "action_id": "fire-return-charge"})
    pos = list(world.charge_zone)
    moves = 0
    for a in mission_actions:
        if a.get("do") == "navigate" and a.get("to"):
            p = astar(world.grid, pos, a["to"])
            moves += path_moves(p) if p else 0
            pos = list(a["to"])
    charge = {"do": "charge", "cell": list(world.charge_zone),
              "seconds": moves + 4, "led": "off"}
    return [charge] + mission_actions


def rover_step(ctx) -> dict:
    """Execute the coordinator's compiled plan against the REAL rover bridge (gz
    cube): charge, drive cell-by-cell, dwell 3s/5s with LED, emit ACTION_COMPLETED
    evidence + energy. One blocking step drives the whole plan."""
    if ctx.phase.get("phase") != "EXECUTE":
        return {"thought": "Жду план миссии.", "messages": [], "idle": True}
    world = ctx.world or {}
    plan = world.get("rover_plan")
    mine = ctx.progress.get(ctx.agent_id) or {}
    if not plan or mine.get("status") == "done":
        return {"thought": "План выполнен или ещё не выдан.", "messages": [], "idle": True}
    if ctx.bridge is None:
        return {"thought": "Нет bridge ровера.", "messages": [], "idle": True}

    grid = (ctx.scenario_map or {}).get("grid")
    energy = EnergyLedger()
    fire_ok = False
    for i, act in enumerate(plan):
        do = act.get("do")
        ctx.bb.write_progress(ctx.agent_id, {"status": "executing", "step": i})
        try:
            if do == "charge":
                ctx.bridge.dwell(act.get("seconds", 0), led="off")
                energy.charge(act.get("seconds", 0))
                ctx.emit({"kind": "city_evidence", "type": "CHARGED",
                          "seconds": act.get("seconds"), "energy": energy.energy})
            elif do == "navigate":
                r = ctx.bridge.move(act["to"], grid=grid)
                for _ in range(max(0, int(r.get("cells", 1)) - 1)):
                    try:
                        energy.spend_move()
                    except EnergyError:
                        ctx.emit({"kind": "city_evidence", "type": "ENERGY_BLOCK",
                                  "cell": act["to"]})
                        break
            elif do == "dwell":
                secs = act.get("seconds", 0)
                r = ctx.bridge.dwell(secs, led=act.get("led", "blink"))
                ok = (float(r.get("stationary_seconds", 0)) >= secs
                      and bool(r.get("led")) and not r.get("moved"))
                aid = act.get("action_id", "")
                if "water" in aid:
                    fire_ok = fire_ok or ok
                ctx.emit({"kind": "city_evidence", "type": "ACTION_COMPLETED",
                          "action_id": aid, "agent_id": ctx.agent_id,
                          "evidence": {"cell": r.get("cell") or act.get("cell"),
                                       "stationary_seconds": r.get("stationary_seconds"),
                                       "led": bool(r.get("led")), "counted": ok}})
        except Exception as exc:
            ctx.emit({"kind": "city_evidence", "type": "ACTION_ERROR",
                      "action_id": act.get("action_id"), "error": type(exc).__name__})
    try:
        ctx.bridge.led("off")
    except Exception:
        pass
    result = {"order": world.get("missions"), "fire_ok": fire_ok,
              "final_energy": energy.energy}
    ctx.bb.write_progress(ctx.agent_id, {"status": "done", "result": result})
    ctx.emit({"kind": "city_evidence", "type": "ROVER_DONE", "energy": energy.energy})
    return {"thought": "Ровер отработал весь план в gz.",
            "messages": [make_msg(ctx, "STATUS", "coordinator", "EXECUTE",
                         body="Ровер выполнил план.", payload=result)], "idle": False}


# ---- scout ------------------------------------------------------------------
def _my_zone(ctx) -> list:
    assign = (ctx.world or {}).get("assign") or {}
    return assign.get(ctx.agent_id) or []


def _detections_in(ctx, cells) -> list:
    """Mock: report fire from map.json ground truth."""
    keys = {cell_key(c) for c in cells}
    sm = ctx.scenario_map
    out = []
    fire = sm.get("fire")
    if isinstance(fire, dict) and cell_key(fire["cell"]) in keys:
        out.append({"type": "fire", "cell": fire["cell"],
                    "level": int(fire.get("level", 1)), "confidence": 0.93})
    return out


def _fly_targets(ctx, zone) -> list:
    """Cells the drone overflies: fire-object cells first, then extras."""
    keys_obj = set()
    sm = ctx.scenario_map
    fire = sm.get("fire")
    if isinstance(fire, dict) and fire.get("cell"):
        keys_obj.add(cell_key(fire["cell"]))
    prio = [c for c in zone if cell_key(c) in keys_obj]
    rest = [c for c in zone if cell_key(c) not in keys_obj]
    return (prio + rest)[:max(2, len(prio) + 2)]


def _vlm_detect_fire(ctx, cell) -> dict | None:
    """Сфотографировать клетку через мост и проанализировать снимок через
    VLM на хосте (ctx.brain.see → gemma4-vlm).

    Возвращает детекцию fire или None. После анализа удаляет снимок."""
    import re

    result = ctx.bridge.photograph_cell(cell)
    image_path = result.get("image_path", "")
    if not image_path:
        ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
                   "error": "no image_path from bridge"})
        return None

    # читаем PNG из общего тома blackboard (агент и мост в одном volume)
    bb_root = os.environ.get("BLACKBOARD", "/blackboard")
    full_path = os.path.join(bb_root, image_path)
    try:
        with open(full_path, "rb") as f:
            image_png = f.read()
    except (OSError, FileNotFoundError):
        ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
                   "error": f"cannot read {image_path}"})
        return None

    if not image_png:
        ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
                   "error": "empty image"})
        _cleanup(full_path)
        return None

    # VLM на хосте: отправляем PNG + промпт → gemma4-vlm
    system = _fire_prompt()
    user = f"Клетка [{cell[0]},{cell[1]}]. Видишь пожар?"
    try:
        vlm_result = ctx.brain.see(system, user, image_png, max_tokens=50, log_context="fire_detect")
    except Exception as e:
        ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
                   "error": f"VLM error: {type(e).__name__}"})
        _cleanup(full_path)
        return None

    # удаляем снимок — проанализирован, больше не нужен
    _cleanup(full_path)

    if not vlm_result:
        ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
                   "fire": False, "confidence": 0, "label": "vlm_empty"})
        return None

    # парсим JSON-ответ VLM: {"fire": true, "count": 2, "confidence": 0.9}
    parsed = None
    try:
        # VLM может обернуть в ```json или добавить текст — ищем JSON-объект
        start = vlm_result.find("{")
        end = vlm_result.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(vlm_result[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        pass

    # fallback: ищем ключевые слова в тексте если JSON не распарсился
    if parsed is None:
        vlm_lower = vlm_result.lower().strip()
        fire_detected = bool(re.search(r"\b(?:fire|пожар)\b", vlm_lower))
        count = 1
        cnt_match = re.search(r"(?:\bcount\b|количество|число|очагов?)[\s:]*(\d+)", vlm_lower)
        if cnt_match:
            count = max(1, int(cnt_match.group(1)))
        conf = 0.85 if fire_detected else 0.9
        conf_match = re.search(r"(?:confidence|уверенность|вероятность)[\s:]*(\d+\.?\d*)", vlm_lower)
        if conf_match:
            conf = min(1.0, max(0.0, float(conf_match.group(1))))
        parsed = {"fire": fire_detected, "count": count, "confidence": conf}

    is_fire = bool(parsed.get("fire"))
    count = max(1, int(parsed.get("count", 1)))
    confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.7))))

    ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
               "fire": is_fire, "count": count, "confidence": confidence,
               "label": vlm_result[:120]})

    if is_fire:
        return {"type": "fire", "cell": list(cell),
                "level": count, "confidence": confidence}
    return None


def _cleanup(filepath: str) -> None:
    try:
        os.remove(filepath)
    except OSError:
        pass


def _fly_zone(ctx, zone):
    """Реальный облёт зоны: лететь → фото → VLM на хосте → детекция пожара."""
    flown, dets = [], []
    for cell in _fly_targets(ctx, zone):
        try:
            ctx.bridge.move(cell)
            flown.append(list(cell))
            fire_det = _vlm_detect_fire(ctx, cell)
            if fire_det:
                dets.append(fire_det)
            ctx.emit({"kind": "artifact", "from": ctx.agent_id, "cell": list(cell),
                       "phase": "SURVEY"})
        except Exception:  # noqa: BLE001 — flaky leg не валит облёт
            continue
    ctx.emit({"kind": "drone_flew", "from": ctx.agent_id, "cells": flown,
              "detections": dets})
    return flown, dets


def scout_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    if phase != "SURVEY":
        return {"thought": "Жду фазу облёта.", "messages": [], "idle": True}
    zone = _my_zone(ctx)
    if not zone:
        return {"thought": "Нет назначенной зоны.", "messages": [], "idle": True}
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "OBSERVATION":
            return {"thought": "Зона снята, наблюдение опубликовано.", "messages": [], "idle": True}
    # announce WHICH cells I take (visible coordination) BEFORE flying, then observe
    if not any(m.get("from") == ctx.agent_id and m.get("type") == "CLAIM"
               for m in ctx.messages):
        cells = " ".join(str(c) for c in zone[:6]) + ("…" if len(zone) > 6 else "")
        return {"thought": f"Беру свою зону ({len(zone)} кл.), подтверждаю и лечу снимать.",
                "messages": [make_msg(ctx, "CLAIM", "coordinator", "SURVEY",
                             body=f"Принял. Беру {len(zone)} клеток: {cells}. Начинаю облёт.",
                             payload={"zone": zone, "name": ctx.agent_id})],
                "idle": False}
    real = ctx.bridge is not None
    if real:
        flown, real_dets = _fly_zone(ctx, zone)    # реальный полёт + VLM-анализ
        dets = real_dets if real_dets else _detections_in(ctx, zone)  # fallback к map.json если VLM молчит
        how = "облетел и снял"
    else:
        dets = _detections_in(ctx, zone)            # мок: данные из map.json
        how = "осмотрел"
    msg = make_msg(ctx, "OBSERVATION", "all", "SURVEY",
                   body=(f"Зона {how} ({len(zone)} кл.): "
                         + (", ".join(f"{d['type']}@{d['cell']}"
                            + (f" ур.{d['level']}" if d.get('level') else "")
                            for d in dets) if dets else "объектов миссий нет") + "."),
                   payload={"cell": zone[0], "detections": dets, "name": ctx.agent_id,
                            "real_flight": real})
    ctx.emit({"kind": "observation", "from": ctx.agent_id, "detections": dets})
    return {"thought": f"{how.capitalize()} зону, нашёл {len(dets)} объект(ов).",
            "messages": [msg], "idle": False}


def step(ctx) -> dict:
    role = ctx.role
    if role == "coordinator":
        return coordinator_step(ctx)
    if role == "rover":
        return rover_step(ctx)
    return scout_step(ctx)
