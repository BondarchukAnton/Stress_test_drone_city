"""Shared debate primitives (reusable turn-based chat / debate / decision engine).

This module is deliberately domain-agnostic: it only knows about *positions*,
*arguments*, *floor grants* and *votes* on the blackboard. Any project can drive
it by supplying a topic (via the scenario map or DEBATE_* env) and a set of
souls — nothing here is tied to painting or safe-passage.

Message protocol (all on the shared blackboard, §3):

  FRAME      moderator -> all      the motion/question (+ options) to resolve
  OPENING    debater   -> all      initial position (parallel, no floor needed)
  FLOOR      moderator -> speaker  "the floor is yours" (turn token)
  ARGUMENT   debater   -> all      a turn: rebut / refine / hold a position
  VOTE       debater   -> all      final ballot over the candidate positions
  CONCLUSION moderator -> all      the tallied outcome (voting-only)

Phases (custom, driven by the moderator — the loop only special-cases DONE):

  FRAME -> OPENING -> DEBATE (rounds) -> VOTE -> CONCLUDE -> DONE
"""
from __future__ import annotations

import hashlib

# ---- message types --------------------------------------------------------
FRAME = "FRAME"
OPENING = "OPENING"
FLOOR = "FLOOR"
ARGUMENT = "ARGUMENT"
VOTE = "VOTE"
CONCLUSION = "CONCLUSION"

# ---- phases ---------------------------------------------------------------
P_FRAME = "FRAME"
P_OPENING = "OPENING"
P_DEBATE = "DEBATE"
P_VOTE = "VOTE"
P_CONCLUDE = "CONCLUDE"
P_DONE = "DONE"


def stable_key(*parts) -> int:
    """Process-stable tie-break (reproducible; no salted hash())."""
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:6], 16)


def clamp01(x, default: float = 0.5) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return 0.0 if v < 0 else 1.0 if v > 1 else round(v, 3)


def debaters(ctx) -> list[str]:
    """Active debaters: hub registry (plug-and-play) else the pinned DEBATERS env."""
    reg = ctx.bb.read_registry() if hasattr(ctx.bb, "read_registry") else {}
    found = sorted(k for k, v in reg.items() if v.get("role") == "debater")
    return found or ctx.config.get("debaters", [])


def frame(ctx) -> dict:
    """The motion under debate. Comes from the scenario map (fixtures/<x>/map.json),
    with env overrides, so any project just drops in a topic file."""
    sm = ctx.scenario_map or {}
    cfg = ctx.config or {}
    topic = cfg.get("topic") or sm.get("topic") or sm.get("motion") or "Open discussion"
    question = cfg.get("question") or sm.get("question") or topic
    options = cfg.get("options") or sm.get("options") or []
    options = [str(o).strip() for o in options if str(o).strip()]
    return {"topic": str(topic), "question": str(question), "options": options}


def _payload(m: dict) -> dict:
    return m.get("payload") or {}


def messages_by(ctx, type_: str, phase: str | None = None) -> list[dict]:
    out = []
    for m in ctx.messages:
        if m.get("type") != type_:
            continue
        if phase is not None and m.get("phase") != phase:
            continue
        out.append(m)
    return out


def openings(ctx) -> list[dict]:
    return messages_by(ctx, OPENING, P_OPENING)


def arguments(ctx, round_: int | None = None) -> list[dict]:
    out = messages_by(ctx, ARGUMENT, P_DEBATE)
    if round_ is not None:
        out = [m for m in out if _payload(m).get("round") == round_]
    return out


def votes(ctx) -> list[dict]:
    return messages_by(ctx, VOTE, P_VOTE)


def floors(ctx, round_: int | None = None) -> list[dict]:
    out = messages_by(ctx, FLOOR, P_DEBATE)
    if round_ is not None:
        out = [m for m in out if _payload(m).get("round") == round_]
    return out


def latest_positions(ctx) -> dict[str, dict]:
    """Each debater's most recent stated position (OPENING then ARGUMENTs),
    as {agent: {stance, confidence, round}}. Used for convergence + candidates."""
    pos: dict[str, dict] = {}
    for m in openings(ctx) + arguments(ctx):
        who = m.get("from")
        p = _payload(m)
        stance = str(p.get("stance") or "").strip()
        if not who or not stance:
            continue
        pos[who] = {
            "stance": stance,
            "confidence": clamp01(p.get("confidence"), 0.6),
            "round": p.get("round", 0),
        }
    return pos


def answered_floor_ids(ctx) -> set[str]:
    """Floor grants that already produced an ARGUMENT (matched by reply_to)."""
    ids = set()
    for a in arguments(ctx):
        rt = _payload(a).get("reply_to")
        if rt:
            ids.add(rt)
    return ids


def my_open_floor(ctx) -> dict | None:
    """The floor grant addressed to *me* (or the whole room) that I have not yet
    answered. Whether *I* answered is a per-agent question (see _already_argued_for)
    — we deliberately do NOT use the global answered_floor_ids here, or a floor
    granted to "all" would look answered for everyone the moment one debater replies."""
    mine = [f for f in floors(ctx)
            if (f.get("to") == ctx.agent_id or f.get("to") == "all")
            and not _already_argued_for(ctx, f)]
    if not mine:
        return None
    # most recent by round, then board-global seq (ts alone scrambles same-second)
    mine.sort(key=lambda f: (_payload(f).get("round", 0), f.get("seq") or 0, f.get("ts", "")))
    return mine[-1]


def _already_argued_for(ctx, floor_msg: dict) -> bool:
    rnd = _payload(floor_msg).get("round", 0)
    fid = floor_msg.get("id")
    for a in arguments(ctx, rnd):
        if a.get("from") != ctx.agent_id:
            continue
        if _payload(a).get("reply_to") == fid:
            return True
        # parallel mode: one FLOOR to "all" per round -> match by round only
        if floor_msg.get("to") == "all":
            return True
    return False


def candidate_positions(ctx) -> list[str]:
    """The ballot for the final vote: explicit options if the motion has them,
    else the distinct stances that emerged — CLUSTERED, not string-deduped, so
    near-duplicate phrasings of one position don't split its votes."""
    fr = frame(ctx)
    if fr["options"]:
        return fr["options"]
    from .ballot import cluster
    stances = [p["stance"] for _, p in sorted(latest_positions(ctx).items())]
    return [c["label"] for c in cluster(stances)]
