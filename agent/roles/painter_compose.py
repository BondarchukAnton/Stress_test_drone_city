"""Abstract color painting — no literal layers or scene templates.

Each painter knows only its spray colour and mark technique. The negotiated
title is mood/inspiration, not a blueprint. Strokes are abstract colour fields
across the full canvas, densified per technique.
"""
from __future__ import annotations

import hashlib
import math
import os
import random

from brain import _parse_json
from context_budget import (
    OUTPUT_STROKE_COMPOSE,
    OUTPUT_VOTE_JSON,
    COMPOSE_PLAN_MAX_CHARS,
    build_system_prompt,
    negotiation_messages,
)
from llm_retry import llm_json
from .paint_shapes import (
    SHAPE_SCHEMA_DOC,
    SHAPE_RECIPES,
    SHAPE_VISUAL_GUIDE,
    extract_compose_plan,
    extract_shape_plan,
    procedural_shape_plan,
    shapes_to_polylines,
)
from .painter_palette import (
    canvas_state_summary,
    my_entry,
    palette_context,
    technique_hint,
)


def _seed(*parts) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16)


def _validate_strokes(strokes, w: int, h: int, *, max_strokes: int | None = None):
    cap = max_strokes or int(os.environ.get("PAINT_MAX_STROKES", "1200"))
    out = []
    if not isinstance(strokes, list):
        return None
    for poly in strokes[:cap]:
        if not isinstance(poly, list) or len(poly) < 2:
            continue
        pts = []
        for pt in poly:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            try:
                x = max(0.0, min(float(w), float(pt[0])))
                y = max(0.0, min(float(h), float(pt[1])))
            except (TypeError, ValueError):
                continue
            pts.append([round(x, 1), round(y, 1)])
        if len(pts) >= 2:
            out.append(pts)
    return out if out else None


def _stroke_budget(technique: str) -> int:
    base = {
        "stippling": 400,
        "hatching": 280,
        "soft_bands": 180,
        "long_strokes": 120,
    }.get(technique, 140)
    mult = float(os.environ.get("PAINT_DENSITY", "3"))
    cap = int(os.environ.get("PAINT_MAX_STROKES", "1200"))
    return min(cap, max(50, int(base * mult)))


def _canvas_bounds(h: int) -> tuple[float, float]:
    return 4.0, float(h - 4)


def _segments_along(p0, p1, step: float) -> list:
    x0, y0 = p0
    x1, y1 = p1
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 0.05:
        return [[[round(x0, 1), round(y0, 1)], [round(x0 + 0.5, 1), round(y0 + 0.5, 1)]]]
    n = max(1, int(dist / step))
    segs = []
    for i in range(n):
        t0, t1 = i / n, (i + 1) / n
        a = [round(x0 + (x1 - x0) * t0, 1), round(y0 + (y1 - y0) * t0, 1)]
        b = [round(x0 + (x1 - x0) * t1, 1), round(y0 + (y1 - y0) * t1, 1)]
        segs.append([a, b])
    return segs


def densify_strokes(
    strokes: list,
    *,
    technique: str,
    w: int,
    h: int,
    seed: int,
    subject: str = "",
) -> list:
    """Expand guide paths into many short spray segments across the full canvas."""
    step = {
        "stippling": 0.9,
        "hatching": 1.4,
        "soft_bands": 1.8,
        "long_strokes": 2.8,
    }.get(technique, 2.0)
    budget = _stroke_budget(technique)
    out: list = []
    for poly in strokes:
        if not poly:
            continue
        if len(poly) == 1:
            p = poly[0]
            out.append([p, [round(p[0] + 0.5, 1), round(p[1] + 0.5, 1)]])
            continue
        for i in range(len(poly) - 1):
            out.extend(_segments_along(poly[i], poly[i + 1], step))

    y_lo, y_hi = _canvas_bounds(h)
    rng = random.Random(_seed(seed, technique, subject, "fill"))

    if technique == "stippling":
        while len(out) < budget:
            x = rng.uniform(3, w - 3)
            y = rng.uniform(y_lo, y_hi)
            d = 0.3 + rng.random() * 1.0
            out.append([[round(x, 1), round(y, 1)], [round(x + d, 1), round(y + d * 0.25, 1)]])
    elif technique == "hatching":
        ang = ((seed % 17) - 8) * 0.12
        spacing = 2.8
        y = y_lo
        while len(out) < budget and y <= y_hi:
            x0 = rng.uniform(4, w * 0.35)
            x1 = rng.uniform(w * 0.65, w - 4)
            dx = math.cos(ang) * 6
            out.append([[round(x0, 1), round(y, 1)], [round(x1 + dx, 1), round(y + math.sin(ang) * 5, 1)]])
            y += spacing
    elif technique == "soft_bands":
        band = 0
        while len(out) < budget:
            y = y_lo + (y_hi - y_lo) * (band / max(1, budget // 25))
            band += 1
            amp = 0.6 + (seed % 5) * 0.3
            n = max(8, int(w / 5))
            pts = []
            for k in range(n):
                x = 3 + (w - 6) * k / max(1, n - 1)
                pts.append([round(x, 1), round(y + amp * math.sin(k * 0.45 + seed * 0.1), 1)])
            for i in range(len(pts) - 1):
                out.append([pts[i], pts[i + 1]])
            if band > budget:
                break
    else:
        while len(out) < budget:
            x0 = rng.uniform(4, w - 24)
            y0p = rng.uniform(y_lo, y_hi)
            length = 14 + rng.random() * 30
            bend = rng.uniform(-3, 3)
            out.append([[round(x0, 1), round(y0p, 1)], [round(x0 + length, 1), round(y0p + bend, 1)]])

    return out[:budget]


def _wandering_curve(w, h, rng: random.Random, seed: int) -> list:
    """Organic path — no semantic placement."""
    n_pts = 5 + rng.randint(0, 6)
    x = rng.uniform(8, w - 8)
    y = rng.uniform(8, h - 8)
    pts = [[round(x, 1), round(y, 1)]]
    for _ in range(n_pts - 1):
        x = max(4, min(w - 4, x + rng.uniform(-18, 18)))
        y = max(4, min(h - 4, y + rng.uniform(-14, 14)))
        pts.append([round(x, 1), round(y, 1)])
    return pts


def abstract_color_guides(
    technique: str,
    w: int,
    h: int,
    seed: int,
    *,
    agent_id: str,
    subject: str,
) -> list:
    """Non-representational guide paths seeded by colour-carrier + mood title."""
    rng = random.Random(_seed(agent_id, seed, subject, technique))
    guides: list = []
    n = 10 + (seed % 12)

    if technique == "long_strokes":
        for _ in range(n):
            x0 = rng.uniform(4, w - 30)
            y0 = rng.uniform(4, h - 4)
            angle = rng.uniform(-0.4, 0.4)
            length = rng.uniform(20, min(w, h) * 0.7)
            guides.append([
                [round(x0, 1), round(y0, 1)],
                [round(x0 + length * math.cos(angle), 1), round(y0 + length * math.sin(angle), 1)],
            ])
    elif technique == "soft_bands":
        for _ in range(n):
            guides.append(_wandering_curve(w, h, rng, seed))
    elif technique == "hatching":
        ang = rng.uniform(-math.pi / 3, math.pi / 3)
        spacing = 7 + (seed % 4)
        y = rng.uniform(8, h * 0.3)
        while y < h - 8 and len(guides) < n + 8:
            x0 = rng.uniform(4, w * 0.2)
            x1 = rng.uniform(w * 0.8, w - 4)
            dx = math.cos(ang) * 5
            dy = math.sin(ang) * 5
            guides.append([[round(x0, 1), round(y, 1)], [round(x1 + dx, 1), round(y + dy, 1)]])
            y += spacing
    else:  # stippling — cluster centres as tiny crosses
        clusters = 6 + (seed % 6)
        for _ in range(clusters):
            cx = rng.uniform(10, w - 10)
            cy = rng.uniform(10, h - 10)
            for _ in range(3 + rng.randint(0, 4)):
                guides.append([
                    [round(cx, 1), round(cy, 1)],
                    [round(cx + rng.uniform(-6, 6), 1), round(cy + rng.uniform(-6, 6), 1)],
                ])

    return guides


def _extract_stroke_list(raw) -> list | None:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("strokes", "polylines", "lines", "paths", "data"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
    return None


def _finalize_strokes(strokes, ctx, canvas: dict, subject: str, paint_seed: int) -> list:
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    technique = my_entry(ctx).get("technique", "soft_bands")
    seed = _seed(ctx.agent_id, subject, paint_seed)
    return densify_strokes(
        strokes, technique=technique, w=w, h=h, seed=seed, subject=subject,
    )


def _compose_messages(ctx, task: str) -> list[dict]:
    return negotiation_messages(
        ctx, task, phases=("PROPOSE", "CONVERGE", "EXECUTE", "REPORT"),
    )


def _validate_compose_shapes(parsed) -> str | None:
    shapes = extract_shape_plan(parsed)
    if not shapes:
        if not isinstance(parsed, dict):
            return "Ответ не JSON-объект"
        if "shapes" not in parsed:
            return "Нет поля shapes — массив фигур [{type, cx, cy, ...}, ...]"
        return "shapes пуст или без поля type у фигур"
    if len(shapes) < 3:
        return f"Слишком мало фигур в shapes: {len(shapes)}, нужно хотя бы 3"
    return None


def plan_strokes(ctx, canvas: dict, subject: str, paint_seed: int) -> tuple[list, str]:
    """Фигуры → полилинии → densify. Возвращает (мазки, замысел)."""
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    me = my_entry(ctx)
    technique = me.get("technique", "soft_bands")
    color = me.get("hex", "#ffffff")
    name = me.get("name", ctx.agent_id)
    seed = _seed(ctx.agent_id, subject, paint_seed)
    budget = _stroke_budget(technique)
    brain = ctx.brain

    compose_thinking = ""

    def _from_shapes(shape_list: list[dict], source: str) -> list:
        polys = shapes_to_polylines(shape_list, w, h)
        ctx.emit({
            "kind": "compose_log", "from": ctx.agent_id,
            "subject": subject, "source": source, "shapes": len(shape_list),
            "polylines": len(polys), "technique": technique, "color": color,
        })
        return polys

    if brain.is_mock:
        shapes = procedural_shape_plan(w, h, seed, technique)
        base = _from_shapes(shapes, "shapes_procedural")
        compose_thinking = (
            f"Процедурный план: {len(shapes)} фигур {technique} цветом {color} "
            f"для «{subject}»."
        )
        return _finalize_strokes(base, ctx, canvas, subject, paint_seed), compose_thinking

    system = build_system_prompt(
        ctx.soul_body or "",
        palette_context(ctx),
        "Ты осознанно компонуешь фигуры на холсте. Отвечай только JSON.",
    )
    task = (
        f"{canvas_state_summary(ctx)}\n\n"
        f"РИСОВАНИЕ: ты — {name}. Пигмент: {me['label']} ({color}). "
        f"Техника: {technique} — {technique_hint(technique)}\n"
        f"Договорённый сюжет: «{subject}»\n"
        f"Твой вклад — только {me['label']} мазки. Не изображай буквально чужие цвета "
        f"(нет фиолетового/розового на холсте).\n\n"
        f"Холст {w}×{h} единиц. Центр ({w // 2},{h // 2}). "
        f"Толщина мазка ≈ 2 ед.\n\n"
        f"{SHAPE_RECIPES}\n{SHAPE_SCHEMA_DOC}\n{SHAPE_VISUAL_GUIDE}\n\n"
        f"Размести 12–35 фигур. В compose_plan опиши каждую крупную фигуру до размещения. "
        f"Крупные массы — fill:true (закрашенные). Композиция цельная, не россыпь.\n"
        f"История переговоров — в сообщениях чата выше. "
        f"Ответ — один JSON-объект, без markdown."
    )
    parsed, raw_text, _log = llm_json(
        ctx, system, _compose_messages(ctx, task),
        validate=_validate_compose_shapes,
        max_tokens=OUTPUT_STROKE_COMPOSE,
        timeout=float(os.environ.get("COMPOSE_TIMEOUT", "120")),
        context="stroke_compose",
    )
    compose_thinking = extract_compose_plan(parsed) or (
        f"Композиция {me['label']} для «{subject}» на холсте {w}×{h}."
    )
    base = None
    source = "abstract"

    shape_plan = extract_shape_plan(parsed) if parsed else None
    if shape_plan:
        base = _from_shapes(shape_plan, "shapes_llm")
        source = "shapes_llm"
    else:
        stroke_list = _extract_stroke_list(parsed)
        if stroke_list is None and isinstance(parsed, list):
            stroke_list = parsed
        strokes = _validate_strokes(stroke_list, w, h, max_strokes=budget) if stroke_list else None
        if strokes:
            base = strokes
            source = "polylines_llm"
        else:
            shapes = procedural_shape_plan(w, h, seed, technique)
            base = _from_shapes(shapes, "shapes_fallback")

    ctx.emit({
        "kind": "compose_log", "from": ctx.agent_id,
        "subject": subject, "source": source, "guides": len(base),
        "technique": technique, "color": color,
    })
    final = _finalize_strokes(base, ctx, canvas, subject, paint_seed)
    ctx.emit({
        "kind": "compose_log", "from": ctx.agent_id,
        "subject": subject, "source": "densified",
        "count": len(final), "technique": technique, "color": color,
    })
    return final, compose_thinking


def mock_vote_scores(my_subject: str, proposals, paint_seed: int):
    """Per-painter scores with seed jitter so the coordinator tally varies."""
    from .painter_palette import subject_color_feasible

    scores = {}
    my_words = set(my_subject.lower().split())
    for p in proposals:
        subj = p.get("payload", {}).get("subject", "")
        if not subj:
            continue
        if subj == my_subject:
            base = 10.0
        else:
            overlap = len(my_words & set(subj.lower().split()))
            base = 4.5 + overlap * 2.5
        jitter = ((_seed(subj, my_subject, paint_seed) % 13) - 6) * 0.35
        if not subject_color_feasible(subj):
            jitter -= 4.0
        scores[subj] = round(max(1.0, min(10.0, base + jitter)), 1)
    endorse = max(scores, key=lambda s: (scores[s], _seed(s, paint_seed)))
    return scores, endorse


def llm_vote_scores(ctx, proposals, paint_seed: int):
    """Ask the model to score proposals; fall back to mock scoring."""
    my_subject = ""
    for p in proposals:
        if p.get("from") == ctx.agent_id:
            my_subject = (p.get("payload", {}).get("subject") or "").strip()
    my_subject = my_subject or "untitled mood"
    fallback = mock_vote_scores(my_subject, proposals, paint_seed)
    brain = ctx.brain
    if brain.is_mock:
        return fallback

    lines = "\n".join(
        f"- \"{p.get('payload', {}).get('subject', '')}\" from {p.get('from', '?')}"
        for p in proposals
        if p.get("payload", {}).get("subject")
    )
    system = build_system_prompt(ctx.soul_body or "", palette_context(ctx))
    user = (
        f"Phase=CONVERGE final vote after heated debate. Proposals:\n{lines}\n\n"
        f"Your preferred mood is '{my_subject}'. Score each 1-10 for YOUR colour vision "
        f"(be harsh on rivals). Pick ONE title to endorse.\n"
        f"Return ONLY JSON: "
        f"{{\"scores\": {{\"exact title\": number, ...}}, "
        f"\"endorse\": \"exact title string\"}}"
    )
    data = brain.think_json(system, user, max_tokens=OUTPUT_VOTE_JSON)
    if not isinstance(data, dict):
        return fallback
    raw_scores = data.get("scores")
    if not isinstance(raw_scores, dict) or not raw_scores:
        return fallback
    scores = {}
    for k, v in raw_scores.items():
        try:
            scores[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    if not scores:
        return fallback
    endorse = data.get("endorse")
    if endorse not in scores:
        endorse = max(scores, key=lambda s: scores[s])
    return scores, str(endorse)
