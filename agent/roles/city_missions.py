"""city_missions agent behaviours (Город дронов): coordinator + scout + rover.

Фазы:
  INIT     — coordinator читает map.json, открывает CHAT
  CHAT     — агенты обсуждают план, приходят к консенсусу запустить city_mission.py
  EXECUTE  — coordinator запускает city_mission.run_mission()
  DONE     — итоги на доске
"""
from __future__ import annotations

from . import make_msg


def _my_zone(ctx) -> list:
    assign = (ctx.world or {}).get("assign") or {}
    return assign.get(ctx.agent_id) or []


def _coordinator_has_said(ctx, tag: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "FACILITATE":
            if m.get("payload", {}).get("tag") == tag:
                return True
    return False


def _chat_messages(ctx):
    return [m for m in ctx.messages if m.get("phase") == "CHAT"]


def _should_end_chat(ctx, scouts: list[str], deadline_passed: bool) -> bool:
    """Завершаем чат если все высказались или вышел дедлайн."""
    if deadline_passed:
        return True
    chatters = {m.get("from") for m in _chat_messages(ctx)
                if m.get("type") in ("CHAT", "FACILITATE", "PROPOSAL")}
    required = set(scouts) | {ctx.config.get("rover", "rover")}
    return required.issubset(chatters)


def coordinator_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    world_bb = ctx.world or {}

    if phase == "INIT":
        scouts = ctx.config.get("scouts", [])
        ctx.bb.write_world({"task": "city_missions",
                            "phase": "CHAT", "scouts": scouts,
                            "rover": ctx.config.get("rover", "rover"),
                            "facts": {}, "done": False})
        ctx.bb.write_phase("CHAT", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "CHAT"})
        return {
            "thought": "Открываю канал связи. Агенты, обсудите план миссии.",
            "messages": [],
            "idle": False,
        }

    if phase == "CHAT":
        scouts = ctx.config.get("scouts", [])
        msgs_out = []

        if not _coordinator_has_said(ctx, "open_chat"):
            msgs_out.append(make_msg(ctx, "FACILITATE", "all", "CHAT",
                body=(
                    "Открытый канал для обсуждения миссии. "
                    "Нам нужно: 1) облететь зоны и найти очаг пожара, "
                    "2) отправить ровера на тушение через водонапорную башню. "
                    "Предлагаю запустить централизованный скрипт city_mission.py — "
                    "он поднимет всех дронов на заданную высоту, сделает снимки, "
                    "проанализирует их через VLM и отправит ровера по маршруту. "
                    "Обсудите и подтвердите готовность."
                ),
                payload={"tag": "open_chat"},
            ))

        from .phase_util import deadline_passed
        if _should_end_chat(ctx, scouts, deadline_passed(ctx)):
            ctx.bb.write_phase("EXECUTE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "EXECUTE"})
            world_bb["phase"] = "EXECUTE"
            ctx.bb.write_world(world_bb)
            n = len(_chat_messages(ctx))
            return {
                "thought": f"Консенсус достигнут ({n} сообщений). Запускаю city_mission.py.",
                "messages": [
                    make_msg(ctx, "DECISION", "all", "CHAT",
                             body=f"Чат завершён ({n} реплик). Перехожу к выполнению — запускаю центральный скрипт миссии.",
                             payload={"action": "run_city_mission"})
                ],
                "idle": False,
            }

        return {
            "thought": f"Идёт обсуждение: {len(_chat_messages(ctx))} реплик.",
            "messages": msgs_out,
            "idle": not msgs_out,
        }

    if phase == "EXECUTE":
        if world_bb.get("done"):
            return {"thought": "Миссия выполнена.", "messages": [], "idle": True}

        ctx.emit({"kind": "mission_phase", "phase": "running_script"})

        try:
            from city_mission import run_mission, MissionConfig

            hover = float(os.environ.get("HOVER_ALTITUDE", "2.0"))
            cfg = MissionConfig(hover_altitude=hover)

            drone_bridges = {}
            for i in range(1, 5):
                key = f"DRONE{i}_URL"
                url = os.environ.get(key, "")
                if url:
                    drone_bridges[f"drone-{i}"] = url

            rover_url = os.environ.get("ROVER_URL", "")

            result = run_mission(
                drone_bridges=drone_bridges,
                rover_bridge=rover_url,
                brain=ctx.brain,
                bb_root=os.environ.get("BLACKBOARD", "/blackboard"),
                scenario_map=ctx.scenario_map,
                config=cfg,
                emit=ctx.emit,
            )
        except Exception as e:
            result = {"status": "error", "error": str(e)}

        world_bb.update(done=True, phase="DONE", result=result)
        ctx.bb.write_world(world_bb)
        ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
        ok = result.get("status") == "completed"
        fire_cell = result.get("fire_cell", "?")
        body = f"Миссия {'выполнена' if ok else 'завершена с ошибкой'}. Огонь: {fire_cell}."
        return {
            "thought": f"Скрипт отработал: {result.get('status')}.",
            "messages": [make_msg(ctx, "REPORT", "all", "DONE", body=body,
                                  payload=result)],
            "idle": False,
        }

    return {"thought": "Готово.", "messages": [], "idle": True}


def scout_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")

    if phase == "CHAT":
        for m in ctx.messages:
            if m.get("from") == ctx.agent_id and m.get("type") == "CHAT":
                return {"thought": "Уже высказался, жду решения.", "messages": [], "idle": True}
        chat_msgs = _chat_messages(ctx)
        n = len(chat_msgs)
        return {
            "thought": f"Участвую в обсуждении ({n} реплик в чате).",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT",
                         body=(
                             f"{ctx.agent_id} на связи. Зоны для облёта приняты. "
                             "Поддерживаю запуск city_mission.py — "
                             "централизованный запуск эффективнее."
                         ),
                         payload={"agent": ctx.agent_id})
            ],
            "idle": False,
        }

    if phase == "EXECUTE":
        return {"thought": "Жду выполнения миссии координатором.",
                "messages": [], "idle": True}

    return {"thought": "Жду фазу CHAT.", "messages": [], "idle": True}


def rover_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")

    if phase == "CHAT":
        for m in ctx.messages:
            if m.get("from") == ctx.agent_id and m.get("type") == "CHAT":
                return {"thought": "Уже участвовал в обсуждении.", "messages": [], "idle": True}
        return {
            "thought": "Подтверждаю готовность к миссии.",
            "messages": [
                make_msg(ctx, "CHAT", "all", "CHAT",
                         body=(
                             f"{ctx.agent_id} готов. Маршрут: старт → водонапорная башня → "
                             "очаг пожара → возврат на старт. Жду координат."
                         ),
                         payload={"agent": ctx.agent_id})
            ],
            "idle": False,
        }

    if phase == "EXECUTE":
        return {"thought": "Координатор управляет миссией.",
                "messages": [], "idle": True}

    return {"thought": "Жду план миссии.", "messages": [], "idle": True}


def step(ctx) -> dict:
    role = ctx.role
    if role == "coordinator":
        return coordinator_step(ctx)
    if role == "rover":
        return rover_step(ctx)
    return scout_step(ctx)