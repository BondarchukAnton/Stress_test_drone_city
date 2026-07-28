"""Collab painting: ONE unified canvas, but each drone authors its OWN colour.

The middle path between:
  * curated     — one LLM (the coordinator) paints ALL colours (coherent, but the
                  drones are only idea-generators, no authorship of pixels);
  * distributed — each drone sprays freely → incoherent overlapping "войлок"
                  (see docs/audit-report.md).

collab keeps ONE coherent scene (NOT four quadrants): a shared plan assigns each
drone a semantic ROLE by pigment hue (blue→water/sky, amber→sun, green→foliage…)
that spans the WHOLE canvas; each drone renders ONLY its colour as z-layered
FILLED shapes, aware of the agreed subject and what the other colours cover; the
viz composites every `shape` event by global z into one finished artwork.

The coordinator here is a THIN facilitator — it publishes the role plan, it does
NOT draw. Each drone owns its colour end-to-end (real brain: composes its own
shapes; mock brain: renders its deterministic colour-role layer).
"""
from __future__ import annotations

import hashlib
import os
import random

from context_budget import (
    OUTPUT_STROKE_COMPOSE,
    build_system_prompt,
    negotiation_messages,
)
from . import make_msg
from .paint_shapes import (
    ROLE_HUE,
    SHAPE_RECIPES,
    _hex_hue,
    _hue_dist,
    _render_one,
    compose_by_color,
    extract_compose_plan,
    extract_shape_plan,
)
from .painter_palette import my_entry, palette_context, roster as painter_roster


def _paint_mode(ctx) -> str:
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    return (cfg.get("paint_mode") or os.environ.get("PAINT_MODE", "curated") or "curated").lower()


def is_collab(ctx) -> bool:
    return _paint_mode(ctx) == "collab"


def _role_for_color(hexstr: str) -> str:
    hue = _hex_hue(hexstr)
    return min(ROLE_HUE, key=lambda k: _hue_dist(hue, ROLE_HUE[k]))


def _shape_bbox(s: dict, w: int, h: int) -> tuple:
    t = str(s.get("type", "")).lower()
    if t == "line" and s.get("from") and s.get("to"):
        xs = [float(s["from"][0]), float(s["to"][0])]
        ys = [float(s["from"][1]), float(s["to"][1])]
        return min(xs), min(ys), max(xs), max(ys)
    cx = float(s.get("cx", w / 2))
    cy = float(s.get("cy", h / 2))
    ex = float(s.get("r") or s.get("rx") or s.get("w") or s.get("size") or s.get("outer") or 12)
    ey = float(s.get("ry") or s.get("h") or ex)
    return cx - ex, cy - ey, cx + ex, cy + ey


def _region_of(shapes: list, w: int, h: int) -> list:
    """Bounding box a role occupies in the coherent scene skeleton (+ padding).
    Each drone is kept inside its own region so colours don't scatter."""
    if not shapes:
        return [0, 0, w, h]
    boxes = [_shape_bbox(s, w, h) for s in shapes]
    pad = max(6.0, w * 0.06)
    return [max(0.0, min(b[0] for b in boxes) - pad),
            max(0.0, min(b[1] for b in boxes) - pad),
            min(float(w), max(b[2] for b in boxes) + pad),
            min(float(h), max(b[3] for b in boxes) + pad)]


def _fnum(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp_to_region(s: dict, region: list, maxd: float | None = None) -> dict:
    """Keep a drone's shape inside its region (centre + size caps). `maxd` also caps
    each dimension to a fraction of the canvas so no single mass blankets the whole
    picture (important in COLLAB_FREE where the region IS the whole canvas).
    All numeric reads are fault-tolerant — LLM shapes carry strings/nulls."""
    x0, y0, x1, y1 = region
    rw, ry = max(1.0, x1 - x0), max(1.0, y1 - y0)
    out = dict(s)
    if "cx" in out:
        out["cx"] = round(max(x0, min(x1, _fnum(out.get("cx"), (x0 + x1) / 2))), 1)
    if "cy" in out:
        out["cy"] = round(max(y0, min(y1, _fnum(out.get("cy"), (y0 + y1) / 2))), 1)
    # radius-like keys span 2× their value — cap them at HALF the allowed extent,
    # else r=maxd gives a diameter of the whole canvas fraction twice over
    for k, cap in (("r", min(rw, ry) / 2), ("rx", rw / 2), ("ry", ry / 2),
                   ("outer", min(rw, ry) / 2)):
        if k in out:
            lim = min(cap, maxd / 2) if maxd else cap
            out[k] = round(min(_fnum(out[k], lim), lim), 1)
    for k, cap in (("w", rw), ("h", ry), ("size", min(rw, ry))):
        if k in out:
            lim = min(cap, maxd) if maxd else cap
            out[k] = round(min(_fnum(out[k], lim), lim), 1)
    # LLMs also emit SVG-style x1/y1/x2/y2 lines — normalize to from/to FIRST or
    # they bypass every endpoint clamp and the length cap (a full-canvas
    # x1:0→x2:120 slash made it into the 2026-07-02 verification canvas)
    if str(out.get("type", "")).lower() == "line" and any(
            k in out for k in ("x1", "y1", "x2", "y2")):
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        out["from"] = [_fnum(out.pop("x1", mx), mx), _fnum(out.pop("y1", my), my)]
        out["to"] = [_fnum(out.pop("x2", mx), mx), _fnum(out.pop("y2", my), my)]
    for key in ("from", "to"):
        p = out.get(key)
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            out[key] = [round(max(x0, min(x1, _fnum(p[0], (x0 + x1) / 2))), 1),
                        round(max(y0, min(y1, _fnum(p[1], (y0 + y1) / 2))), 1)]
        elif key in out:
            del out[key]  # malformed endpoint → let the renderer use its safe default
    # a `line` clamped only by endpoints can still span the whole region/canvas —
    # cap its LENGTH too (shrink symmetrically around the midpoint)
    if (str(out.get("type", "")).lower() == "line"
            and isinstance(out.get("from"), list) and isinstance(out.get("to"), list)):
        fx, fy = out["from"]
        tx, ty = out["to"]
        length = ((tx - fx) ** 2 + (ty - fy) ** 2) ** 0.5
        lim = maxd if maxd else max(rw, ry)
        if length > lim > 0:
            k = lim / length
            mx, my = (fx + tx) / 2, (fy + ty) / 2
            out["from"] = [round(mx + (fx - mx) * k, 1), round(my + (fy - my) * k, 1)]
            out["to"] = [round(mx + (tx - mx) * k, 1), round(my + (ty - my) * k, 1)]
    if isinstance(out.get("points"), list):
        out["points"] = [[round(max(x0, min(x1, _fnum(p[0]))), 1),
                          round(max(y0, min(y1, _fnum(p[1]))), 1)]
                         for p in out["points"] if isinstance(p, (list, tuple)) and len(p) >= 2]
    return out


def _seed(agent_id: str, base: int = 0) -> int:
    return base + int(hashlib.sha256(agent_id.encode()).hexdigest()[:6], 16)


def _enrich_layer(shapes: list, region: list, seed: int, target: int) -> list:
    """Pad a colour layer up to `target` shapes with VARIED types placed in-region
    (so even the deterministic/fallback path isn't one lonely ellipse per drone)."""
    out = list(shapes)
    if len(out) >= target:
        return out
    x0, y0, x1, y1 = region
    rw, rh = max(4.0, x1 - x0), max(4.0, y1 - y0)
    base_z = min((int(s.get("_z", 0)) for s in out), default=0)
    rng = random.Random(seed)
    pool = ["circle", "triangle", "star", "wave", "arc", "ellipse", "ring", "cross"]
    while len(out) < target:
        t = pool[rng.randrange(len(pool))]
        cx = rng.uniform(x0 + rw * 0.15, x1 - rw * 0.15)
        cy = rng.uniform(y0 + rh * 0.15, y1 - rh * 0.15)
        d = min(rw, rh) * rng.uniform(0.14, 0.3)
        s = {"type": t, "cx": round(cx, 1), "cy": round(cy, 1),
             "fill": True, "outline": False, "_z": base_z + 1 + len(out)}
        if t in ("circle", "ring"):
            s["r"] = round(d, 1)
            if t == "ring":
                s["thickness"] = round(max(1.0, d * 0.3), 1)
        elif t == "ellipse":
            s["rx"] = round(d, 1); s["ry"] = round(d * 0.62, 1)
        elif t == "triangle":
            s["size"] = round(d * 1.4, 1); s["rotation"] = round(rng.uniform(0, 3.14), 2)
        elif t == "star":
            s["outer"] = round(d, 1); s["inner"] = round(d * 0.45, 1); s["points"] = rng.randint(5, 7)
        elif t == "wave":
            s["w"] = round(rw * 0.55, 1); s["h"] = round(d * 0.5, 1)
        elif t == "arc":
            s["r"] = round(d, 1); s["start"] = rng.uniform(0, 160); s["end"] = s["start"] + rng.uniform(60, 180)
        elif t == "cross":
            s["size"] = round(d, 1)
        out.append(s)
    return out


def _build_plan(ctx, canvas: dict, subject: str, seed: int):
    """Deterministic coherent scene → per-painter colour layer + role labels.
    Returns (by_pid: {pid: layer}, roles: [(color, role), ...])."""
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    roster = painter_roster(ctx)
    layers = compose_by_color(subject, w, h, roster, seed)  # [{painter_id,color,shapes(_z)}]
    by_pid = {ly["painter_id"]: ly for ly in layers}
    roles = []
    for p in roster:
        col = p.get("hex", p.get("color", "#888888"))
        roles.append((col, _role_for_color(col)))
    return by_pid, roles


def assign_collab(ctx, canvas: dict, painters: list[str]) -> list[dict]:
    """Thin facilitator: publish each drone's colour ROLE for the agreed subject.
    Writes assignments; each drone then renders its own layer."""
    dec = ctx.bb.read_decision()
    subject = dec.get("subject") or "без названия"
    cfg = ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}
    seed = int(cfg.get("paint_seed", 0))
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    # COLLAB_FREE=1 → maximum expressive freedom: the WHOLE canvas is each drone's
    # zone (they overlap/interleave anywhere), coordination comes only from the
    # agreed subject + role hints, not hard regions.
    free = os.environ.get("COLLAB_FREE", "0") not in ("0", "", "false", "no")
    by_pid, roles = _build_plan(ctx, canvas, subject, seed)

    amap: dict = {}
    vizmap: dict = {}
    msgs: list[dict] = []
    for pid in painters:
        ly = by_pid.get(pid, {"color": "#cccccc", "shapes": []})
        col = ly.get("color", "#cccccc")
        skeleton = ly.get("shapes", [])
        zs = [int(s.get("_z", 0)) for s in skeleton]
        z = min(zs) if zs else 0
        # role = the scene group this drone actually owns (unique), fallback to hue
        role = ly.get("role") or _role_for_color(col)
        region = [0.0, 0.0, float(w), float(h)] if free else _region_of(skeleton, w, h)
        amap[pid] = {"collab": True, "subject": subject, "canvas": canvas,
                     "paint_seed": seed, "color": col, "z": z, "role": role,
                     "roles": roles, "region": region, "layer_shapes": skeleton}
        vizmap[pid] = z
        msgs.append(make_msg(
            ctx, "ASSIGNMENT", pid, "EXECUTE",
            body=(f"Твой цвет {col} — роль «{role}» в общей картине «{subject}». "
                  f"Нарисуй свой слой на своих местах во всей сцене."),
            payload={"role": role, "color": col, "subject": subject}))
    ctx.bb.write_assignments(amap)
    ctx.emit({"kind": "assignment", "map": vizmap})
    return msgs


def _compose_drone_shapes(ctx, subject: str, color: str, role: str,
                          roles: list, canvas: dict, region: list,
                          target: int = 4) -> tuple[list[dict], str]:
    """Real brain: the drone composes ITS OWN colour's shapes, aware of the whole
    scene (subject + what each other colour covers) but CONSTRAINED to its region
    so the composite stays coherent (one scene, not scattered). Clamps every shape
    into the region; falls back to [] on failure."""
    from llm_retry import llm_json
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    x0, y0, x1, y1 = [int(v) for v in region]
    cast = "; ".join(f"{c}→{r}" for c, r in roles) or "—"
    system = build_system_prompt(
        ctx.soul_body or "", palette_context(ctx),
        "Ты — художник ОДНОГО пигмента в общей картине. Рисуешь ТОЛЬКО свой цвет, "
        "строго в СВОЕЙ зоне холста — не рисуй чужие цвета и не залезай в чужие зоны.")
    task = (
        f"ОБЩИЙ СЮЖЕТ: «{subject}». Холст {w}×{h} (центр {w // 2},{h // 2}).\n"
        f"Роли по цвету: {cast}.\n"
        f"ТВОЙ ЦВЕТ: {color}. ТВОЯ РОЛЬ: «{role}».\n"
        f"ТВОЯ ЗОНА: x от {x0} до {x1}, y от {y0} до {y1}. "
        f"Все твои фигуры ДОЛЖНЫ лежать в этой зоне (центры cx,cy внутри).\n"
        f"Нарисуй {target}–{target + 1} ЗАЛИТЫХ фигур ТОЛЬКО своим цветом, РАЗНЫХ типов "
        "(не только овалы: circle, triangle, star, wave, arc, line, ring, cross) — "
        "крупная масса под роль + детали/акценты, чтобы слой был живым. "
        "Каждой фигуре добавь целое поле \"z\" (меньше = дальше в фон).\n"
        f"{SHAPE_RECIPES}\n"
        'Верни JSON: {"compose_plan":"1-2 предложения","shapes":['
        '{"type":"ellipse","cx":60,"cy":80,"rx":50,"ry":16,"fill":true,"z":1},'
        '{"type":"triangle","cx":50,"cy":70,"size":18,"fill":true,"z":2},'
        '{"type":"star","cx":72,"cy":66,"outer":8,"fill":true,"z":3}]}')
    msgs = negotiation_messages(ctx, task, phases=("CHAT", "PROPOSE", "BUILD", "CONVERGE"))
    # NO json_schema here, deliberately: grammar-guided decoding on this exact
    # deploy stalls LARGE outputs (measured 2026-07-02: 3/4 schema-guided compose
    # calls hit the 120s timeout, their plain retries answered in 27-39s — the
    # vllm#34650 MTP+grammar trap). Small calls (chat turns, votes) keep schemas;
    # this one relies on no-think + regex extraction, which has been 100% here.
    parsed, _raw, _log = llm_json(
        ctx, system, msgs,
        max_tokens=OUTPUT_STROKE_COMPOSE,
        timeout=float(os.environ.get("COLLAB_COMPOSE_TIMEOUT", "120")),
        context="collab_compose")
    raw_shapes = extract_shape_plan(parsed) or []
    # cap each mass to a fraction of the canvas so no single shape blankets everything
    maxd = w * float(os.environ.get("COLLAB_MAX_FRAC", "0.5"))
    shapes = [_clamp_to_region(s, region, maxd) for s in raw_shapes]
    plan = extract_compose_plan(parsed) or f"Мой слой {color} («{role}») для «{subject}»."
    return shapes, plan


def paint_collab(ctx) -> dict:
    """Drone renders its own colour layer as z-shapes → composited by the viz."""
    a = ctx.assignments.get(ctx.agent_id)
    if not isinstance(a, dict) or not a.get("collab"):
        return {"thought": "Жду распределения ролей.", "thinking": "",
                "messages": [], "idle": True}
    mine = ctx.progress.get(ctx.agent_id, {})
    subject = a.get("subject", "без названия")
    color = a.get("color") or my_entry(ctx).get("hex", "#cccccc")
    role = a.get("role", "")
    if mine.get("status") == "done":
        return {"thought": f"Мой слой ({color}, «{role}») для «{subject}» готов.",
                "thinking": mine.get("compose_plan", ""), "messages": [], "idle": True}

    canvas = a.get("canvas", {"w": 120, "h": 120})
    w = int(canvas.get("w", 120))
    h = int(canvas.get("h", 120))
    region = a.get("region") or [0, 0, w, h]
    seed = _seed(ctx.agent_id, int(a.get("paint_seed", 0)))
    target = max(1, int(os.environ.get("COLLAB_SHAPES", "4")))  # shapes per drone
    # Each drone free-composes its OWN 3–5 varied shapes (its creativity),
    # constrained to its region so the composite stays coherent. Set
    # COLLAB_LLM_SHAPES=0 to fall back to the deterministic scene skeleton.
    use_llm = (not ctx.brain.is_mock
               and os.environ.get("COLLAB_LLM_SHAPES", "1") not in ("0", "", "false", "no"))
    if not use_llm:
        shapes = _enrich_layer(a.get("layer_shapes", []), region, seed, target)
        plan = f"Мой слой ({color}, роль «{role}», {len(shapes)} фигур) в «{subject}»."
    else:
        shapes, plan = _compose_drone_shapes(
            ctx, subject, color, role, a.get("roles", []), canvas, region, target)
        if len(shapes) < target:  # top up thin LLM output with varied in-region shapes
            shapes = _enrich_layer(shapes or a.get("layer_shapes", []), region, seed, target)

    # The facilitator's z-slot NAMESPACES this drone's layer: every drone's LLM
    # emits local z of 1,2,3…, so raw values interleave layers at random across
    # drones (the global-z race). slot*1000 + local keeps each drone's internal
    # order while the slot decides who sits behind whom.
    slot = int(_fnum(a.get("z", 0)))
    events: list[dict] = []
    for shp in shapes:
        if not isinstance(shp, dict) or not shp.get("type"):
            continue
        local_z = int(_fnum(shp.get("z", shp.get("_z", 0))))
        local_z = max(0, min(999, local_z))
        alpha = _fnum(shp.get("alpha", 1.0), 1.0)
        alpha = max(0.05, min(1.0, alpha))
        clean = {k: v for k, v in shp.items() if k not in ("z", "_z", "alpha")}
        want_fill = bool(clean.get("fill"))
        polys = _render_one(clean, w, h)
        if not polys:
            continue
        events.append({"kind": "shape", "from": ctx.agent_id, "by": ctx.agent_id,
                       "color": color, "z": slot * 1000 + local_z, "alpha": alpha,
                       "phase": "EXECUTE", "shape": clean, "polys": polys,
                       "fill": want_fill,
                       "outline": bool(clean.get("outline", not want_fill))})
    # emit only after the whole layer rendered cleanly — an exception mid-loop
    # used to leave half a layer on the canvas and re-emit it all next cycle
    painted = 0
    for ev in events:
        ctx.emit(ev)
        painted += 1

    name = my_entry(ctx).get("name", ctx.agent_id)
    ctx.bb.write_progress(ctx.agent_id, {
        "status": "done", "subject": subject, "color": color,
        "technique": my_entry(ctx).get("technique", ""), "strokes": painted,
        "compose_plan": plan[:500], "paint_mode": "collab", "role": role})
    report = make_msg(
        ctx, "REPORT", "coordinator", "REPORT",
        body=f"{name}: {painted} фигур {color} («{role}») для «{subject}».",
        payload={"subject": subject, "color": color, "role": role, "strokes": painted})
    return {"thought": f"Нарисовал свой слой ({color}, «{role}») — {painted} фигур в «{subject}».",
            "thinking": plan, "messages": [report], "idle": False}
