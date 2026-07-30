#!/usr/bin/env python3
"""Agent runtime loop (spec §7).

read phase/messages -> decide (role behavior) -> novelty-gate -> write
message/progress -> append thought + events. One binary, behavior switches on
ROLE + phase. The brain (mock or a real LLM) supplies the per-cycle reasoning
that becomes the `thought` event the visualizer renders.
"""
from __future__ import annotations

import os
import sys
import time
import types
from datetime import timedelta

from bb import Blackboard, make_board, novelty, now_iso
from binding import load_binding, registration_meta, resolve as resolve_binding, save_binding
from brain import Brain, parse_reasoning
from souls import load_soul
from bridge_client import BridgeClient
import run_log
import roles
from mission_journal import init_journal as _init_journal, journal_record as _jr, get_journal as _jg


def env_list(name, default):
    raw = os.environ.get(name)
    return [x.strip() for x in raw.split(",")] if raw else default


def build_config() -> dict:
    return {
        "task": os.environ.get("TASK", "safe_passage"),  # safe_passage | painting | debate | survey | scan_debate
        "scouts": env_list("SCOUTS", ["drone-1", "drone-2", "drone-3", "drone-4"]),
        # scan_debate (test-3x3): two pilots argue over the scan route; one flyer
        # (the single bort) executes whichever pilot is "at the helm".
        "controllers": env_list("CONTROLLERS", ["pilot-a", "pilot-b"]),
        "flyer": os.environ.get("FLYER", "drone-1"),
        "painters": env_list("PAINTERS", ["painter-1", "painter-2", "painter-3", "painter-4"]),
        "debaters": env_list("DEBATERS", ["debater-1", "debater-2", "debater-3"]),
        # Reusable turn-based debate/decision engine (moderator.py + debate.py).
        # Topic comes from the scenario map (fixtures/<SCENARIO>/map.json); these
        # knobs shape turn order, length, and how the conclusion is decided.
        "debate": {
            "turns": os.environ.get("DEBATE_TURNS", "moderated").lower(),  # moderated|round_robin|parallel
            "min_rounds": int(os.environ.get("DEBATE_MIN_ROUNDS", "1")),
            "max_rounds": int(os.environ.get("DEBATE_MAX_ROUNDS", "3")),
            "rule": ("weighted" if os.environ.get("DEBATE_RULE",
                     os.environ.get("CONVERGENCE", "majority")).lower() == "weighted"
                     else "majority"),
            "consensus": float(os.environ.get("DEBATE_CONSENSUS", "1.0")),  # early-stop share
        },
        "rover": os.environ.get("ROVER", "rover"),
        # city_missions: real_rover=1 -> coordinator compiles the plan and the rover
        # agent drives it through its bridge (real gz cube / hardware Nav2); else the
        # deterministic executor runs inline (mock/test).
        "real_rover": os.environ.get("CITY_REAL_ROVER", "0") not in ("0", "", "false", "no"),
        "sectors": env_list("SECTORS", ["A", "B", "C", "D"]),
        "quadrants": env_list("QUADRANTS", ["Q1", "Q2", "Q3", "Q4"]),
        "zones": env_list("ZONES", ["Z1", "Z2", "Z3", "Z4"]),
        # Survey task (город дронов v2 — поиск груза на поле клеток).
        # wait_per_cell — модель времени долёта («подождать 20 сек»); wait_scale
        # сжимает ожидания в симуляции (0.05 → 20с ≈ 1с), на железе ставьте 1.0.
        "survey": {
            "chat": os.environ.get("SURVEY_CHAT", "1") not in ("0", "", "false", "no"),
            "cells_per_turn": int(os.environ.get("SURVEY_CELLS_PER_TURN", "2")),
            "wait_per_cell": float(os.environ.get("SURVEY_WAIT_SEC", "20")),
            "wait_max": float(os.environ.get("SURVEY_WAIT_MAX", "60")),
            "wait_scale": float(os.environ.get("WAIT_SCALE", "1.0")),
            "quorum": int(os.environ.get("VERIFY_QUORUM", "2")),
            "verify_all": os.environ.get("VERIFY_ALL", "1") not in ("0", "", "false", "no"),
            # тайминги ниже должны переживать МЕДЛЕННЫЙ ход дрона (LLM-план на
            # sverk = до минуты + перелёты): короткий leg на живом прогоне
            # выкидывал живого проверяющего из очереди
            "verify_leg_sec": int(os.environ.get("VERIFY_LEG_SEC", "240")),
            "steal": os.environ.get("SURVEY_STEAL", "1") not in ("0", "", "false", "no"),
            "stale_sec": float(os.environ.get("SURVEY_STALE_SEC", "240")),
            "max_rounds": int(os.environ.get("SURVEY_MAX_ROUNDS", "2")),
            # mapping-режим: цель — ПОЛНЫЙ облёт поля (собрать всю карту), а не
            # поиск груза. Полное покрытие = успех, груз/ровер не задействованы.
            # Включается env SURVEY_MAPPING=1 или map.json "mode":"mapping".
            "mapping": os.environ.get("SURVEY_MAPPING", "0") not in ("0", "", "false", "no"),
        },
        "convergence": os.environ.get("CONVERGENCE", "score"),
        "novelty_min": float(os.environ.get("NOVELTY_MIN", "0.3")),
        "coverage_min": float(os.environ.get("COVERAGE_MIN", "0.9")),
        "paint_mode": os.environ.get("PAINT_MODE", "curated").lower(),
        # Phase deadlines must EXCEED per-call LLM latency, else the phase machine
        # races ahead of slow agents and everything degenerates to fallbacks
        # (bricks/votes land after the phase already closed). Generous by default,
        # env-overridable (DEADLINE_PROPOSE, …).
        "deadlines": {
            "propose": int(os.environ.get("DEADLINE_PROPOSE", "90")),
            "build": int(os.environ.get("DEADLINE_BUILD", "150")),
            "converge": int(os.environ.get("DEADLINE_CONVERGE", "90")),
            "execute": int(os.environ.get("DEADLINE_EXECUTE", "240")),
            "report": int(os.environ.get("DEADLINE_REPORT", "90")),
            # debate phases: opening (all speak once), debate (per round / per
            # floor-grant), vote (all ballots). floor = one speaker's turn budget.
            "opening": int(os.environ.get("DEADLINE_OPENING", "90")),
            "debate": int(os.environ.get("DEADLINE_DEBATE", "120")),
            "floor": int(os.environ.get("DEADLINE_FLOOR", "60")),
            "vote": int(os.environ.get("DEADLINE_VOTE", "90")),
            # studio decentralized chat (free stigmergic discussion before the vote)
            "chat": int(os.environ.get("DEADLINE_CHAT", "180")),
        },
    }


def load_scenario_map(bb: Blackboard) -> dict:
    scenario = os.environ.get("SCENARIO", "scenario-1")
    fixtures = os.environ.get("FIXTURES", "./test_fixtures")
    from pathlib import Path
    p = Path(fixtures) / scenario / "map.json"
    return Blackboard.read_json(p, {}) or {}


def build_ctx(bb, brain, soul_meta, soul_body, agent_id, role, bridge, config, scenario_map):
    ctx = types.SimpleNamespace()
    ctx.bb = bb
    ctx.brain = brain
    ctx.soul = soul_meta
    ctx.soul_body = soul_body
    ctx.agent_id = agent_id
    ctx.role = role
    ctx.bridge = bridge
    ctx.config = config
    ctx.scenario_map = scenario_map
    ctx.emit = bb.append_event
    ctx.phase = bb.read_phase()
    ctx.messages = bb.list_messages()
    ctx.assignments = bb.read_assignments()
    ctx.progress = bb.read_all_progress()
    ctx.world = bb.read_world()
    return ctx


def _sanitize(text: str, limit: int = 320) -> str:
    """Collapse whitespace/newlines so the thought stays one compact event line
    (events.jsonl appends must stay < PIPE_BUF to remain atomic)."""
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def _thought_prompt(ctx, fallback: str):
    system = (ctx.soul_body or "")[:1500]
    if ctx.config.get("task") in ("painting", "survey", "scan_debate", "city_missions"):
        user = (
            f"Фаза={ctx.phase.get('phase')}. Ты — {ctx.agent_id} ({ctx.role}). "
            f"Продумай шаг вслух, затем сформулируй решение. "
            f"Планируемое действие: {fallback}\n"
            f"Пиши на русском языке."
        )
    else:
        user = (
            f"Phase={ctx.phase.get('phase')}. You are {ctx.agent_id} ({ctx.role}). "
            f"Think through your reasoning for this step out loud, then state your "
            f"decision. Planned action: {fallback}"
        )
    return system, user


# --- демо-темп ------------------------------------------------------------
# Живая модель отдаёт токены быстрее, чем человек читает, и на показе лента
# мыслей и чат пролетают сплошным полотном. Эти две паузы растягивают вывод
# для зрителя, не трогая саму логику: 0 = прежнее поведение (максимально
# быстро), demo-slow в Makefile выставляет читаемые значения.
def _pace(var: str) -> float:
    try:
        return max(0.0, float(os.environ.get(var, "0") or 0))
    except ValueError:
        return 0.0


def stream_pause() -> None:
    """Пауза после куска стрима мыслей (эффект печати)."""
    delay = _pace("STREAM_DELAY")
    if delay:
        time.sleep(delay)


def message_pause() -> None:
    """Пауза после отправленного сообщения, чтобы реплики не слипались."""
    delay = _pace("MESSAGE_DELAY")
    if delay:
        time.sleep(delay)


def emit_thought_precooked(bb, agent_id, phase_name, answer: str, thinking: str = ""):
    """Emit thought from an agent turn that already called the LLM (painters)."""
    answer = (answer or "").strip()
    thinking = (thinking or "").strip()
    if thinking:
        bb.append_event({"kind": "thought_start", "from": agent_id, "phase": phase_name})
        chunk = 48
        for i in range(0, len(thinking), chunk):
            bb.append_event({"kind": "thought_delta", "from": agent_id,
                             "text": thinking[i:i + chunk]})
            stream_pause()
        bb.append_event({
            "kind": "thought_end", "from": agent_id, "phase": phase_name,
            "text": _sanitize(answer, 800) or _sanitize(thinking, 800),
            "thinking": _sanitize(thinking, 2400),
        })
    else:
        bb.append_event({"kind": "thought", "from": agent_id,
                         "phase": phase_name, "text": answer or "…"})


def _handle_commands(bb, bridge, agent_id, cfg, handled: set,
                     messages: list | None = None) -> None:
    """Операторские COMMAND-сообщения, адресованные этому дрону (через доску/хаб):

      {"led": {"effect": "blink", "color": "#00ffcc"}}  — LED-регистрация:
          оператор на /fleet «тыкает» дрона, тот мигает лентой выбранным
          цветом — так физический борт сопоставляется с id;
      {"bind": {"handler_id", "fleet", "hub_url"}}      — подтверждение
          привязки: пишем её в ПОСТОЯННУЮ память (BIND_FILE) — переживает
          перезагрузку Raspberry; {"unbind": true} — стираем.

    Работает и на паузе (мигать во время замены АКБ — самый частый случай).
    Идемпотентность: по message id в пределах процесса; повторный blink после
    рестарта безвреден."""
    try:
        msgs = messages if messages is not None else bb.list_messages()
    except Exception:  # noqa: BLE001
        return
    for m in msgs:
        if m.get("type") != "COMMAND" or m.get("to") != agent_id:
            continue
        mid = m.get("id") or f"?{m.get('seq')}"
        if mid in handled:
            continue
        handled.add(mid)
        p = m.get("payload") or {}
        if p.get("led") is not None and bridge is not None:
            led = p["led"] if isinstance(p["led"], dict) else {}
            try:
                bridge.led(effect=str(led.get("effect") or "blink"),
                           color=led.get("color"),
                           r=int(led.get("r", 0)), g=int(led.get("g", 0)),
                           b=int(led.get("b", 0)))
                bb.append_event({"kind": "led", "from": agent_id,
                                 "effect": led.get("effect") or "blink",
                                 "color": led.get("color"), "cmd": mid})
            except Exception as exc:  # noqa: BLE001
                bb.append_event({"kind": "error", "from": agent_id,
                                 "context": "led_command",
                                 "error": f"{type(exc).__name__}: {exc}"})
        if p.get("bind"):
            b = p["bind"] if isinstance(p["bind"], dict) else {}
            cfg.update({
                "handler_id": b.get("handler_id") or cfg.get("handler_id"),
                "fleet": b.get("fleet") or cfg.get("fleet"),
                "hub_url": b.get("hub_url") or os.environ.get("HUB_URL")
                           or cfg.get("hub_url"),
                "agent_id": agent_id, "bound": True, "ts": now_iso(),
            })
            persisted = save_binding({k: cfg[k] for k in
                                      ("handler_id", "hub_url", "agent_id",
                                       "fleet", "bound", "ts") if k in cfg})
            if hasattr(bb, "register"):
                bb.register(os.environ.get("ROLE", ""), registration_meta(cfg))
            bb.append_event({"kind": "bound", "from": agent_id,
                             "handler_id": cfg.get("handler_id"),
                             "fleet": cfg.get("fleet"),
                             "persisted": persisted, "cmd": mid})
            print(f"[{agent_id}] привязан к {cfg.get('handler_id')} "
                  f"(persisted={persisted})", flush=True)
        if p.get("unbind"):
            try:
                from binding import bind_file
                bind_file().unlink(missing_ok=True)
            except OSError:
                pass
            cfg["bound"] = False
            bb.append_event({"kind": "unbound", "from": agent_id, "cmd": mid})


def _pause_enter(bb, bridge, agent_id, phase_name):
    """Вход в глобальную паузу (замена аккумуляторов): посадить дрон (роль с
    бриджем), пометить свой progress как paused (prev_status сохраняем для
    восстановления). Всё best-effort — пауза не должна ронять агента."""
    if bridge is not None:
        try:
            bridge.land()
            bb.append_event({"kind": "pause_land", "from": agent_id})
        except Exception as exc:  # noqa: BLE001
            bb.append_event({"kind": "error", "from": agent_id, "phase": phase_name,
                             "context": "pause_land",
                             "error": f"{type(exc).__name__}: {exc}"})
    try:
        mine = (bb.read_all_progress() or {}).get(agent_id) or {}
        if mine.get("status") != "paused":
            mine["prev_status"] = mine.get("status")
            mine["status"] = "paused"
            mine["ts"] = now_iso()
            bb.write_progress(agent_id, mine)
    except Exception:  # noqa: BLE001
        pass
    bb.append_event({"kind": "thought", "from": agent_id, "phase": phase_name,
                     "text": "⏸ Пауза: сажусь и жду замены аккумулятора."})


def _pause_exit(bb, bridge, agent_id, role, phase_name, paused_secs: float):
    """Выход из паузы: взлёт, ресинк РЕАЛЬНОЙ позиции из бриджа (после замены
    аккумулятора дрон могли переставить — верим телеметрии, не памяти),
    восстановление progress; владелец фазовой машины сдвигает дедлайн фазы на
    длительность паузы, чтобы пауза не съедала бюджет фазы."""
    pose_xy = None
    if bridge is not None:
        try:
            bridge.takeoff()
            pose_xy = (bridge.pose() or {}).get("xy")
            bb.append_event({"kind": "pause_resume", "from": agent_id,
                             "pose": pose_xy})
        except Exception as exc:  # noqa: BLE001
            bb.append_event({"kind": "error", "from": agent_id, "phase": phase_name,
                             "context": "pause_takeoff",
                             "error": f"{type(exc).__name__}: {exc}"})
    try:
        mine = (bb.read_all_progress() or {}).get(agent_id) or {}
        if mine.get("status") == "paused":
            prev = mine.pop("prev_status", None)
            mine["status"] = prev or "resuming"
            if pose_xy is not None:
                mine["cell"] = [int(pose_xy[0]), int(pose_xy[1])]
            mine["ts"] = now_iso()
            bb.write_progress(agent_id, mine)
    except Exception:  # noqa: BLE001
        pass
    if role in ("coordinator", "moderator"):
        try:
            from roles.phase_util import iso as _iso, parse_iso as _parse_iso
            ph = bb.read_phase()
            dl = _parse_iso(ph.get("deadline"))
            if dl is not None:
                bb.write_phase(ph.get("phase", "INIT"), ph.get("round", 0),
                               _iso(dl + timedelta(seconds=paused_secs)))
            bb.append_event({"kind": "pause_shift", "from": agent_id,
                             "secs": round(paused_secs)})
        except Exception:  # noqa: BLE001
            pass
    bb.append_event({"kind": "thought", "from": agent_id, "phase": phase_name,
                     "text": "▶ Пауза снята: взлетаю"
                             + (f", я в клетке {pose_xy}." if pose_xy else ".")})


def emit_thought(bb, brain, ctx, agent_id, phase_name, fallback):
    """Stream the model's reasoning to events.jsonl so the dashboard shows the
    agent thinking live (thought_start -> thought_delta* -> thought_end). With
    the mock brain (no LLM) we just emit the single templated thought."""
    if brain.is_mock:
        # carry the provider on the event: the dashboard must show a MOCK brain
        # for what it is — canned lines are not "the LLM talking"
        bb.append_event({"kind": "thought", "from": agent_id, "brain": "mock",
                         "phase": phase_name, "text": fallback})
        return
    system, user = _thought_prompt(ctx, fallback)
    bb.append_event({"kind": "thought_start", "from": agent_id, "phase": phase_name,
                     "brain": brain.provider})
    full, buf = "", ""
    for delta in brain.think_stream(system, user):  # never raises
        full += delta
        buf += delta
        if len(buf) >= 24:  # batch tokens to keep the event count sane
            bb.append_event({"kind": "thought_delta", "from": agent_id, "text": buf})
            buf = ""
            stream_pause()
    if buf:
        bb.append_event({"kind": "thought_delta", "from": agent_id, "text": buf})
    thinking, answer = parse_reasoning(full)
    display = _sanitize(answer or fallback, 800) or _sanitize(full, 800)
    bb.append_event({
        "kind": "thought_end", "from": agent_id, "phase": phase_name,
        "text": display,
        "thinking": _sanitize(thinking or full, 2400),
    })


def main():
    # привязка из постоянной памяти дрона (BIND_FILE) + env-профиль проекта;
    # env приоритетнее (см. agent/binding.py). Локальные запуски без
    # HANDLER_ID/файла работают как раньше.
    bind_cfg = resolve_binding(dict(os.environ), load_binding())
    agent_id = os.environ.get("AGENT_ID") or bind_cfg.get("agent_id") \
        or sys.exit("AGENT_ID required (env или сохранённая привязка)")
    role = os.environ.get("ROLE") or sys.exit("ROLE required")
    if bind_cfg.get("stored_conflict"):
        print(f"[{agent_id}] ВНИМАНИЕ: env HANDLER_ID={os.environ.get('HANDLER_ID')} "
              f"≠ сохранённой привязке — считаю дрона непривязанным", flush=True)
    # FileBoard (co-located / dev) or HttpBoard (drone -> hub) by HUB_URL
    bb = make_board(agent_id)
    bb.ensure_layout()

    # Fresh-start reset: the coordinator owns the board, so it wipes a previous
    # run's stale state at boot (default on). This is what keeps "make demo" /
    # a re-`make up` on the persisted volume from replaying an old run -- without
    # it, agents read a leftover phase=DONE + old messages and look like they
    # "already talked" before the demo even started. Disable with RESET_ON_START=0.
    reset_on_start = os.environ.get("RESET_ON_START", "1") not in ("0", "", "false", "no")
    # coordinator OR moderator: whichever role owns the phase machine owns the board
    if role in ("coordinator", "moderator") and reset_on_start and isinstance(bb, Blackboard):
        bb.reset_runtime()
        print(f"[{agent_id}] reset blackboard for a fresh run", flush=True)

    brain = Brain()
    config = build_config()

    soul_path = os.environ.get("SOUL", f"./souls/{agent_id}.md")
    if role == "coordinator" and config["task"] == "painting" and not os.environ.get("SOUL"):
        alt = os.path.join(os.path.dirname(soul_path), "coordinator-painting.md")
        if os.path.isfile(alt):
            soul_path = alt
    try:
        soul_meta, soul_body = load_soul(soul_path)
    except FileNotFoundError:
        soul_meta, soul_body = {}, ""
    scenario_map = load_scenario_map(bb)

    # distributed: wait for the hub, then register so it shows up & is discoverable.
    # Регистрация несёт handler_id/fleet/bound: чужой хаб (другая команда или
    # другой флот) откажет сразу — команды не путаются, чей это дрон.
    if os.environ.get("HUB_URL"):
        for _ in range(60):
            if bb.healthz():
                break
            time.sleep(1)
        bb.register(role, registration_meta(bind_cfg, {
            "name": soul_meta.get("name", agent_id),
            "capabilities": soul_meta.get("capabilities", []),
            "bridge": bool(os.environ.get("BRIDGE_URL"))}))
        state = ("bound to " + bind_cfg["handler_id"]) if bind_cfg.get("bound") \
            else "UNBOUND (жду LED-регистрации на /fleet)"
        print(f"[{agent_id}] registered with hub {os.environ['HUB_URL']} — {state}",
              flush=True)

    bridge = None
    bridge_url = os.environ.get("BRIDGE_URL")
    if bridge_url:
        # BRIDGE_TIMEOUT must exceed the bridge's max /move time (REACH_TIMEOUT +
        # takeoff/climb); real sverk flight is slower than the mock, so a 30s
        # client timeout disconnects mid-flight -> BrokenPipe + confused coverage.
        bridge = BridgeClient(bridge_url,
                              timeout=float(os.environ.get("BRIDGE_TIMEOUT", "120")))
        for _ in range(60):
            if bridge.healthz():
                break
            time.sleep(1)

    poll = float(os.environ.get("POLL_INTERVAL", "1.0"))
    keep_alive = os.environ.get("KEEP_ALIVE", "0") not in ("0", "", "false", "no")
    last_thought = None
    pause_active = False
    pause_t0 = 0.0
    done_final_step = False
    handled_cmds: set = set()
    print(f"[{agent_id}] role={role} provider={brain.provider} online"
          f"{'' if not keep_alive else ' keep_alive=1'}", flush=True)

    # инициализация журнала миссии (coordinator сбрасывает старый)
    if role == "coordinator":
        j = _init_journal(os.environ.get("BLACKBOARD", "./blackboard"))
        j.record("mission_start", task=config["task"], scenario=os.environ.get("SCENARIO", "?"))
    _prev_phase = ""

    while True:
        run_log.refresh_from_bb(bb)

        # --- глобальная пауза (замена аккумуляторов), state/pause.json ------
        # Оператор ставит паузу через hub/viz (POST /pause) или make pause.
        # Дроны садятся и молчат, фазовая машина замирает; на выходе — взлёт,
        # ресинк позы из бриджа и сдвиг фазового дедлайна на время паузы.
        pause = bb.read_pause() if hasattr(bb, "read_pause") else {"paused": False}
        if pause.get("paused"):
            if not pause_active:
                pause_active = True
                pause_t0 = time.monotonic()
                cur_phase = bb.read_phase().get("phase", "?")
                print(f"[{agent_id}] ⏸ pause: {pause.get('reason') or 'battery swap'}",
                      flush=True)
                _pause_enter(bb, bridge, agent_id, cur_phase)
            # LED-команды работают и на паузе — мигнуть бортом при замене АКБ
            _handle_commands(bb, bridge, agent_id, bind_cfg, handled_cmds)
            time.sleep(poll)
            continue
        if pause_active:
            pause_active = False
            paused_secs = time.monotonic() - pause_t0
            cur_phase = bb.read_phase().get("phase", "?")
            print(f"[{agent_id}] ▶ resume after {paused_secs:.0f}s", flush=True)
            _pause_exit(bb, bridge, agent_id, role, cur_phase, paused_secs)

        ctx = build_ctx(bb, brain, soul_meta, soul_body, agent_id, role,
                        bridge, config, scenario_map)
        phase_name = ctx.phase.get("phase")
        run_log.set_agent_context(agent_id, phase_name)

        # запись смены фазы в журнал
        if phase_name and phase_name != _prev_phase:
            _jr("phase_transition", agent=agent_id, phase=phase_name)
            _prev_phase = phase_name

        _handle_commands(bb, bridge, agent_id, bind_cfg, handled_cmds,
                         messages=ctx.messages)

        if phase_name == "DONE":
            # финальный шаг для ролей из STEP_AT_DONE (критик выносит вердикт
            # по ГОТОВОЙ картине) — один раз, дальше обычный выход
            if role in getattr(roles, "STEP_AT_DONE", ()) and not done_final_step:
                done_final_step = True
            elif keep_alive:
                time.sleep(poll)
                continue
            else:
                if last_thought != "DONE":
                    bb.append_event({"kind": "thought", "from": agent_id,
                                     "phase": "DONE", "text": "Запуск завершён."})
                print(f"[{agent_id}] phase=DONE, exiting", flush=True)
                break

        # Never let one bad decide-cycle halt the phase machine. The coordinator
        # is a single writer/driver; an uncaught exception here would freeze every
        # agent forever (the one real non-termination hole). Log, surface, idle.
        try:
            result = roles.step(ctx)
        except Exception as exc:  # noqa: BLE001 — resilience boundary
            import traceback
            bb.append_event({"kind": "error", "from": agent_id,
                             "phase": phase_name or "?", "context": "role_step",
                             "error": f"{type(exc).__name__}: {exc}"})
            print(f"[{agent_id}] step error: {exc}\n{traceback.format_exc()}", flush=True)
            result = {"thought": "", "messages": [], "idle": True}
        thought = result.get("thought", "")
        thinking = result.get("thinking")

        # emit thought (deduped). Phase-machine owners (coordinator/moderator)
        # narrate deterministic decisions — never burn a streaming LLM call per
        # thought there (as ROLE=moderator it once queued the CONCLUDE tally
        # behind minutes of narration streams).
        if thought and thought != last_thought:
            if thinking is not None:
                emit_thought_precooked(bb, agent_id, phase_name, thought, thinking)
            elif ctx.role in ("coordinator", "moderator"):
                emit_thought_precooked(bb, agent_id, phase_name, thought, "")
            else:
                emit_thought(bb, brain, ctx, agent_id, phase_name, thought)
            last_thought = thought
            # журнал: мысль агента
            _jr("agent_thought", agent=agent_id, phase=phase_name, thought=thought[:300])

        # novelty-gate + post messages. The gate (§6.4) suppresses redundant
        # free-form chatter (PROPOSALs); protocol-critical messages always post
        # but still carry a novelty score for the visualizer.
        gated_types = {"PROPOSAL"}
        existing = [f"{m.get('body','')} {m.get('payload','')}" for m in ctx.messages]
        for msg in result.get("messages", []):
            cand = f"{msg.get('body','')} {msg.get('payload','')}"
            nov = novelty(cand, existing)
            if msg.get("type") in gated_types and nov < config["novelty_min"]:
                bb.append_event({"kind": "drop", "from": agent_id,
                                 "type": msg.get("type"), "novelty": nov})
                continue
            msg["novelty"] = nov
            msg.setdefault("ts", now_iso())
            stored = bb.write_message(msg)  # returns the stored msg (with id)
            ev = {"kind": "message", "from": msg["from"],
                  "to": msg["to"], "phase": msg.get("phase"),
                  "type": msg["type"], "novelty": nov,
                  "body": msg.get("body", ""),
                  "payload": msg.get("payload") or {},
                  "id": stored.get("id")}
            bb.append_event(ev)
            run_log.log_event(ev)
            existing.append(cand)
            # журнал: сообщение агента
            _jr("agent_message", agent=agent_id, to=msg.get("to"),
                phase=msg.get("phase"), msg_type=msg.get("type"),
                body=msg.get("body", "")[:200])
            message_pause()

        time.sleep(poll)


if __name__ == "__main__":
    main()
