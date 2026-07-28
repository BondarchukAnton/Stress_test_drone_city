"""LLM context/output budgets (default window ≈ 32k tokens per call)."""
from __future__ import annotations

import os

# ~32k total window; reserve headroom for output + schema docs in the user turn.
CONTEXT_TOKENS = int(os.environ.get("LLM_CONTEXT_TOKENS", "32000"))
CHARS_PER_TOKEN = float(os.environ.get("LLM_CHARS_PER_TOKEN", "3.2"))

SOUL_MAX_CHARS = int(os.environ.get("LLM_SOUL_MAX_CHARS", "12000"))
TRANSCRIPT_MAX_LINES = int(os.environ.get("LLM_TRANSCRIPT_LINES", "500"))
TRANSCRIPT_MAX_CHARS = int(os.environ.get("LLM_TRANSCRIPT_CHARS", "28000"))
# echoed-bad-output budget per retry turn: Cyrillic runs ~1.5-2.5 chars/token,
# so 20k chars ≈ 8-13k tokens of garbage per attempt — 6k is plenty for the fix
RETRY_RAW_MAX_CHARS = int(os.environ.get("LLM_RETRY_RAW_CHARS", "6000"))
COMPOSE_PLAN_MAX_CHARS = int(os.environ.get("LLM_COMPOSE_PLAN_CHARS", "4000"))

# Output (completion) token caps per call type
OUTPUT_AGENT_TURN = int(os.environ.get("LLM_OUTPUT_AGENT", "1200"))
OUTPUT_STROKE_COMPOSE = int(os.environ.get("LLM_OUTPUT_COMPOSE", "4096"))
OUTPUT_CURATED_COMPOSE = int(os.environ.get("LLM_OUTPUT_CURATED", "8192"))
OUTPUT_VOTE_JSON = int(os.environ.get("LLM_OUTPUT_VOTE", "800"))


def clip_text(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def build_system_prompt(soul_body: str, *sections: str) -> str:
    parts = [clip_text(soul_body or "", SOUL_MAX_CHARS)]
    parts.extend(s for s in sections if s)
    return "\n\n".join(parts)


def negotiation_messages(
    ctx,
    final_task: str,
    *,
    phases: tuple[str, ...] = ("PROPOSE", "CONVERGE"),
    include_self_as_assistant: bool = True,
) -> list[dict]:
    """Board history as multi-turn chat + final instruction (no duplicate transcript)."""
    msgs: list[dict] = []
    for m in ctx.messages:
        if m.get("phase") not in phases:
            continue
        body = (m.get("body") or "").strip()
        if not body:
            continue
        who = m.get("payload", {}).get("name", m.get("from", "?"))
        if include_self_as_assistant and m.get("from") == ctx.agent_id:
            msgs.append({"role": "assistant", "content": body})
        else:
            msgs.append({"role": "user", "content": f"{who} ({m['from']}): {body}"})
    msgs.append({"role": "user", "content": final_task})
    return msgs
