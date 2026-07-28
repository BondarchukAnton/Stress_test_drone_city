"""Survey scout: дрон-разведчик миссии «поиск груза» (task=survey).

CHAT    — договаривается с остальными о зоне облёта (та же механика, что
          scout_chat: последний валидный claim побеждает, done-консенсус).
EXECUTE — ходами облетает свою зону. Каждый ход — JSON-план:
              [{"do":"fly_to","cell":[1,1]},
               {"do":"wait","seconds":20},
               {"do":"photo_analyze"}]
          План строит LLM (schema-enforced) или детерминированный планировщик
          (mock/фолбэк); исполняется через bridge (move → pose → photograph →
          analyze). Позиция после каждого перелёта берётся ИЗ БРИДЖА (closed
          loop): на железе это реальная поза из ROS2, не намерение.
          Находка → FOUND всем; очередь верификации приходит ASSIGNMENT'ом от
          координатора — дрон летит к чужой находке, смотрит вблизи и голосует
          VERIFY. Пока не его очередь, он продолжает осмотр своей зоны.
"""
from __future__ import annotations

import time

from brain import json_schema_format, parse_llm_json, schema_supported
from context_budget import OUTPUT_AGENT_TURN, build_system_prompt, negotiation_messages

from . import make_msg
from .phase_util import iso, now, parse_iso
from .survey_common import (
    cell_key,
    grid_size,
    plan_summary,
    sweep_plan,
    validate_plan,
    verify_plan,
)


def _cfg(ctx) -> dict:
    return ctx.config.get("survey", {})


def _name(ctx) -> str:
    return ctx.soul.get("name", ctx.agent_id)


def _my_zone(ctx) -> tuple[str, list[list[int]]]:
    lab = ctx.assignments.get(ctx.agent_id)
    if not isinstance(lab, str):
        return "", []
    zones = (ctx.world or {}).get("zones") or {}
    return lab, [list(c) for c in zones.get(lab, [])]


def _covered_keys(ctx) -> set[str]:
    mine = ctx.progress.get(ctx.agent_id) or {}
    return {cell_key(c) for c in mine.get("covered") or []}


def _world_cell_status(ctx, cell) -> str:
    cells = (ctx.world or {}).get("cells") or {}
    return (cells.get(cell_key(cell)) or {}).get("status", "unknown")


def _pose(ctx) -> list[int]:
    """Closed loop: ask the bridge where we actually are; fall back to the last
    progress-reported cell, then to the field start."""
    try:
        p = ctx.bridge.pose()
        xy = p.get("xy")
        if isinstance(xy, list) and len(xy) == 2:
            return [int(xy[0]), int(xy[1])]
    except Exception:  # noqa: BLE001 — flaky link must not kill the turn
        pass
    mine = ctx.progress.get(ctx.agent_id) or {}
    if isinstance(mine.get("cell"), list):
        return [int(c) for c in mine["cell"]]
    return list((ctx.world or {}).get("start") or [0, 0])


def _pose_info(ctx) -> dict:
    """Full /pose dict (не только xy): нужен cv2-aruco reality-check полей
    reality_ok / aruco_cell / on_field. Mock-бридж их не отдаёт → {}."""
    try:
        p = ctx.bridge.pose()
        return p if isinstance(p, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _reality_trusts(info: dict, claim_cell) -> tuple[bool, str]:
    """Гейт реальности: доверяем ПОЛОЖИТЕЛЬНОЙ находке/голосу, только если
    независимая cv2-aruco камера подтверждает, что дрон реально над той клеткой,
    которую он фотографирует. Fail-open: если бридж не отдаёт reality-поля
    (mock) — доверяем. Отклоняем ТОЛЬКО при явном противоречии:
      • on_field=false     — камера не видит поле (дрон снесло за поле),
      • aruco_cell ≠ claim — камера видит другую клетку,
      • reality_ok=false   — позиция не подтверждена телеметрией aruco."""
    if not isinstance(info, dict) or not info:
        return True, ""
    if info.get("on_field") is False:
        return False, "камера не видит поле (off-field)"
    ac = info.get("aruco_cell")
    if ac is not None and claim_cell is not None and len(ac) == 2:
        if [int(ac[0]), int(ac[1])] != [int(claim_cell[0]), int(claim_cell[1])]:
            return False, (f"aruco видит [{int(ac[0])},{int(ac[1])}], "
                           f"не [{int(claim_cell[0])},{int(claim_cell[1])}]")
    if info.get("reality_ok") is False:
        return False, "reality_ok=false (позиция не подтверждена aruco)"
    return True, ""


# ---- plan execution --------------------------------------------------------
def _pause_requested(ctx) -> bool:
    """Свежее чтение паузы ПРЯМО во время исполнения плана: глобальная пауза
    (замена АКБ) должна прерывать многоклеточный ход, а не ждать его конца."""
    try:
        return bool(ctx.bb.read_pause().get("paused")) \
            if hasattr(ctx.bb, "read_pause") else False
    except Exception:  # noqa: BLE001
        return False


def _wait_interruptible(ctx, secs: float) -> bool:
    """Sleep in short chunks; True = пауза прервала ожидание."""
    remaining = min(secs, 120.0)
    while remaining > 0:
        step = min(2.0, remaining)
        time.sleep(step)
        remaining -= step
        if _pause_requested(ctx):
            return True
    return False


def _execute_plan(ctx, plan: list[dict]) -> tuple[list[dict], list[int], list[list[int]]]:
    """Run the JSON action plan against the bridge. Returns (results, final
    pose, cells photographed). Closed loop: after every fly_to the drone reads
    the bridge pose and photographs where it ACTUALLY is. Глобальная пауза
    прерывает план между действиями (и внутри wait) — остаток хода просто не
    исполняется, дрон сядет на следующем цикле loop."""
    cfg = _cfg(ctx)
    scale = float(cfg.get("wait_scale", 1.0))
    results: list[dict] = []
    pose = _pose(ctx)
    photographed: list[list[int]] = []
    for act in plan:
        if _pause_requested(ctx):
            results.append({"do": "abort", "reason": "pause"})
            break
        if act["do"] == "fly_to":
            target = act["cell"]
            try:
                ctx.bridge.move(target)
            except Exception as exc:  # noqa: BLE001
                results.append({"do": "fly_to", "cell": target, "ok": False,
                                "error": type(exc).__name__})
                continue
            pose = _pose(ctx)
            if pose != [int(target[0]), int(target[1])]:
                # ветер/дрейф: одна повторная попытка, дальше работаем там, где мы есть
                try:
                    ctx.bridge.move(target)
                    pose = _pose(ctx)
                except Exception:  # noqa: BLE001
                    pass
            ctx.emit({"kind": "drone_move", "from": ctx.agent_id, "cell": pose})
            results.append({"do": "fly_to", "cell": target, "pose": pose,
                            "ok": pose == [int(target[0]), int(target[1])]})
        elif act["do"] == "wait":
            secs = float(act.get("seconds", 0)) * scale
            interrupted = _wait_interruptible(ctx, secs)
            results.append({"do": "wait", "seconds": act.get("seconds", 0)})
            if interrupted:
                results.append({"do": "abort", "reason": "pause"})
                break
        elif act["do"] == "photo_analyze":
            close = bool(act.get("close_look"))
            try:
                shot = ctx.bridge.photograph_cell(pose)
                res = ctx.bridge.analyze(pose, close_look=close)
            except Exception as exc:  # noqa: BLE001
                results.append({"do": "photo_analyze", "cell": pose, "ok": False,
                                "error": type(exc).__name__})
                continue
            # независимый reality-check камеры В МОМЕНТ снимка (cv2 aruco)
            info = _pose_info(ctx)
            ctx.emit({"kind": "artifact", "from": ctx.agent_id,
                      "path": shot.get("image_path", ""), "phase": "EXECUTE",
                      "cell": pose})
            # вердикт детектора — на дашборд; "fallback:map(...)" в поле vlm =
            # VLM недоступен и бридж тихо ответил правдой карты — это надо ВИДЕТЬ
            ctx.emit({"kind": "analyze", "from": ctx.agent_id, "cell": list(pose),
                      "cargo": bool(res.get("cargo")),
                      "confidence": res.get("confidence"),
                      "vlm": str(res.get("vlm") or "")[:120]})
            photographed.append(list(pose))
            results.append({"do": "photo_analyze", "cell": list(pose),
                            "close_look": close,
                            "cargo": bool(res.get("cargo")),
                            "confidence": res.get("confidence"),
                            "label": res.get("label"), "ok": True,
                            "vlm": res.get("vlm"),
                            "reality_ok": info.get("reality_ok"),
                            "aruco_cell": info.get("aruco_cell"),
                            "on_field": info.get("on_field")})
    return results, pose, photographed


def _write_progress(ctx, status: str, pose, covered_keys: set[str],
                    zone: str, target: list | None = None) -> None:
    ctx.bb.write_progress(ctx.agent_id, {
        "status": status, "zone": zone, "cell": pose,
        "covered": [list(map(int, k.split(","))) for k in sorted(covered_keys)],
        # куда дрон СОБИРАЕТСЯ лететь этим ходом (для маркеров-целей на дашборде)
        "target": [list(map(int, c)) for c in (target or [])],
        "ts": iso(now()),
    })


def _plan_targets(plan: list[dict]) -> list[list[int]]:
    """Клетки, в которые дрон намерен лететь этим ходом (fly_to из плана)."""
    return [[int(a["cell"][0]), int(a["cell"][1])] for a in plan
            if a.get("do") == "fly_to" and isinstance(a.get("cell"), (list, tuple))]


def _emit_targets(ctx, targets: list[list[int]]) -> None:
    """Показать на дашборде намерение дрона ДО перелёта (живой маркер цели)."""
    ctx.emit({"kind": "target", "from": ctx.agent_id, "phase": "EXECUTE",
              "cells": targets})


def _plan_message(ctx, plan, results, pose, say: str = "") -> dict:
    body = (f"{say} " if say else "") + f"Ход: {plan_summary(plan)}"
    found = [r for r in results if r.get("do") == "photo_analyze" and r.get("cargo")]
    if found:
        c = found[0]["cell"]
        body += f" — в [{c[0]},{c[1]}] похоже на ГРУЗ ({found[0].get('confidence')})"
    return make_msg(ctx, "PLAN", "all", "EXECUTE", body=body,
                    payload={"plan": plan, "results": results, "pose": pose,
                             "name": _name(ctx)})


def _has_found(ctx, cell) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "FOUND":
            p = m.get("payload") or {}
            if p.get("cell") is not None and cell_key(p["cell"]) == cell_key(cell):
                return True
    return False


def _has_verified(ctx, cell) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "VERIFY":
            p = m.get("payload") or {}
            if p.get("cell") is not None and cell_key(p["cell"]) == cell_key(cell):
                return True
    return False


def _stealable_cells(ctx, covered_all: set[str]) -> list[list[int]]:
    """Cells this drone may take over when its own zone is done. ONLY cells of
    a STALE owner (no progress heartbeat for survey.stale_sec) or of no owner —
    a live drone's pending cells are spoken for. Prevents the first mock run's
    failure mode: a fast drone «helped» a busy-but-alive neighbour and the
    field got double-photographed."""
    cfg = _cfg(ctx)
    stale_sec = float(cfg.get("stale_sec", 60))
    zones = (ctx.world or {}).get("zones") or {}
    owner_of: dict[str, str] = {}
    for sid, lab in (ctx.assignments or {}).items():
        if isinstance(lab, str):
            for c in zones.get(lab, []):
                owner_of[cell_key(c)] = sid
    out: list[list[int]] = []
    w, h = grid_size(ctx.scenario_map)
    for y in range(h):
        for x in range(w):
            k = cell_key([x, y])
            if k in covered_all:
                continue
            owner = owner_of.get(k)
            if owner and owner != ctx.agent_id:
                t = parse_iso((ctx.progress.get(owner) or {}).get("ts"))
                alive = t is not None and (now() - t).total_seconds() < stale_sec
                if alive:
                    continue  # владелец жив — не лезем в его зону
            out.append([x, y])
    pose0 = _pose(ctx)
    out.sort(key=lambda c: (max(abs(c[0] - pose0[0]), abs(c[1] - pose0[1])), c[1], c[0]))
    return out[:int(cfg.get("cells_per_turn", 2))]


def _my_verify_duty(ctx) -> list | None:
    """The cell I must verify NOW: the coordinator assigned it to me
    (sequential queue — «по очереди») and I haven't answered yet."""
    cand = (ctx.world or {}).get("candidate") or {}
    cell = cand.get("cell")
    if cell is None:
        return None
    for m in ctx.messages:
        if (m.get("type") == "ASSIGNMENT" and m.get("to") == ctx.agent_id
                and (m.get("payload") or {}).get("verify_cell") is not None
                and cell_key(m["payload"]["verify_cell"]) == cell_key(cell)):
            return list(cell) if not _has_verified(ctx, cell) else None
    return None


# ---- LLM sweep planning -----------------------------------------------------
def _llm_sweep_plan(ctx, pose, targets: list[list[int]], fallback: list[dict],
                    zone: str) -> tuple[list[dict], str, str]:
    """Ask the brain for the next turn's action plan. Returns (plan, say,
    thinking); falls back to the deterministic plan on any failure."""
    cfg = _cfg(ctx)
    w, h = grid_size(ctx.scenario_map)
    name = _name(ctx)
    world = ctx.world or {}
    cand = world.get("candidate") or {}
    others = {k: v for k, v in (world.get("positions") or {}).items()
              if k != ctx.agent_id}
    tgt_txt = ", ".join(f"[{c[0]},{c[1]}]" for c in targets[:12])
    system = build_system_prompt(
        ctx.soul_body or "",
        f"Ты — дрон-разведчик {name} ({ctx.agent_id}) в миссии поиска груза на "
        f"поле {w}×{h}. Твоя зона {zone}. Ты планируешь СЛЕДУЮЩИЙ ход как JSON-"
        "план действий; исполняет его автопилот. Доступные действия: fly_to "
        "(перелёт в клетку, можно через несколько клеток), wait (секунды на "
        "долёт, ~20с на клетку), photo_analyze (фото текущей клетки сверху + "
        "мгновенный анализ). Фотографируй только клетки своей зоны.")
    task = (
        f"Ты в клетке [{pose[0]},{pose[1]}]. Непройденные клетки твоей зоны: "
        f"{tgt_txt}{'…' if len(targets) > 12 else ''}.\n"
        f"Позиции остальных: {others}.\n"
        + (f"Идёт проверка находки в [{cand['cell'][0]},{cand['cell'][1]}] — "
           f"не твоя очередь, продолжай осмотр.\n" if cand.get("cell") else "")
        + f"За ход можно осмотреть до {int(cfg.get('cells_per_turn', 2))} клеток "
          "(fly_to → wait → photo_analyze на каждую).\n"
          'Ответ — ОДИН JSON: {"thinking":"краткий расчёт",'
          '"say":"реплика в канал или \'\'",'
          '"actions":[{"do":"fly_to","cell":[x,y]},{"do":"wait","seconds":20},'
          '{"do":"photo_analyze"}]}')
    rf = None
    if schema_supported(ctx.brain):
        rf = json_schema_format("survey_plan", {
            "type": "object",
            "properties": {
                "thinking": {"type": "string"},
                "say": {"type": "string", "maxLength": 200},
                "actions": {
                    "type": "array", "minItems": 1, "maxItems": 9,
                    "items": {
                        "type": "object",
                        "properties": {
                            "do": {"type": "string",
                                   "enum": ["fly_to", "wait", "photo_analyze"]},
                            "cell": {"type": "array", "minItems": 2, "maxItems": 2,
                                     "items": {"type": "integer", "minimum": 0,
                                               "maximum": max(w, h) - 1}},
                            "seconds": {"type": "number", "minimum": 0,
                                        "maximum": float(cfg.get("wait_max", 60))},
                        },
                        "required": ["do"],
                    },
                },
            },
            "required": ["actions"],
        })
    msgs = negotiation_messages(ctx, task, phases=("CHAT",))
    raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                         log_context="survey_plan", response_format=rf)
    if not raw and rf is not None:
        raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                             log_context="survey_plan")
    data, parse_err = parse_llm_json(raw or "")
    if not isinstance(data, dict):
        ctx.emit({"kind": "llm_error", "from": ctx.agent_id, "phase": "EXECUTE",
                  "context": "survey_plan", "attempt": 1, "max_attempts": 1,
                  "error": ((parse_err or "пустой ответ")
                            + " — использую детерминированный план")[:600],
                  "exhausted": True})
        return fallback, "", ""
    plan, problems = validate_plan(
        data.get("actions"), w=w, h=h, allowed_cells=targets,
        max_actions=3 * int(cfg.get("cells_per_turn", 2)) + 3,
        wait_max=float(cfg.get("wait_max", 60)))
    if not plan:
        ctx.emit({"kind": "llm_error", "from": ctx.agent_id, "phase": "EXECUTE",
                  "context": "survey_plan", "attempt": 1, "max_attempts": 1,
                  "error": ("план отклонён: " + "; ".join(problems)
                            + " — использую детерминированный")[:600],
                  "exhausted": True})
        return fallback, str(data.get("say") or "")[:200], str(data.get("thinking") or "")[:2000]
    if problems:
        ctx.emit({"kind": "plan_fix", "from": ctx.agent_id,
                  "problems": problems[:6]})
    return plan, str(data.get("say") or "")[:200], str(data.get("thinking") or "")[:2000]


# ---- chat turn (zone negotiation, survey wording) ---------------------------
def survey_chat_turn(ctx) -> dict:
    from .scout_chat import _should_speak  # same local speak/yield policy
    scouts = list(ctx.config.get("scouts") or [])
    labels = list(((ctx.world or {}).get("zones") or {}).keys()) or \
        list(ctx.config.get("zones") or [])
    speak, reply_name, yield_now = _should_speak(ctx, scouts, labels)
    if not speak:
        return {"thought": "Слушаю канал — моя заявка на зону в силе.",
                "thinking": "", "messages": [], "idle": True}

    from .studio_chat import chat_messages
    name = _name(ctx)
    zones = (ctx.world or {}).get("zones") or {}
    chat = chat_messages(ctx)
    others_claimed = {
        str((m.get("payload") or {}).get("claim") or "").strip().upper()
        for m in chat if m.get("from") != ctx.agent_id}
    idx = scouts.index(ctx.agent_id) if ctx.agent_id in scouts else 0
    pref = labels[idx % len(labels)] if labels else "Z1"
    claim = pref if pref not in others_claimed else next(
        (z for z in labels if z not in others_claimed), pref)
    n_cells = len(zones.get(claim, []))
    fallback = {
        "line": (f"{name}: беру зону {claim} ({n_cells} кл.) — залетаю змейкой."
                 + (f" {reply_name}, не против?" if reply_name else "")),
        "claim": claim, "address": reply_name, "done": True, "thinking": "",
    }
    if yield_now:
        fallback["line"] = f"{name}: раздел меня устраивает — закрепляю {claim}."
    turn = dict(fallback)

    if not ctx.brain.is_mock:
        recent = "\n".join(
            f"- {(m.get('payload') or {}).get('name', m.get('from'))}: {m.get('body', '')}"
            for m in chat[-6:]) or "(тишина — ты открываешь переговоры)"
        brief = "; ".join(f"{lab}: {len(cs)} кл." for lab, cs in zones.items())
        system = build_system_prompt(
            ctx.soul_body or "",
            f"Ты — дрон-разведчик {name} ({ctx.agent_id}). Команда "
            f"({', '.join(scouts)}) делит поле поиска груза на зоны ({brief}) — "
            "каждому по одной, без дыр. Переговоры в открытом канале: заяви "
            "зону, обоснуй, отвечай коллегам по имени, меняйся при конфликте. "
            "Коротко и конкретно, по-русски.")
        task = (
            f"Недавние реплики:\n{recent}\n\n"
            + (f"{reply_name} обратился к тебе — ответь. " if reply_name else "")
            + ("Похоже, раздел сложился. Если согласен — подтверди зону и "
               "поставь done:true. " if yield_now else
               "Заяви (или защити/поменяй) СВОЮ зону. Занята — поспорь или "
               "возьми другую. ")
            + 'Ответ — один JSON: {"thinking":"…","line":"1-2 предложения в канал",'
              f'"claim":"одна из {labels}","address":"имя или \'\'",'
              '"done": true когда раздел устраивает}')
        rf = None
        if schema_supported(ctx.brain):
            rf = json_schema_format("survey_chat", {
                "type": "object",
                "properties": {
                    "thinking": {"type": "string"},
                    "line": {"type": "string"},
                    "claim": {"type": "string", "enum": labels or ["Z1"]},
                    "address": {"type": "string", "maxLength": 40},
                    "done": {"type": "boolean"},
                },
                "required": ["line", "claim", "done"],
            })
        msgs = negotiation_messages(ctx, task, phases=("CHAT",))
        raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                             log_context="survey_chat", response_format=rf)
        if not raw and rf is not None:
            raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                                 log_context="survey_chat")
        data, parse_err = parse_llm_json(raw or "")
        if isinstance(data, dict):
            c = str(data.get("claim") or "").strip().upper()
            if c in labels:
                turn["claim"] = c
            ln = str(data.get("line") or "").strip()
            if ln:
                turn["line"] = ln[:300]
            turn["address"] = str(data.get("address") or "").strip()[:40]
            if isinstance(data.get("done"), bool):
                turn["done"] = data["done"]
            turn["thinking"] = str(data.get("thinking") or "")[:2000]
        else:
            ctx.emit({"kind": "llm_error", "from": ctx.agent_id, "phase": "CHAT",
                      "context": "survey_chat", "attempt": 1, "max_attempts": 1,
                      "error": ((parse_err or "пустой ответ") + " — заготовка")[:600],
                      "exhausted": True})

    valid_names = {s.lower() for s in scouts}
    for m in chat:
        n = str((m.get("payload") or {}).get("name") or "").strip()
        if n:
            valid_names.add(n.lower())
    address = turn.get("address", "")
    if address and address.lower() not in valid_names and address != reply_name:
        address = ""
    msg = make_msg(ctx, "CHAT", "all", "CHAT", body=turn["line"],
                   payload={"name": name, "claim": turn["claim"],
                            "address": address, "done": bool(turn.get("done"))})
    tag = " ✓зона за мной" if turn.get("done") else ""
    return {"thought": turn["line"] + tag, "thinking": turn.get("thinking", ""),
            "messages": [msg], "idle": False}


# ---- EXECUTE turn ------------------------------------------------------------
def _finish_if_over(ctx, zone: str) -> dict | None:
    world = ctx.world or {}
    mine = ctx.progress.get(ctx.agent_id) or {}
    over = bool(world.get("confirmed")) or world.get("mode") == "done" \
        or ctx.phase.get("phase") == "REPORT"
    if not over:
        return None
    if mine.get("status") != "done":
        covered = _covered_keys(ctx)
        _write_progress(ctx, "done", _pose(ctx) if ctx.bridge else
                        mine.get("cell") or [0, 0], covered, zone, target=[])
        msg = make_msg(ctx, "REPORT", "coordinator", "REPORT",
                       body=f"Осмотр завершён: {len(covered)} клеток снято.",
                       payload={"covered": len(covered), "zone": zone})
        return {"thought": "Миссия решена — фиксирую свой итог.",
                "messages": [msg], "idle": False}
    return {"thought": "Готов; жду финального вердикта.", "messages": [], "idle": True}


def execute_turn(ctx) -> dict:
    zone, zone_cells = _my_zone(ctx)
    if ctx.bridge is None:
        return {"thought": "Нет бриджа — лететь нечем.", "messages": [], "idle": True}
    if not zone:
        return {"thought": "Жду назначения зоны.", "messages": [], "idle": True}

    fin = _finish_if_over(ctx, zone)
    if fin is not None:
        return fin

    cfg = _cfg(ctx)
    covered = _covered_keys(ctx)

    # 1) verification duty first: «моя очередь проверить чужую находку»
    duty = _my_verify_duty(ctx)
    if duty is not None:
        pose = _pose(ctx)
        plan = verify_plan(pose, duty,
                           wait_per_cell=float(cfg.get("wait_per_cell", 20)),
                           wait_max=float(cfg.get("wait_max", 60)))
        _emit_targets(ctx, [list(duty)])
        results, pose, _shot = _execute_plan(ctx, plan)
        verdicts = [r for r in results if r.get("do") == "photo_analyze" and r.get("ok")]
        saw = bool(verdicts and verdicts[-1].get("cargo"))
        conf = verdicts[-1].get("confidence") if verdicts else None
        label = verdicts[-1].get("label") if verdicts else "no_data"
        # гейт реальности на голосе: подтверждаем груз ТОЛЬКО если камера
        # реально видит проверяемую клетку — иначе голос «нет» (безопасно:
        # снесённый за поле дрон не должен ложно подтверждать чужую находку)
        why = ""
        if saw:
            trust, why = _reality_trusts(verdicts[-1], duty)
            if not trust:
                saw = False
                ctx.emit({"kind": "reality_reject", "from": ctx.agent_id,
                          "cell": list(duty), "why": why, "phase": "EXECUTE"})
        covered.add(cell_key(pose))
        _write_progress(ctx, "verifying", pose, covered, zone, target=[list(duty)])
        body = (f"Проверил [{duty[0]},{duty[1]}] вблизи: "
                + (f"ДА, груз на месте ({label}, {conf})." if saw
                   else (f"камера не подтвердила позицию ({why}) — не считаю."
                         if why else f"груза НЕТ — {label} ({conf}).")))
        msgs = [
            _plan_message(ctx, plan, results, pose),
            make_msg(ctx, "VERIFY", "all", "EXECUTE", body=body,
                     payload={"cell": duty, "cargo": saw, "confidence": conf,
                              "label": label, "name": _name(ctx)}),
        ]
        return {"thought": f"Очередь моя: лечу перепроверять [{duty[0]},{duty[1]}]. "
                           + ("Подтверждаю груз." if saw else "Пусто, ложная тревога."),
                "messages": msgs, "idle": False}

    # 2) sweep my zone (продолжаем даже пока другие верифицируют — очередь дойдёт)
    world_cells = (ctx.world or {}).get("cells") or {}
    # свежее покрытие: world.cells отстаёт на цикл координатора, поэтому
    # объединяем с progress-файлами всех дронов (они пишутся первыми)
    covered_all = set(world_cells.keys()) | set(covered)
    for sid, pr in (ctx.progress or {}).items():
        for c in (pr or {}).get("covered") or []:
            covered_all.add(cell_key(c))
    targets = [c for c in zone_cells
               if cell_key(c) not in covered_all
               and (world_cells.get(cell_key(c)) or {}).get("status")
               not in ("rejected",)]
    stolen = False
    if not targets and cfg.get("steal", True):
        targets = _stealable_cells(ctx, covered_all)
        stolen = bool(targets)

    if not targets:
        mine = ctx.progress.get(ctx.agent_id) or {}
        if mine.get("status") != "zone_done":
            _write_progress(ctx, "zone_done", _pose(ctx), covered, zone, target=[])
            n_found = sum(1 for m in ctx.messages
                          if m.get("from") == ctx.agent_id and m.get("type") == "FOUND")
            note = (f"кандидатов на груз: {n_found}" if n_found else "груза не видел")
            msg = make_msg(ctx, "STATUS", "coordinator", "EXECUTE",
                           body=f"Зона {zone} осмотрена ({len(covered)} кл.), {note}.",
                           payload={"zone": zone, "covered": len(covered),
                                    "found": n_found})
            return {"thought": f"Зона {zone} закрыта. Дежурю на подхвате.",
                    "messages": [msg], "idle": False}
        return {"thought": "Зона закрыта; жду очереди на проверку или финала.",
                "messages": [], "idle": True}

    pose = _pose(ctx)
    fallback = sweep_plan(pose, targets,
                          cells_per_turn=int(cfg.get("cells_per_turn", 2)),
                          wait_per_cell=float(cfg.get("wait_per_cell", 20)),
                          wait_max=float(cfg.get("wait_max", 60)))
    say, thinking = "", ""
    plan = fallback
    if not ctx.brain.is_mock:
        plan, say, thinking = _llm_sweep_plan(ctx, pose, targets, fallback, zone)

    # показать НАМЕРЕНИЕ (куда дрон собрался) на дашборде ДО перелёта
    tgt = _plan_targets(plan)
    _emit_targets(ctx, tgt)
    results, pose, photographed = _execute_plan(ctx, plan)
    for c in photographed:
        covered.add(cell_key(c))
    _write_progress(ctx, "sweeping", pose, covered, zone, target=tgt)

    msgs = [_plan_message(ctx, plan, results, pose, say)]
    for r in results:
        if r.get("do") == "photo_analyze" and r.get("ok") and r.get("cargo") \
                and not r.get("close_look") and not _has_found(ctx, r["cell"]):
            c = r["cell"]
            # гейт реальности: не поднимаем тревогу, если камера не подтверждает,
            # что дрон реально над этой клеткой (снесло за поле / aruco-рассинхрон)
            trust, why = _reality_trusts(r, c)
            if not trust:
                ctx.emit({"kind": "reality_reject", "from": ctx.agent_id,
                          "cell": c, "why": why, "phase": "EXECUTE"})
                msgs.append(make_msg(
                    ctx, "STATUS", "coordinator", "EXECUTE",
                    body=(f"В [{c[0]},{c[1]}] VLM увидел груз, но камера НЕ "
                          f"подтвердила позицию ({why}) — находку не заявляю, "
                          "долечу и переснимет."),
                    payload={"cell": c, "reality_ok": False, "why": why,
                             "name": _name(ctx)}))
                continue
            msgs.append(make_msg(
                ctx, "FOUND", "all", "EXECUTE",
                body=(f"НАХОДКА: в клетке [{c[0]},{c[1]}] вижу груз "
                      f"({r.get('label')}, conf={r.get('confidence')}). Прошу "
                      "проверить по очереди."),
                payload={"cell": c, "confidence": r.get("confidence"),
                         "label": r.get("label"), "name": _name(ctx)}))
    found_note = " Есть находка!" if any(
        m.get("type") == "FOUND" for m in msgs) else ""
    steal_note = " (беру ничейные клетки)" if stolen else ""
    return {"thought": f"Ход{steal_note}: {plan_summary(plan)}.{found_note}",
            "thinking": thinking, "messages": msgs, "idle": False}


def step(ctx):
    phase = ctx.phase.get("phase")
    if phase == "CHAT":
        return survey_chat_turn(ctx)
    if phase in ("EXECUTE", "REPORT"):
        return execute_turn(ctx)
    return {"thought": "Готовлюсь к вылету.", "messages": [], "idle": True}
