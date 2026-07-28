"""Rover role: gated on world.ready, then navigate A -> B over the grid (§5)."""
from __future__ import annotations

from . import has_posted, make_msg


def step(ctx):
    # город дронов: исполнение атомарного плана ровера ведёт координатор через
    # city_executor (оракул); на железе тот же план исполняет RoverBridge.
    if ctx.config.get("task") == "city_missions":
        from .city_missions import step as city_step
        return city_step(ctx)

    world = ctx.world
    mine = ctx.progress.get(ctx.agent_id, {})
    if mine.get("status") == "done":
        return {"thought": "Arrived; nothing left to do.", "messages": [], "idle": True}

    ready = bool(world.get("ready"))
    if not ready:
        # assignment is {wait_for: world.ready}: post a single BLOCK, then idle
        if not has_posted(ctx, "BLOCK"):
            msg = make_msg(
                ctx, "BLOCK", "coordinator", ctx.phase.get("phase", "EXECUTE"),
                body="Holding: map not yet certified safe (waiting world.ready).",
                payload={"wait_for": "world.ready"},
            )
            return {"thought": "Map uncertified. I must not move until coordinator "
                               "certifies safe passage.", "messages": [msg], "idle": False}
        return {"thought": "Still waiting for world.ready.", "messages": [], "idle": True}

    if ctx.bridge is None:
        return {"thought": "No bridge available.", "messages": [], "idle": True}

    grid = world.get("grid")
    start = world.get("start")
    goal = world.get("goal")
    if grid is None or start is None or goal is None:
        return {"thought": "World certified but grid/start/goal missing.",
                "messages": [], "idle": True}

    thought = (f"Map certified PASS. Planning route {start} -> {goal}, "
               f"routing around localized obstacles.")
    arrived = False
    last = None
    steps = 0
    for frame in ctx.bridge.navigate(start, goal, grid):
        if "pose" in frame:
            steps += 1
            ctx.emit({"kind": "pose", "from": ctx.agent_id, "xy": frame["pose"],
                      "progress": frame.get("progress", 0.0)})
            last = frame["pose"]
        if frame.get("status") == "arrived":
            arrived = True
            last = frame.get("pose", last)
        if frame.get("status") == "blocked":
            arrived = False

    ctx.bb.write_progress(ctx.agent_id, {
        "status": "done", "arrived": arrived, "final_pose": last, "path_steps": steps,
    })
    body = ("Arrived at goal via a collision-free path around the obstacle."
            if arrived else "Blocked: no safe path found.")
    msg = make_msg(
        ctx, "REPORT", "coordinator", "REPORT",
        body=body, payload={"arrived": arrived, "final_pose": last, "path_steps": steps},
    )
    return {"thought": thought, "messages": [msg], "idle": False}
