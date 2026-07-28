"""Survey task coordinator: город дронов v2 — поиск груза на поле W×H.

Фазовая машина (те же примитивы, что safe_passage/painting):

    INIT → CHAT → EXECUTE → REPORT → DONE
             ▲        │  ▲______│ (reopen: не всё покрыто)
             └ SURVEY_CHAT=0 пропускает переговоры

* CHAT — дроны в открытом канале делят поле на зоны (Z1..Zn, список клеток
  каждой зоны задаёт фикстура или автонарезка змейкой). Механика та же, что
  scout_chat: claim/argue/trade, done-консенсус.
* EXECUTE — каждый дрон ходами обходит свою зону; ход = JSON-план
  [{fly_to},{wait},{photo_analyze}]. Находка (FOUND) переключает мир в режим
  verify: остальные дроны ПО ОЧЕРЕДИ летят к клетке и голосуют VERIFY.
  Кворум подтверждает → world.ready + goal → ровер едет (existing rover role).
  Отказ → клетка помечена rejected, поиск продолжается.
* REPORT — финальный вердикт: PASS (груз подтверждён, ровер прибыл) или FAIL
  (груз не найден / ровер заблокирован).

Только координатор пишет phase/decision/assignments/world (single-writer).
"""
from __future__ import annotations

from datetime import timedelta

from .phase_util import deadline_passed as _deadline_passed, iso, now, parse_iso, transition as _transition
from .survey_common import (
    cell_key,
    grid_size,
    next_verifier,
    quorum_state,
    zones_from_map,
)


def _cfg(ctx) -> dict:
    return ctx.config.get("survey", {})


def _zone_labels(ctx, n: int) -> list[str]:
    labels = list(ctx.config.get("zones") or [])
    while len(labels) < n:
        labels.append(f"Z{len(labels) + 1}")
    return labels[:n]


def _discover(ctx):
    """Registry discovery (plug-and-play), env fallback — as in safe_passage."""
    reg = ctx.bb.read_registry() if hasattr(ctx.bb, "read_registry") else {}
    reg_scouts = sorted(k for k, v in reg.items() if v.get("role") == "scout")
    if reg_scouts:
        ctx.config["scouts"] = reg_scouts
    reg_rover = next((k for k, v in reg.items() if v.get("role") == "rover"), None)
    if reg_rover:
        ctx.config["rover"] = reg_rover


def _coordinator_said(ctx, tag: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "FACILITATE":
            if (m.get("payload") or {}).get("tag") == tag:
                return True
    return False


def _zone_brief(zones: dict[str, list]) -> str:
    parts = []
    for lab, cells in zones.items():
        head = ",".join(f"[{c[0]},{c[1]}]" for c in cells[:3])
        parts.append(f"{lab} — {len(cells)} кл. ({head}…)")
    return "; ".join(parts)


def _found_messages(ctx) -> list[dict]:
    return [m for m in ctx.messages if m.get("type") == "FOUND"]


def _verify_votes(ctx, cell) -> dict[str, bool]:
    """Latest VERIFY verdict per verifier for this cell (last-wins, board order)."""
    votes: dict[str, bool] = {}
    for m in ctx.messages:
        if m.get("type") != "VERIFY":
            continue
        p = m.get("payload") or {}
        if p.get("cell") is not None and cell_key(p["cell"]) == cell_key(cell):
            votes[m.get("from", "?")] = bool(p.get("cargo"))
    return votes


def _verify_assigned(ctx, cell, verifier: str) -> bool:
    for m in ctx.messages:
        if (m.get("from") == ctx.agent_id and m.get("type") == "ASSIGNMENT"
                and m.get("to") == verifier):
            p = m.get("payload") or {}
            if p.get("verify_cell") is not None and cell_key(p["verify_cell"]) == cell_key(cell):
                return True
    return False


def _merge_world(ctx, world: dict) -> dict:
    """Fold per-drone progress into the coordinator-owned world model:
    positions (closed loop — the drones report the pose their bridge measured)
    + covered cells. Candidate/rejected/confirmed statuses win over covered."""
    cells = world.setdefault("cells", {})
    positions = world.setdefault("positions", {})
    targets = world.setdefault("targets", {})
    for sid in ctx.config["scouts"]:
        pr = ctx.progress.get(sid) or {}
        if isinstance(pr.get("cell"), list):
            positions[sid] = pr["cell"]
        # куда дрон СОБИРАЕТСЯ лететь (intended targets) — для маркеров на дашборде
        targets[sid] = [list(c) for c in (pr.get("target") or [])]
        for c in pr.get("covered") or []:
            k = cell_key(c)
            cur = cells.get(k) or {}
            if cur.get("status") in ("candidate", "rejected", "confirmed"):
                continue
            if cur.get("status") != "covered":
                cells[k] = {"status": "covered", "by": sid}
    world["covered_count"] = sum(
        1 for v in cells.values()
        if v.get("status") in ("covered", "candidate", "rejected", "confirmed"))
    return world


def _unresolved_found(ctx, world: dict) -> list[dict]:
    """FOUND messages whose cell is not yet verified (rejected/confirmed) and
    not the active candidate — the verification backlog, in board order."""
    resolved = {cell_key(c) for c in world.get("rejected") or []}
    if world.get("confirmed"):
        resolved.add(cell_key(world["confirmed"]["cell"]))
    if world.get("candidate"):
        resolved.add(cell_key(world["candidate"]["cell"]))
    out, seen = [], set()
    for m in _found_messages(ctx):  # board order = seq order
        cell = (m.get("payload") or {}).get("cell")
        if cell is None:
            continue
        k = cell_key(cell)
        if k in resolved or k in seen:
            continue
        seen.add(k)
        out.append(m)
    return out


def _open_candidate(ctx, world: dict) -> bool:
    """Promote the oldest unresolved FOUND to the active candidate. True if a
    verification just opened."""
    if world.get("candidate") or world.get("confirmed"):
        return False
    for m in _unresolved_found(ctx, world):
        p = m.get("payload") or {}
        cell = p.get("cell")
        finder = m.get("from", "?")
        queue = [s for s in ctx.config["scouts"] if s != finder]
        world["candidate"] = {
            "cell": [int(cell[0]), int(cell[1])], "by": finder,
            "queue": queue, "asked": {}, "opened": iso(now()),
            "confidence": p.get("confidence"),
        }
        world["mode"] = "verify"
        world.setdefault("cells", {})[cell_key(cell)] = {
            "status": "candidate", "by": finder, "conf": p.get("confidence")}
        ctx.emit({"kind": "cell", "cell": [int(cell[0]), int(cell[1])],
                  "status": "candidate", "by": finder})
        return True
    return False


def _drive_verification(ctx, world: dict, msgs_out: list) -> str:
    """Advance the sequential verification queue. Returns quorum state."""
    cand = world["candidate"]
    cell = cand["cell"]
    votes = _verify_votes(ctx, cell)
    votes = {k: v for k, v in votes.items() if k in cand["queue"]}
    leg_sec = int(_cfg(ctx).get("verify_leg_sec", 240))

    # A verifier that was asked but stayed silent past its leg is SKIPPED (the
    # queue moves on — a dead drone must not freeze the mission), but the skip
    # is reversible: a late VERIFY still counts. The first sverk run proved the
    # hard-drop version wrong — drone-1's verdict landed after the timeout and
    # a queue-membership filter threw the real vote away.
    asked = cand.setdefault("asked", {})
    skipped = set(cand.get("skipped") or []) - set(votes)
    for v in cand["queue"]:
        t = parse_iso(asked.get(v))
        if (v not in votes and v not in skipped and t
                and (now() - t).total_seconds() > leg_sec):
            skipped.add(v)
            ctx.emit({"kind": "verify_skip", "verifier": v, "cell": cell,
                      "reason": f"no VERIFY within {leg_sec}s"})
    cand["skipped"] = sorted(skipped)
    effective = [q for q in cand["queue"] if q not in skipped]

    nxt = next_verifier(effective, votes)
    if nxt and not _verify_assigned(ctx, cell, nxt):
        asked[nxt] = iso(now())
        msgs_out.append({
            "from": ctx.agent_id, "to": nxt, "phase": "EXECUTE",
            "type": "ASSIGNMENT",
            "body": (f"Твоя очередь проверить находку: лети к клетке "
                     f"[{cell[0]},{cell[1]}], снизься, сфотографируй и доложи "
                     f"VERIFY (груз или нет)."),
            "payload": {"verify_cell": cell, "found_by": cand["by"]},
        })
    cand["votes"] = votes
    return quorum_state(votes, effective, int(_cfg(ctx).get("quorum", 2)),
                        bool(_cfg(ctx).get("verify_all", True)))


def _resolve_candidate(ctx, world: dict, state: str, msgs_out: list) -> None:
    cand = world.pop("candidate")
    cell = cand["cell"]
    votes = cand.get("votes", {})
    tally = f"{sum(1 for v in votes.values() if v)}/{len(cand['queue'])}"
    if state == "confirmed":
        world["confirmed"] = {"cell": cell, "by": cand["by"], "votes": votes}
        world["cells"][cell_key(cell)] = {"status": "confirmed", "by": cand["by"]}
        world["goal"] = cell
        world["ready"] = True
        world["mode"] = "done"
        dec = ctx.bb.read_decision()
        dec["result"] = f"PASS: груз подтверждён в клетке [{cell[0]},{cell[1]}]"
        dec["cargo"] = cell
        ctx.bb.write_decision(dec)
        ctx.emit({"kind": "cell", "cell": cell, "status": "confirmed", "votes": tally})
        ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
        msgs_out.append({
            "from": ctx.agent_id, "to": "all", "phase": "EXECUTE", "type": "FACILITATE",
            "body": (f"Груз ПОДТВЕРЖДЁН в [{cell[0]},{cell[1]}] "
                     f"(голоса {tally}, нашёл {cand['by']}). world.ready=true — "
                     f"ровер, выезжай."),
            "payload": {"tag": f"confirm-{cell_key(cell)}", "cell": cell},
        })
    else:
        world.setdefault("rejected", []).append(cell)
        world["cells"][cell_key(cell)] = {"status": "rejected", "by": cand["by"]}
        world["mode"] = "sweep"
        ctx.emit({"kind": "cell", "cell": cell, "status": "rejected", "votes": tally})
        msgs_out.append({
            "from": ctx.agent_id, "to": "all", "phase": "EXECUTE", "type": "FACILITATE",
            "body": (f"Ложная тревога в [{cell[0]},{cell[1]}] — проверяющие груз "
                     f"не увидели ({tally}). Продолжаем осмотр по клеткам."),
            "payload": {"tag": f"reject-{cell_key(cell)}", "cell": cell},
        })


def _extend_deadline_if_alive(ctx, world: dict) -> bool:
    """Liveness watchdog: the EXECUTE deadline is a stall detector, not a fixed
    budget. «Жив» — это не только свежий progress: дроны в zone_done прогресс
    не пишут, пока идёт верификация, и первый sverk-прогон умер по дедлайну с
    находкой на доске. Считаем живым любой из сигналов: свежий progress,
    свежее СООБЩЕНИЕ на доске (VERIFY/ASSIGNMENT/PLAN…), активная верификация."""
    dl = parse_iso(ctx.phase.get("deadline"))
    if not dl or (dl - now()).total_seconds() > 30:
        return False
    window = 150.0
    fresh = any(
        (t := parse_iso((ctx.progress.get(sid) or {}).get("ts")))
        and (now() - t).total_seconds() < window
        for sid in ctx.config["scouts"])
    if not fresh and ctx.messages:
        t = parse_iso(ctx.messages[-1].get("ts"))
        fresh = bool(t and (now() - t).total_seconds() < window)
    if not fresh and world.get("candidate"):
        asked = (world["candidate"].get("asked") or {}).values()
        fresh = any((t := parse_iso(a)) and (now() - t).total_seconds() < window
                    for a in asked)
    if fresh:
        ctx.bb.write_phase("EXECUTE", ctx.phase.get("round", 0),
                           iso(now() + timedelta(seconds=120)))
        ctx.emit({"kind": "deadline_extend", "phase": "EXECUTE", "secs": 120})
        return True
    return False


def survey_step(ctx):
    phase = ctx.phase.get("phase", "INIT")
    round_ = ctx.phase.get("round", 0)
    _discover(ctx)
    scouts = ctx.config["scouts"]
    sm = ctx.scenario_map
    w, h = grid_size(sm)
    labels = _zone_labels(ctx, len(scouts))
    zones = zones_from_map(sm, labels)
    total_cells = w * h
    chat_on = bool(_cfg(ctx).get("chat", True))
    # mapping-режим: цель — полный облёт (собрать карту), а не поиск груза.
    # Источник — env SURVEY_MAPPING или map.json "mode":"mapping".
    mapping = bool(_cfg(ctx).get("mapping")) or sm.get("mode") == "mapping"
    # точка Б («пожар»): после сбора карты ровер выезжает СЮДА из старта (точка А)
    fire = sm.get("fire") if isinstance(sm.get("fire"), list) else None

    # ---- INIT -----------------------------------------------------------
    if phase == "INIT":
        if hasattr(ctx.bb, "root"):  # coordinator is always co-located (FileBoard)
            (ctx.bb.root / "task.md").write_text(
                f"# Задача\n\nОбследовать поле {w}×{h} клеток и найти груз.\n"
                "Дроны договариваются о разделе поля, затем ходами облетают свои "
                "зоны: полёт → ожидание → фото + анализ. Каждая находка "
                "перепроверяется остальными дронами ПО ОЧЕРЕДИ; кворум "
                "подтверждает. К подтверждённой клетке выезжает ровер.\n",
                encoding="utf-8")
            ctx.bb.write_json(ctx.bb.root / "config.yaml.json", {
                "task": "survey", "scouts": scouts, "rover": ctx.config["rover"],
                "zones": labels, "grid_size": [w, h],
                "survey": _cfg(ctx), "scout_chat": chat_on,
            })
        world = {
            "grid_size": [w, h],
            "grid": sm.get("grid") or [[0] * w for _ in range(h)],
            "start": sm.get("start") or [0, 0],
            "goal": None,
            "zones": zones,
            "cells": {},
            "positions": {},
            "mode": "sweep",
            "candidate": None,
            "confirmed": None,
            "rejected": [],
            "ready": False,
            "total_cells": total_cells,
            "covered_count": 0,
            "mapping": mapping,
            "fire": fire,
            "targets": {},
        }
        ctx.bb.write_world(world)
        from run_log import start_run
        start_run(ctx.bb, meta={"task": "survey", "scouts": scouts,
                                "grid_size": [w, h], "scout_chat": chat_on})
        if chat_on:
            _transition(ctx, "CHAT", 0, ctx.config["deadlines"].get("chat", 180))
            return {"thought": f"Поле {w}×{h}, груз где-то в {total_cells} клетках. "
                               "Открываю канал — дроны сами делят зоны облёта.",
                    "messages": [], "idle": False}
        # no-chat mode: assign zones round-robin immediately
        amap = {sid: labels[i % len(labels)] for i, sid in enumerate(scouts)}
        return _start_execute(ctx, world, amap, zones, round_, negotiated=False)

    # ---- CHAT (дроны делят зоны) -----------------------------------------
    if phase == "CHAT":
        from .scout_chat import derive_assignment, should_end_chat
        from .studio_chat import chat_messages
        msgs_out = []
        if not _coordinator_said(ctx, "open_survey_chat"):
            msgs_out.append({
                "from": ctx.agent_id, "to": "all", "phase": "CHAT", "type": "FACILITATE",
                "body": (f"Открытый канал. Делим поле {w}×{h} на зоны: "
                         f"{_zone_brief(zones)}. Каждому — одна зона, без дыр. "
                         "Заявляйте, спорьте, меняйтесь; скажите done, когда "
                         "раздел устроит."),
                "payload": {"tag": "open_survey_chat", "zones": labels},
            })
        if should_end_chat(ctx, scouts, _deadline_passed(ctx)):
            amap = derive_assignment(ctx, scouts, labels)
            world = ctx.bb.read_world()
            return _start_execute(ctx, world, amap, zones, round_, negotiated=True,
                                  extra_msgs=msgs_out)
        return {"thought": f"Переговоры о зонах: {len(chat_messages(ctx))} реплик.",
                "messages": msgs_out, "idle": not msgs_out}

    # ---- EXECUTE (осмотр + верификация) -----------------------------------
    if phase == "EXECUTE":
        world = ctx.bb.read_world()
        msgs_out: list[dict] = []
        _merge_world(ctx, world)

        if world.get("candidate"):
            state = _drive_verification(ctx, world, msgs_out)
            if state in ("confirmed", "rejected"):
                _resolve_candidate(ctx, world, state, msgs_out)
        # after (or instead of) resolving, promote the next queued FOUND in the
        # SAME cycle — otherwise a backlog find races the sweep-complete check
        # (proven in the first live mock run: [1,3] was found while [2,1] was
        # being rejected, and the mission FAILed with the cargo on the board)
        _open_candidate(ctx, world)
        ctx.bb.write_world(world)

        if world.get("ready"):
            _transition(ctx, "REPORT", round_, ctx.config["deadlines"].get("report", 90))
            return {"thought": "Груз подтверждён кворумом. Открываю REPORT — жду ровер.",
                    "messages": msgs_out, "idle": False}

        covered = world.get("covered_count", 0)
        backlog = _unresolved_found(ctx, world)
        # «не найден» объявляем только когда: всё покрыто, нет активного
        # кандидата, нет непроверенных находок И все дроны отчитались как
        # закончившие. Последнее закрывает гонку «progress уже записан, FOUND
        # ещё не долетел» (progress пишется внутри step до постинга сообщений).
        all_idle = all(
            (ctx.progress.get(s) or {}).get("status") in ("zone_done", "done")
            for s in scouts)
        if (covered >= total_cells and world.get("mode") == "sweep"
                and not world.get("candidate") and not backlog and all_idle):
            dec = ctx.bb.read_decision()
            if world.get("mapping"):
                fire_cell = world.get("fire")
                if fire_cell and not world.get("ready"):
                    # карта собрана → дроны отправляют ровер из точки А (старт) в
                    # точку Б (пожар): world.goal=пожар, ready=true — ровер едет
                    # (обычная роль rover, без изменений), в Gazebo катится куб.
                    world["goal"] = [int(fire_cell[0]), int(fire_cell[1])]
                    world["ready"] = True
                    world["mode"] = "dispatch"
                    ctx.bb.write_world(world)
                    dec["result"] = (f"PASS: карта собрана ({covered}/{total_cells}) — "
                                     f"ровер выезжает к пожару [{fire_cell[0]},{fire_cell[1]}]")
                    ctx.bb.write_decision(dec)
                    ctx.emit({"kind": "cell", "cell": world["goal"], "status": "fire"})
                    ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
                    msgs_out.append({
                        "from": ctx.agent_id, "to": "all", "phase": "EXECUTE",
                        "type": "FACILITATE",
                        "body": (f"Карта собрана ({covered}/{total_cells}). Обнаружен "
                                 f"ПОЖАР в клетке [{fire_cell[0]},{fire_cell[1]}]. "
                                 "Ровер — выезжай из точки старта к пожару "
                                 "(world.ready=true)."),
                        "payload": {"tag": "dispatch-fire", "fire": world["goal"]}})
                    _transition(ctx, "REPORT", round_,
                                ctx.config["deadlines"].get("report", 90))
                    return {"thought": f"Карта собрана целиком. Пожар в "
                                       f"[{fire_cell[0]},{fire_cell[1]}] — отправляю ровер. REPORT.",
                            "messages": msgs_out, "idle": False}
                # маппинг без пожара: полный облёт сам по себе — успех
                dec["result"] = f"PASS: карта собрана — осмотрено {covered}/{total_cells} клеток"
                thought = f"Все {total_cells} клеток осмотрены — карта собрана целиком. REPORT."
            else:
                dec["result"] = "FAIL: поле покрыто полностью, груз не найден"
                thought = f"Все {total_cells} клеток осмотрены, груза нет. REPORT."
            ctx.bb.write_decision(dec)
            ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
            _transition(ctx, "REPORT", round_, 30)
            return {"thought": thought, "messages": msgs_out, "idle": False}

        if _deadline_passed(ctx):
            if _extend_deadline_if_alive(ctx, world):
                return {"thought": f"Дедлайн близко, но миссия жива (покрыто "
                                   f"{covered}/{total_cells}) — продлеваю.",
                        "messages": msgs_out, "idle": not msgs_out}
            _transition(ctx, "REPORT", round_, 30)
            return {"thought": f"Дедлайн EXECUTE: покрыто {covered}/{total_cells}, "
                               "прогресса нет. REPORT.", "messages": msgs_out, "idle": False}

        mode = world.get("mode", "sweep")
        cand = world.get("candidate") or {}
        note = (f", проверяем [{cand['cell'][0]},{cand['cell'][1]}] "
                f"({len(cand.get('votes') or {})}/{len(cand.get('queue') or [])} голосов)"
                if mode == "verify" and cand else "")
        return {"thought": f"Осмотр: {covered}/{total_cells} клеток, режим {mode}{note}.",
                "messages": msgs_out, "idle": not msgs_out}

    # ---- REPORT ------------------------------------------------------------
    if phase == "REPORT":
        world = ctx.bb.read_world()
        _merge_world(ctx, world)
        ctx.bb.write_world(world)
        dec = ctx.bb.read_decision()
        if world.get("ready"):
            rp = ctx.progress.get(ctx.config["rover"], {})
            if rp.get("status") == "done":
                arrived = bool(rp.get("arrived"))
                cell = world.get("goal") or []
                if world.get("mapping"):
                    dec["result"] = (
                        f"PASS: карта собрана, ровер прибыл к пожару [{cell[0]},{cell[1]}]"
                        if arrived else
                        f"FAIL: ровер не доехал до пожара [{cell[0]},{cell[1]}]")
                else:
                    dec["result"] = (f"PASS: груз подтверждён в [{cell[0]},{cell[1]}], "
                                     "ровер прибыл" if arrived else "FAIL: ровер заблокирован")
                ctx.bb.write_decision(dec)
                ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
                from run_log import finalize_run
                finalize_run(ctx.bb, summary={
                    "result": dec["result"], "cargo": world.get("goal"),
                    "covered": world.get("covered_count"),
                    "rejected": world.get("rejected"), "rounds": round_})
                _transition(ctx, "DONE", round_, 0)
                return {"thought": f"Финал: {dec['result']}.", "messages": [], "idle": False}
            waiting = ("Карта собрана; ровер в пути к пожару." if world.get("mapping")
                       else "Груз подтверждён; ровер в пути.")
            return {"thought": waiting, "messages": [], "idle": True}
        covered = world.get("covered_count", 0)
        max_rounds = int(_cfg(ctx).get("max_rounds", 2))
        # незакрытая верификация (активный кандидат или бэклог FOUND) — это НЕ
        # повод для вердикта: возвращаемся в EXECUTE и доводим очередь до
        # кворума. Первый sverk-прогон завершился FAIL с находкой на доске
        # именно через этот путь (дедлайн -> REPORT -> вердикт).
        if (world.get("candidate") or _unresolved_found(ctx, world)) \
                and round_ < max_rounds * 2 and not world.get("confirmed"):
            _transition(ctx, "EXECUTE", round_ + 1,
                        _execute_deadline_secs(ctx, total_cells, len(scouts)))
            return {"thought": "Есть непроверенные находки — возвращаюсь в "
                               f"EXECUTE (раунд {round_ + 1}) довести верификацию.",
                    "messages": [], "idle": False}
        if covered < total_cells and round_ + 1 < max_rounds and not world.get("confirmed"):
            _transition(ctx, "EXECUTE", round_ + 1,
                        _execute_deadline_secs(ctx, total_cells, len(scouts)))
            return {"thought": f"Покрыто лишь {covered}/{total_cells} — раунд "
                               f"{round_ + 1}, дообследуем пропуски.",
                    "messages": [], "idle": False}
        if not dec.get("result"):
            if world.get("mapping"):
                done_full = covered >= total_cells
                dec["result"] = (
                    f"PASS: карта собрана — осмотрено {covered}/{total_cells} клеток"
                    if done_full else
                    f"PARTIAL: карта собрана частично ({covered}/{total_cells} клеток)")
            else:
                dec["result"] = f"FAIL: груз не найден (покрыто {covered}/{total_cells})"
            ctx.bb.write_decision(dec)
            ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
        from run_log import finalize_run
        finalize_run(ctx.bb, summary={
            "result": dec["result"], "cargo": world.get("goal"),
            "covered": covered, "rejected": world.get("rejected"),
            "rounds": round_})
        _transition(ctx, "DONE", round_, 0)
        return {"thought": f"Финал: {dec['result']}.", "messages": [], "idle": False}

    return {"thought": "ГОТОВО.", "messages": [], "idle": True}


def _execute_deadline_secs(ctx, total_cells: int, n_drones: int) -> int:
    """Deadline ∝ workload: cells × flight-wait, split across drones, ×2 slack
    for LLM latency + verification legs. env DEADLINE_EXECUTE still floors it.
    На реальном мозге ход стоит до минуты LLM-латентности — поднимаем пол,
    иначе дедлайн гарантированно съедает верификацию (watchdog его продлит,
    но стартовать с честной оценки дешевле)."""
    cfg = _cfg(ctx)
    per_cell = float(cfg.get("wait_per_cell", 20)) * float(cfg.get("wait_scale", 1.0))
    est = int(total_cells * (per_cell + 2) / max(1, n_drones) * 2.5) + 120
    if not ctx.brain.is_mock:
        est = max(est, 900)
    return max(int(ctx.config["deadlines"].get("execute", 240)), est)


def _start_execute(ctx, world: dict, amap: dict[str, str], zones: dict,
                   round_: int, *, negotiated: bool, extra_msgs: list | None = None):
    scouts = ctx.config["scouts"]
    msgs_out = list(extra_msgs or [])
    decision = {"scheme": "chat-negotiated" if negotiated else "round-robin",
                "claims": amap, "rule": "stigmergic-chat" if negotiated else "static"}
    ctx.bb.write_decision(decision)
    ctx.emit({"kind": "decision", "decision": {"scheme": decision["scheme"]}})
    full: dict = dict(amap)
    full[ctx.config["rover"]] = {"wait_for": "world.ready"}
    ctx.bb.write_assignments(full)
    ctx.emit({"kind": "assignment", "map": amap})
    for sid, lab in amap.items():
        cells = zones.get(lab, [])
        msgs_out.append({
            "from": ctx.agent_id, "to": sid, "phase": "EXECUTE", "type": "ASSIGNMENT",
            "body": (f"Твоя зона {lab}: {len(cells)} клеток. Ходами облети её — "
                     "полёт → ожидание → фото+анализ; можно несколько клеток за "
                     "ход. О находке докладывай FOUND всем."),
            "payload": {"zone": lab, "cells": cells, "round": round_},
        })
    msgs_out.append({
        "from": ctx.agent_id, "to": ctx.config["rover"], "phase": "EXECUTE",
        "type": "ASSIGNMENT",
        "body": "Жди: поедешь к грузу, когда кворум дронов подтвердит клетку (world.ready).",
        "payload": {"wait_for": "world.ready"},
    })
    world["mode"] = "sweep"
    ctx.bb.write_world(world)
    secs = _execute_deadline_secs(ctx, world.get("total_cells", 25), len(scouts))
    _transition(ctx, "EXECUTE", round_, secs)
    how = "переговорами" if negotiated else "по кругу"
    return {"thought": f"Зоны розданы {how}: {amap}. Дроны — в облёт; "
                       f"ровер ждёт world.ready (дедлайн {secs}с).",
            "messages": msgs_out, "idle": False}
