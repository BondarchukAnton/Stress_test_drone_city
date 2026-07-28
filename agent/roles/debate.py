"""Debater role: opening -> turn-based arguments (floor-gated) -> final vote.

One LLM call per turn with the full debate transcript as multi-turn chat. The
role is idempotent against the board (§7): it only speaks when it has something
new to say — an unposted opening, an *unanswered floor grant* addressed to it,
or an uncast vote. The moderator (see moderator.py) owns turn order.

Language-agnostic and project-agnostic: the persona (soul) supplies the voice
and default leaning; the motion comes from the scenario map. Reuse as-is in any
project by swapping souls + the topic fixture.
"""
from __future__ import annotations

from brain import clean_chat_line, is_reasoning_leak, parse_llm_json
from context_budget import OUTPUT_AGENT_TURN, build_system_prompt, negotiation_messages

from . import debate_common as dc
from . import has_posted

DEBATE_RULES = (
    "DEBATE RULES:\n"
    "- You are a participant in a moderated, turn-based debate. Stay fully in character.\n"
    "- Argue for the position your persona believes. Address other speakers by name.\n"
    "- Make the STRONGEST, best-reasoned case you can: cite concrete reasons, weigh\n"
    "  trade-offs, and rebut the specific claims others made — do not just repeat yourself.\n"
    "- Change your position ONLY if genuinely persuaded, and say so plainly.\n"
    "- `line` is what the room hears (1-3 sentences). Put all private reasoning in `thinking`.\n"
    "- Reply with ONE JSON object, first character '{'. No markdown, no numbered steps."
)


def _persona_name(ctx) -> str:
    return ctx.soul.get("name", ctx.agent_id)


def _default_stance(ctx, options: list[str]) -> str:
    """Deterministic starting position (soul override, else spread across options)."""
    s = ctx.soul.get("stance") or ctx.soul.get("position")
    if s:
        return str(s)
    if options:
        return options[dc.stable_key(ctx.agent_id) % len(options)]
    return f"{_persona_name(ctx)}'s view"


def _turn(ctx, task: str, fallback: dict, *, phases: tuple[str, ...]) -> dict:
    """One debate turn -> {thinking, line, stance?, confidence?, rebut?, choice?, ...}."""
    brain = ctx.brain
    if brain.is_mock:
        return dict(fallback)
    system = build_system_prompt(ctx.soul_body or "", DEBATE_RULES)
    msgs = negotiation_messages(ctx, task, phases=phases)
    raw = brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN, log_context="debate")
    out = dict(fallback)
    data, _err = parse_llm_json(raw or "")
    if isinstance(data, dict):
        for k in ("stance", "rebut", "choice"):
            v = data.get(k)
            if isinstance(v, str) and v.strip() and not is_reasoning_leak(v):
                out[k] = v.strip()[:160]
        if data.get("confidence") is not None:
            out["confidence"] = dc.clamp01(data.get("confidence"), out.get("confidence", 0.6))
        for b in ("yield", "request_floor"):
            if isinstance(data.get(b), bool):
                out[b] = data[b]
        think = str(data.get("thinking") or data.get("reasoning") or "").strip()
        if think:
            out["thinking"] = think[:2400]
        cleaned = clean_chat_line(str(data.get("line") or ""), fallback.get("line", ""),
                                  thinking=str(out.get("thinking", "")))
        if cleaned and not is_reasoning_leak(cleaned):
            out["line"] = cleaned[:400]
    else:
        cleaned = clean_chat_line(raw or "", fallback.get("line", ""))
        if cleaned and not is_reasoning_leak(cleaned):
            out["line"] = cleaned[:400]
    return out


def _resolve_choice(choice: str, candidates: list[str], fallback_stance: str) -> str:
    """Map a free-text ballot onto exactly one candidate position."""
    from .ballot import clusters_from_state, resolve as resolve_ballot
    pool = [c for c in candidates if c]
    if not pool:
        return choice or fallback_stance or "abstain"
    c = (choice or "").strip()
    for cand in pool:  # exact / case-insensitive
        if c == cand or c.lower() == cand.lower():
            return cand
    for cand in pool:  # substring either way
        if c and (c.lower() in cand.lower() or cand.lower() in c.lower()):
            return cand
    clusters = clusters_from_state(pool, None)
    hit = resolve_ballot(c, clusters)  # fold / token / unique-distinctive-token
    if hit:
        return hit
    fs = (fallback_stance or "").strip().lower()
    for cand in pool:
        if fs and (fs in cand.lower() or cand.lower() in fs):
            return cand
    hit = resolve_ballot(fallback_stance or "", clusters)
    if hit:
        return hit
    # No genuine match → ABSTAIN (empty), not a silent hash-picked vote for an
    # arbitrary candidate. The tally counts empty ballots as abstentions.
    return ""


def step(ctx):
    phase = ctx.phase.get("phase", dc.P_FRAME)
    fr = dc.frame(ctx)
    name = _persona_name(ctx)

    # ---- OPENING: everyone states an initial position (parallel) ----
    if phase == dc.P_OPENING:
        if has_posted(ctx, dc.OPENING, phase=dc.P_OPENING):
            return {"thought": "Opening already on the board.", "thinking": "",
                    "messages": [], "idle": True}
        stance = _default_stance(ctx, fr["options"])
        opts = (("Pick the position you most believe from: "
                 + "; ".join(fr["options"])) if fr["options"]
                else "State your position in your own words (keep the stance label short).")
        fallback = {
            "line": f"I open by arguing: {stance}.",
            "stance": stance,
            "confidence": 0.6,
        }
        turn = _turn(
            ctx,
            f"MOTION: {fr['question']}\n\n"
            f"This is your OPENING statement. {opts}\n"
            'Return JSON: {"thinking":"private notes","line":"1-3 sentences to the room",'
            '"stance":"your position, short label <=80 chars","confidence":0.0-1.0}',
            fallback, phases=(dc.P_FRAME, dc.P_OPENING),
        )
        stance = str(turn.get("stance") or stance)[:80]
        line = str(turn.get("line") or fallback["line"])[:400]
        msg = {"from": ctx.agent_id, "to": "all", "phase": dc.P_OPENING,
               "type": dc.OPENING, "ref": None, "body": line,
               "payload": {"name": name, "stance": stance,
                           "confidence": dc.clamp01(turn.get("confidence"), 0.6),
                           "round": 0, "rationale": line}}
        return {"thought": line, "thinking": str(turn.get("thinking", "")),
                "messages": [msg], "idle": False}

    # ---- DEBATE: speak only while holding the floor ----
    if phase == dc.P_DEBATE:
        floor = dc.my_open_floor(ctx)
        if not floor:
            return {"thought": "Listening — waiting for the floor.", "thinking": "",
                    "messages": [], "idle": True}
        fp = floor.get("payload") or {}
        round_ = fp.get("round", 1)
        respond_to = fp.get("respond_to") or ""
        my_pos = dc.latest_positions(ctx).get(ctx.agent_id, {})
        stance = my_pos.get("stance") or _default_stance(ctx, fr["options"])
        ask = (f"The moderator asks you to respond to {respond_to}. "
               if respond_to else "")
        fallback = {
            "line": f"I hold my position: {stance}.",
            "stance": stance, "confidence": my_pos.get("confidence", 0.6),
            "rebut": respond_to, "yield": False, "request_floor": False,
        }
        turn = _turn(
            ctx,
            f"MOTION: {fr['question']}\n\n"
            f"It is YOUR turn (round {round_}). {ask}"
            "Make your strongest argument: engage the latest points others raised, "
            "rebut them by name, and either hold or update your position.\n"
            'Return JSON: {"thinking":"private notes","line":"1-3 sentences to the room",'
            '"stance":"your current position, short label","confidence":0.0-1.0,'
            '"rebut":"name you answer, or \'\'","yield":true if you have nothing to add,'
            '"request_floor":true if you want another turn after this}',
            fallback, phases=(dc.P_FRAME, dc.P_OPENING, dc.P_DEBATE),
        )
        stance = str(turn.get("stance") or stance)[:80]
        line = str(turn.get("line") or fallback["line"])[:400]
        msg = {"from": ctx.agent_id, "to": "all", "phase": dc.P_DEBATE,
               "type": dc.ARGUMENT, "ref": floor.get("id"), "body": line,
               "payload": {"name": name, "stance": stance,
                           "confidence": dc.clamp01(turn.get("confidence"), 0.6),
                           "round": round_, "reply_to": floor.get("id"),
                           "rebut": str(turn.get("rebut") or "")[:60],
                           "yield": bool(turn.get("yield", False)),
                           "request_floor": bool(turn.get("request_floor", False)),
                           "rationale": line}}
        return {"thought": line, "thinking": str(turn.get("thinking", "")),
                "messages": [msg], "idle": False}

    # ---- VOTE: one final ballot over the candidate positions ----
    if phase == dc.P_VOTE:
        if has_posted(ctx, dc.VOTE, phase=dc.P_VOTE):
            return {"thought": "Ballot already cast.", "thinking": "",
                    "messages": [], "idle": True}
        candidates = dc.candidate_positions(ctx)
        my_pos = dc.latest_positions(ctx).get(ctx.agent_id, {})
        my_stance = my_pos.get("stance") or _default_stance(ctx, fr["options"])
        listing = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(candidates)) or "  (open)"
        fallback = {
            "line": f"I vote for: {my_stance}.",
            "choice": my_stance, "confidence": my_pos.get("confidence", 0.7),
        }
        turn = _turn(
            ctx,
            f"MOTION: {fr['question']}\n\n"
            f"Final VOTE. Cast your ballot for EXACTLY ONE of these positions:\n{listing}\n"
            "Vote for what the debate persuaded you is best — you may vote against your "
            "own opening if genuinely convinced.\n"
            'Return JSON: {"thinking":"private notes","line":"one sentence explaining your vote",'
            '"choice":"exact position text from the list","confidence":0.0-1.0}',
            fallback, phases=(dc.P_FRAME, dc.P_OPENING, dc.P_DEBATE, dc.P_VOTE),
        )
        choice = _resolve_choice(str(turn.get("choice") or ""), candidates, my_stance)
        line = str(turn.get("line")
                   or (f"I vote for: {choice}." if choice else "I abstain."))[:400]
        msg = {"from": ctx.agent_id, "to": "all", "phase": dc.P_VOTE,
               "type": dc.VOTE, "ref": None, "body": line,
               "payload": {"name": name, "choice": choice,
                           "confidence": dc.clamp01(turn.get("confidence"), 0.7),
                           "rationale": line}}
        return {"thought": line, "thinking": str(turn.get("thinking", "")),
                "messages": [msg], "idle": False}

    return {"thought": "Standing by.", "thinking": "", "messages": [], "idle": True}
