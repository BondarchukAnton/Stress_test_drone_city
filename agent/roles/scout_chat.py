"""City-of-drones negotiation chat: scouts TALK to divide the city sectors.

Same architecture as the painting studio (studio_chat.py): the coordinator is a
thin facilitator that opens the floor once and derives the outcome; the scouts
self-organize in a free stigmergic CHAT — each drone reads the board, claims a
sector, defends or trades it when contested, addresses teammates by name, and
signals `done` when it is satisfied with the split. The chat ends on
done-consensus (not a turn cap), then the facilitator turns the last claims
into the sector assignment; conflicts and gaps resolve deterministically.

Enabled by default for task=safe_passage; SCOUT_CHAT=0 restores the legacy
silent PROPOSE→CONVERGE flow.

TODO(crypto): agent-to-agent payments. When drones can hold wallets, a
contested sector becomes a market: a drone that wants to swap zones offers
payment ("I'll take far-out D if you compensate my travel"), escrowed on the
blackboard and settled on delivery (progress.status=done for the traded
sector). Design sketch: (1) wallet id in the soul frontmatter / persona,
(2) an OFFER/ACCEPT message pair inside this same CHAT protocol with
{sector, amount, currency} payload, (3) settlement hook in the coordinator's
REPORT phase once coverage for the traded sector verifies. Not implemented —
revisit after the payment rail is chosen.
"""
from __future__ import annotations

import os

from brain import json_schema_format, parse_llm_json, schema_supported
from context_budget import OUTPUT_AGENT_TURN, build_system_prompt, negotiation_messages

# Reuse the studio chat plumbing — these helpers are task-agnostic
from .studio_chat import MAX_CHAT_TURNS, MIN_CHAT_EACH, _latest_done, chat_counts, chat_messages


def is_scout_chat(ctx) -> bool:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    if "scout_chat" in cfg:
        return bool(cfg.get("scout_chat"))
    return os.environ.get("SCOUT_CHAT", "1") not in ("0", "", "false", "no")


def _name(ctx) -> str:
    return ctx.soul.get("name", ctx.agent_id)


def should_end_chat(ctx, scouts: list[str], deadline_passed: bool) -> bool:
    """Same termination shape as the studio: everyone spoke and everyone's
    LATEST message is done (capped counts as done), or the safety cap/deadline."""
    chat = chat_messages(ctx)
    counts = chat_counts(ctx, scouts)
    everyone_spoke = all(counts.get(s, 0) >= MIN_CHAT_EACH for s in scouts)
    all_done = everyone_spoke and all(
        _latest_done(chat, s) or counts.get(s, 0) >= MAX_CHAT_TURNS for s in scouts)
    all_capped = all(counts.get(s, 0) >= MAX_CHAT_TURNS for s in scouts)
    return deadline_passed or all_capped or all_done


def last_claims(ctx, scouts: list[str], sectors: list[str]) -> dict[str, tuple[str, int]]:
    """Each scout's LAST valid sector claim -> {scout: (sector, seq)}."""
    out: dict[str, tuple[str, int]] = {}
    for m in chat_messages(ctx):
        who = m.get("from")
        c = str((m.get("payload") or {}).get("claim") or "").strip().upper()
        if who in scouts and c in sectors:
            out[who] = (c, int(m.get("seq") or 0))
    return out


def derive_assignment(ctx, scouts: list[str], sectors: list[str]) -> dict[str, str]:
    """Claims -> one sector per scout. A contested sector goes to whoever
    settled on it FIRST (earlier last-claim seq); losers and silent scouts get
    the leftover sectors in stable order. Always total: every scout ends up
    with a sector, every sector with an owner (when counts allow)."""
    claims = last_claims(ctx, scouts, sectors)
    amap: dict[str, str] = {}
    taken: set[str] = set()
    for who in sorted(claims, key=lambda w: claims[w][1]):  # earliest settled first
        sec = claims[who][0]
        if sec not in taken:
            amap[who] = sec
            taken.add(sec)
    leftover = [s for s in sectors if s not in taken]
    for who in scouts:
        if who not in amap:
            amap[who] = leftover.pop(0) if leftover else sectors[len(amap) % len(sectors)]
    return amap


# ---- drone side: local decision to speak + one chat turn --------------------
def _should_speak(ctx, scouts: list[str], sectors: list[str]) -> tuple[bool, str, bool]:
    """(speak, reply_to_name, yield_now). Positions, not timestamps."""
    chat = chat_messages(ctx)
    me = ctx.agent_id
    my_name = _name(ctx).lower()
    idx = [i for i, m in enumerate(chat) if m.get("from") == me]
    mine = [chat[i] for i in idx]
    last_pos = idx[-1] if idx else -1
    others_after = [m for m in chat[last_pos + 1:] if m.get("from") != me]

    addressed = ""
    for m in others_after:
        p = m.get("payload") or {}
        if str(p.get("address", "")).lower() in (me.lower(), my_name) or my_name in m.get("body", "").lower():
            addressed = str(p.get("name") or m.get("from") or "")
    my_claim = ""
    for m in reversed(mine):
        c = str((m.get("payload") or {}).get("claim") or "").strip().upper()
        if c in sectors:
            my_claim = c
            break
    contested = any(
        str((m.get("payload") or {}).get("claim") or "").strip().upper() == my_claim
        for m in others_after) if my_claim else False

    if not mine:
        return True, "", False              # open with my claim
    if len(mine) >= MAX_CHAT_TURNS:
        return False, "", False             # runaway safety cap
    if addressed or contested:
        return True, addressed, False       # called out / my sector was grabbed
    if (mine[-1].get("payload") or {}).get("done"):
        return False, "", False             # satisfied and nobody re-engaged me
    if others_after:
        return True, "", False              # new info since my last turn
    return True, "", True                   # stalled → wrap up


def _mock_turn(ctx, name: str, sectors: list[str], scouts: list[str],
               reply_name: str, yield_now: bool) -> dict:
    """Deterministic no-LLM turn: claim the first free sector by my index."""
    chat = chat_messages(ctx)
    others_claimed = {
        str((m.get("payload") or {}).get("claim") or "").strip().upper()
        for m in chat if m.get("from") != ctx.agent_id}
    idx = scouts.index(ctx.agent_id) if ctx.agent_id in scouts else 0
    pref = sectors[idx % len(sectors)]
    claim = pref if pref not in others_claimed else next(
        (s for s in sectors if s not in others_claimed), pref)
    prios = ", ".join(str(p) for p in ctx.soul.get("priorities", [])) or "coverage"
    line = (f"{name}: I'll take sector {claim} — it fits my focus ({prios})."
            + (f" {reply_name}, does that work for you?" if reply_name else ""))
    if yield_now:
        line = f"{name}: the split looks solid to me — locking {claim}."
    return {"line": line, "claim": claim, "address": reply_name, "done": True,
            "thinking": ""}


def scout_chat_turn(ctx, make_msg, scouts: list[str]) -> dict:
    sectors = list(ctx.config.get("sectors") or [])
    speak, reply_name, yield_now = _should_speak(ctx, scouts, sectors)
    if not speak:
        return {"thought": "Listening — my sector claim stands.",
                "thinking": "", "messages": [], "idle": True}

    name = _name(ctx)
    fallback = _mock_turn(ctx, name, sectors, scouts, reply_name, yield_now)
    turn = dict(fallback)
    if not ctx.brain.is_mock:
        chat_all = chat_messages(ctx)
        recent = "\n".join(
            f"- {(m.get('payload') or {}).get('name', m.get('from'))}: {m.get('body', '')}"
            for m in chat_all[-6:]) or "(silence — you open the negotiation)"
        roster = ", ".join(scouts)
        system = build_system_prompt(
            ctx.soul_body or "",
            f"You are scout drone {name} ({ctx.agent_id}). The team ({roster}) must "
            f"split the city into sectors {', '.join(sectors)} — ONE sector each, no "
            "gaps. Negotiate in the open channel: claim the sector that fits your "
            "priorities, argue for it, address teammates by name, and trade if two of "
            "you want the same zone. Short, concrete lines.")
        task = (
            f"Recent channel:\n{recent}\n\n"
            + (f"{reply_name} addressed you — answer them. " if reply_name else "")
            + ("The negotiation looks settled. If you agree with the split, confirm "
               "your sector and set done:true. " if yield_now else
               "State (or defend / update) YOUR claim. If your preferred sector is "
               "taken, argue or pick another. ")
            + 'Reply with ONE JSON object: {"thinking":"private notes",'
              '"line":"1-2 sentences to the channel",'
              f'"claim":"one of {sectors}","address":"teammate name or \'\'",'
              '"done": true when you are satisfied with the split}')
        rf = None
        if schema_supported(ctx.brain):
            rf = json_schema_format("scout_chat", {
                "type": "object",
                "properties": {
                    "thinking": {"type": "string"},
                    "line": {"type": "string"},
                    "claim": {"type": "string", "enum": sectors},
                    "address": {"type": "string", "maxLength": 40},
                    "done": {"type": "boolean"},
                },
                "required": ["line", "claim", "done"],
            })
        msgs = negotiation_messages(ctx, task, phases=("CHAT",))
        raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                             log_context="scout_chat", response_format=rf)
        if not raw and rf is not None:
            raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                                 log_context="scout_chat")
        data, parse_err = parse_llm_json(raw or "")
        if isinstance(data, dict):
            c = str(data.get("claim") or "").strip().upper()
            if c in sectors:
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
                      "context": "scout_chat", "attempt": 1, "max_attempts": 1,
                      "error": ((parse_err or "empty reply") + " — canned line used")[:600],
                      "exhausted": True})

    # only address a fellow scout — ids from the roster, display names as they
    # appeared in the chat (souls carry the names; the config only has ids)
    valid_names = {s.lower() for s in scouts}
    for m in chat_messages(ctx):
        n = str((m.get("payload") or {}).get("name") or "").strip()
        if n:
            valid_names.add(n.lower())
    address = turn.get("address", "")
    if address and address.lower() not in valid_names and address != reply_name:
        address = ""
    msg = make_msg(ctx, "CHAT", "all", "CHAT",
                   body=turn["line"],
                   payload={"name": name, "claim": turn["claim"],
                            "address": address, "done": bool(turn.get("done"))})
    tag = " ✓settled" if turn.get("done") else ""
    return {"thought": turn["line"] + tag, "thinking": turn.get("thinking", ""),
            "messages": [msg], "idle": False}
