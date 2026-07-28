"""scan_debate — 3×3 single-drone scan with two arguing LLM pilots.

A clean end-to-end smoke test of the whole stack (multi-agent dispute + per-cell
photos + pause/reset + a REAL bort) on a SMALL map, with no rover and no mission
detection (fire/delivery removed).

Roles (all dispatched from here; `roles.step` routes TASK==scan_debate to this
module, so loop.py is untouched — the `coordinator` role still owns the board
reset + phase machine):

  coordinator  owns INIT→DEBATE→SCAN→DONE. Builds the WxH world, seeds
               state/control.json (which pilot is "at the helm"), consolidates
               both pilots' proposed routes into world.routes.
  controller   pilot-a / pilot-b — each proposes a DIFFERENT scan route over the
               cells and ARGUES for it with a real LLM (mock => a deterministic
               distinct route + a templated line). The loop streams their
               reasoning into the chat automatically (non-coordinator role).
  flyer        drone-1 — the ONE physical bort. Holds the only bridge. In SCAN it
               reads control.json, flies the ACTIVE pilot's route cell-by-cell and
               photographs each. Switching the helm (operator, during pause) makes
               it fly the OTHER pilot's route — "see the second path".

One bridge client => no two-masters conflict over the single PX4. Without a bridge
(local preview / no bort yet) the flyer SIMULATES the flight so the dashboard still
animates the path.
"""
from __future__ import annotations

import os
import time

from . import make_msg, has_posted
from .survey_common import grid_size
from bb import now_iso
from brain import clean_chat_line

# per-cell dwell when SIMULATING flight (no bridge) so the front animates; tests
# set 0. With a real bridge the move/photograph round-trip paces the loop instead.
SIM_STEP_SEC = float(os.environ.get("SCAN_SIM_STEP", "0.8"))
# how many rebuttal exchanges each pilot makes before the scan starts (1 = one
# jab each; live demos want 2-3 for a real back-and-forth). The coordinator holds
# DEBATE until every pilot spoke DEBATE_ROUNDS times or DEBATE_GRACE of its own
# cycles passed with the routes on the table (LLM down must not stall the flight).
DEBATE_ROUNDS = max(1, int(os.environ.get("DEBATE_ROUNDS", "1")))
DEBATE_GRACE = max(1, int(os.environ.get("DEBATE_GRACE", "6")))


# ---- deterministic, persona-aligned scan routes -----------------------------
def _perimeter_cw(w: int, h: int) -> list:
    """The outer ring, clockwise from the top-left corner."""
    if w <= 0 or h <= 0:
        return []
    if w == 1:
        return [[0, y] for y in range(h)]
    if h == 1:
        return [[x, 0] for x in range(w)]
    ring = [[x, 0] for x in range(w)]                 # top L->R
    ring += [[w - 1, y] for y in range(1, h)]         # right T->B
    ring += [[x, h - 1] for x in range(w - 2, -1, -1)]  # bottom R->L
    ring += [[0, y] for y in range(h - 2, 0, -1)]     # left B->T
    return ring


def _interior(w: int, h: int) -> list:
    """Non-border cells in serpentine order (centre of a 3×3 = just [1,1])."""
    out = []
    for y in range(1, h - 1):
        xs = range(1, w - 1) if y % 2 else range(w - 2, 0, -1)
        out += [[x, y] for x in xs]
    return out


def _is_free(scenario_map: dict, cell) -> bool:
    g = scenario_map.get("grid")
    if not g:
        return True
    x, y = int(cell[0]), int(cell[1])
    try:
        return int(g[y][x]) == 0
    except (IndexError, TypeError, ValueError):
        return True


def route_for(scenario_map: dict, idx: int) -> list:
    """A pilot's scan route by its index. 0 = periphery-first (corners/edges, then
    centre); 1 = centre-first (spiral out). Both are permutations of all free
    cells, DIFFERENT orderings — that is what the two pilots argue about."""
    w, h = grid_size(scenario_map)
    if idx % 2 == 0:
        seq = _perimeter_cw(w, h) + _interior(w, h)
    else:
        seq = _interior(w, h) + _perimeter_cw(w, h)
    return [c for c in seq if _is_free(scenario_map, c)]


def _fmt(route: list, n: int = 9) -> str:
    head = " ".join(f"[{c[0]},{c[1]}]" for c in route[:n])
    return head + ("…" if len(route) > n else "")


# ---- pilot argument (live LLM, mock => templated) ---------------------------
_TMPL = {
    "propose_0": "Иду по периметру: сначала углы и края, центр последним — край терять нельзя.",
    "propose_1": "Стартую с центрального маркера и по спирали наружу — минимум перелётов, сразу чёткая привязка.",
    "rebut_0": "Спираль из центра рискует не закрыть углы вовремя — периметр надёжнее.",
    "rebut_1": "Периметр — это лишние длинные перелёты; с центра короче и стабильнее по локализации.",
}


def _argue(ctx, idx: int, mode: str, my_route: list, other_route: list | None,
           opp_line: str = "") -> tuple[str, str]:
    """Return (line, thinking) for a pilot's proposal / rebuttal. Live LLM when a
    provider is set; deterministic template in mock so the plumbing/tests stand
    on their own. `opp_line` is the opponent's last chat line — quoting it makes
    later rounds a real back-and-forth instead of restated openers."""
    tmpl = _TMPL.get(f"{mode}_{idx % 2}", "")
    if ctx.brain.is_mock:
        return tmpl, ""
    system = ctx.soul_body or "Ты — пилот-планировщик облёта поля. Отвечай кратко, по-русски."
    what = ("предложи и защити СВОЙ маршрут" if mode == "propose"
            else "ответь на последнюю реплику оппонента и отстаивай свой маршрут")
    user = (
        f"Тестовый облёт поля дроном. {what}. Твой порядок клеток: {_fmt(my_route)}."
        + (f" Маршрут оппонента: {_fmt(other_route)}." if other_route else "")
        + (f" Оппонент только что сказал: «{opp_line}»." if opp_line else "")
        # маршруты уже видны в чате; пересказ координат моделью их только путает
        + " НЕ перечисляй координаты клеток — спорь о стратегии (риски, длина пути,"
          " локализация, углы)."
        + " Ответь СТРОГО одним JSON-объектом {\"thinking\": \"...\", \"line\": \"1-2 фразы для чата\"}."
    )
    data = ctx.brain.agent_json(system, [{"role": "user", "content": user}]) or {}
    thinking = str(data.get("thinking") or "")
    line = clean_chat_line(data.get("line"), tmpl, thinking=thinking)
    return line or tmpl, thinking


# ---- coordinator: phase machine ---------------------------------------------
def _controllers(ctx) -> list:
    return ctx.world.get("controllers") if (ctx.world or {}).get("controllers") \
        else ctx.config.get("controllers", ["pilot-a", "pilot-b"])


def _collect(ctx, controllers: list, type_: str) -> dict:
    out = {}
    for m in ctx.messages:
        if m.get("type") == type_ and m.get("from") in controllers:
            out[m["from"]] = m.get("payload") or {}
    return out


def coordinator_step(ctx) -> dict:
    phase = ctx.phase.get("phase", "INIT")
    world = ctx.world or {}

    if phase == "INIT":
        w, h = grid_size(ctx.scenario_map)
        controllers = ctx.config.get("controllers", ["pilot-a", "pilot-b"])
        flyer = ctx.config.get("flyer", "drone-1")
        active = controllers[0]
        # seed the helm (persists across a /rerun reset, which does NOT clear it —
        # so re-write it every INIT to restore the default controller)
        ctx.bb.write_json(ctx.bb.state / "control.json",
                          {"active": active, "by": "coordinator", "ts": now_iso()})
        ctx.bb.write_world({"task": "scan_debate", "w": w, "h": h, "phase": "DEBATE",
                            "controllers": controllers, "flyer": flyer, "active": active,
                            "routes": {}, "cells": w * h, "flown": []})
        ctx.bb.write_phase("DEBATE", ctx.phase.get("round", 0))
        ctx.emit({"kind": "phase", "phase": "DEBATE"})
        ctx.emit({"kind": "control", "active": active})
        return {"thought": f"Поле {w}×{h}, один борт {flyer}. Пусть пилоты предложат "
                           f"маршруты и поспорят; у руля {active}.",
                "messages": [make_msg(ctx, "FACILITATE", "all", "DEBATE",
                             body=f"Тестовый облёт {w}×{h}. {controllers[0]} и "
                                  f"{controllers[1]}: предложите порядок облёта всех клеток "
                                  f"и обоснуйте. Летит один борт по маршруту того, кто у руля "
                                  f"(сейчас {active}); оператор может переключить руль в паузе.",
                             payload={"controllers": controllers})], "idle": False}

    if phase == "DEBATE":
        controllers = _controllers(ctx)
        routes = _collect(ctx, controllers, "ROUTE")
        n_rebuts = sum(1 for m in ctx.messages
                       if m.get("type") == "REBUTTAL" and m.get("from") in controllers)
        ready = len(routes) >= len(controllers)
        waited = int(world.get("debate_waited") or 0)
        if ready and (n_rebuts >= len(controllers) * DEBATE_ROUNDS
                      or waited >= DEBATE_GRACE):
            rt = {cid: [[int(c[0]), int(c[1])] for c in (p.get("route") or [])]
                  for cid, p in routes.items()}
            world.update(routes=rt, phase="SCAN")
            ctx.bb.write_world(world)
            ctx.bb.write_phase("SCAN", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "SCAN"})
            ctx.emit({"kind": "routes", "routes": rt})
            active = world.get("active") or controllers[0]
            return {"thought": "Оба маршрута собраны — запускаю облёт активным пилотом.",
                    "messages": [make_msg(ctx, "DECISION", "all", "SCAN",
                                 body=f"Маршруты на столе. У руля {active} — борт пошёл. "
                                      f"Переключить руль можно в паузе.",
                                 payload={"routes": rt, "active": active})], "idle": False}
        if ready:
            world["debate_waited"] = waited + 1     # bounded grace for rebuttals
            ctx.bb.write_world(world)
        return {"thought": f"Жду пилотов: маршрутов {len(routes)}/{len(controllers)}, "
                           f"реплик спора {n_rebuts}/{len(controllers) * DEBATE_ROUNDS}.",
                "messages": [], "idle": True}

    if phase == "SCAN":
        flyer = world.get("flyer") or ctx.config.get("flyer", "drone-1")
        fp = ctx.progress.get(flyer) or {}
        # DONE once the bort finishes a FULL route. Switching the helm mid-flight
        # (operator, in pause) resets the bort onto the other route, so it flies
        # that path to completion instead — that is how you "see the second path".
        if world.get("done") or fp.get("status") == "route_done":
            flown_routes = fp.get("flown_routes") or []
            world.update(done=True, phase="DONE", flown_routes=flown_routes)
            ctx.bb.write_world(world)
            ctx.bb.write_phase("DONE", ctx.phase.get("round", 0))
            ctx.emit({"kind": "phase", "phase": "DONE"})
            return {"thought": f"Маршрут {fp.get('active')} снят полностью — тест пройден.",
                    "messages": [make_msg(ctx, "REPORT", "all", "DONE",
                                 body=f"Готово. Борт {flyer} снял поле по маршруту "
                                      f"{fp.get('active')} (пройдено путей: "
                                      f"{', '.join(flown_routes) or '—'}).",
                                 payload={"flown_routes": flown_routes,
                                          "active": fp.get("active")})], "idle": False}
        active = fp.get("active") or world.get("active")
        return {"thought": f"Идёт облёт по маршруту {active}. "
                           f"Переключите руль в паузе, чтобы проверить второй путь.",
                "messages": [], "idle": True}

    return {"thought": "Готово.", "messages": [], "idle": True}


# ---- controller (pilot): propose a route + argue ----------------------------
def controller_step(ctx) -> dict:
    if ctx.phase.get("phase") != "DEBATE":
        return {"thought": "Спор не идёт — жду фазу.", "messages": [], "idle": True}
    controllers = _controllers(ctx)
    try:
        idx = controllers.index(ctx.agent_id)
    except ValueError:
        idx = 0
    my_route = route_for(ctx.scenario_map, idx)

    if not has_posted(ctx, "ROUTE"):
        line, thinking = _argue(ctx, idx, "propose", my_route, None)
        body = f"Мой маршрут ({len(my_route)} кл.): {_fmt(my_route)}. {line}"
        return {"thought": line, "thinking": thinking,
                "messages": [make_msg(ctx, "ROUTE", "all", "DEBATE", body=body,
                             payload={"route": my_route, "argument": line,
                                      "name": ctx.agent_id})], "idle": False}

    # both proposed? then argue DEBATE_ROUNDS times, each round answering the
    # opponent's LATEST line — a back-and-forth, not two parallel monologues
    opponents = [c for c in controllers if c != ctx.agent_id]
    others = _collect(ctx, opponents, "ROUTE")
    if others:
        other_route = next(iter(others.values())).get("route") or []
        mine_rebuts = sum(1 for m in ctx.messages
                          if m.get("type") == "REBUTTAL" and m.get("from") == ctx.agent_id)
        opp_lines = [m.get("body") or "" for m in ctx.messages
                     if m.get("from") in opponents
                     and m.get("type") in ("ROUTE", "REBUTTAL")]
        if mine_rebuts < DEBATE_ROUNDS and (mine_rebuts == 0 or len(opp_lines) > mine_rebuts):
            line, thinking = _argue(ctx, idx, "rebut", my_route, other_route,
                                    opp_line=(opp_lines[-1] if opp_lines else ""))
            return {"thought": line, "thinking": thinking,
                    "messages": [make_msg(ctx, "REBUTTAL", "all", "DEBATE", body=line,
                                 payload={"name": ctx.agent_id,
                                          "round": mine_rebuts + 1})], "idle": False}

    return {"thought": "Маршрут предложен, спор высказан.", "messages": [], "idle": True}


# ---- flyer (the one bort): fly the active pilot's route ----------------------
def _fly_cell(ctx, cell) -> bool:
    """Fly to + photograph one cell. Real bridge when present; else simulate."""
    if ctx.bridge is not None:
        try:
            ctx.bridge.move(cell)
            ctx.bridge.photograph_cell(cell)
            return True
        except Exception:  # noqa: BLE001 — a flaky leg must not strand the scan
            return False
    if SIM_STEP_SEC > 0:
        time.sleep(SIM_STEP_SEC)               # pace the animation
    return True


def flyer_step(ctx) -> dict:
    if ctx.phase.get("phase") != "SCAN":
        return {"thought": "Жду фазу облёта.", "messages": [], "idle": True}
    world = ctx.world or {}
    routes = world.get("routes") or {}
    controllers = _controllers(ctx)
    control = ctx.bb.read_json(ctx.bb.state / "control.json", {}) or {}
    active = control.get("active") or world.get("active") or (controllers[0] if controllers else None)
    route = routes.get(active) or []
    if not route:
        return {"thought": f"Нет маршрута активного пилота ({active}).", "messages": [], "idle": True}

    mine = ctx.progress.get(ctx.agent_id) or {}
    flying = mine.get("flying")
    idx = int(mine.get("idx", 0))
    flown = mine.get("flown") or []
    flown_routes = set(mine.get("flown_routes") or [])

    if flying != active:                        # helm switched (or first run) -> restart on new route
        flying, idx = active, 0
        ctx.emit({"kind": "helm", "from": ctx.agent_id, "active": active})

    if idx >= len(route):                        # active route fully flown
        flown_routes.add(active)
        ctx.bb.write_progress(ctx.agent_id, {"status": "route_done", "flying": flying,
                              "idx": idx, "flown": flown, "flown_routes": sorted(flown_routes),
                              "active": active, "ts": now_iso()})
        ctx.emit({"kind": "drone_flew", "from": ctx.agent_id, "cells": flown, "active": active})
        return {"thought": f"Маршрут {active} снят полностью ({len(route)} кл.). "
                           f"Переключите руль или сброс.",
                "messages": [make_msg(ctx, "STATUS", "coordinator", "SCAN",
                             body=f"Маршрут {active} облетан ({len(route)} кл.).",
                             payload={"active": active, "flown_routes": sorted(flown_routes)})],
                "idle": False}

    cell = [int(route[idx][0]), int(route[idx][1])]
    ok = _fly_cell(ctx, cell)
    sim = ctx.bridge is None                     # no bort -> the flight is simulated
    failed = mine.get("failed") or []
    if cell not in flown:
        flown.append(cell)
    if not ok and cell not in failed:            # advanced past, but NOT photographed
        failed.append(cell)
    idx += 1
    ctx.bb.write_progress(ctx.agent_id, {"status": "scanning", "flying": flying, "idx": idx,
                          "flown": flown, "failed": failed,
                          "flown_routes": sorted(flown_routes),
                          "active": active, "cell": cell, "ok": ok, "sim": sim,
                          "ts": now_iso()})
    if ok:
        ctx.emit({"kind": "artifact", "from": ctx.agent_id, "cell": cell,
                  "phase": "SCAN", "by": active, "sim": sim})
    ctx.emit({"kind": "drone_step", "from": ctx.agent_id, "cell": cell,
              "idx": idx, "total": len(route), "active": active, "sim": sim})
    return {"thought": f"[{active}] снимаю клетку [{cell[0]},{cell[1]}] ({idx}/{len(route)}).",
            "messages": [], "idle": False}


def step(ctx) -> dict:
    role = ctx.role
    if role == "coordinator":
        return coordinator_step(ctx)
    if role in ("flyer", "drone", "scout"):
        return flyer_step(ctx)
    if role == "controller":
        return controller_step(ctx)
    return {"thought": f"scan_debate: неизвестная роль {role}", "messages": [], "idle": True}
