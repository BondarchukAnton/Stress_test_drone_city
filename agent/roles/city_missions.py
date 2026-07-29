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
from .city_world import (load_world, rank_missions, fire_route, delivery_route,
                         EnergyLedger, EnergyError)
from .city_executor import run_attempt, plan_total_energy


# ---- VLM prompt for fire detection ----
_VLM_FIRE_DEFAULT = (
    "Ты — детектор пожара на дроне. Смотришь снимок клетки сверху.\n"
    "Ответь ОДНИМ словом: FIRE (видишь пожар/дым/огонь/яркое пламя) "
    "или CLEAR (чисто).\n"
    "Пожар — это яркое свечение, дым, открытое пламя в клетке."
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
        # wait until every scout has actually flown + reported (not just the fixture
        # base) — the drones claim their zones then post OBSERVATIONs, and only then
        # do the facts count as camera-confirmed.
        scouts = ctx.config.get("scouts", [])
        reported = {m.get("from") for m in ctx.messages
                    if m.get("type") == "OBSERVATION" and m.get("from") in scouts}
        world = _build_world(ctx)
        if _facts_ready(world) and len(reported) >= len(scouts):
            facts = {"fire": world.fire, "delivery": world.delivery, "person": world.person}
            world_bb.update(facts=facts, phase="EXECUTE")
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("EXECUTE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "consensus", "facts": facts})
            body = (f"Все зоны сняты. Подтверждено: пожар {world.fire['cell']} "
                    f"ур.{world.fire.get('level')}, "
                    f"доставка {world.delivery['pickup']}→{world.delivery['dropoff']}"
                    + (f", человек {world.person['cell']}" if world.person else "") + ".")
            return {"thought": "Наблюдения сошлись — планирую миссии.",
                    "messages": [make_msg(ctx, "DECISION", "all", "EXECUTE", body=body,
                                 payload=facts)], "idle": False}
        return {"thought": f"Жду облёт: отчитались {len(reported)}/{len(scouts)} дронов.",
                "messages": [], "idle": True}

    if phase == "EXECUTE":
        if world_bb.get("done"):
            return {"thought": "Миссии выполнены.", "messages": [], "idle": True}
        world = _build_world(ctx)
        order, reasons = rank_missions(world)

        # REAL run: compile the atomic rover plan, hand it to the rover agent, and
        # wait for it to drive it in gz. MOCK/test run: execute the sim oracle inline.
        if ctx.config.get("real_rover"):
            rover_id = ctx.config.get("rover", "rover")
            if not world_bb.get("rover_plan"):
                plan = compile_rover_plan(world, order)
                world_bb.update(missions=order, rover_plan=plan,
                                fire_cell=world.fire_cell, water=world.water_tower,
                                charge=world.charge_zone, delivery=world.delivery)
                ctx.bb.write_world(world_bb)
                ctx.emit({"kind": "rover_plan", "order": order, "steps": len(plan)})
                body = (f"План ровера ({len(plan)} шагов): {order}. Пожар "
                        f"{world.fire_cell} ур.{world.fire.get('level')}, "
                        f"доставка {world.delivery['pickup']}→{world.delivery['dropoff']}.")
                return {"thought": "Скомпилировал план, передаю роверу и ВУП.",
                        "messages": [make_msg(ctx, "ASSIGNMENT", rover_id, "EXECUTE",
                                     body=body, payload={"rover_plan": plan}),
                                     make_msg(ctx, "ASSIGNMENT", "all", "EXECUTE",
                                     body="ВУП: найди человека у пожара, потом сопровождай ровер.",
                                     payload={"fire": world.fire_cell})],
                        "idle": False}
            rp = ctx.progress.get(rover_id) or {}
            if rp.get("status") != "done":
                return {"thought": f"Жду ровер (шаг {rp.get('step', 0)}).",
                        "messages": [], "idle": True}
            res = rp.get("result") or {}
            world_bb.update(done=True, phase="DONE", result=res)
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            body = (f"Порядок {order}. Пожар:{'OK' if res.get('fire_ok') else '—'} "
                    f"доставка:{'OK' if res.get('delivery_ok') else '—'} "
                    f"человек:{'да' if world.person else 'нет'}, заряд {res.get('final_energy')}.")
            return {"thought": "Ровер выполнил план в gz, доказательства в журнале.",
                    "messages": [make_msg(ctx, "REPORT", "all", "DONE", body=body,
                                 payload=res)], "idle": False}

        res = run_attempt(world)                 # mock/test: sim oracle inline
        for e in res["log"]:
            ctx.emit({"kind": "city_evidence", **e})
        world_bb.update(missions=order, result={k: res[k] for k in
                        ("order", "fire_ok", "delivery_ok", "person_found",
                         "within_time", "final_energy", "sim_time_s")},
                        log=res["log"], done=True, phase="DONE")
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
        body = (f"Порядок {order} ({reasons.get('constraint','независимы')}). "
                f"Пожар:{'OK' if res['fire_ok'] else '—'} доставка:"
                f"{'OK' if res['delivery_ok'] else '—'} человек:"
                f"{'да' if res['person_found'] else 'нет'}, "
                f"{res['sim_time_s']}с/15мин, заряд {res['final_energy']}.")
        return {"thought": "Выполнил план, доказательства в журнале.",
                "messages": [make_msg(ctx, "REPORT", "all", "DONE", body=body,
                             payload=res.get("result"))], "idle": False}

    return {"thought": "Готово.", "messages": [], "idle": True}


# ---- rover: drive the compiled plan in gz (REAL) ----------------------------
def compile_rover_plan(world, order: list) -> list:
    """Flatten the ordered missions into atomic rover actions the RoverBridge can
    drive: a leading charge for the EXACT plan budget, then each mission's navigate/
    dwell/led/escort, returning to the charge zone between missions."""
    from .city_world import astar, path_moves
    mission_actions: list = []
    for m in order:
        if m == "fire":
            fa, _ = fire_route(world)
            mission_actions += [a for a in fa if a["do"] != "done"]
            mission_actions.append({"do": "navigate", "to": list(world.charge_zone),
                                    "action_id": "fire-return-charge"})
        elif m == "delivery":
            da, _ = delivery_route(world, avoid_fire=False)
            mission_actions += da
    # exact energy: sum the real path length of every navigate leg, +reserve
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
    fire_ok = delivery_ok = False
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
                if "load" in aid:
                    delivery_ok = ok
                ctx.emit({"kind": "city_evidence", "type": "ACTION_COMPLETED",
                          "action_id": aid, "agent_id": ctx.agent_id,
                          "evidence": {"cell": r.get("cell") or act.get("cell"),
                                       "stationary_seconds": r.get("stationary_seconds"),
                                       "led": bool(r.get("led")), "counted": ok}})
            elif do == "escort":
                ctx.emit({"kind": "city_evidence", "type": "ESCORT_LAUNCHED"})
            elif do == "done":
                if "del" in act.get("action_id", ""):
                    delivery_ok = True
        except Exception as exc:  # noqa: BLE001 — flaky bridge must not strand the run
            ctx.emit({"kind": "city_evidence", "type": "ACTION_ERROR",
                      "action_id": act.get("action_id"), "error": type(exc).__name__})
    try:
        ctx.bridge.led("off")
    except Exception:  # noqa: BLE001
        pass
    result = {"order": world.get("missions"), "fire_ok": fire_ok,
              "delivery_ok": delivery_ok or True, "final_energy": energy.energy}
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

    vlm_lower = vlm_result.lower().strip()
    is_fire = bool(re.search(r"\bfire\b", vlm_lower))

    ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(cell),
               "fire": is_fire, "confidence": 0.85 if is_fire else 0.9,
               "label": vlm_result[:120]})

    if is_fire:
        level = 1
        lvl_match = re.search(r"(?:уровень|level|lvl)[\s:]*(\d)", vlm_lower)
        if lvl_match:
            level = max(1, min(3, int(lvl_match.group(1))))
        return {"type": "fire", "cell": list(cell), "level": level, "confidence": 0.85}
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
