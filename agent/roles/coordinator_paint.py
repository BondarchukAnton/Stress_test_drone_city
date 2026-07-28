"""Curated painting: coordinator composes and paints the whole canvas after debate.

PAINT_MODE=curated — painters only negotiate; the coordinator lays every
painter's pigment in one meaningful composition of simple FILLED SHAPES. Shapes
are emitted as ``shape`` events with a ``z`` layer so the SVG dashboard renders a
clean, ordered picture (background → foreground) instead of overlapping spray.

Roster (who paints in which colour) is the per-run persona roster published by
the coordinator at INIT — nothing here is pinned to painter-1..4.
"""
from __future__ import annotations

import os
import time

from context_budget import (
    OUTPUT_CURATED_COMPOSE,
    COMPOSE_PLAN_MAX_CHARS,
    build_system_prompt,
    negotiation_messages,
)
from llm_retry import llm_json
from .paint_shapes import (
    SHAPE_RECIPES,
    SHAPE_VISUAL_GUIDE,
    extract_compose_plan,
    extract_curated_layers,
    compose_by_color,
    _match_scene,
    _render_one,
)
from .painter_palette import palette_context, roster as painter_roster


def _paint_mode(ctx) -> str:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    return (
        cfg.get("paint_mode")
        or os.environ.get("PAINT_MODE", "curated")
        or "curated"
    ).lower()


def is_curated(ctx) -> bool:
    return _paint_mode(ctx) in ("curated", "coordinator", "single")


def _roster_colors(ctx) -> dict[str, str]:
    return {p["id"]: p.get("hex", p.get("color", "#cccccc")) for p in painter_roster(ctx)}


def _negotiation_brief(ctx) -> str:
    """What the painters actually agreed on — bricks, draft, candidates (fixes
    the old bug where the curator read a non-existent PROPOSAL type)."""
    from .collaborative import (
        read_collab, bricks_from_messages, build_notes_from_messages,
    )
    collab = read_collab(ctx.bb)
    bricks = collab.get("bricks") or bricks_from_messages(ctx)
    notes = build_notes_from_messages(ctx)
    lines = []
    if collab.get("draft"):
        lines.append(f"ОБЩИЙ ЧЕРНОВИК: «{collab['draft']}»")
    if bricks:
        lines.append("Кирпичи художников:")
        for b in bricks:
            shp = ", ".join(b.get("shapes") or []) or "—"
            lines.append(f"  • {b.get('name', b.get('from'))} ({b.get('color','?')}): "
                         f"«{b.get('piece','?')}» [{shp}]")
    if notes:
        lines.append("Что добавляли на сборке:")
        for n in notes[-6:]:
            if n.get("addition"):
                lines.append(f"  • {n.get('name')}: {n['addition']}")
    if collab.get("candidates"):
        lines.append("Финальные варианты: " + " · ".join(f"«{c}»" for c in collab["candidates"]))
    return "\n".join(lines) if lines else "(переговоры пусты)"


def _make_validate(painters: list[str]):
    ids = set(painters)
    n = len(painters)

    def _validate(parsed) -> str | None:
        layers = extract_curated_layers(parsed)
        if not layers:
            if not isinstance(parsed, dict):
                return "Ответ не объект JSON"
            if "layers" not in parsed:
                return f"Нет поля layers — нужен массив из {n} слоёв {{painter, color, shapes}}"
            return "layers пуст или без shapes с полем type"
        got = {l["painter_id"] for l in layers}
        missing = ids - got
        if missing:
            return (f"Не хватает слоёв для: {', '.join(sorted(missing))}. "
                    f"Нужен ровно один слой на каждого: {', '.join(sorted(ids))}.")
        for layer in layers:
            if not layer.get("shapes"):
                return f"Слой {layer.get('painter_id')} без фигур shapes"
        return None

    return _validate


def _curated_schema(painters: list[str], colors: dict[str, str]) -> str:
    rows = ",\n".join(
        f'    {{"painter":"{pid}","color":"{colors.get(pid, "#cccccc")}",'
        f'"role":"роль слоя","shapes":[ '
        f'{{"type":"rect","cx":60,"cy":22,"w":118,"h":40,"fill":true}}, '
        f'{{"type":"circle","cx":40,"cy":30,"r":12,"fill":true}} ]}}'
        for pid in painters
    )
    return (
        "Формат ответа куратора:\n"
        "{\n"
        '  "compose_plan": "пошаговый замысел: что рисуем и какими фигурами (2-5 предложений)",\n'
        '  "layers": [\n' + rows + "\n  ]\n}\n"
        f"Ровно {len(painters)} слоёв — по одному на каждого художника, "
        f"painter = его id, color = его пигмент (как указано выше). "
        "У КАЖДОГО художника 2–4 фигуры (крупная масса + деталь), чтобы холст был "
        "заполнен, а не пустой. "
        "Порядок слоёв: фон (небо/вода) → земля → объекты/акценты — большие "
        "залитые массы первыми. Можно добавить полю каждой фигуры \"z\" (целое, "
        "меньше = дальше на фон). Композиция цельная под договорённый сюжет.\n"
        + SHAPE_RECIPES + SHAPE_VISUAL_GUIDE
    )


def plan_curated_composition(ctx, canvas: dict, subject: str, paint_seed: int) -> tuple[list[dict], str]:
    """-> [{painter_id, color, shapes}, ...], compose_plan text"""
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    brain = ctx.brain
    roster = painter_roster(ctx)
    colors = _roster_colors(ctx)
    painters = [p["id"] for p in roster]

    if brain.is_mock:
        layers = compose_by_color(subject, w, h, roster, paint_seed)
        scene = _match_scene(subject)
        plan = (f"Колор-композиция «{subject}» (сцена: {scene}): роли распределены по "
                f"цвету пигмента ({len(painters)} художников).")
        return layers, plan

    palette = palette_context(ctx)
    system = build_system_prompt(
        ctx.soul_body or "",
        palette,
        "Ты — куратор-художник. После дебатов рисуешь ВСЮ картину сразу: "
        "каждый слой — пигмент одного художника, но композиция единая и осмысленная.",
    )
    final_task = (
        f"ИТОГ ПЕРЕГОВОРОВ:\n{_negotiation_brief(ctx)}\n\n"
        f"ДОГОВОРЁННЫЙ СЮЖЕТ: «{subject}»\n"
        f"Холст {w}×{h}. Спланируй цельную картину из простых ЗАЛИТЫХ фигур.\n"
        f"{SHAPE_RECIPES}\n"
        f"{_curated_schema(painters, colors)}\n"
        f"Перед каждой фигурой продумай её место в сюжете. "
        f"Используй fill:true для крупных масс. Ответ — один JSON, без markdown."
    )
    msgs = negotiation_messages(ctx, final_task, phases=("PROPOSE", "BUILD", "CONVERGE"))
    parsed, _raw, _log = llm_json(
        ctx, system, msgs,
        validate=_make_validate(painters),
        max_tokens=OUTPUT_CURATED_COMPOSE,
        timeout=float(os.environ.get("CURATED_COMPOSE_TIMEOUT", "180")),
        context="curated_compose",
    )
    plan = extract_compose_plan(parsed) or f"Единая композиция для «{subject}»."
    layers = extract_curated_layers(parsed) if parsed else None
    if layers:
        # Trust the model's composition as-is — its compose_plan IS the link
        # between the agreed subject and the shapes. (Mixing in procedural shapes
        # here is what previously made the canvas drift from both the plan and
        # the title.) Only re-tint to the canonical roster colour.
        for ly in layers:
            ly["color"] = colors.get(ly["painter_id"], ly.get("color", "#cccccc"))
        return layers, plan
    # The model gave nothing usable → deterministic colour-semantic scene.
    return (compose_by_color(subject, w, h, roster, paint_seed),
            plan + " (колор-композиция: LLM не дал фигур)")


def execute_curated(ctx, canvas: dict, painters: list[str]) -> dict:
    """Paint entire canvas from coordinator; emit shape events; mark progress."""
    dec = ctx.bb.read_decision()
    subject = dec.get("subject") or "без названия"
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    paint_seed = int(cfg.get("paint_seed", 0))
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))

    mine = ctx.progress.get(ctx.agent_id, {})
    if mine.get("status") == "done":
        return {
            "thought": f"Картина «{subject}» уже на холсте ({mine.get('strokes', 0)} фигур).",
            "thinking": mine.get("compose_plan", ""),
            "messages": [], "idle": True,
        }

    layers, compose_plan = plan_curated_composition(ctx, canvas, subject, paint_seed)
    # Surface the curator's reasoning so the dashboard can show HOW the title
    # became these shapes (the title↔picture link).
    ctx.emit({"kind": "compose_plan", "from": ctx.agent_id,
              "subject": subject, "text": compose_plan})
    roster_map = {p["id"]: p for p in painter_roster(ctx)}
    batch = os.environ.get("PAINT_BATCH", "1") not in ("0", "false", "no")
    step_sec = float(os.environ.get("PAINT_STEP_SEC", "0.02" if batch else "0.08"))
    per_painter: dict[str, int] = {p: 0 for p in painters}
    total = 0

    # Холст уже очищен на фазе INIT; повторный clear мигал пустым экраном.
    for li, layer in enumerate(layers):
        pid = layer["painter_id"]
        color = layer["color"]
        for shp in layer.get("shapes", []):
            if not isinstance(shp, dict) or not shp.get("type"):
                continue
            z = int(shp.get("z", shp.get("_z", li)))
            alpha = float(shp.get("alpha", 1.0))
            clean = {k: v for k, v in shp.items() if k not in ("z", "_z", "alpha")}
            want_fill = bool(clean.get("fill"))
            polys = _render_one(clean, w, h)   # outline point paths (reused by SVG)
            ctx.emit({
                "kind": "shape", "from": pid, "by": pid, "color": color,
                "z": z, "alpha": alpha, "phase": "EXECUTE", "shape": clean,
                "polys": polys, "fill": want_fill,
                "outline": bool(clean.get("outline", not want_fill)),
            })
            per_painter[pid] = per_painter.get(pid, 0) + 1
            total += 1
            if not batch:
                time.sleep(step_sec)

    ctx.bb.write_progress(ctx.agent_id, {
        "status": "done", "subject": subject, "strokes": total,
        "compose_plan": compose_plan[:COMPOSE_PLAN_MAX_CHARS], "paint_mode": "curated",
    })
    for pid in painters:
        entry = roster_map.get(pid, {})
        ctx.bb.write_progress(pid, {
            "status": "done", "subject": subject,
            "color": entry.get("hex", entry.get("color", "")),
            "technique": entry.get("technique", ""),
            "strokes": per_painter.get(pid, 0),
            "compose_plan": f"Слой куратора ({entry.get('name', pid)}, {entry.get('hex', '')})",
        })

    thought = (
        f"Нарисовал «{subject}» целиком: {total} фигур, "
        f"{len(layers)} слоёв (режим куратора)."
    )
    return {"thought": thought, "thinking": compose_plan, "messages": [], "idle": False}
