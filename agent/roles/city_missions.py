"""city_missions agent behaviours (Город дронов): coordinator + scout + rover.

Двухэтапный диалог агентов:

Фазы:
  INIT           — coordinator читает map.json, переходит в CHAT_SCRIPT
  CHAT_SCRIPT    — агенты обсуждают observer.py vs seeker.py,
                   координатор фиксирует выбор
  EXECUTE_FLIGHT — координатор запускает выбранный скрипт облёта
  CHAT_TARGET    — агенты обсуждают результаты VLM, выбирают 1 целевой квадрат
  DONE           — итоги на доске: target_cell + count
"""
from __future__ import annotations

import os
import re

from . import make_msg


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
    """Собрать список кандидатов-огней из результата облёта."""
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


def _do_flight(ctx, script: str) -> dict:
    """Запустить выбранный скрипт облёта."""
    if script == "seeker":
        from seeker import run_seeker
        runner = run_seeker
    else:
        from observer import run_observer
        runner = run_observer

    from city_mission import MissionConfig
    hover = float(os.environ.get("HOVER_ALTITUDE", "2.0"))
    cfg = MissionConfig(hover_altitude=hover)

    drone_bridges = {}
    for i in range(1, 5):
        key = f"DRONE{i}_URL"
        url = os.environ.get(key, "")
        if url:
            drone_bridges[f"drone-{i}"] = url

    return runner(
        drone_bridges=drone_bridges,
        brain=ctx.brain,
        bb_root=os.environ.get("BLACKBOARD", "/blackboard"),
        scenario_map=ctx.scenario_map,
        config=cfg,
        emit=ctx.emit,
    )


# ============================ coordinator_step ============================
def coordinator_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    world_bb = ctx.world or {}
    scouts = ctx.config.get("scouts", [])

    # ---- INIT ----
    if phase == "INIT":
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
            n = len(_chat_phase(ctx, "CHAT_SCRIPT"))
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

    # ---- EXECUTE_FLIGHT: запуск облёта ----
    if phase == "EXECUTE_FLIGHT":
        if world_bb.get("flight_done"):
            return {"thought": "Облёт выполнен.", "messages": [], "idle": True}

        chosen = world_bb.get("chosen_script", "observer")
        ctx.emit({"kind": "mission_phase", "phase": "flight",
                  "script": chosen})

        try:
            flight_result = _do_flight(ctx, chosen)
        except Exception as e:
            flight_result = {"status": "error", "error": str(e)}

        candidates = _build_candidates_from_result(flight_result)
        world_bb["flight_result"] = flight_result
        world_bb["candidates"] = candidates
        world_bb["flight_done"] = True
        world_bb["phase"] = "CHAT_TARGET"
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("CHAT_TARGET", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "CHAT_TARGET"})

        status = flight_result.get("status", "error")
        n_candidates = len(candidates)
        best = candidates[0] if candidates else None
        best_str = (f"лучший: {best['fire_cell']} conf={best['confidence']:.2f}"
                    if best else "нет")
        return {
            "thought": f"Облёт {chosen}.py: {status}, кандидатов: {n_candidates}, {best_str}. "
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
            world_bb["phase"] = "DONE"
            world_bb["done"] = True
            ctx.bb.write_world(world_bb)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "DONE"})
            ctx.emit({"kind": "target_chosen",
                      "target_cell": verdict["target_cell"],
                      "count": verdict["count"]})

            tc = verdict["target_cell"]
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
        return {"thought": "Облёт выполняется координатором.",
                "messages": [], "idle": True}

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

    return {"thought": f"Жду ({phase}).", "messages": [], "idle": True}


def step(ctx) -> dict:
    role = ctx.role
    if role == "coordinator":
        return coordinator_step(ctx)
    if role == "rover":
        return rover_step(ctx)
    return scout_step(ctx)