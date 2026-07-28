"""Moderator: the turn-based debate phase machine + voting-only conclusion.

Owns phase.json/decision.json (single-writer, §1) and drives:

  INIT/FRAME -> OPENING -> DEBATE (rounds) -> VOTE -> CONCLUDE -> DONE

Turn order (config DEBATE_TURNS):
  * moderated   -> the moderator picks the next speaker each turn (LLM-assisted,
                   deterministic fallback). One live speaker at a time.
  * round_robin -> fixed rotation through the debaters.
  * parallel    -> the whole room speaks each round (opening-statement style).

Dynamic length: rounds continue past DEBATE_MIN_ROUNDS while the debate is still
productive (fresh arguments / a speaker asks for the floor) and stop early on
consensus; they hard-stop at DEBATE_MAX_ROUNDS. Then the room votes and the
moderator tallies (majority or soul-weighted). No judge — voting only.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import debate_common as dc
from .phase_util import (
    deadline_in as _dl,
    deadline_passed as _deadline_passed,
    now as _now,
    parse_iso as _parse_iso,
)
from .phase_util import transition as _pu_transition


def _floor_expired(floor_msg: dict) -> bool:
    dt = _parse_iso((floor_msg.get("payload") or {}).get("deadline"))
    return bool(dt and _now() > dt)


def _transition(ctx, phase: str, round_: int, deadline_key: str, default: int):
    _pu_transition(ctx, phase, round_, default, key=deadline_key)


def _facilitate(ctx, phase: str, body: str, tag: str, extra: dict | None = None) -> dict:
    return {"from": ctx.agent_id, "to": "all", "phase": phase, "type": dc.FRAME
            if tag == "frame" else "FACILITATE", "ref": None, "body": body,
            "payload": {"tag": tag, "name": ctx.soul.get("name", "moderator"), **(extra or {})}}


def _said(ctx, tag: str) -> bool:
    for m in ctx.messages:
        if m.get("from") == ctx.agent_id and (m.get("payload") or {}).get("tag") == tag:
            return True
    return False


# ---- soul weights (for weighted voting) -----------------------------------
def _load_weights(ctx, debs: list[str]) -> dict[str, float]:
    from souls import load_soul
    base = os.environ.get("SOUL", "./souls/moderator.md")
    dirs = [os.environ.get("SOUL_DIR"), os.path.dirname(base) or ".", "./souls", "/app/souls"]
    out = {d: 1.0 for d in debs}
    for did in debs:
        for d in dirs:
            if not d:
                continue
            p = os.path.join(d, f"{did}.md")
            if os.path.isfile(p):
                try:
                    meta, _ = load_soul(p)
                    out[did] = float(meta.get("vote_weight", 1.0) or 1.0)
                except Exception:  # noqa: BLE001
                    pass
                break
    return out


# ---- speaker selection ----------------------------------------------------
def _last_argument(ctx):
    # board-global seq is the true append order; ts alone scrambled same-second turns
    args = sorted(dc.arguments(ctx) + dc.openings(ctx),
                  key=lambda m: (m.get("seq") or 0, m.get("ts", "")))
    return args[-1] if args else None


def _select_speaker(ctx, remaining: list[str], mode: str, round_: int) -> str:
    """Choose who speaks next among those who have not spoken this round."""
    order = sorted(remaining, key=lambda d: ctx.config.get("debaters", []).index(d)
                   if d in ctx.config.get("debaters", []) else 99)
    if mode == "round_robin" or ctx.brain.is_mock:
        return order[0]

    # deterministic default: the speaker who was just rebutted gets to answer
    last = _last_argument(ctx)
    named = str((last.get("payload") if last else {} or {}).get("rebut") or "").strip().lower()
    if named:
        for d in order:
            nm = ctx.config.get("names", {}).get(d, d).lower()
            if named in (d.lower(), nm):
                return d
    default = order[0]

    # moderated: let the LLM call on the most compelling next voice
    fr = dc.frame(ctx)
    recent = "\n".join(
        f"- {(_p(m)).get('name', m.get('from'))} [{m.get('from')}]: {m.get('body', '')}"
        for m in sorted(dc.arguments(ctx) + dc.openings(ctx),
                        key=lambda m: (m.get("seq") or 0, m.get("ts", "")))[-8:]
    ) or "(no arguments yet)"
    system = (ctx.soul_body or "") + (
        "\nYou are the debate moderator. Pick who should speak next to make the "
        "debate most productive — call on someone challenged by name, someone with a "
        "dissenting view, or the strongest voice yet to respond this round."
    )
    user = (f"MOTION: {fr['question']}\nRound {round_}. Recent floor:\n{recent}\n\n"
            f"Choose the NEXT speaker from exactly these ids: {order}.\n"
            'Reply JSON only: {"next":"<id>","reason":"short"}')
    data = ctx.brain.think_json(system, user, max_tokens=200)
    if isinstance(data, dict):
        nxt = str(data.get("next") or "").strip()
        if nxt in order:
            return nxt
    return default


def _p(m: dict) -> dict:
    return m.get("payload") or {}


def _who_to_answer(ctx, speaker: str) -> str:
    last = _last_argument(ctx)
    if last and last.get("from") != speaker:
        return _p(last).get("name") or last.get("from") or ""
    return ""


# ---- convergence ----------------------------------------------------------
def _top_share(ctx) -> float:
    from .ballot import fold
    pos = dc.latest_positions(ctx)
    if not pos:
        return 0.0
    counts: dict[str, int] = {}
    for p in pos.values():
        key = fold(p["stance"])[:80]
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(pos)


def _keep_debating(ctx, cur_round: int, ds: dict) -> tuple[bool, str]:
    if cur_round < ds["min_rounds"]:
        return True, f"below min_rounds ({cur_round}/{ds['min_rounds']})"
    if cur_round >= ds["max_rounds"]:
        return False, f"reached max_rounds ({ds['max_rounds']})"
    if _top_share(ctx) >= ds["consensus"]:
        return False, "consensus reached"
    last_args = dc.arguments(ctx, cur_round)
    if last_args and all(_p(a).get("yield") for a in last_args):
        return False, "all speakers yielded"
    novel = any((a.get("novelty") or 0) >= ctx.config.get("novelty_min", 0.3) for a in last_args)
    wants = any(_p(a).get("request_floor") for a in last_args)
    if novel or wants:
        return True, "fresh arguments / floor requested"
    return False, "debate stalled (no new arguments)"


# ---- main step ------------------------------------------------------------
def step(ctx):
    """First-class role entry point (ROLE=moderator). The coordinator's
    task==debate dispatch still lands here too, for backward compatibility."""
    return debate_step(ctx)


def debate_step(ctx):
    phase = ctx.phase.get("phase", "INIT")
    round_ = ctx.phase.get("round", 0)
    ds = ctx.config["debate"]
    debs = dc.debaters(ctx)
    ctx.config["debaters"] = debs  # keep discovered list for speaker ordering
    fr = dc.frame(ctx)
    mode = ds["turns"]

    # ---- INIT / FRAME: announce the motion ----
    if phase in ("INIT", dc.P_FRAME):
        from run_log import start_run
        (ctx.bb.root / "task.md").write_text(
            f"# Debate\n\n**Motion:** {fr['question']}\n\n"
            + ("**Options:** " + ", ".join(fr["options"]) + "\n" if fr["options"] else "")
            + f"\nFormat: {mode}, {ds['min_rounds']}–{ds['max_rounds']} rounds, "
              f"conclusion by {ds['rule']} vote.\n",
            encoding="utf-8")
        ctx.bb.write_json(ctx.bb.root / "config.yaml.json", {
            "task": "debate", "debaters": debs, "topic": fr["topic"],
            "question": fr["question"], "options": fr["options"],
            "turns": mode, "rule": ds["rule"],
            "min_rounds": ds["min_rounds"], "max_rounds": ds["max_rounds"],
        })
        start_run(ctx.bb, meta={
            "task": "debate", "topic": fr["topic"], "debaters": debs, "turns": mode})
        ctx.emit({"kind": "debate", "topic": fr["topic"], "question": fr["question"],
                  "options": fr["options"], "debaters": debs, "turns": mode})
        frame_msg = _facilitate(
            ctx, dc.P_FRAME,
            f"Motion on the floor: {fr['question']}"
            + (f"  Options: {', '.join(fr['options'])}." if fr["options"] else "")
            + "  Opening statements, please.", "frame",
            {"topic": fr["topic"], "question": fr["question"], "options": fr["options"]})
        _transition(ctx, dc.P_OPENING, 1, "opening", 60)
        return {"thought": f"Motion framed: {fr['question']}. Opening statements.",
                "messages": [frame_msg], "idle": False}

    # ---- OPENING: wait for all initial positions ----
    if phase == dc.P_OPENING:
        opened = {m.get("from") for m in dc.openings(ctx) if m.get("from") in debs}
        if len(opened) >= len(debs) or _deadline_passed(ctx):
            _transition(ctx, dc.P_DEBATE, 1, "debate", 90)
            return {"thought": f"{len(opened)}/{len(debs)} opened. Debate round 1.",
                    "messages": [], "idle": False}
        return {"thought": f"OPENING: {len(opened)}/{len(debs)} statements in.",
                "messages": [], "idle": True}

    # ---- DEBATE: run the current round ----
    if phase == dc.P_DEBATE:
        cur = round_ or 1

        if mode == "parallel":
            existing = [f for f in dc.floors(ctx, cur) if f.get("to") == "all"]
            if not existing:
                fmsg = {"from": ctx.agent_id, "to": "all", "phase": dc.P_DEBATE,
                        "type": dc.FLOOR, "ref": None,
                        "body": f"Round {cur}: everyone respond to the latest arguments.",
                        "payload": {"speaker": "all", "round": cur,
                                    "deadline": _dl(ctx, "debate", 90)}}
                ctx.emit({"kind": "floor", "speaker": "all", "round": cur})
                return {"thought": f"Round {cur}: floor open to the room.",
                        "messages": [fmsg], "idle": False}
            spoke = {a.get("from") for a in dc.arguments(ctx, cur)}
            if len(spoke) >= len(debs) or _floor_expired(existing[0]):
                return _end_of_round(ctx, cur, ds, debs)
            return {"thought": f"Round {cur}: {len(spoke)}/{len(debs)} spoke.",
                    "messages": [], "idle": True}

        # sequential (moderated / round_robin): one live speaker at a time
        answered = dc.answered_floor_ids(ctx)
        floors_round = dc.floors(ctx, cur)
        live = [f for f in floors_round if f.get("id") not in answered and not _floor_expired(f)]
        if live:
            who = live[-1].get("to")
            return {"thought": f"Round {cur}: floor is with {who}.",
                    "messages": [], "idle": True}

        granted = {f.get("to") for f in floors_round}
        remaining = [d for d in debs if d not in granted]
        if remaining:
            nxt = _select_speaker(ctx, remaining, mode, cur)
            respond_to = _who_to_answer(ctx, nxt)
            fmsg = {"from": ctx.agent_id, "to": nxt, "phase": dc.P_DEBATE,
                    "type": dc.FLOOR, "ref": None,
                    "body": (f"{nxt}, the floor is yours"
                             + (f" — respond to {respond_to}." if respond_to else ".")),
                    "payload": {"speaker": nxt, "round": cur, "respond_to": respond_to,
                                "deadline": _dl(ctx, "floor", 45)}}
            ctx.emit({"kind": "floor", "speaker": nxt, "round": cur, "respond_to": respond_to})
            return {"thought": f"Round {cur}: calling on {nxt}"
                               + (f" (vs {respond_to})." if respond_to else "."),
                    "messages": [fmsg], "idle": False}

        return _end_of_round(ctx, cur, ds, debs)

    # ---- VOTE: wait for all ballots ----
    if phase == dc.P_VOTE:
        cast = {m.get("from") for m in dc.votes(ctx) if m.get("from") in debs}
        if len(cast) >= len(debs) or _deadline_passed(ctx):
            _transition(ctx, dc.P_CONCLUDE, round_, "vote", 30)
            return {"thought": f"{len(cast)}/{len(debs)} ballots in. Tallying.",
                    "messages": [], "idle": False}
        return {"thought": f"VOTE: {len(cast)}/{len(debs)} ballots.",
                "messages": [], "idle": True}

    # ---- CONCLUDE: tally (voting only) ----
    if phase == dc.P_CONCLUDE:
        return _conclude(ctx, ds, debs, fr)

    return {"thought": "Debate concluded.", "messages": [], "idle": True}


def _end_of_round(ctx, cur: int, ds: dict, debs: list[str]):
    keep, why = _keep_debating(ctx, cur, ds)
    if keep:
        _transition(ctx, dc.P_DEBATE, cur + 1, "debate", 90)
        msg = _facilitate(ctx, dc.P_DEBATE,
                          f"Round {cur + 1}. Sharpen your case or concede ground.",
                          f"round-{cur + 1}")
        return {"thought": f"Round {cur} closed ({why}). Opening round {cur + 1}.",
                "messages": [msg], "idle": False}
    _transition(ctx, dc.P_VOTE, cur, "vote", 60)
    candidates = dc.candidate_positions(ctx)
    msg = _facilitate(ctx, dc.P_VOTE,
                      "Debate closed. Cast your final vote on: "
                      + "; ".join(candidates) + ".", "call_vote",
                      {"candidates": candidates})
    return {"thought": f"Debate closed after round {cur} ({why}). Calling the vote.",
            "messages": [msg], "idle": False}


def _conclude(ctx, ds: dict, debs: list[str], fr: dict):
    from run_log import finalize_run

    from .ballot import clusters_from_state, resolve as _resolve_ballot

    weighted = ds["rule"] == "weighted"
    weights = _load_weights(ctx, debs) if weighted else {d: 1.0 for d in debs}
    candidates = dc.candidate_positions(ctx)
    clusters = clusters_from_state(candidates, None)
    counts: dict[str, int] = {}
    wsum: dict[str, float] = {}
    conf: dict[str, float] = {}
    record: dict[str, dict] = {}
    # one ballot per voter (last wins) — a re-vote must not be double-counted
    last_ballot: dict[str, dict] = {}
    for v in dc.votes(ctx):
        last_ballot[v.get("from")] = v
    for voter, v in last_ballot.items():
        p = _p(v)
        raw_choice = str(p.get("choice") or "").strip()
        if not raw_choice or raw_choice.lower() in ("abstain", "воздержался", "воздерживаюсь"):
            record[voter] = {"choice": "abstain", "weight": weights.get(voter, 1.0),
                             "confidence": dc.clamp01(p.get("confidence"), 0.5)}
            continue
        # every ballot maps onto a published candidate («Hybrid» → «Hybrid (2-3
        # days in office)»); an unmatched write-in is an explicit abstain, never
        # a pool entry that fragments the count
        choice = _resolve_ballot(raw_choice, clusters)
        if not choice:
            record[voter] = {"choice": "abstain", "raw": raw_choice,
                             "weight": weights.get(voter, 1.0),
                             "confidence": dc.clamp01(p.get("confidence"), 0.5)}
            continue
        w = weights.get(voter, 1.0)
        counts[choice] = counts.get(choice, 0) + 1
        wsum[choice] = wsum.get(choice, 0.0) + w
        conf[choice] = conf.get(choice, 0.0) + dc.clamp01(p.get("confidence"), 0.6)
        record[voter] = {"choice": choice, "raw": raw_choice, "weight": w,
                         "confidence": dc.clamp01(p.get("confidence"), 0.6)}

    if not counts:
        winner, total, share = "no consensus", 0, 0.0
    else:
        ranked = sorted(
            candidates,
            key=lambda s: (wsum.get(s, 0.0) if weighted else counts.get(s, 0),
                           counts.get(s, 0), conf.get(s, 0.0), -dc.stable_key(s)),
            reverse=True)
        winner = ranked[0]
        total = sum(counts.values())
        share = round(counts.get(winner, 0) / total, 3) if total else 0.0

    result = (f"DECISION: {winner}  ·  {counts.get(winner, 0)}/{total} votes"
              + (f", weight {round(wsum.get(winner, 0.0), 2)}" if weighted else "")
              + f" ({int(share * 100)}%)")
    decision = {"topic": fr["topic"], "question": fr["question"], "winner": winner,
                "rule": ds["rule"], "counts": counts, "weighted": wsum,
                "confidence": conf, "votes": record, "share": share, "result": result}
    ctx.bb.write_decision(decision)
    ctx.emit({"kind": "decision", "decision": {"result": result, "winner": winner,
                                               "counts": counts, "share": share}})
    concl = {"from": ctx.agent_id, "to": "all", "phase": dc.P_CONCLUDE,
             "type": dc.CONCLUSION, "ref": None,
             "body": (f"The room decides: “{winner}” "
                      f"({counts.get(winner, 0)} of {total} votes, {int(share * 100)}%). "
                      + ("Unanimous." if share >= 1.0 else
                         "Majority." if share > 0.5 else "Plurality — no majority.")),
             "payload": {"name": ctx.soul.get("name", "moderator"), **decision}}
    finalize_run(ctx.bb, summary={"winner": winner, "counts": counts,
                                  "share": share, "result": result})
    ctx.bb.write_phase(dc.P_DONE, ctx.phase.get("round", 0))
    ctx.emit({"kind": "phase", "phase": dc.P_DONE, "round": ctx.phase.get("round", 0)})
    return {"thought": result, "messages": [concl], "idle": False}
