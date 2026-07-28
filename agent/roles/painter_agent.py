"""Context-aware painter agent: bricks → shared build → vote on final titles.

One LLM call per action with full negotiation transcript. Mock brain invents
varied visual pieces from personality + run seed (nothing hardcoded in souls).
"""
from __future__ import annotations

import os
import random

from brain import (
    clean_chat_line,
    extract_quoted_subject,
    is_reasoning_leak,
    json_schema_format,
    parse_agent_turn,
    schema_supported,
)
from context_budget import OUTPUT_AGENT_TURN, build_system_prompt, negotiation_messages
from .collaborative import (
    bricks_from_messages,
    candidates_for_vote,
    collab_summary_text,
    read_collab,
)
from .painter_palette import (
    my_entry,
    palette_context,
    palette_rules_short,
    sanitize_subject,
)

BUILD_ROUNDS = int(os.environ.get("PAINT_BUILD_ROUNDS", "2"))

_LANG_NOTE = (
    "ВАЖНО: поля line, subject, thinking и endorse — только на русском языке. "
    "Названия настроения пиши по-русски."
)


def _seed(*parts) -> int:
    from .painter_compose import _seed as s
    return s(*parts)


def _paint_seed(ctx) -> int:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    return int(cfg.get("paint_seed", 0))


def _inspiration(ctx) -> list[str]:
    sm = ctx.scenario_map
    return sm.get("inspiration") or sm.get("subjects") or []


def my_brick(ctx) -> dict | None:
    for b in bricks_from_messages(ctx):
        if b.get("from") == ctx.agent_id:
            return b
    return None


def my_piece(ctx) -> str:
    b = my_brick(ctx)
    return (b or {}).get("piece", "")


def my_build_count(ctx) -> int:
    return sum(
        1 for m in ctx.messages
        if m.get("from") == ctx.agent_id
        and m.get("type") == "BUILD"
        and m.get("phase") == "BUILD"
    )


def resolve_candidate_endorse(
    endorse: str,
    candidates: list[str],
    scores: dict,
    paint_seed: int,
) -> str:
    """Голос за один из общих финальных вариантов."""
    pool = [c for c in candidates if c]
    if not pool:
        return endorse or "без названия"
    if endorse.strip() in pool:
        return endorse.strip()
    scored = {k: float(v) for k, v in scores.items() if k in pool}
    if scored:
        return max(scored, key=lambda s: (scored[s], _seed(s, paint_seed)))
    return pool[paint_seed % len(pool)]


def format_vote_line(endorse: str) -> str:
    return f"Голосую за общий финальный вариант «{endorse}»."


# Persona-driven fallbacks (used only if the LLM reply is unusable) — keyed by
# the painter's *leaning*, NOT by a hardcoded name. Simple, shape-renderable.
_LEAN_PIECES = {
    "тепло": ["круг солнца и полоса неба", "золотая полоса заката", "рассвет над горизонтом"],
    "холод": ["овал озера и небо", "полоса воды с волной", "лунный диск над водой"],
    "контраст": ["диагональная линия-молния", "резкий акцент по диагонали", "крест на небе"],
    "природа": ["треугольник ели", "ряд холмов-треугольников", "дерево — крона и ствол"],
    "минимализм": ["одна крупная форма в центре", "пустое небо и линия горизонта"],
    "драма": ["бурное небо полосами", "штормовая диагональ", "огненный круг"],
}

_LEAN_SHAPES = {
    "тепло": ["круг", "прямоугольник"],
    "холод": ["овал", "волна"],
    "контраст": ["линия", "крест"],
    "природа": ["треугольник", "прямоугольник"],
    "минимализм": ["прямоугольник", "круг"],
    "драма": ["дуга", "линия"],
}


def _fallback_piece(me: dict, rng) -> str:
    pool = _LEAN_PIECES.get(me.get("leaning", ""), ["простой пейзаж", "дом и солнце"])
    return pool[rng.randrange(len(pool))]


def _fallback_shapes(me: dict) -> list[str]:
    return _LEAN_SHAPES.get(me.get("leaning", ""), ["прямоугольник", "круг"])


def _agent_log(ctx, phase: str, task: str, raw: str, out: dict, *, fallback_used: bool):
    if os.environ.get("AGENT_LOG", "1") in ("0", "false", "no"):
        return
    ctx.emit({
        "kind": "agent_log",
        "from": ctx.agent_id,
        "phase": phase,
        "task": task[:100],
        "raw": (raw or "")[:800],
        "line": (out.get("line") or "")[:280],
        "thinking": (out.get("thinking") or "")[:400],
        "fallback_used": fallback_used,
        "raw_line": (out.get("_raw_line") or "")[:120],
    })
    try:
        from run_log import log_agent_turn
        log_agent_turn(phase, task, raw, out, fallback_used=fallback_used)
    except Exception:
        pass
    print(
        f"[{ctx.agent_id}] turn phase={phase} fallback={fallback_used} "
        f"line={(out.get('line') or '')[:80]!r}",
        flush=True,
    )


def _agent_turn(
    ctx,
    task: str,
    fallback: dict,
    *,
    phases: tuple[str, ...] = ("PROPOSE", "CONVERGE"),
    response_format: dict | None = None,
) -> dict:
    """One turn -> {thinking, line, subject?, endorse?, scores?, reply_to?}."""
    me = my_entry(ctx)
    name = me.get("name", ctx.agent_id)
    brain = ctx.brain
    inspiration = _inspiration(ctx)
    insp = ""
    if inspiration:
        insp = "\nПодсказки настроения (не копируй дословно — придумай своё): " + ", ".join(inspiration)
    persona_note = (
        f"\nХАРАКТЕР: ты {me.get('temperament','')} — {me.get('temperament_desc','')}. "
        f"Тебя тянет в «{me.get('leaning_desc', me.get('leaning',''))}». "
        f"Спорь и отстаивай своё в этом характере, отвечай соперникам по имени, "
        f"не повторяй чужое дословно."
    )
    json_schema = (
        'Ответ — ОДИН JSON-объект, первый символ {. Без markdown, без нумерованных шагов.\n'
        '{"thinking":"заметки (не в чат)","line":"короткое сообщение художникам (1–2 предложения, русский)",'
        '"piece":"визуальный кирпич","shapes":["треугольник"],"draft_line":"общая формулировка",'
        '"addition":"что добавляешь к черновику","endorse":"точный вариант из списка кандидатов",'
        '"scores":{...}}'
    )
    system = build_system_prompt(
        ctx.soul_body or "",
        palette_context(ctx),
        palette_rules_short(),
        _LANG_NOTE,
    )
    full_task = (
        f"ТЫ: {name} ({ctx.agent_id}), пигмент {me.get('color_name','')} ({me.get('hex','')}), "
        f"техника {me.get('technique','')}.{persona_note}{insp}\n\n"
        f"ЗАДАЧА: {task}\n\n"
        f"История переговоров — в предыдущих сообщениях чата выше.\n"
        f"В чат попадает только поле line. Весь разбор — в thinking.\n"
        f"{json_schema}"
    )
    phase = ctx.phase.get("phase", "?")
    msgs = negotiation_messages(ctx, full_task, phases=phases)

    if brain.is_mock:
        return {**fallback, "thinking": fallback.get("thinking", "")}

    raw = brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN,
                     response_format=response_format)
    if not raw and response_format is not None:
        # gateway rejected the schema → one plain retry before falling back
        raw = brain.chat(system, msgs, max_tokens=OUTPUT_AGENT_TURN)
    parsed = parse_agent_turn(raw or "", fallback)
    out = {**fallback, **{k: v for k, v in parsed.items() if v is not None}}
    raw_line = str(parsed.get("line") or out.get("line") or "")
    out["_raw_line"] = raw_line
    fb_line = fallback.get("line", "")
    cleaned = clean_chat_line(out.get("line"), fb_line, thinking=str(out.get("thinking", "")))
    fallback_used = (cleaned == fb_line and bool(raw_line) and raw_line.strip() != fb_line.strip())
    if not cleaned or is_reasoning_leak(cleaned):
        cleaned = fb_line
        fallback_used = True
    out["line"] = cleaned

    _agent_log(ctx, phase, task, raw or "", out, fallback_used=fallback_used)
    out.pop("_raw_line", None)
    return out


def compose_brick(ctx) -> tuple[str, str, str, str, list[str]]:
    """(answer, thinking, chat_body, piece, shapes)"""
    me = my_entry(ctx)
    name = me.get("name", ctx.agent_id)
    seed = _paint_seed(ctx)
    rng = random.Random(_seed(name, seed, ctx.agent_id, "brick"))
    piece = _fallback_piece(me, rng)
    shapes = _fallback_shapes(me)
    color = me.get("hex", "")
    technique = me.get("technique", "")
    fallback = {
        "thinking": f"Даю кусочек «{piece}» — мой {color}, {technique}.",
        "line": (
            f"Мой кирпич: «{piece}» ({', '.join(shapes)}). "
            f"Цвет {color}, техника {technique} — не целый сюжет, а деталь для общей картины."
        ),
        "piece": piece,
        "shapes": shapes,
    }
    turn = _agent_turn(
        ctx,
        "КИРПИЧ: предложи ОДИН визуальный кусочек картины (не целый сюжет и не название). "
        f"Только твой цвет {color} и фигуры из палитры. Укажи shapes: треугольник, круг, овал, "
        "линия, прямоугольник. Не дублируй чужие кирпичи.",
        fallback,
        phases=("PROPOSE",),
    )
    thinking = str(turn.get("thinking", ""))
    line = str(turn.get("line", fallback["line"]))[:280]
    raw_piece = str(turn.get("piece", "")).strip()[:100]
    if not raw_piece or is_reasoning_leak(raw_piece):
        raw_piece = extract_quoted_subject(line) or piece
    piece_out = sanitize_subject(raw_piece, piece)
    raw_shapes = turn.get("shapes")
    if isinstance(raw_shapes, list) and raw_shapes:
        shapes_out = [str(s)[:30] for s in raw_shapes[:4]]
    else:
        shapes_out = shapes
    return line, thinking, line, piece_out, shapes_out


def compose_build(ctx, round_idx: int) -> tuple[str, str, str, dict]:
    """(answer, thinking, chat_body, payload with addition + draft_line)"""
    name = ctx.soul.get("name", ctx.agent_id)
    collab = read_collab(ctx.bb)
    bricks = collab.get("bricks") or bricks_from_messages(ctx)
    draft = collab.get("draft") or "общая картина"
    others = [b for b in bricks if b.get("from") != ctx.agent_id]
    ref = others[round_idx % len(others)] if others else {"name": "команда", "piece": "?"}
    my = my_piece(ctx) or "мой кирпич"
    draft_line = f"{draft} — усиливаю {ref.get('piece', '?')} моим «{my}»"
    fallback = {
        "thinking": f"Склеиваю «{my}» с кирпичом {ref.get('name')}.",
        "line": (
            f"{ref.get('name', '?')}, беру твой «{ref.get('piece', '?')}» и добавляю "
            f"свой «{my}» — получается: {draft_line[:80]}."
        ),
        "addition": f"связать «{my}» с «{ref.get('piece', '?')}»",
        "draft_line": draft_line[:120],
    }
    summary = collab_summary_text(collab)
    turn = _agent_turn(
        ctx,
        f"СБОРКА, раунд {round_idx + 1}/{BUILD_ROUNDS}:\n{summary}\n\n"
        "Дополни общий черновик: поле draft_line — одна короткая формулировка ОБЩЕЙ идеи "
        "(не четыре конкурирующих сюжета). addition — что именно добавляешь. "
        "Ссылайся на чужие кирпичи по имени.",
        fallback,
        phases=("PROPOSE", "BUILD"),
    )
    thinking = str(turn.get("thinking", ""))
    line = str(turn.get("line", fallback["line"]))[:280]
    addition = str(turn.get("addition", fallback["addition"]))[:200]
    dl = str(turn.get("draft_line", fallback["draft_line"]))[:120]
    if is_reasoning_leak(dl):
        dl = fallback["draft_line"]
    payload = {"addition": addition, "draft_line": dl, "round": round_idx}
    return line, thinking, line, payload


def _my_last_chat_subject(ctx) -> str:
    """The drone's FINAL stated position in the studio chat (its last subject)."""
    for m in reversed(ctx.messages):
        if (m.get("from") == ctx.agent_id and m.get("type") == "CHAT"
                and m.get("phase") == "CHAT"):
            s = str((m.get("payload") or {}).get("subject") or "").strip()
            if s:
                return s
    return ""


def compose_vote(ctx, paint_seed: int) -> tuple[str, str, str, dict]:
    """(answer, thinking, vote_body, scores+endorse payload fields)"""
    from .ballot import clusters_from_state, resolve as resolve_ballot
    name = ctx.soul.get("name", ctx.agent_id)
    my = my_piece(ctx)
    collab = read_collab(ctx.bb)
    candidates = candidates_for_vote(ctx)
    if not candidates:
        candidates = collab.get("candidates") or [collab.get("draft") or "без названия"]
    candidates = [c for c in candidates if c]
    clusters = clusters_from_state(candidates, collab.get("candidate_variants"))

    # chat/vote coherence: the vote must start from the drone's own final chat
    # stance — in the live audit run drones voted a subject the chat had already
    # argued away from, and the executed picture diverged from the consensus
    stance = _my_last_chat_subject(ctx)
    stance_cand = resolve_ballot(stance, clusters) if stance else ""

    rng = random.Random(_seed(name, paint_seed, "vote"))
    scores = {s: round(rng.uniform(5.0, 9.5), 1) for s in candidates}
    if stance_cand:
        scores[stance_cand] = 10.0  # fallback vote follows the chat stance
    endorse = resolve_candidate_endorse(stance_cand, candidates, scores, paint_seed)

    list_txt = "\n".join(f"  {i+1}. «{c}»" for i, c in enumerate(candidates))
    fallback = {
        "thinking": f"Кандидаты:\n{list_txt}\nОценки: {scores}. Выбор: «{endorse}».",
        "line": format_vote_line(endorse),
        "endorse": endorse,
        "scores": scores,
    }
    stance_note = ""
    if stance:
        stance_note = (
            f"\nВ чате твоя последняя позиция была «{stance}»"
            + (f" (в бюллетене это «{stance_cand}»)" if stance_cand and stance_cand != stance else "")
            + ". Голосуй согласованно с тем, к чему ты пришёл в споре; "
              "если передумал — объясни почему в line.")
    rf = None
    if schema_supported(ctx.brain) and candidates:
        # enum-locked ballot: the model physically cannot vote off-ballot
        rf = json_schema_format("painter_vote", {
            "type": "object",
            "properties": {
                "thinking": {"type": "string"},
                "line": {"type": "string"},
                "endorse": {"type": "string", "enum": candidates},
                "scores": {"type": "object",
                           "additionalProperties": {"type": "number"}},
            },
            "required": ["endorse", "line"],
        })
    turn = _agent_turn(
        ctx,
        f"ГОЛОСОВАНИЕ за ОДИН общий финальный вариант:\n{list_txt}\n{stance_note}\n"
        "Оцени КАЖДЫЙ вариант от 1 до 10 в scores. endorse — точная строка из списка. "
        "Это общие формулировки команды, не «чьи-то» сюжеты.",
        fallback,
        phases=("CHAT", "PROPOSE", "BUILD", "CONVERGE"),
        response_format=rf,
    )
    thinking = str(turn.get("thinking", ""))
    raw_endorse = str(turn.get("endorse", endorse)).strip()
    # map a paraphrased vote onto its ballot line before the exact-match resolver
    endorse = resolve_candidate_endorse(
        resolve_ballot(raw_endorse, clusters) or raw_endorse,
        candidates, scores, paint_seed,
    )
    raw_scores = turn.get("scores")
    if isinstance(raw_scores, dict):
        for k, v in raw_scores.items():
            if k in candidates:
                try:
                    scores[k] = float(v)
                except (TypeError, ValueError):
                    pass
    endorse = resolve_candidate_endorse(endorse, candidates, scores, paint_seed)
    line = format_vote_line(endorse)
    vote_payload = {
        "subject": endorse,
        "endorse": endorse,
        "my_piece": my,
        "scores": scores,
        "voter": ctx.agent_id,
        "for_author": "общий вариант",
    }
    return line, thinking, line, vote_payload
