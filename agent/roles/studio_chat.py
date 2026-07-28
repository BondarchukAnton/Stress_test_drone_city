"""Decentralized studio chat: a free common channel + optional private threads.

The coordinator is reduced to a THIN FACILITATOR — it opens the floor once, then
only *triggers* the vote when the chat has run its course. It does NOT grant
turns. Drones self-organize (stigmergy): each drone reads the board and decides
locally whether to speak — it opens with its idea, then replies only when
addressed by name or when something new was said since it last spoke, and it
caps its own turns so the conversation always terminates.

Flow (painting task, STUDIO_CHAT=1):  INIT → CHAT → CONVERGE → EXECUTE(collab) → REPORT → DONE
"""
from __future__ import annotations

import os

from brain import (
    clean_chat_line,
    is_reasoning_leak,
    json_schema_format,
    parse_llm_json,
    schema_supported,
)
from context_budget import OUTPUT_AGENT_TURN, build_system_prompt, negotiation_messages
from .painter_palette import my_entry, roster

# The chat is NOT cut off by a turn count — it ends when the drones themselves
# are done (each signals `done` = convinced / nothing to add). MAX_CHAT_TURNS is
# only a runaway SAFETY cap, so set it high.
MAX_CHAT_TURNS = int(os.environ.get("STUDIO_CHAT_TURNS", "8"))    # per drone (safety net)
MIN_CHAT_EACH = int(os.environ.get("STUDIO_CHAT_MIN", "1"))       # everyone speaks ≥ this


def is_studio(ctx) -> bool:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    return (cfg.get("studio")
            or os.environ.get("STUDIO_CHAT", "0") not in ("0", "", "false", "no"))


def _inspiration(ctx) -> list[str]:
    sm = ctx.scenario_map or {}
    return sm.get("inspiration") or sm.get("subjects") or []


def chat_messages(ctx) -> list[dict]:
    return [m for m in ctx.messages if m.get("type") == "CHAT" and m.get("phase") == "CHAT"]


def chat_counts(ctx, painters: list[str]) -> dict[str, int]:
    c = {p: 0 for p in painters}
    for m in chat_messages(ctx):
        if m.get("from") in c:
            c[m["from"]] += 1
    return c


def _name(ctx, agent_id: str) -> str:
    for m in ctx.messages:
        if m.get("from") == agent_id and (m.get("payload") or {}).get("name"):
            return m["payload"]["name"]
    return agent_id


def derive_candidates(ctx) -> list[str]:
    """Raw ballot material = the distinct subjects proposed in the chat.
    (The facilitator clusters near-duplicates via ballot.py before the vote —
    exact-string dedup alone split votes in the 2026-07-02 live run.)"""
    subs: list[str] = []
    for m in chat_messages(ctx):
        s = str((m.get("payload") or {}).get("subject") or "").strip()
        if s and s not in subs:
            subs.append(s[:60])
    return subs


def latest_stances(ctx, painters: list[str]) -> dict[str, str]:
    """Each drone's FINAL chat position (last stated subject). Carried into the
    CONVERGE vote so the ballot reflects the chat (chat/vote coherence)."""
    out: dict[str, str] = {}
    for m in chat_messages(ctx):
        f = m.get("from")
        s = str((m.get("payload") or {}).get("subject") or "").strip()
        if f in painters and s:
            out[f] = s[:60]
    return out


def _drone_msgs(chat: list[dict], d: str) -> list[dict]:
    return [m for m in chat if m.get("from") == d]


def _latest_done(chat: list[dict], d: str) -> bool:
    """True if this drone's MOST RECENT message says it's done (convinced / nothing
    to add). A drone that gets re-addressed speaks again → its latest is not done,
    so the debate reopens for it."""
    ms = _drone_msgs(chat, d)
    return bool((ms[-1].get("payload") or {}).get("done")) if ms else False


def should_end_chat(ctx, painters: list[str], deadline_passed: bool) -> bool:
    """End only when the drones themselves are finished — EVERYONE's latest message
    is `done` (they convinced each other / ran out of arguments) — or the runaway
    safety cap / deadline fires. The turn count never cuts a live argument short."""
    chat = chat_messages(ctx)
    counts = chat_counts(ctx, painters)
    everyone_spoke = all(counts.get(p, 0) >= MIN_CHAT_EACH for p in painters)
    # a drone that burned its safety cap can never post a `done` message again —
    # treat capped as done, or one talkative drone freezes the exit and the chat
    # always burns the full DEADLINE_CHAT (real-brain-only stall).
    all_done = everyone_spoke and all(
        _latest_done(chat, p) or counts.get(p, 0) >= MAX_CHAT_TURNS for p in painters)
    all_capped = all(counts.get(p, 0) >= MAX_CHAT_TURNS for p in painters)
    enough_ideas = len(derive_candidates(ctx)) >= 1
    return deadline_passed or all_capped or (all_done and enough_ideas)


# ---- drone side: local decision to speak + one chat turn --------------------
def _should_speak(ctx, painters: list[str]) -> tuple[bool, str, bool]:
    """Local decision. Returns (speak, reply_to_id, yield_now).
    Uses message POSITIONS (not timestamps) so it works even when several messages
    share the same second. A drone stays quiet once it's `done`, UNLESS someone
    addresses it again (then it re-engages — it can rethink)."""
    chat = chat_messages(ctx)
    me = ctx.agent_id
    my_name = my_entry(ctx).get("name", me).lower()
    idx = [i for i, m in enumerate(chat) if m.get("from") == me]
    mine = [chat[i] for i in idx]
    last_pos = idx[-1] if idx else -1
    others_after = [m for m in chat[last_pos + 1:] if m.get("from") != me]

    addressed = ""
    for m in others_after:
        p = m.get("payload") or {}
        if str(p.get("address", "")).lower() in (me.lower(), my_name) or my_name in m.get("body", "").lower():
            addressed = m.get("from", "")

    if not mine:
        return True, "", False              # open with my idea
    if len(mine) >= MAX_CHAT_TURNS:
        return False, "", False             # runaway safety cap
    if addressed:
        return True, addressed, False       # called out by name → answer (even if I was done)
    if (mine[-1].get("payload") or {}).get("done"):
        return False, "", False             # satisfied and nobody re-addressed me → quiet
    if others_after:
        return True, "", False              # new arguments since my last turn → respond
    return True, "", True                   # conversation stalled for me → say "я всё"


def artist_chat_turn(ctx, make_msg, painters: list[str]) -> dict:
    speak, reply_to_id, yield_now = _should_speak(ctx, painters)
    if not speak:
        return {"thought": "Слушаю мастерскую — мне добавить нечего.",
                "thinking": "", "messages": [], "idle": True}

    me = my_entry(ctx)
    name = me.get("name", ctx.agent_id)
    color = me.get("hex", "#cccccc")
    insp = _inspiration(ctx)
    chat_all = chat_messages(ctx)
    first_turn = not any(m.get("from") == ctx.agent_id for m in chat_all)
    # who a drone may address / thread with: ONLY fellow artists (never the
    # coordinator/facilitator, never an unknown name)
    valid_targets = set()
    for pp in roster(ctx):
        valid_targets.add(str(pp.get("id", "")).lower())
        if pp.get("name"):
            valid_targets.add(str(pp["name"]).lower())
    reply_name = _name(ctx, reply_to_id) if reply_to_id else ""
    recent = chat_all[-6:]
    recent_txt = "\n".join(
        f"- {(m.get('payload') or {}).get('name', m.get('from'))}: {m.get('body', '')}"
        for m in recent) or "(тишина — ты открываешь разговор)"

    fallback_sub = (insp[hash(ctx.agent_id) % len(insp)] if insp else "пейзаж")
    yield_line = f"{name}: согласен, мне добавить нечего."
    fallback = {
        "line": (yield_line if yield_now else
                 f"{name}: предлагаю сюжет «{fallback_sub}»."
                 + (f" {reply_name}, что скажешь?" if reply_name else "")),
        "subject": fallback_sub,
        "address": reply_name,
        # mock always wraps up after one turn; real brain decides for itself
        "done": bool(yield_now or ctx.brain.is_mock),
    }

    fallback_reason = ""
    if ctx.brain.is_mock:
        turn = dict(fallback)
    else:
        system = build_system_prompt(
            ctx.soul_body or "",
            f"Ты — художник {name}, пигмент {color}. Ты в свободном чате мастерской "
            "с другими художниками. Спорь и УБЕЖДАЙ: защищай свой сюжет доводами или "
            "искренне согласись, если тебя убедили. Отвечай соперникам по имени. "
            "Коротко, живо, в характере — не повторяйся.")
        task = (
            # mood hint only on the FIRST turn, and tell them to riff, not parrot —
            # otherwise every drone repeats the same fixture words and it reads scripted
            ((f"Настроение задачи (оттолкнись, но НЕ повторяй эти слова дословно — "
              f"предложи свой конкретный образ): {', '.join(insp)}.\n") if insp and first_turn else "")
            + f"Недавние реплики:\n{recent_txt}\n\n"
            + (f"{reply_name} обратился к тебе — ответь ему. " if reply_name else "")
            + ("Кажется, спор исчерпан. Если ты согласен с общим направлением — "
               "подытожь и поставь done:true. " if yield_now else
               "Приведи НОВЫЙ довод или согласись, если тебя убедили. ")
            + "Назови КОНКРЕТНЫЙ сюжет, который поддерживаешь (можешь принять чужой).\n"
              'Ответ — один JSON: {"thinking":"…","line":"реплика в чат (1-2 предложения)",'
              '"subject":"короткое название сюжета","address":"имя, кому отвечаешь, или \'\'",'
              '"done": true если тебя убедили ИЛИ добавить больше нечего, иначе false}')
        # sampler-enforced JSON (verified on the sverk gateway: json_schema is a
        # real vLLM grammar there, not a soft hint) — kills the parse-failure class
        rf = None
        if schema_supported(ctx.brain):
            rf = json_schema_format("studio_chat", {
                "type": "object",
                "properties": {
                    "thinking": {"type": "string"},
                    "line": {"type": "string"},
                    "subject": {"type": "string", "maxLength": 80},
                    "address": {"type": "string", "maxLength": 60},
                    "done": {"type": "boolean"},
                },
                "required": ["line", "subject", "done"],
            })
        msgs = negotiation_messages(ctx, task, phases=("CHAT",))
        raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                             log_context="studio_chat", response_format=rf)
        if not raw and rf is not None:  # gateway rejected the schema → plain retry
            raw = ctx.brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                                 log_context="studio_chat")
        turn = dict(fallback)
        data, parse_err = parse_llm_json(raw or "")
        if not isinstance(data, dict):
            fallback_reason = parse_err or "нет JSON в ответе"
        if isinstance(data, dict):
            for k in ("subject", "address"):
                v = data.get(k)
                if isinstance(v, str) and v.strip() and not is_reasoning_leak(v):
                    turn[k] = v.strip()[:60]
            if isinstance(data.get("done"), bool):
                turn["done"] = data["done"]
            th = str(data.get("thinking") or "").strip()
            if th:
                turn["thinking"] = th[:2000]
            ln = clean_chat_line(str(data.get("line") or ""), fallback["line"],
                                 thinking=str(turn.get("thinking", "")))
            if ln and not is_reasoning_leak(ln):
                turn["line"] = ln[:300]

    line = str(turn.get("line") or fallback["line"])[:300]
    subject = str(turn.get("subject") or fallback_sub)[:60]
    address = str(turn.get("address") or reply_name or "")[:40]
    # only address a fellow artist — never the coordinator/facilitator or an unknown
    if address and address.lower() not in valid_targets:
        address = ""
    done = bool(turn.get("done", False))
    # a scripted fallback line must be VISIBLE as such — otherwise an sverk outage
    # produces a fake canned chat indistinguishable from a real one on the dashboard
    if fallback_reason and line == fallback["line"]:
        ctx.emit({"kind": "llm_error", "from": ctx.agent_id, "phase": "CHAT",
                  "context": "studio_chat", "attempt": 1, "max_attempts": 1,
                  "error": (fallback_reason + " — реплика заменена заготовкой")[:600],
                  "exhausted": True})
    thread = ""
    if address:
        pair = sorted([name, address])
        thread = f"{pair[0]}~{pair[1]}"
    msg = make_msg(ctx, "CHAT", (reply_to_id or "all") if address else "all", "CHAT",
                   body=line,
                   payload={"name": name, "color": color, "subject": subject,
                            "address": address, "thread": thread, "done": done})
    tag = " ✓готов" if done else ""
    return {"thought": line + tag, "thinking": str(turn.get("thinking", "")),
            "messages": [msg], "idle": False}
