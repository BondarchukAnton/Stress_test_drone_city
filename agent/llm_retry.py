"""LLM JSON retry helpers + error events for the dashboard."""
from __future__ import annotations

import os
from typing import Any, Callable

from brain import chat_json_with_retry

# Retry + ошибки на дашборде — только при планировании мазков (фаза рисования).
PAINT_JSON_CONTEXTS = frozenset({"curated_compose", "stroke_compose", "collab_compose"})


def max_llm_retries() -> int:
    return max(1, int(os.environ.get("LLM_JSON_RETRIES", "3")))


def emit_llm_error(
    ctx,
    *,
    attempt: int,
    error: str,
    raw: str = "",
    context: str = "json",
    exhausted: bool = False,
) -> None:
    phase = "?"
    if hasattr(ctx, "phase") and isinstance(ctx.phase, dict):
        phase = ctx.phase.get("phase", "?")
    ctx.emit({
        "kind": "llm_error",
        "from": ctx.agent_id,
        "phase": phase,
        "attempt": attempt,
        "max_attempts": max_llm_retries(),
        "error": (error or "неизвестная ошибка")[:600],
        "context": context,
        "raw_preview": (raw or "")[:400],
        "exhausted": exhausted,
    })


def llm_json(
    ctx,
    system: str,
    messages: list[dict],
    *,
    validate: Callable[[Any], str | None] | None = None,
    max_tokens: int = 1400,
    timeout: float | None = None,
    context: str = "json",
    schema: dict | None = None,
) -> tuple[Any | None, str, list[dict]]:
    """chat_json_with_retry + llm_error events on each failed attempt."""
    retries = max_llm_retries()

    def on_error(attempt: int, error: str, raw: str, ctx_name: str) -> None:
        if ctx_name not in PAINT_JSON_CONTEXTS or attempt >= retries:
            return
        emit_llm_error(
            ctx, attempt=attempt, error=error, raw=raw, context=ctx_name,
        )

    parsed, raw, log = chat_json_with_retry(
        ctx.brain,
        system,
        messages,
        validate=validate,
        max_tokens=max_tokens,
        max_retries=retries,
        timeout=timeout,
        on_error=on_error,
        context=context,
        schema=schema,
    )
    if not parsed and log and context in PAINT_JSON_CONTEXTS:
        suffix = {
            "curated_compose": " — запасной план композиции",
            "stroke_compose": " — процедурный набор фигур",
            "collab_compose": " — детерминированный слой по скелету сцены",
        }.get(context, " — используется запасной вариант")
        emit_llm_error(
            ctx,
            attempt=log[-1]["attempt"],
            error=log[-1]["error"] + suffix,
            raw=raw,
            context=context,
            exhausted=True,
        )
    return parsed, raw, log
