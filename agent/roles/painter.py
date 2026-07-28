"""Painter drone role (spec §10, worked example B).

PROPOSE  -> BRICK: one visual piece (your color).
BUILD    -> merge pieces into a shared idea.
CONVERGE -> vote on assembled final title(s).
EXECUTE  -> paint (or wait for curator).
"""
from __future__ import annotations

import os
import time

from .coordinator_paint import is_curated
from . import has_posted, make_msg
from .painter_compose import plan_strokes
from .painter_agent import (
    BUILD_ROUNDS,
    compose_brick,
    compose_build,
    compose_vote,
    my_build_count,
)


def _agreed_subject(ctx) -> str:
    dec = ctx.bb.read_decision() if hasattr(ctx.bb, "read_decision") else {}
    return dec.get("subject") or "без названия"


def _paint_seed(ctx) -> int:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    return int(cfg.get("paint_seed", 0))


def step(ctx):
    from .painter_palette import my_entry
    phase = ctx.phase.get("phase")
    me = my_entry(ctx)
    name = me.get("name", ctx.agent_id)
    color = me.get("hex", "#ffffff")
    technique = me.get("technique", "")
    paint_seed = _paint_seed(ctx)

    # studio (decentralized) mode: free stigmergic chat instead of BRICK/BUILD
    if phase == "CHAT":
        from .studio_chat import artist_chat_turn
        return artist_chat_turn(ctx, make_msg, ctx.config.get("painters", []))

    if phase == "PROPOSE":
        if has_posted(ctx, "BRICK", phase="PROPOSE"):
            return {"thought": "Мой кирпич на доске — участвую в сборке идеи.",
                    "thinking": "", "messages": [], "idle": True}
        answer, thinking, body, piece, shapes = compose_brick(ctx)
        msg = make_msg(
            ctx, "BRICK", "all", "PROPOSE",
            body=body,
            payload={"piece": piece, "color": color, "technique": technique,
                     "name": name, "shapes": shapes},
        )
        return {"thought": answer, "thinking": thinking, "messages": [msg], "idle": False}

    if phase == "BUILD":
        builds = my_build_count(ctx)
        if builds < BUILD_ROUNDS:
            answer, thinking, body, payload = compose_build(ctx, builds)
            msg = make_msg(
                ctx, "BUILD", "all", "BUILD",
                body=body,
                payload={**payload, "round": builds, "name": name},
            )
            return {"thought": answer, "thinking": thinking, "messages": [msg], "idle": False}
        return {"thought": "Мой вклад в сборку отправлен — жду голосования.",
                "thinking": "", "messages": [], "idle": True}

    if phase == "CONVERGE":
        if has_posted(ctx, "VOTE", phase="CONVERGE"):
            return {"thought": "Голос отдан — жду консенсуса.",
                    "thinking": "", "messages": [], "idle": True}

        answer, thinking, vote_body, vote_payload = compose_vote(ctx, paint_seed)
        msg = make_msg(
            ctx, "VOTE", "coordinator", "CONVERGE",
            body=vote_body,
            payload={**vote_payload, "voter": ctx.agent_id},
        )
        return {"thought": answer, "thinking": thinking, "messages": [msg], "idle": False}

    if phase in ("EXECUTE", "REPORT"):
        if is_curated(ctx):
            mine = ctx.progress.get(ctx.agent_id, {})
            mood = _agreed_subject(ctx)
            if mine.get("status") == "done":
                return {"thought": f"Мой слой ({color}) в картине «{mood}» — куратор нарисовал.",
                        "thinking": mine.get("compose_plan", ""), "messages": [], "idle": True}
            return {"thought": "Куратор рисует всю картину — жду.",
                    "thinking": "", "messages": [], "idle": True}
        from .collab_paint import is_collab, paint_collab
        if is_collab(ctx):
            # collab mode: I author my OWN colour as z-shapes into the shared canvas
            return paint_collab(ctx)
        assignment = ctx.assignments.get(ctx.agent_id)
        if not isinstance(assignment, dict) or "canvas" not in assignment:
            return {"thought": "Жду очереди на холст.", "thinking": "",
                    "messages": [], "idle": True}
        mine = ctx.progress.get(ctx.agent_id, {})
        mood = assignment.get("subject", _agreed_subject(ctx))
        paint_order = assignment.get("paint_order", 1)
        for pid, ass in ctx.assignments.items():
            if not isinstance(ass, dict):
                continue
            if ass.get("paint_order", 99) < paint_order:
                if ctx.progress.get(pid, {}).get("status") != "done":
                    ahead = ass.get("paint_order", "?")
                    return {
                        "thought": f"Жду проход {ahead} — предыдущий художник ещё на холсте.",
                        "thinking": "", "messages": [], "idle": True,
                    }
        if mine.get("status") == "done":
            return {"thought": f"Мои мазки {color} для «{mood}» уже на холсте.",
                    "thinking": "", "messages": [], "idle": True}
        if ctx.bridge is None:
            return {"thought": "Мост распыления недоступен.", "thinking": "",
                    "messages": [], "idle": True}

        canvas = assignment.get("canvas", {"w": 120, "h": 120})
        seed = assignment.get("paint_seed", paint_seed)
        strokes, compose_thinking = plan_strokes(ctx, canvas, mood, seed)
        batch = os.environ.get("PAINT_BATCH", "1") not in ("0", "false", "no")
        step_sec = float(os.environ.get("PAINT_STEP_SEC", "0.02" if batch else "0.12"))
        thought = (f"Закрашиваю холст цветом {color} ({technique}) — "
                   f"«{mood}», {len(strokes)} мазков.")

        painted = 0
        for poly in strokes:
            ctx.bridge.move(poly[0])
            res = ctx.bridge.spray(poly, color=color, width=2)
            pts = res.get("points", poly)
            ctx.emit({"kind": "stroke", "from": ctx.agent_id, "color": color,
                      "points": pts, "by": ctx.agent_id, "phase": phase})
            painted += 1
            if not batch:
                time.sleep(step_sec)

        ctx.bb.write_progress(ctx.agent_id, {
            "status": "done", "subject": mood,
            "color": color, "technique": technique, "strokes": painted,
            "compose_plan": compose_thinking[:500],
        })
        report = make_msg(
            ctx, "REPORT", "coordinator", "REPORT",
            body=f"{name}: {painted} мазков {color} ({technique}) для «{mood}».",
            payload={"subject": mood, "color": color, "technique": technique,
                     "strokes": painted},
        )
        return {"thought": thought, "thinking": compose_thinking, "messages": [report], "idle": False}

    return {"thought": "Жду у мольберта.", "thinking": "",
            "messages": [], "idle": True}
