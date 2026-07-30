"""city_missions agent behaviours (Город дронов): coordinator + scout + rover.

Двухэтапный диалог агентов + движение ровера:

Фазы:
  INIT           — coordinator читает map.json, переходит в CHAT_SCRIPT
  CHAT_SCRIPT    — агенты обсуждают observer.py vs seeker.py,
                   координатор фиксирует выбор
  EXECUTE_FLIGHT — координатор запускает выбранный скрипт облёта
  CHAT_TARGET    — агенты обсуждают результаты VLM, выбирают целевой квадрат
  ROVER_EXECUTE  — координатор запускает ровер: старт → башня → огонь → старт
  DONE           — итоги на доске: target_cell + count
"""
from __future__ import annotations

import json
import os
import re
import time

from . import make_msg
from mission_journal import journal_record as _jr


_STAGE_TOTAL = 4


def _write_mission_report(bb_root: str, report: dict) -> None:
    """Сохранить итоговый mission_report.json."""
    from pathlib import Path
    path = Path(bb_root) / "mission_report.json"
    try:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"[REPORT] mission_report.json written to {path}", flush=True)
    except Exception as e:
        print(f"[REPORT] failed to write mission_report.json: {e}", flush=True)


def _stage_log(stage: int, msg: str) -> None:
    print(f"[STAGE {stage}/{_STAGE_TOTAL}] {msg}", flush=True)


def _chat_phase(ctx, phase_name: str):
    return [m for m in ctx.messages if m.get("phase") == phase_name]


def _chatters(ctx, phase_name: str) -> set:
    return {m.get("from") for m in _chat_phase(ctx, phase_name)
            if m.get("type") in ("CHAT", "FACILITATE", "PROPOSAL", "VOTE")}


def _coordinator_has_said(ctx, tag: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "FACILITATE":
            if m.get("payload", {}).get("tag") == tag:
                return True
    return False


def _has_posted(ctx, phase_name: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("phase") == phase_name:
            return True
    return False


def _pick_script_from_chat(ctx) -> str:
    """Извлечь выбранный скрипт из сообщений чата. По умолчанию observer."""
    obs_votes = 0
    seek_votes = 0
    for m in _chat_phase(ctx, "CHAT_SCRIPT"):
        body = (m.get("body") or "").lower()
        if "observer" in body:
            obs_votes += 1
        if "seeker" in body:
            seek_votes += 1
    if seek_votes > obs_votes:
        return "seeker"
    return "observer"


def _pick_target_from_chat(ctx) -> dict:
    """Извлечь целевую клетку и count из чата. Ищет [x,y] и count:N паттерны."""
    cell_votes: dict[str, list[dict]] = {}
    for m in _chat_phase(ctx, "CHAT_TARGET"):
        body = m.get("body", "")
        matches = re.findall(r"\[(\d+)\s*,\s*(\d+)\]", body)
        if not matches:
            continue
        cnt = 1
        cm = re.search(r"(?:count|количество)[\s:]*(\d+)", body.lower())
        if cm:
            cnt = int(cm.group(1))
        for x, y in matches:
            key = f"{x},{y}"
            cell_votes.setdefault(key, []).append({
                "from": m.get("from"),
                "cell": [int(x), int(y)],
                "count": cnt,
            })

    if not cell_votes:
        return {"target_cell": None, "count": 1}

    best = max(cell_votes.items(), key=lambda kv: len(kv[1]))
    cell = [int(x) for x in best[0].split(",")]
    counts = [v["count"] for v in best[1]]
    count = max(set(counts), key=counts.count) if counts else 1
    return {"target_cell": cell, "count": count}


def _build_candidates_from_result(flight_result: dict) -> list[dict]:
    """Собрать список кандидатов-огней из результата облёта (observer или seeker)."""
    candidates: list[dict] = []
    drone_results = flight_result.get("drone_results") or {}

    for agent_id, dr in drone_results.items():
        photos = dr.get("photos")  # seeker
        if photos:
            for p in photos:
                if p.get("fire"):
                    candidates.append({
                        "fire_cell": p.get("fire_cell"),
                        "confidence": p.get("confidence", 0),
                        "count": p.get("count", 1) if "count" in p else 1,
                        "summary": p.get("summary", ""),
                        "detected_by": agent_id,
                        "photo_cell": p.get("cell"),
                        "direction": p.get("direction", "none"),
                    })
        elif dr.get("fire"):
            candidates.append({
                "fire_cell": dr.get("fire_cell"),
                "confidence": dr.get("confidence", 0),
                "count": dr.get("count", 1) if "count" in dr else 1,
                "summary": dr.get("summary", ""),
                "detected_by": agent_id,
                "photo_cell": dr.get("cell"),
                "direction": dr.get("direction", "none"),
            })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def _extract_detections(dr: dict, agent_id: str, script: str) -> list[dict]:
    """Извлечь список находок из результата одного дрона."""
    dets: list[dict] = []
    photos = dr.get("photos")
    if photos:
        for p in photos:
            if p.get("fire"):
                dets.append({"cell": p.get("fire_cell"),
                             "confidence": p.get("confidence", 0)})
    elif dr.get("fire"):
        dets.append({"cell": dr.get("fire_cell"),
                     "confidence": dr.get("confidence", 0)})
    return dets


def _do_flight_per_drone(ctx) -> dict:
    """Один дрон выполняет облёт через sverk_interfaces."""
    from drone_api import init_drone

    chosen = (ctx.world or {}).get("chosen_script", "observer")
    drone = init_drone(ctx.agent_id)

    if chosen == "seeker":
        from seeker import run_seeker
        return run_seeker(drone=drone, brain=ctx.brain,
                          bb_root=os.environ.get("BLACKBOARD", "/blackboard"),
                          scenario_map=ctx.scenario_map, emit=ctx.emit)
    else:
        from observer import run_observer
        return run_observer(drone=drone, brain=ctx.brain,
                            bb_root=os.environ.get("BLACKBOARD", "/blackboard"),
                            scenario_map=ctx.scenario_map, emit=ctx.emit)


# ============================ coordinator_step ============================
def coordinator_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    world_bb = ctx.world or {}
    scouts = ctx.config.get("scouts", [])

    # ---- INIT ----
    if phase == "INIT":
        _stage_log(1, "Agents debating flight script (observer vs seeker)...")
        ctx.bb.write_world({"task": "city_missions",
                            "phase": "CHAT_SCRIPT", "scouts": scouts,
                            "rover": ctx.config.get("rover", "rover"),
                            "done": False})
        ctx.bb.write_phase("CHAT_SCRIPT", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "CHAT_SCRIPT"})
        return {
            "thought": "Открываю Этап 1: выбор скрипта облёта.",
            "messages": [], "idle": False,
        }

    # ---- CHAT_SCRIPT: выбор observer.py vs seeker.py ----
    if phase == "CHAT_SCRIPT":
        from .phase_util import deadline_passed
        msgs_out = []

        if not _coordinator_has_said(ctx, "open_script_chat"):
            msgs_out.append(make_msg(ctx, "FACILITATE", "all", "CHAT_SCRIPT",
                body=(
                    "=== ЭТАП 1: ВЫБОР СКРИПТА ОБЛЁТА ===\n"
                    "У нас два варианта:\n"
                    "  • observer.py — быстрый экспресс-осмотр: взлёт, зависание "
                    "20 сек над своей клеткой, 1 снимок на дрона, посадка.\n"
                    "  • seeker.py — полный детальный обход: каждый дрон обходит "
                    "9 клеток (3×3) по спирали, делает 9 снимков, посадка.\n\n"
                    "Ресурсы батареи ограничены — приоритетен быстрый экспресс-осмотр "
                    "(observer.py), если нет веских причин для полного обхода.\n"
                    "Обсудите и выберите ОДИН скрипт. Назовите его явно: «observer» или «seeker»."
                ),
                payload={"tag": "open_script_chat"},
            ))

        deadline = deadline_passed(ctx)
        chatters = _chatters(ctx, "CHAT_SCRIPT")
        all_spoken = set(scouts).issubset(chatters)

        if all_spoken or deadline:
            chosen = _pick_script_from_chat(ctx)
            world_bb["chosen_script"] = chosen
            world_bb["phase"] = "EXECUTE_FLIGHT"
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("EXECUTE_FLIGHT", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "EXECUTE_FLIGHT"})
            ctx.emit({"kind": "script_chosen", "script": chosen})
            _jr("script_chosen", agent=ctx.agent_id, script=chosen)
            n = len(_chat_phase(ctx, "CHAT_SCRIPT"))
            _stage_log(2, f"Executing flight script {chosen}.py...")
            return {
                "thought": f"Выбран {chosen}.py ({n} реплик). Запускаю облёт.",
                "messages": [
                    make_msg(ctx, "DECISION", "all", "CHAT_SCRIPT",
                             body=f"Решение принято: запускаем {chosen}.py.",
                             payload={"chosen_script": chosen})
                ],
                "idle": False,
            }

        return {
            "thought": f"Идёт выбор скрипта: {len(_chat_phase(ctx, 'CHAT_SCRIPT'))} реплик, "
                       f"высказались {len(chatters)}/{len(scouts)}.",
            "messages": msgs_out, "idle": not msgs_out,
        }

    # ---- EXECUTE_FLIGHT: дроны летят сами ----
    if phase == "EXECUTE_FLIGHT":
        if world_bb.get("flight_done"):
            return {"thought": "Облёт выполнен.", "messages": [], "idle": True}

        scouts = ctx.config.get("scouts", [])

        # ждём OBSERVATION от каждого скаута (с дедлайном)
        observed = {m.get("from") for m in ctx.messages
                    if m.get("type") == "OBSERVATION"
                    and m.get("from") in scouts}
        from .phase_util import deadline_passed as _dl
        if len(observed) < len(scouts) and not _dl(ctx):
            return {
                "thought": f"Жду облёт: отчитались {len(observed)}/{len(scouts)} дронов.",
                "messages": [], "idle": True,
            }

        if not observed:
            world_bb["flight_done"] = True
            world_bb["candidates"] = []
            world_bb["phase"] = "DONE"
            world_bb["done"] = True
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "DONE"})
            _stage_log(0, "No drones reported — mission cannot continue.")
            return {
                "thought": "Ни один дрон не отчитался. Завершаю.",
                "messages": [
                    make_msg(ctx, "REPORT", "all", "DONE",
                             body="Облёт не выполнен: все дроны недоступны.",
                             payload={})
                ],
                "idle": False,
            }

        all_results: dict = {"drone_results": {}, "diagnostics": {}, "errors_log": []}
        for m in ctx.messages:
            if m.get("type") != "OBSERVATION" or m.get("from") not in scouts:
                continue
            payload = m.get("payload") or {}
            dr = payload.get("drone_results") or {}
            for aid, data in dr.items():
                all_results["drone_results"][aid] = data
            diag = payload.get("diagnostics") or {}
            for aid, status in diag.items():
                all_results["diagnostics"][aid] = status
            errs = payload.get("errors_log") or []
            all_results["errors_log"].extend(errs)

        candidates = _build_candidates_from_result(all_results)
        world_bb["flight_result"] = all_results
        world_bb["candidates"] = candidates
        world_bb["flight_done"] = True
        world_bb["phase"] = "CHAT_TARGET"
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("CHAT_TARGET", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "CHAT_TARGET"})
        _stage_log(3, "Agents selecting target cell from VLM results...")

        n_candidates = len(candidates)
        best = candidates[0] if candidates else None
        best_str = (f"лучший: {best['fire_cell']} conf={best['confidence']:.2f}"
                    if best else "нет")
        return {
            "thought": f"Все дроны отчитались. Кандидатов: {n_candidates}, {best_str}. "
                       f"Открываю Этап 2.",
            "messages": [], "idle": False,
        }

    # ---- CHAT_TARGET: выбор целевого квадрата ----
    if phase == "CHAT_TARGET":
        from .phase_util import deadline_passed
        candidates = world_bb.get("candidates") or []
        msgs_out = []

        if not _coordinator_has_said(ctx, "open_target_chat"):
            cand_str = "\n".join(
                f"  • [{c['fire_cell'][0]},{c['fire_cell'][1]}] "
                f"conf={c['confidence']:.2f} "
                f"count={c.get('count', 1)} "
                f"от {c['detected_by']} — {c.get('summary', '')[:80]}"
                for c in candidates[:10]
            ) if candidates else "  (кандидатов нет — огонь не обнаружен)"

            msgs_out.append(make_msg(ctx, "FACILITATE", "all", "CHAT_TARGET",
                body=(
                    f"=== ЭТАП 2: ВЫБОР ЦЕЛЕВОГО КВАДРАТА ===\n"
                    f"Результаты VLM-анализа ({len(candidates)} находок):\n"
                    f"{cand_str}\n\n"
                    f"Проанализируйте кандидатов. Путём обсуждения выберите СТРОГО "
                    f"ОДИН квадрат [x, y], в котором целевой объект находится "
                    f"с наибольшей вероятностью, и укажите количество объектов count.\n"
                    f"Формат ответа: «цель [x, y], count: N»."
                ),
                payload={"tag": "open_target_chat", "candidates_count": len(candidates)},
            ))

        if not candidates:
            world_bb["target_cell"] = None
            world_bb["target_count"] = 0
            world_bb["phase"] = "DONE"
            world_bb["done"] = True
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "DONE"})
            _stage_log(0, "No fire detected — mission finished without target.")
            return {
                "thought": "Кандидатов нет — миссия завершена без цели.",
                "messages": [
                    make_msg(ctx, "REPORT", "all", "DONE",
                             body="Огонь не обнаружен ни одним дроном.",
                             payload={"target_cell": None, "count": 0})
                ],
                "idle": False,
            }

        deadline = deadline_passed(ctx)
        chatters = _chatters(ctx, "CHAT_TARGET")
        all_spoken = set(scouts).issubset(chatters)

        if all_spoken or deadline:
            verdict = _pick_target_from_chat(ctx)
            world_bb["target_cell"] = verdict["target_cell"]
            world_bb["target_count"] = verdict["count"]
            world_bb["phase"] = "ROVER_EXECUTE"
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("ROVER_EXECUTE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "ROVER_EXECUTE"})
            ctx.emit({"kind": "target_chosen",
                      "target_cell": verdict["target_cell"],
                      "count": verdict["count"]})
            _jr("target_chosen", agent=ctx.agent_id,
                cell=verdict["target_cell"], count=verdict["count"])
            tc = verdict["target_cell"]
            cnt = verdict["count"]
            _stage_log(4, f"Rover executing firefighting loop (count={cnt}, target={tc})...")
            n = len(_chat_phase(ctx, "CHAT_TARGET"))
            body = (f"Вердикт: целевой квадрат [{tc[0]},{tc[1]}], "
                    f"объектов: {verdict['count']}." if tc
                    else "Не удалось определить целевую клетку.")
            return {
                "thought": f"Этап 2 завершён ({n} реплик). Цель: {tc}, count={verdict['count']}.",
                "messages": [
                    make_msg(ctx, "DECISION", "all", "CHAT_TARGET",
                             body=body, payload=verdict)
                ],
                "idle": False,
            }

        return {
            "thought": f"Идёт обсуждение цели: {len(_chat_phase(ctx, 'CHAT_TARGET'))} реплик, "
                       f"высказались {len(chatters)}/{len(scouts)}.",
            "messages": msgs_out, "idle": not msgs_out,
        }

    # ---- ROVER_EXECUTE: движение ровера ----
    if phase == "ROVER_EXECUTE":
        if world_bb.get("done"):
            return {"thought": "Миссия выполнена.", "messages": [], "idle": True}

        target = world_bb.get("target_cell")
        target_count = world_bb.get("target_count", 1)
        if not target:
            world_bb["done"] = True
            world_bb["phase"] = "DONE"
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "DONE"})
            _stage_log(0, "No target cell — rover stays at start.")
            return {
                "thought": "Нет цели — ровер не запускается.",
                "messages": [
                    make_msg(ctx, "REPORT", "all", "DONE",
                             body="Цель не определена, ровер остаётся на месте.",
                             payload={"target_cell": None})
                ],
                "idle": False,
            }

        water = list(ctx.scenario_map.get("water_tower") or [1, 3])
        init = list(ctx.scenario_map.get("charge_zone") or [1, 1])

        ctx.emit({"kind": "mission_phase", "phase": "rover",
                  "target_cell": target, "count": target_count,
                  "water_tower": water, "init_cell": init})

        try:
            from rover_executor import run_rover_mission

            rover_api = os.environ.get("ROVER_API_URL", "")
            rover_bridge = os.environ.get("ROVER_URL", "")

            rover_result = run_rover_mission(
                target_cell=target,
                count=target_count,
                water_cell=water,
                init_cell=init,
                rover_api_url=rover_api,
                rover_bridge_url=rover_bridge,
                emit=ctx.emit,
            )
        except Exception as e:
            rover_result = {"status": "error", "error": str(e)}

        world_bb["rover_result"] = rover_result
        world_bb["done"] = True
        world_bb["phase"] = "DONE"
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "DONE"})

        ok = rover_result.get("status") == "completed"
        _stage_log(0, f"Mission {'completed successfully!' if ok else 'finished with errors.'}")

        ext = rover_result.get("extinguished_count", 0)
        tgt_count = rover_result.get("target_count", target_count)
        failed_step = rover_result.get("failed_step")
        rover_errors = rover_result.get("errors_log", [])

        all_diagnostics = world_bb.get("flight_result", {}).get("diagnostics", {})
        all_errors = world_bb.get("flight_result", {}).get("errors_log", []) + rover_errors
        vlm_used_fallback = any("VLM Fallback" in e for e in all_errors)

        report = {
            "mission_status": "success" if ok else
                              "completed_with_warnings" if ext > 0 else "partial_success",
            "target_cell": target,
            "fire_count_target": tgt_count,
            "extinguished_count": ext,
            "diagnostics": {
                "drones": all_diagnostics,
                "vlm_status": "fallback_used" if vlm_used_fallback else "ok",
                "rover_status": failed_step or "ok",
            },
            "errors_log": all_errors,
        }
        _write_mission_report(os.environ.get("BLACKBOARD", "/blackboard"), report)
        body = (f"Ровер {'выполнил миссию' if ok else 'завершил с ошибкой'}. "
                f"Огонь: {target}.")
        return {
            "thought": f"Ровер: {rover_result.get('status')}.",
            "messages": [
                make_msg(ctx, "REPORT", "all", "DONE", body=body,
                         payload=rover_result)
            ],
            "idle": False,
        }

    return {"thought": "Готово.", "messages": [], "idle": True}


# ============================ scout_step ============================
def scout_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")

    if phase == "CHAT_SCRIPT":
        if _has_posted(ctx, "CHAT_SCRIPT"):
            return {"thought": "Уже проголосовал за скрипт.", "messages": [], "idle": True}
        return {
            "thought": "Выбираю скрипт облёта.",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT_SCRIPT",
                         body=(
                             f"{ctx.agent_id}: выбираю observer.py — быстрый осмотр, "
                             f"батарею надо экономить. Одного снимка с высоты достаточно "
                             f"для первичной оценки."
                         ),
                         payload={"vote": "observer"})
            ],
            "idle": False,
        }

    if phase == "EXECUTE_FLIGHT":
        chosen = (ctx.world or {}).get("chosen_script", "observer")
        if _has_posted(ctx, "EXECUTE_FLIGHT"):
            return {"thought": f"Облёт ({chosen}) выполнен.",
                    "messages": [], "idle": True}

        _stage_log(2, f"{ctx.agent_id}: executing flight script {chosen}.py...")
        try:
            result = _do_flight_per_drone(ctx)
        except Exception as e:
            result = {"status": "error", "error": str(e),
                      "drone_results": {ctx.agent_id: {"error": str(e)}}}

        ctx.bb.write_progress(ctx.agent_id, {"status": "done",
                                             "result": result})
        dr = result.get("drone_results", {}).get(ctx.agent_id, {})
        dets = _extract_detections(dr, ctx.agent_id, chosen)
        body = (f"Облёт {chosen}.py завершён. "
                + (f"Находок: {len(dets)}." if dets else "Огонь не обнаружен."))
        return {
            "thought": f"Облёт {chosen}.py: {result.get('status')}.",
            "messages": [
                make_msg(ctx, "OBSERVATION", "all", "EXECUTE_FLIGHT",
                         body=body,
                         payload={"drone_results": result.get("drone_results", {}),
                                  "diagnostics": result.get("diagnostics", {}),
                                  "errors_log": result.get("errors_log", []),
                                  "chosen_script": chosen})
            ],
            "idle": False,
        }

    if phase == "CHAT_TARGET":
        if _has_posted(ctx, "CHAT_TARGET"):
            return {"thought": "Уже участвовал в выборе цели.",
                    "messages": [], "idle": True}
        world = ctx.world or {}
        candidates = world.get("candidates") or []
        my_detections = [c for c in candidates if c.get("detected_by") == ctx.agent_id]
        if my_detections:
            best = max(my_detections, key=lambda c: c["confidence"])
            return {
                "thought": f"Голосую за свою находку: {best['fire_cell']} conf={best['confidence']:.2f}.",
                "messages": [
                    make_msg(ctx, "CHAT", "all", "CHAT_TARGET",
                             body=(
                                 f"{ctx.agent_id}: голосую за квадрат "
                                 f"[{best['fire_cell'][0]},{best['fire_cell'][1]}], "
                                 f"count: {best.get('count', 1)}. "
                                 f"Уверенность {best['confidence']:.2f}. "
                                 f"{best.get('summary', '')[:60]}"
                             ),
                             payload={"cell": best["fire_cell"],
                                      "count": best.get("count", 1),
                                      "confidence": best["confidence"]})
                ],
                "idle": False,
            }
        return {
            "thought": "Нет моих находок среди кандидатов.",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT_TARGET",
                         body=(
                             f"{ctx.agent_id}: в моей зоне огонь не обнаружен. "
                             f"Согласен с наиболее уверенной находкой коллег."
                         ),
                         payload={})
            ],
            "idle": False,
        }

    if phase == "ROVER_EXECUTE":
        return {"thought": "Ровер выполняет тушение.",
                "messages": [], "idle": True}

    return {"thought": f"Жду фазу ({phase}).", "messages": [], "idle": True}


# ============================ rover_step ============================
def rover_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")

    if phase == "CHAT_SCRIPT":
        if _has_posted(ctx, "CHAT_SCRIPT"):
            return {"thought": "Уже высказался по скрипту.", "messages": [], "idle": True}
        return {
            "thought": "Поддерживаю observer.py.",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT_SCRIPT",
                         body=(
                             f"{ctx.agent_id}: согласен с observer.py. Мне нужны "
                             f"только координаты цели — быстрый осмотр даст их быстрее."
                         ),
                         payload={"vote": "observer"})
            ],
            "idle": False,
        }

    if phase == "EXECUTE_FLIGHT":
        return {"thought": "Жду результатов облёта.",
                "messages": [], "idle": True}

    if phase == "CHAT_TARGET":
        if _has_posted(ctx, "CHAT_TARGET"):
            return {"thought": "Уже участвовал в выборе цели.",
                    "messages": [], "idle": True}
        world = ctx.world or {}
        candidates = world.get("candidates") or []
        if not candidates:
            return {"thought": "Нет кандидатов.", "messages": [], "idle": True}
        best = candidates[0]
        return {
            "thought": f"Ровер выбирает цель: {best['fire_cell']}.",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT_TARGET",
                         body=(
                             f"{ctx.agent_id}: как ровер, поддерживаю цель "
                             f"[{best['fire_cell'][0]},{best['fire_cell'][1]}] "
                             f"— наибольшая уверенность {best['confidence']:.2f}. "
                             f"Маршрут: старт → башня → [{best['fire_cell'][0]},"
                             f"{best['fire_cell'][1]}] → старт."
                         ),
                         payload={"cell": best["fire_cell"],
                                  "confidence": best["confidence"]})
            ],
            "idle": False,
        }

    if phase == "ROVER_EXECUTE":
        return {"thought": "Выполняю маршрут тушения.",
                "messages": [], "idle": True}

    return {"thought": f"Жду ({phase}).", "messages": [], "idle": True}


def step(ctx) -> dict:
    role = ctx.role
    if role == "coordinator":
        return coordinator_step(ctx)
    if role == "rover":
        return rover_step(ctx)
    return scout_step(ctx)