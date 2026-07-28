"""Scout drone role: PROPOSE -> CONVERGE -> EXECUTE (photograph + detect)."""
from __future__ import annotations

from . import has_posted, messages_of, make_msg


def _preferred_sector(ctx) -> str:
    """Default claim: stable map of drone index -> sector, biased by priority."""
    sectors = ctx.config["sectors"]
    idx = ctx.config["scouts"].index(ctx.agent_id) if ctx.agent_id in ctx.config["scouts"] else 0
    return sectors[idx % len(sectors)]


def step(ctx):
    # survey task (поиск груза): своя механика ходов/верификации целиком
    if ctx.config.get("task") == "survey":
        from .survey_scout import step as survey_step
        return survey_step(ctx)

    # город дронов: дрон-монитор публикует структурированные наблюдения
    if ctx.config.get("task") == "city_missions":
        from .city_missions import step as city_step
        return city_step(ctx)

    phase = ctx.phase.get("phase")

    # city-of-drones mode: free negotiation chat instead of silent PROPOSE/VOTE
    if phase == "CHAT":
        from .scout_chat import scout_chat_turn
        return scout_chat_turn(ctx, make_msg, ctx.config.get("scouts", []))

    if phase == "PROPOSE":
        if has_posted(ctx, "PROPOSAL", phase="PROPOSE"):
            return {"thought": "Proposal already on the board.", "messages": [], "idle": True}
        sector = _preferred_sector(ctx)
        prios = ", ".join(str(p) for p in ctx.soul.get("priorities", [])) or "coverage"
        thought = (
            f"I favor {prios}. Splitting the street into 4 length sectors and "
            f"claiming {sector} where my priority pays off."
        )
        msg = make_msg(
            ctx, "PROPOSAL", "all", "PROPOSE",
            body=(f"Split street into 4 length sectors; I take {sector} "
                  f"(priority: {prios})."),
            payload={"scheme": "length-4", "claim_sector": sector,
                     "priorities": ctx.soul.get("priorities", [])},
        )
        return {"thought": thought, "messages": [msg], "idle": False}

    if phase == "CONVERGE":
        if has_posted(ctx, "VOTE", phase="CONVERGE"):
            return {"thought": "Vote already cast.", "messages": [], "idle": True}
        proposals = messages_of(ctx, type_="PROPOSAL", phase="PROPOSE")
        if not proposals:
            return {"thought": "Waiting for proposals to score.", "messages": [], "idle": True}
        # score each proposal 0-10; reward the shared length-4 scheme (no blind spots)
        scores = {}
        for p in proposals:
            base = 8 if p.get("payload", {}).get("scheme") == "length-4" else 5
            scores[p.get("id", p["from"])] = base
        best = max(scores, key=scores.get)
        name = ctx.soul.get("name", ctx.agent_id)
        top = (ctx.soul.get("priorities") or ["coverage"])[0]
        thought = "length-4 guarantees no blind spots; scoring it highest."
        msg = make_msg(
            ctx, "VOTE", "coordinator", "CONVERGE",
            body=(f"{name} endorses the length-4 split (priority: {top}): "
                  f"explicit per-sector coverage checks."),
            payload={"scheme": "length-4", "scores": scores, "voter": ctx.agent_id},
            ref=best,
        )
        return {"thought": thought, "messages": [msg], "idle": False}

    if phase in ("EXECUTE", "REPORT"):
        sector = ctx.assignments.get(ctx.agent_id)
        if not sector or not isinstance(sector, str):
            return {"thought": "No sector assigned yet.", "messages": [], "idle": True}
        mine = ctx.progress.get(ctx.agent_id, {})
        if mine.get("status") == "done":
            return {"thought": f"Sector {sector} reported done.", "messages": [], "idle": True}
        if ctx.bridge is None:
            return {"thought": "No bridge available.", "messages": [], "idle": True}

        thought = f"Sector {sector} is mine. Low pass, then obstacle detection."
        # photograph -> artifact event
        shot = ctx.bridge.photograph(sector)
        image_path = shot["image_path"]
        ctx.emit({"kind": "artifact", "from": ctx.agent_id, "path": image_path,
                  "phase": phase, "sector": sector})
        status = make_msg(
            ctx, "STATUS", "coordinator", "EXECUTE",
            body=f"Photographed sector {sector}.",
            payload={"sector": sector, "artifact": image_path},
        )
        # detect obstacles -> coverage
        det = ctx.bridge.detect_obstacle(image_path)
        obstacles = det.get("obstacles", [])
        coverage = det.get("coverage", 0.0)
        ctx.bb.write_progress(ctx.agent_id, {
            "status": "done", "sector": sector, "artifact": image_path,
            "coverage": coverage, "obstacles": obstacles,
        })
        report = make_msg(
            ctx, "REPORT", "coordinator", "REPORT",
            body=(f"Sector {sector} done: coverage={coverage:.2f}, "
                  f"{len(obstacles)} obstacle(s) localized."),
            payload={"sector": sector, "artifact": image_path,
                     "coverage": coverage, "obstacles": obstacles},
        )
        return {"thought": thought, "messages": [status, report], "idle": False}

    return {"thought": "Standing by.", "messages": [], "idle": True}
