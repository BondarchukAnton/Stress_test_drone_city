"""Coordinator role: owns the phase machine (§5), convergence (§6.3),
world.json merge + safety verdict, and rover gating (§5).

Only the coordinator writes phase.json / decision.json / assignments.json /
world.json (single-writer rule, §1). It also emits the non-message events
(phase / decision / assignment) that the visualizer keys its timeline on.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from pointcloud import export_pointcloud

from .phase_util import (
    deadline_passed as _deadline_passed,
    stable_key as _stable_key,
    transition as _transition,
)


def _distinct_from(ctx, type_, phase):
    return {m["from"] for m in ctx.messages
            if m.get("type") == type_ and m.get("phase") == phase
            and m.get("from") in ctx.config["scouts"]}


def _build_world(ctx):
    sm = ctx.scenario_map
    sectors = ctx.config["sectors"]
    cov_min = ctx.config["coverage_min"]
    reported = {}
    for sid in ctx.config["scouts"]:
        pr = ctx.progress.get(sid, {})
        if pr.get("status") == "done" and pr.get("sector"):
            reported[pr["sector"]] = pr
    covered = [s for s in sectors if reported.get(s, {}).get("coverage", 0.0) >= cov_min]
    gaps = [s for s in sectors if s not in covered]
    obstacles = []
    for s in sectors:
        for o in reported.get(s, {}).get("obstacles", []):
            obstacles.append({**o, "sector": s})
    localized = all(("xy" in o and o["xy"] is not None) for o in obstacles)
    all_done = len(reported) == len(ctx.config["scouts"])
    ready = bool(all_done and not gaps and localized)
    return {
        "covered": covered,
        "gaps": gaps,
        "obstacles": obstacles,
        "grid": sm["grid"],
        "start": sm["start"],
        "goal": sm["goal"],
        "sectors": sectors,
        "ready": ready,
    }, all_done


def step(ctx):
    # The painting task (spec §10 example B) runs its own phase behaviors; the
    # phase machine, deadlines, and event emission are shared with safe_passage.
    if ctx.config.get("task") == "painting":
        return painting_step(ctx)

    # The debate task runs the reusable turn-based chat/decision engine
    # (moderator.py): FRAME -> OPENING -> DEBATE rounds -> VOTE -> CONCLUDE.
    if ctx.config.get("task") == "debate":
        from .moderator import debate_step
        return debate_step(ctx)

    # The survey task (город дронов v2): zone negotiation -> per-turn JSON
    # action plans over a cell grid -> sequential find verification -> rover.
    if ctx.config.get("task") == "survey":
        from .survey import survey_step
        return survey_step(ctx)

    # Город дронов (city_missions): пожар + доставка + ВУП + энергетика ровера
    if ctx.config.get("task") == "city_missions":
        from .city_missions import step as city_step
        return city_step(ctx)

    phase = ctx.phase.get("phase", "INIT")
    round_ = ctx.phase.get("round", 0)

    # Discover participants from the hub registry so drones that join the
    # network are picked up automatically (plug-and-play). Falls back to the
    # pinned SCOUTS/ROVER env when no one has registered (the single-host demo).
    reg = ctx.bb.read_registry() if hasattr(ctx.bb, "read_registry") else {}
    reg_scouts = sorted(k for k, v in reg.items() if v.get("role") == "scout")
    if reg_scouts:
        ctx.config["scouts"] = reg_scouts
    reg_rover = next((k for k, v in reg.items() if v.get("role") == "rover"), None)
    if reg_rover:
        ctx.config["rover"] = reg_rover

    scouts = ctx.config["scouts"]
    sectors = ctx.config["sectors"]

    # ---- INIT ----------------------------------------------------------
    if phase == "INIT":
        # city-of-drones negotiation: scouts TALK to divide the sectors (same
        # architecture as the painting studio). SCOUT_CHAT=0 = legacy silent flow.
        chat_on = os.environ.get("SCOUT_CHAT", "1") not in ("0", "", "false", "no")
        # publish the shared task + run config (§1), read-only to agents
        (ctx.bb.root / "task.md").write_text(
            "# Task\n\nPhotograph the model street completely enough to certify a "
            "car can pass safely. No coverage gaps, all obstacles localized. "
            "Then the rover drives A -> B on the certified map.\n", encoding="utf-8")
        ctx.bb.write_json(ctx.bb.root / "config.yaml.json", {
            "scouts": scouts, "rover": ctx.config["rover"], "sectors": sectors,
            "convergence": ctx.config["convergence"],
            "novelty_min": ctx.config["novelty_min"],
            "coverage_min": ctx.config["coverage_min"],
            "scout_chat": chat_on,
        })
        if chat_on:
            _transition(ctx, "CHAT", 0, ctx.config["deadlines"].get("chat", 180))
            return {"thought": "Read task.md. Opening the open channel — scouts "
                               "negotiate the sector split by talking.",
                    "messages": [], "idle": False}
        _transition(ctx, "PROPOSE", 0, 20)
        return {"thought": "Read task.md. Safe-passage mapping run: opening PROPOSE.",
                "messages": [], "idle": False}

    # ---- CHAT (city of drones: scouts negotiate the split, thin facilitator) ----
    if phase == "CHAT":
        from .scout_chat import derive_assignment, should_end_chat
        from .studio_chat import chat_messages
        msgs_out = []
        if not _coordinator_said(ctx, "open_scout_chat"):
            msgs_out.append({
                "from": ctx.agent_id, "to": "all", "phase": "CHAT", "type": "FACILITATE",
                "body": (f"Open channel. Split the city sectors {', '.join(sectors)} "
                         "between you — one each, no gaps. Claim, argue, trade; "
                         "say you're done when the split satisfies you."),
                "payload": {"tag": "open_scout_chat"}})
        if should_end_chat(ctx, scouts, _deadline_passed(ctx)):
            amap = derive_assignment(ctx, scouts, sectors)
            bounds = {s: ctx.scenario_map["sectors"][s].get("bounds", [])
                      for s in sectors}
            decision = {"scheme": "chat-negotiated", "claims": amap, "bounds": bounds,
                        "rule": "stigmergic-chat"}
            ctx.bb.write_decision(decision)
            ctx.emit({"kind": "decision", "decision": {"scheme": "chat-negotiated"}})
            full = dict(amap)
            full[ctx.config["rover"]] = {"wait_for": "world.ready"}
            ctx.bb.write_assignments(full)
            ctx.emit({"kind": "assignment", "map": amap})
            for sid, sec in amap.items():
                msgs_out.append({"from": ctx.agent_id, "to": sid, "phase": "EXECUTE",
                                 "type": "ASSIGNMENT", "ref": None,
                                 "body": f"Your negotiated sector is {sec} — photograph it.",
                                 "payload": {"sector": sec, "round": round_}})
            msgs_out.append({"from": ctx.agent_id, "to": ctx.config["rover"],
                             "phase": "EXECUTE", "type": "ASSIGNMENT", "ref": None,
                             "body": "Stand by: drive A->B once the map is certified safe.",
                             "payload": {"wait_for": "world.ready"}})
            _transition(ctx, "EXECUTE", round_, 60)
            return {"thought": f"Negotiation settled ({len(chat_messages(ctx))} lines): "
                               f"{amap}. Scouts fly, rover gated on world.ready.",
                    "messages": msgs_out, "idle": False}
        return {"thought": f"Sector negotiation underway: {len(chat_messages(ctx))} lines.",
                "messages": msgs_out, "idle": not msgs_out}

    # ---- PROPOSE -------------------------------------------------------
    if phase == "PROPOSE":
        proposed = _distinct_from(ctx, "PROPOSAL", "PROPOSE")
        if len(proposed) >= len(scouts) or _deadline_passed(ctx):
            _transition(ctx, "CONVERGE", round_, 20)
            return {"thought": f"{len(proposed)}/{len(scouts)} proposals in. "
                               f"Opening CONVERGE.", "messages": [], "idle": False}
        return {"thought": f"PROPOSE: {len(proposed)}/{len(scouts)} proposals so far.",
                "messages": [], "idle": True}

    # ---- CONVERGE ------------------------------------------------------
    if phase == "CONVERGE":
        voted = _distinct_from(ctx, "VOTE", "CONVERGE")
        if len(voted) >= len(scouts) or _deadline_passed(ctx):
            # score rule: tally endorsements; length-4 is the converged scheme
            bounds = {s: ctx.scenario_map["sectors"][s].get("bounds", [])
                      for s in sectors}
            decision = {"scheme": "length-4", "style": None, "bounds": bounds,
                        "rule": ctx.config["convergence"]}
            ctx.bb.write_decision(decision)
            ctx.emit({"kind": "decision", "decision": {"scheme": "length-4"}})
            _transition(ctx, "EXECUTE", round_, 60)
            return {"thought": "Converged on length-4 (договорились). Writing decision, "
                               "moving to EXECUTE.", "messages": [], "idle": False}
        return {"thought": f"CONVERGE: {len(voted)}/{len(scouts)} votes in.",
                "messages": [], "idle": True}

    # ---- EXECUTE -------------------------------------------------------
    if phase == "EXECUTE":
        msgs = []
        if not ctx.assignments:
            amap = {sid: sectors[i % len(sectors)] for i, sid in enumerate(scouts)}
            amap[ctx.config["rover"]] = {"wait_for": "world.ready"}
            ctx.bb.write_assignments(amap)
            ctx.emit({"kind": "assignment", "map": {k: v for k, v in amap.items()
                                                    if isinstance(v, str)}})
            for sid in scouts:
                msgs.append({"from": ctx.agent_id, "to": sid, "phase": "EXECUTE",
                             "type": "ASSIGNMENT", "ref": None,
                             "body": f"Photograph sector {amap[sid]}.",
                             "payload": {"sector": amap[sid], "round": round_}})
            msgs.append({"from": ctx.agent_id, "to": ctx.config["rover"], "phase": "EXECUTE",
                         "type": "ASSIGNMENT", "ref": None,
                         "body": "Stand by: drive A->B once the map is certified safe.",
                         "payload": {"wait_for": "world.ready"}})
            return {"thought": "Assigning sectors to drones; rover gated on world.ready.",
                    "messages": msgs, "idle": False}
        done = sum(1 for s in scouts if ctx.progress.get(s, {}).get("status") == "done")
        if done >= len(scouts) or _deadline_passed(ctx):
            _transition(ctx, "REPORT", round_, 60)
            return {"thought": f"{done}/{len(scouts)} sectors photographed. "
                               f"Merging into world model (REPORT).",
                    "messages": [], "idle": False}
        return {"thought": f"EXECUTE: {done}/{len(scouts)} sectors done.",
                "messages": [], "idle": True}

    # ---- REPORT --------------------------------------------------------
    if phase == "REPORT":
        world, all_done = _build_world(ctx)
        prev = ctx.world
        ctx.bb.write_world(world)

        if world["ready"]:
            dec = ctx.bb.read_decision()
            if dec.get("result") != "PASS: safe passage":
                dec["result"] = "PASS: safe passage"
                ctx.bb.write_decision(dec)
                ctx.emit({"kind": "decision",
                          "decision": {"result": "PASS: safe passage"}})
            rover_done = ctx.progress.get(ctx.config["rover"], {}).get("status") == "done"
            if rover_done:
                arrived = ctx.progress[ctx.config["rover"]].get("arrived")
                final = "PASS: safe passage" if arrived else "FAIL: rover blocked"
                dec["result"] = final
                ctx.bb.write_decision(dec)
                _transition(ctx, "DONE", round_, 0)
                return {"thought": f"Rover reported. Verdict: {final}. Phase DONE.",
                        "messages": [], "idle": False}
            return {"thought": "Map certified PASS; world.ready=true. Rover unblocked, "
                               "awaiting arrival.", "messages": [], "idle": True}

        if all_done and world["gaps"]:
            # §5 reopen: reassign just the gaps, clear their progress, bump round
            amap = ctx.bb.read_assignments()
            owners = [s for s in scouts if amap.get(s) in world["gaps"]]
            for sid in owners:
                ctx.bb.write_progress(sid, {"status": "reassigned"})
            ctx.emit({"kind": "phase", "phase": "EXECUTE", "round": round_ + 1,
                      "note": "reopen for gaps: " + ",".join(world["gaps"])})
            _transition(ctx, "EXECUTE", round_ + 1, 60)
            return {"thought": f"Gaps remain {world['gaps']}; reopening EXECUTE round "
                               f"{round_ + 1}.", "messages": [], "idle": False}

        return {"thought": f"REPORT: covered={world['covered']} gaps={world['gaps']}.",
                "messages": [], "idle": True}

    return {"thought": "DONE.", "messages": [], "idle": True}


# ===== painting task (spec §10, worked example B) =========================
def _painters(ctx):
    """Active painter list: discovered from the hub registry (plug-and-play)
    when present, else the pinned PAINTERS env (single-host demo)."""
    reg = ctx.bb.read_registry() if hasattr(ctx.bb, "read_registry") else {}
    found = sorted(k for k, v in reg.items() if v.get("role") == "painter")
    return found or ctx.config["painters"]


def _distinct_painters(ctx, type_, phase, painters):
    return {m["from"] for m in ctx.messages
            if m.get("type") == type_ and m.get("phase") == phase
            and m.get("from") in painters}


def _load_painter_souls(painters) -> dict:
    """Best-effort: read each painter's soul (same image bundles all souls) so a
    soul can OVERRIDE its seed-derived persona. Missing souls just fall through
    to the seed-derived defaults."""
    from souls import load_soul
    base = os.environ.get("SOUL", "./souls/coordinator.md")
    dirs = [os.environ.get("SOUL_DIR"), os.path.dirname(base) or ".",
            "./souls", "/app/souls"]
    out = {}
    for pid in painters:
        for d in dirs:
            if not d:
                continue
            p = os.path.join(d, f"{pid}.md")
            if os.path.isfile(p):
                try:
                    meta, _ = load_soul(p)
                    out[pid] = meta
                    break
                except Exception:  # noqa: BLE001
                    pass
    return out


def _debate_counts(ctx, painters) -> dict[str, int]:
    counts = {p: 0 for p in painters}
    for m in ctx.messages:
        if (m.get("type") == "DEBATE" and m.get("phase") == "CONVERGE"
                and m.get("from") in painters):
            counts[m["from"]] = counts.get(m["from"], 0) + 1
    return counts


def _build_counts(ctx, painters) -> dict[str, int]:
    counts = {p: 0 for p in painters}
    for m in ctx.messages:
        if (m.get("type") == "BUILD" and m.get("phase") == "BUILD"
                and m.get("from") in painters):
            counts[m["from"]] = counts.get(m["from"], 0) + 1
    return counts


def _coordinator_said(ctx, tag: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and m.get("type") == "FACILITATE":
            if m.get("payload", {}).get("tag") == tag:
                return True
    return False


def painting_step(ctx):
    phase = ctx.phase.get("phase", "INIT")
    round_ = ctx.phase.get("round", 0)
    painters = _painters(ctx)
    sm = ctx.scenario_map
    # painters collaborate on ONE canvas: each adds their colour in paint order
    paint_order = painters  # order only — no sky/water/light semantics
    canvas = sm.get("canvas") or {"w": 120, "h": 120}
    subjects = sm.get("inspiration") or sm.get("subjects") or []

    # ---- INIT ----
    if phase == "INIT":
        from run_log import start_run
        from .collaborative import write_collab
        from .personas import roster as build_roster

        paint_mode = ctx.config.get("paint_mode", "curated")
        paint_seed = secrets.randbelow(2**31)
        roster_list = build_roster(painters, paint_seed, souls=_load_painter_souls(painters))
        mode_note = (
            "Куратор рисует всю картину после дебатов (режим curated)."
            if paint_mode in ("curated", "coordinator", "single")
            else "Каждый художник рисует свой слой (режим distributed)."
        )
        (ctx.bb.root / "task.md").write_text(
            "# Задача\n\nСовместно создать ОДНУ картину на общем холсте.\n"
            "Сначала каждый даёт кусочек (кирпич), потом вместе склеиваете идею, "
            "затем голосуете за финальное название.\n\n"
            f"{mode_note}\n"
            "Палитра: янтарь, синий, алый, зелёный — других цветов нет.\n",
            encoding="utf-8")
        from .studio_chat import is_studio
        studio = is_studio(ctx)
        ctx.bb.write_json(ctx.bb.root / "config.yaml.json", {
            "task": "painting", "painters": painters,
            "inspiration": subjects, "convergence": ctx.config["convergence"],
            "novelty_min": ctx.config["novelty_min"],
            "paint_seed": paint_seed,
            "paint_mode": paint_mode,
            "studio": studio,
            "roster": roster_list,
        })
        write_collab(ctx.bb, {"bricks": [], "draft": "", "build_notes": [], "candidates": []})
        start_run(ctx.bb, paint_seed=paint_seed, meta={
            "task": "painting", "paint_mode": paint_mode, "painters": painters,
            "studio": studio,
        })
        ctx.emit({"kind": "canvas_clear"})
        if studio:
            _transition(ctx, "CHAT", 0, ctx.config["deadlines"].get("chat", 150))
            return {"thought": "Холст готов. Открываю СВОБОДНЫЙ ЧАТ мастерской — "
                               "художники сами решают, что писать.",
                    "messages": [], "idle": False}
        _transition(ctx, "PROPOSE", 0, 20)
        return {"thought": "Холст готов. Фаза КИРПИЧИ — каждый предлагает кусочек картины.",
                "messages": [], "idle": False}

    # ---- CHAT (studio mode: thin facilitator over a free stigmergic chat) ----
    if phase == "CHAT":
        from .studio_chat import should_end_chat, derive_candidates, chat_messages
        from .collaborative import read_collab, write_collab
        msgs_out = []
        if not _coordinator_said(ctx, "open_chat"):
            insp = ", ".join(subjects) if subjects else ""
            msgs_out.append({
                "from": ctx.agent_id, "to": "all", "phase": "CHAT", "type": "FACILITATE",
                "body": ("Свободный чат мастерской. Обсудите и предложите, ЧТО напишем на "
                         "общем холсте — спорьте, отвечайте друг другу по имени. Когда идей "
                         "хватит, я объявлю голосование."
                         + (f" Вдохновение: {insp}." if insp else "")),
                "payload": {"tag": "open_chat"}})
        if should_end_chat(ctx, painters, _deadline_passed(ctx)):
            from .ballot import cluster, llm_merge, resolve
            from .studio_chat import latest_stances
            subs = derive_candidates(ctx) or ["пейзаж"]
            # fold near-duplicate subjects into ONE ballot line each — separate
            # lines for the same idea split its votes (proven in the live run)
            clusters = llm_merge(ctx.brain, cluster(subs))
            # rank by FINAL-stance support, then by how late the idea was still
            # alive — first-seen truncation once cut the chat's actual consensus
            # («Солнечная волна», 4/4 stances) off a first-5 ballot entirely
            stances = latest_stances(ctx, painters)
            support = {c["label"]: sum(1 for s in stances.values() if resolve(s, [c]))
                       for c in clusters}
            last_seen = {c["label"]: max((subs.index(v) for v in c["variants"] if v in subs),
                                         default=-1)
                         for c in clusters}
            clusters.sort(key=lambda c: (-support[c["label"]], -last_seen[c["label"]]))
            clusters = clusters[:5]
            cands = [c["label"] for c in clusters]
            collab = read_collab(ctx.bb)
            collab["candidates"] = cands
            collab["candidate_variants"] = {c["label"]: c["variants"] for c in clusters}
            collab["stances"] = stances
            collab["draft"] = cands[0]
            write_collab(ctx.bb, collab)
            ctx.emit({"kind": "collaborative", "collaborative": collab})
            _transition(ctx, "CONVERGE", round_, 40)
            merged_n = len(subs) - len(cands)
            return {"thought": f"Идей достаточно ({len(chat_messages(ctx))} реплик). "
                               f"Кандидаты: {cands}"
                               + (f" (склеено дублей: {merged_n})." if merged_n > 0 else ".")
                               + " Открываю голосование.",
                    "messages": msgs_out, "idle": False}
        return {"thought": f"Свободный чат идёт: {len(chat_messages(ctx))} реплик.",
                "messages": msgs_out, "idle": not msgs_out}

    # ---- PROPOSE (collaborative bricks) ----
    if phase == "PROPOSE":
        bricked = _distinct_painters(ctx, "BRICK", "PROPOSE", painters)
        if len(bricked) >= len(painters) or _deadline_passed(ctx):
            _transition(ctx, "BUILD", round_, 50)
            return {"thought": f"{len(bricked)}/{len(painters)} кирпичей. "
                               f"Открываю СБОРКУ — склеиваем общую идею.",
                    "messages": [], "idle": False}
        return {"thought": f"КИРПИЧИ: {len(bricked)}/{len(painters)} кусочков на доске.",
                "messages": [], "idle": True}

    # ---- BUILD (merge pieces into shared draft) ----
    if phase == "BUILD":
        from .collaborative import (
            bricks_from_messages,
            build_notes_from_messages,
            merge_draft,
            read_collab,
            synthesize_candidates,
            write_collab,
        )
        from .painter_agent import BUILD_ROUNDS

        bc = _build_counts(ctx, painters)
        min_b = min((bc.get(p, 0) for p in painters), default=0)
        msgs_out = []
        collab = read_collab(ctx.bb)

        if not collab.get("bricks"):
            bricks = bricks_from_messages(ctx)
            collab["bricks"] = bricks
            collab["draft"] = merge_draft(bricks, [])
            write_collab(ctx.bb, collab)
            ctx.emit({"kind": "collaborative", "collaborative": collab})

        if not _coordinator_said(ctx, "open_build"):
            msgs_out.append({
                "from": ctx.agent_id, "to": "all", "phase": "BUILD",
                "type": "FACILITATE",
                "body": ("Кирпичи на доске. Дополняйте и склеивайте в ОДНУ идею — "
                         "не четыре конкурирующих сюжета."),
                "payload": {"tag": "open_build"},
            })

        if min_b < BUILD_ROUNDS and not _deadline_passed(ctx):
            done = sum(1 for p in painters if bc.get(p, 0) > min_b)
            return {
                "thought": (f"Сборка {min_b + 1}/{BUILD_ROUNDS}: "
                            f"{done}/{len(painters)} ответили."),
                "messages": msgs_out, "idle": not msgs_out,
            }

        collab = read_collab(ctx.bb)
        collab["build_notes"] = build_notes_from_messages(ctx)
        collab["draft"] = merge_draft(collab.get("bricks") or [], collab["build_notes"])
        cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
        paint_seed = int(cfg.get("paint_seed", 0))
        from .ballot import cluster as _cluster
        clusters = _cluster(synthesize_candidates(
            collab["draft"], collab.get("bricks") or [], paint_seed,
        ))
        collab["candidates"] = [c["label"] for c in clusters]
        collab["candidate_variants"] = {c["label"]: c["variants"] for c in clusters}
        write_collab(ctx.bb, collab)
        ctx.emit({"kind": "collaborative", "collaborative": collab})
        _transition(ctx, "CONVERGE", round_, 40)
        return {
            "thought": (f"Черновик: «{collab['draft'][:60]}…». "
                        f"Варианты на голос: {len(collab['candidates'])}."),
            "messages": msgs_out, "idle": False,
        }

    # ---- CONVERGE (vote on shared candidates) ----
    if phase == "CONVERGE":
        from .collaborative import candidates_for_vote, read_collab

        voted = _distinct_painters(ctx, "VOTE", "CONVERGE", painters)
        msgs_out = []
        candidates = candidates_for_vote(ctx)

        if not _coordinator_said(ctx, "call_vote"):
            listing = "; ".join(f"«{c}»" for c in candidates) or "(нет кандидатов)"
            msgs_out.append({
                "from": ctx.agent_id, "to": "all", "phase": "CONVERGE",
                "type": "FACILITATE",
                "body": (f"Кандидаты от художников: {listing}. "
                         "Голосуйте за ОДИН финальный вариант."),
                "payload": {"tag": "call_vote", "candidates": candidates},
            })

        if len(voted) < len(painters) and not _deadline_passed(ctx):
            return {
                "thought": f"ГОЛОСОВАНИЕ: {len(voted)}/{len(painters)}.",
                "messages": msgs_out, "idle": not msgs_out,
            }

        if len(voted) < len(painters) and not candidates:
            return {"thought": "Жду голоса и кандидатов.", "messages": msgs_out,
                    "idle": not msgs_out}

        cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
        paint_seed = int(cfg.get("paint_seed", 0))
        collab_state = read_collab(ctx.bb)
        if not candidates:
            candidates = collab_state.get("candidates") or ["без названия"]
        from .ballot import clusters_from_state, resolve
        clusters = clusters_from_state(candidates, collab_state.get("candidate_variants"))
        from .collaborative import bricks_from_messages
        brick_by = {b["from"]: b["piece"] for b in bricks_from_messages(ctx)}
        score_totals: dict[str, float] = {}
        endorse: dict[str, int] = {}
        vote_record: dict[str, dict] = {}
        # One ballot per voter: a dropped write-ack (HttpBoard) can make a drone
        # re-vote, and counting per-message would double-count it. Collapse to the
        # voter's LAST VOTE (messages are board-ordered) before tallying.
        last_vote: dict[str, dict] = {}
        for m in ctx.messages:
            if m.get("type") == "VOTE" and m.get("phase") == "CONVERGE":
                last_vote[m.get("from", "")] = m
        for voter, m in last_vote.items():
            p = m.get("payload", {}) or {}
            raw_vote = (p.get("subject") or p.get("endorse") or "").strip()
            # every vote maps onto the published ballot (near-dup variants tally
            # to their cluster); an unmatched write-in is an explicit abstain,
            # never a fifth candidate that fragments the count
            voted_for = resolve(raw_vote, clusters)
            own = (p.get("my_piece") or brick_by.get(voter, "")).strip()
            vote_record[voter] = {
                "for": voted_for,
                "raw": raw_vote,
                "own": own,
                "invalid": bool(raw_vote and not voted_for),
            }
            if not voted_for:
                continue
            endorse[voted_for] = endorse.get(voted_for, 0) + 1
            for subj, sc in p.get("scores", {}).items():
                canon = resolve(subj, clusters)
                if not canon:
                    continue
                try:
                    score_totals[canon] = score_totals.get(canon, 0.0) + float(sc)
                except (TypeError, ValueError):
                    continue
        if endorse or score_totals:
            ranked = sorted(
                candidates,
                key=lambda s: (
                    endorse.get(s, 0),
                    score_totals.get(s, 0.0),
                    _stable_key(s, paint_seed),
                ),
                reverse=True,
            )
            subject = ranked[0]
            from .painter_palette import subject_color_feasible
            for cand in ranked:
                if subject_color_feasible(cand):
                    subject = cand
                    break
        else:
            subject = candidates[0] if candidates else "без названия"
        palette = []
        for b in bricks_from_messages(ctx):
            c = b.get("color")
            if c and c not in palette:
                palette.append(c)
        decision = {"subject": subject, "canvas": "shared",
                    "palette": palette, "rule": "ballot_then_scores"}
        ctx.bb.write_decision(decision)
        ctx.emit({"kind": "decision", "decision": {"subject": subject}})
        consensus_body = (
            f"Консенсус: «{subject}» — "
            f"голоса: {endorse.get(subject, 0)} из {len(vote_record)}"
            + (f" (ничья {len(endorse)}×{max(endorse.values()) if endorse else 0} — "
               f"по сумме оценок)"
               if endorse and len(set(endorse.values())) == 1 and len(endorse) > 1
               else "")
            + ". "
            + ("Куратор рисует всю картину — художники, ждите."
               if ctx.config.get("paint_mode", "curated") in ("curated", "coordinator", "single")
               else "Начинаем РИСОВАНИЕ.")
        )
        consensus = {
            "from": ctx.agent_id, "to": "all", "phase": "CONVERGE",
            "type": "CONSENSUS",
            "body": consensus_body,
            "payload": {
                "subject": subject,
                "scores": score_totals,
                "endorse_counts": endorse,
                "votes": vote_record,
            },
        }
        _transition(ctx, "EXECUTE", round_, 90)
        return {"thought": f"Договорились: «{subject}». "
                            + ("Рисую картину целиком." if "Куратор" in consensus_body
                               else "Назначаю очередь мазков."),
                "messages": [consensus], "idle": False}

    # ---- EXECUTE ----
    if phase == "EXECUTE":
        from .coordinator_paint import is_curated, execute_curated
        if is_curated(ctx):
            res = execute_curated(ctx, canvas, painters)
            if not res.get("idle", True):
                _transition(ctx, "REPORT", round_, 30)
            return res
        if not ctx.assignments:
            from .collab_paint import is_collab, assign_collab
            if is_collab(ctx):
                msgs = assign_collab(ctx, canvas, painters)
                return {"thought": "Роли по цвету розданы — каждый художник рисует "
                                   "свой цвет в ОДНОЙ общей картине.",
                        "messages": msgs, "idle": False}
            dec = ctx.bb.read_decision()
            subject = dec.get("subject") or "без названия"
            cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
            paint_seed = int(cfg.get("paint_seed", 0))
            amap, msgs, vizmap = {}, [], {}
            for i, pid in enumerate(painters):
                assignment = {"paint_order": i + 1, "canvas": canvas,
                              "subject": subject, "paint_seed": paint_seed}
                amap[pid] = assignment
                vizmap[pid] = i + 1
                msgs.append({"from": ctx.agent_id, "to": pid, "phase": "EXECUTE",
                             "type": "ASSIGNMENT", "ref": None,
                             "body": (f"Проход {i + 1}: закрась холст ТОЛЬКО своим цветом. "
                                      f"Настроение «{subject}» — абстрактные мазки, без буквальных форм."),
                             "payload": assignment})
            ctx.bb.write_assignments(amap)
            ctx.emit({"kind": "assignment", "map": vizmap})
            return {"thought": f"Настроение «{subject}». Очередь мазков: {vizmap}.",
                    "messages": msgs, "idle": False}
        done = sum(1 for p in painters if ctx.progress.get(p, {}).get("status") == "done")
        if done >= len(painters) or _deadline_passed(ctx):
            _transition(ctx, "REPORT", round_, 30)
            return {"thought": f"{done}/{len(painters)} художников закончили. Смотрю холст.",
                    "messages": [], "idle": False}
        return {"thought": f"РИСОВАНИЕ: {done}/{len(painters)} художников готово.",
                "messages": [], "idle": True}

    # ---- REPORT ----
    if phase == "REPORT":
        from .coordinator_paint import is_curated
        dec = ctx.bb.read_decision()
        subject = dec.get("subject") or "без названия"
        if is_curated(ctx):
            done = ctx.progress.get(ctx.agent_id, {}).get("status") == "done"
            total = ctx.progress.get(ctx.agent_id, {}).get("strokes", 0)
        else:
            done = sum(1 for p in painters if ctx.progress.get(p, {}).get("status") == "done")
            total = sum(ctx.progress.get(p, {}).get("strokes", 0) for p in painters)
        if done:
            dec["result"] = f"Картина готова: {subject}"
            dec["strokes"] = total
            ctx.bb.write_decision(dec)
            try:
                fixtures = Path(os.environ.get("FIXTURES", "./test_fixtures"))
                scenario = os.environ.get("SCENARIO", "painters-1")
                out = export_pointcloud(ctx.bb.root, fixtures=fixtures, scenario=scenario)
                meta = ctx.bb.read_json(out / "pointcloud.json", {}) or {}
                ctx.emit({"kind": "pointcloud", "path": str(out.relative_to(ctx.bb.root)),
                           "agents": list(meta.get("agents", {}).keys())})
            except Exception as exc:  # noqa: BLE001 — export must not block DONE
                ctx.emit({"kind": "pointcloud_error", "error": type(exc).__name__})
            ctx.emit({"kind": "decision", "decision": {"result": dec["result"]}})
            from run_log import finalize_run
            finalize_run(ctx.bb, summary={
                "subject": subject, "strokes": total, "result": dec["result"],
            })
            _transition(ctx, "DONE", round_, 0)
            return {"thought": f"Все мазки на месте ({total} шт.). "
                               f"«{subject}» завершена. Фаза ГОТОВО.",
                    "messages": [], "idle": False}
        if is_curated(ctx):
            idle_msg = "Жду куратора на холсте."
        else:
            n = sum(1 for p in painters if ctx.progress.get(p, {}).get("status") == "done")
            idle_msg = f"ОТЧЁТ: {n}/{len(painters)} художников отчитались."
        return {"thought": idle_msg, "messages": [], "idle": True}

    return {"thought": "ГОТОВО.", "messages": [], "idle": True}
