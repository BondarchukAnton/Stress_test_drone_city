"""Shape plan DSL: LLM returns JSON shapes → rasterized to spray polylines.

The bridge only understands move + spray(polylines). Agents think in shapes;
this module turns {type, cx, cy, scale, rotation, ...} into point paths.
"""
from __future__ import annotations

import colorsys
import math
import random
from typing import Any

# Catalog shown to the model (keep in sync with _RENDERERS)
SHAPE_TYPES = (
    "line", "polyline", "rect", "square", "circle", "ellipse",
    "triangle", "arc", "star", "ring", "cross", "wave",
)

SHAPE_SCHEMA_DOC = """
Верни JSON: {"compose_plan":"замысел 1-3 предложения на русском","shapes":[...]}
ИЛИ {"shapes":[...]} если замысел уже ясен.

Координаты холста: X от 0 до W, Y от 0 до H (единицы ≈ пиксели дашборда).
Центр холста: (W/2, H/2). Толщина линии напыления ≈ 2 ед.

Каждая фигура — объект с полем type (англ. ключи):
""" + ", ".join(SHAPE_TYPES) + """

Поля размеров (в единицах холста):
  line: from:[x,y], to:[x,y] — отрезок; типичная длина 15–70
  polyline: points:[[x,y],...] — ломаная, 3–8 точек
  rect/square: size или w,h — сторона 8–40; cx,cy — центр прямоугольника
  circle: r — радиус 4–28; cx,cy — центр
  ellipse: rx, ry — полуоси 6–25
  triangle: size — сторона 10–30; rotation — поворот в радианах
  arc: r 8–25; start,end — углы в градусах (дуга молнии/улыбки)
  star: outer 10–25, inner ≈ 0.4×outer, points 5–8
  ring: r, thickness — кольцо
  cross: size — плечо креста 5–18
  wave: w 30–90, h 4–16 — синусоидальная полоса
  rotation: радианы вокруг cx,cy
  fill: true — ЗАКРАСИТЬ фигуру изнутри (не только контур); для rect/circle/ellipse/triangle/star
  outline: false — без контура, только заливка (по умолчанию true если fill)

Пример с заливкой: {"compose_plan":"Синее море — залитый овал","shapes":[
  {"type":"ellipse","cx":60,"cy":75,"rx":50,"ry":22,"fill":true,"outline":false}]}
"""

SHAPE_VISUAL_GUIDE = """
КАК ВЫГЛЯДЯТ ФИГУРЫ ПОСЛЕ НАПЫЛЕНИЯ:
  line — прямой или почти прямой штрих между двумя точками
  polyline — изломанная линия (зигзаг, контур)
  rect/square — прямоугольный контур (закрытый)
  circle — круг; fill:true = плотная заливка диска
  ellipse — овал; fill:true = залитый эллипс (море, облако)
  triangle — треугольник; fill:true = залитый клин
  arc — дуга (часть окружности), хорошо для «молнии», «улыбки», «горизонта»
  star — звезда с лучами
  ring — два концентрических круга (ободок)
  cross — крест из двух пересекающихся линий
  wave — волнистая горизонтальная полоса (рябь, мягкое небо)

МАСШТАБ: холст 120×120 — фигура r=20 занимает ~⅓ ширины. Не делай всё в одной точке.
Распредели фигуры осмысленно: небо сверху, вода снизу, энергия по диагонали и т.д.
Используй fill:true для крупных масс (небо, море, земля), line/arc для акцентов.

Перед каждой фигурой в compose_plan кратко объясни ЗАЧЕМ она (позиция, размер, роль в сюжете).
"""

SHAPE_RECIPES = """
ПРОСТЫЕ СЮЖЕТЫ — только из доступных фигур (предлагай такие названия, не абстрактную поэзию):
  дерево / лес: 1–4 залитых triangle (крона) + узкий rect (ствол); земля — rect внизу
  дом: rect (стены) + triangle сверху (крыша) по центру
  солнце / закат: circle fill в верхней трети + горизонтальный rect (небо)
  дорога / шоссе: две line сходятся к центру горизонта + rect земля
  озеро / море: ellipse fill в нижней половине + rect или ellipse небо сверху
  горы: 2–4 triangle разной ширины в ряд на линии горизонта
  туман: полупрозрачная полоса — ellipse или wave посередине + силуэты triangle (ели)

Думай над КАЖДОЙ фигурой: тип, cx/cy, размер — чтобы вместе читался сюжет.
"""

# (Curated layer schema is built dynamically per active roster in
# coordinator_paint._curated_schema — no fixed 4-painter template here.)


def _match_scene(subject: str) -> str:
    s = subject.lower()
    rules = (
        ("fog", ("туман", "дымк", "игл", "сосн")),
        ("forest", ("дерев", "лес", "елк", "папорот", "ели", "крон")),
        ("house", ("дом", "изб", "хижин", "крыш")),
        ("road", ("шоссе", "дорог")),
        ("lake", ("озер", "мор", "океан", "волн")),
        ("sunset", ("закат", "солн", "рассвет", "золот")),
        ("mountains", ("гор", "вершин")),
    )
    for name, keys in rules:
        if any(k in s for k in keys):
            return name
    return "generic"


def _sky_band(w: float, h: float) -> dict:
    return {"type": "rect", "cx": w / 2, "cy": h * 0.18, "w": w * 0.98, "h": h * 0.32,
            "fill": True, "outline": False}


def _ground_band(w: float, h: float) -> dict:
    return {"type": "rect", "cx": w / 2, "cy": h * 0.9, "w": w * 0.98, "h": h * 0.18,
            "fill": True, "outline": False}


# Scene builders return ROLE-GROUPS: ordered [{"role","z","shapes"}], background
# first. They carry NO colour/painter id — `compose_by_color` assigns each group
# to the painter whose pigment hue best fits the role (blue→water/sky,
# green→foliage, amber→sun, …), so the picture reads correctly by colour.

def _g(role: str, z: int, *shapes: dict) -> dict:
    return {"role": role, "z": z, "shapes": list(shapes)}


def _scene_forest(w: int, h: int, rng: random.Random) -> list[dict]:
    trees: list[dict] = []
    for x in (w * 0.22, w * 0.42, w * 0.58, w * 0.78):
        sz = 12 + rng.randint(0, 10)
        trees.append({"type": "triangle", "cx": x, "cy": h * 0.52, "size": sz,
                      "fill": True, "outline": False})
        trees.append({"type": "rect", "cx": x, "cy": h * 0.58, "w": 5, "h": sz * 0.55,
                      "fill": True, "outline": False})
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": w * 0.75, "cy": h * 0.14, "r": 10,
                      "fill": True, "outline": False}),
        _g("ground", 2, _ground_band(w, h)),
        _g("foliage", 3, *trees),
    ]


def _scene_house(w: int, h: int, rng: random.Random) -> list[dict]:
    hx, hy = w * 0.5, h * 0.55
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": w * 0.25, "cy": h * 0.12, "r": 12,
                      "fill": True, "outline": False}),
        _g("ground", 2, _ground_band(w, h)),
        _g("structure", 3,
           {"type": "rect", "cx": hx, "cy": hy + 8, "w": 28, "h": 22, "fill": True, "outline": False},
           {"type": "triangle", "cx": hx, "cy": hy - 10, "size": 34, "fill": True, "outline": False}),
    ]


def _scene_road(w: int, h: int, rng: random.Random) -> list[dict]:
    vanish = (w / 2, h * 0.42)
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": w * 0.5, "cy": h * 0.38, "r": 14,
                      "fill": True, "outline": False}),
        _g("ground", 2, _ground_band(w, h)),
        _g("accent", 3,
           {"type": "line", "from": [w * 0.08, h * 0.95], "to": list(vanish)},
           {"type": "line", "from": [w * 0.92, h * 0.95], "to": list(vanish)}),
    ]


def _scene_lake(w: int, h: int, rng: random.Random) -> list[dict]:
    cx = w / 2
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": w * 0.7, "cy": h * 0.12, "r": 10,
                      "fill": True, "outline": False}),
        _g("ground", 1, _ground_band(w, h)),
        _g("water", 2,
           {"type": "ellipse", "cx": cx, "cy": h * 0.7, "rx": w * 0.48, "ry": h * 0.2,
            "fill": True, "outline": False},
           {"type": "wave", "cx": cx, "cy": h * 0.54, "w": w * 0.9, "h": 7}),
    ]


def _scene_sunset(w: int, h: int, rng: random.Random) -> list[dict]:
    return [
        _g("sky", 0, {"type": "rect", "cx": w / 2, "cy": h * 0.25, "w": w * 0.98, "h": h * 0.45,
                      "fill": True, "outline": False}),
        _g("sun", 1, {"type": "circle", "cx": w / 2, "cy": h * 0.42, "r": 17,
                      "fill": True, "outline": False}),
        _g("water", 2, {"type": "ellipse", "cx": w / 2, "cy": h * 0.62, "rx": w * 0.45, "ry": h * 0.16,
                        "fill": True, "outline": False}),
        _g("ground", 3, _ground_band(w, h)),
    ]


def _scene_mountains(w: int, h: int, rng: random.Random) -> list[dict]:
    peaks = []
    for x, sz in ((w * 0.25, 22), (w * 0.5, 30), (w * 0.75, 18)):
        peaks.append({"type": "triangle", "cx": x, "cy": h * 0.5, "size": sz + rng.randint(-3, 3),
                      "fill": True, "outline": False})
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": w * 0.8, "cy": h * 0.16, "r": 9,
                      "fill": True, "outline": False}),
        _g("mountain", 2, *peaks),
        _g("ground", 3, _ground_band(w, h)),
    ]


def _scene_fog(w: int, h: int, rng: random.Random) -> list[dict]:
    needles = [{"type": "triangle", "cx": x, "cy": h * 0.5, "size": 10 + rng.randint(0, 6),
                "fill": True, "outline": False, "rotation": 3.14}
               for x in (w * 0.15, w * 0.35, w * 0.55, w * 0.75, w * 0.9)]
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("water", 1,
           {"type": "ellipse", "cx": w / 2, "cy": h * 0.45, "rx": w * 0.5, "ry": h * 0.14,
            "fill": True, "outline": False},
           {"type": "wave", "cx": w / 2, "cy": h * 0.38, "w": w * 0.85, "h": 12}),
        _g("ground", 2, _ground_band(w, h)),
        _g("foliage", 3, *needles),
    ]


def _scene_generic(w: int, h: int, rng: random.Random) -> list[dict]:
    """Pleasant default landscape (never ugly): sky, sun, water, ground."""
    cx = w / 2
    off = rng.randint(-6, 6)
    return [
        _g("sky", 0, _sky_band(w, h)),
        _g("sun", 1, {"type": "circle", "cx": cx + off, "cy": h * 0.2, "r": 13,
                      "fill": True, "outline": False}),
        _g("water", 2, {"type": "ellipse", "cx": cx, "cy": h * 0.7, "rx": w * 0.47, "ry": h * 0.2,
                        "fill": True, "outline": False}),
        _g("ground", 3, _ground_band(w, h)),
    ]


_SCENE_BUILDERS = {
    "forest": _scene_forest,
    "house": _scene_house,
    "road": _scene_road,
    "lake": _scene_lake,
    "sunset": _scene_sunset,
    "mountains": _scene_mountains,
    "fog": _scene_fog,
    "generic": _scene_generic,
}

# Ideal hue (deg) per scene role — painters are matched to roles by colour.
ROLE_HUE = {
    "sky": 210, "sun": 48, "water": 205, "ground": 30,
    "foliage": 130, "accent": 0, "mountain": 250, "structure": 22,
}


def _stable_offset(subject: str) -> int:
    import hashlib
    return int(hashlib.sha256(subject.encode()).hexdigest()[:6], 16) % 10007


def _hex_hue(hexstr: str) -> float:
    h = (hexstr or "").lstrip("#")
    if len(h) < 6:
        return 0.0
    try:
        r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    except ValueError:
        return 0.0
    hh, _, _ = colorsys.rgb_to_hls(r, g, b)
    return hh * 360.0


def _hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def compose_by_color(subject: str, w: int, h: int, roster: list[dict], seed: int) -> list[dict]:
    """Colour-aware composition: assign each scene role to the painter whose
    pigment hue fits best (blue→water/sky, green→foliage, amber→sun, …) so the
    canvas reads as a coherent simple picture. -> [{painter_id, color, shapes}]."""
    rng = random.Random(seed + _stable_offset(subject))
    groups = _SCENE_BUILDERS.get(_match_scene(subject), _scene_generic)(w, h, rng)
    personas = list(roster) or [{"id": "painter-1", "hex": "#cccccc"}]
    layers = {p["id"]: {"painter_id": p["id"], "role": "",
                        "color": p.get("hex", p.get("color", "#cccccc")), "shapes": []}
              for p in personas}
    used: set[str] = set()
    for grp in groups:
        ideal = ROLE_HUE.get(grp.get("role"), 180)
        free = [p for p in personas if p["id"] not in used] or personas
        best = min(free, key=lambda p: _hue_dist(
            _hex_hue(p.get("hex", p.get("color", "#888"))), ideal))
        used.add(best["id"])
        # each drone's role = the scene group it actually owns (unique per drone),
        # not an independent hue guess — avoids two drones labelled the same role.
        if not layers[best["id"]]["role"]:
            layers[best["id"]]["role"] = grp.get("role", "")
        for s in grp["shapes"]:
            shp = dict(s)
            shp["_z"] = grp.get("z", 0)
            layers[best["id"]]["shapes"].append(shp)
    # leftover painters → a small accent so the whole cast contributes
    top_z = max((g.get("z", 0) for g in groups), default=0) + 1
    idle = [p for p in personas if not layers[p["id"]]["shapes"]]
    for i, p in enumerate(idle):
        layers[p["id"]].setdefault("role", "accent")
        if not layers[p["id"]]["role"]:
            layers[p["id"]]["role"] = "accent"
        layers[p["id"]]["shapes"].append({
            "type": "star", "cx": w * (0.15 + 0.7 * ((i + 1) / (len(idle) + 1))),
            "cy": h * (0.22 + 0.4 * rng.random()), "outer": 5 + rng.randint(0, 5),
            "points": 5, "fill": True, "outline": False, "_z": top_z,
        })
    return [layers[p["id"]] for p in personas]


def _scanline_fill(vertices: list, spacing: float, w: int, h: int) -> list:
    """Горизонтальная заливка замкнутого многоугольника → горизонтальные отрезки."""
    if len(vertices) < 3:
        return []
    spacing = max(1.0, spacing)
    ys = [p[1] for p in vertices]
    y_min = max(0.0, min(ys))
    y_max = min(float(h), max(ys))
    n = len(vertices)
    out: list = []
    y = y_min
    while y <= y_max:
        xs: list[float] = []
        for i in range(n):
            x0, y0 = vertices[i][0], vertices[i][1]
            x1, y1 = vertices[(i + 1) % n][0], vertices[(i + 1) % n][1]
            if abs(y1 - y0) < 1e-9:
                continue
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0)
                xs.append(x0 + t * (x1 - x0))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            if i + 1 < len(xs) and xs[i + 1] - xs[i] > 0.4:
                out.append([
                    _clamp_pt(xs[i], y, w, h),
                    _clamp_pt(xs[i + 1], y, w, h),
                ])
        y += spacing
    return out


def _poly_closed_coords(poly: list) -> list | None:
    if len(poly) < 3:
        return None
    verts = [list(p) for p in poly]
    if verts[0] != verts[-1]:
        verts.append(verts[0][:])
    return verts[:-1]


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp_pt(x: float, y: float, w: int, h: int) -> list[float]:
    return [round(max(0.0, min(w, x)), 1), round(max(0.0, min(h, y)), 1)]


def _rotate(x: float, y: float, cx: float, cy: float, ang: float) -> tuple[float, float]:
    if not ang:
        return x, y
    s, c = math.sin(ang), math.cos(ang)
    dx, dy = x - cx, y - cy
    return cx + dx * c - dy * s, cy + dx * s + dy * c


def _poly_from_points(pts: list, w: int, h: int, closed: bool = False) -> list:
    if len(pts) < 2:
        return []
    out = [_clamp_pt(p[0], p[1], w, h) for p in pts]
    if closed and out[0] != out[-1]:
        out.append(out[0][:])
    return out


def _circle_pts(cx, cy, r, w, h, n=24) -> list:
    pts = []
    for i in range(n):
        a = i / n * 2 * math.pi
        pts.append(_clamp_pt(cx + r * math.cos(a), cy + r * math.sin(a), w, h))
    pts.append(pts[0][:])
    return pts


def _render_one(shape: dict, w: int, h: int) -> list[list[list[float]]]:
    """One shape → list of open/closed polylines (each ≥2 points)."""
    t = str(shape.get("type", "")).lower()
    cx = _num(shape.get("cx"), w / 2)
    cy = _num(shape.get("cy"), h / 2)
    rot = _num(shape.get("rotation"))

    polylines: list[list[list[float]]] = []

    if t == "line":
        # missing endpoints default to a modest stroke around the centre — the
        # old (0,0)->(w,h) default painted a full-canvas diagonal on any partial
        # LLM shape (visible as stray slashes in the 2026-07-02 audit canvas)
        fr = shape.get("from") if isinstance(shape.get("from"), (list, tuple)) and len(shape.get("from") or []) >= 2 else None
        to = shape.get("to") if isinstance(shape.get("to"), (list, tuple)) and len(shape.get("to") or []) >= 2 else None
        span = w * 0.125
        x1 = _num(shape.get("x1"), _num(fr[0], cx - span) if fr else cx - span)
        y1 = _num(shape.get("y1"), _num(fr[1], cy) if fr else cy)
        x2 = _num(shape.get("x2"), _num(to[0], cx + span) if to else cx + span)
        y2 = _num(shape.get("y2"), _num(to[1], cy) if to else cy)
        polylines.append([_clamp_pt(x1, y1, w, h), _clamp_pt(x2, y2, w, h)])

    elif t == "polyline":
        raw = shape.get("points") or []
        pts = []
        for p in raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                x, y = _rotate(_num(p[0]), _num(p[1]), cx, cy, rot)
                pts.append(_clamp_pt(x, y, w, h))
        if len(pts) >= 2:
            polylines.append(pts)

    elif t in ("rect", "square"):
        size = _num(shape.get("size", shape.get("w", 20)))
        rw = _num(shape.get("w", size))
        rh = _num(shape.get("h", size if t == "square" else size * 0.7))
        corners = [(-rw / 2, -rh / 2), (rw / 2, -rh / 2), (rw / 2, rh / 2), (-rw / 2, rh / 2)]
        pts = [_clamp_pt(*_rotate(cx + x, cy + y, cx, cy, rot), w, h) for x, y in corners]
        pts.append(pts[0][:])
        polylines.append(pts)

    elif t == "circle":
        r = max(0.5, _num(shape.get("r"), 10))
        polylines.append(_circle_pts(cx, cy, r, w, h))

    elif t == "ellipse":
        rx = max(0.5, _num(shape.get("rx"), 12))
        ry = max(0.5, _num(shape.get("ry"), 8))
        pts = []
        for i in range(28):
            a = i / 28 * 2 * math.pi
            x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
            pts.append(_clamp_pt(*_rotate(x, y, cx, cy, rot), w, h))
        pts.append(pts[0][:])
        polylines.append(pts)

    elif t == "triangle":
        size = max(1.0, _num(shape.get("size"), 14))
        hgt = size * math.sqrt(3) / 2
        verts = [(0, -hgt * 2 / 3), (-size / 2, hgt / 3), (size / 2, hgt / 3)]
        pts = [_clamp_pt(*_rotate(cx + x, cy + y, cx, cy, rot), w, h) for x, y in verts]
        pts.append(pts[0][:])
        polylines.append(pts)

    elif t == "arc":
        r = max(0.5, _num(shape.get("r"), 12))
        start = math.radians(_num(shape.get("start"), 0))
        end = math.radians(_num(shape.get("end"), 180))
        if end <= start:
            end += 2 * math.pi
        steps = max(6, int(abs(end - start) / 0.25))
        pts = []
        for i in range(steps + 1):
            a = start + (end - start) * i / steps
            pts.append(_clamp_pt(cx + r * math.cos(a), cy + r * math.sin(a), w, h))
        polylines.append(pts)

    elif t == "star":
        outer = max(1.0, _num(shape.get("outer"), _num(shape.get("r"), 14)))
        inner = max(0.5, _num(shape.get("inner"), outer * 0.45))
        n = int(shape.get("points", 5))
        n = max(3, min(12, n))
        pts = []
        for i in range(n * 2):
            a = i / (n * 2) * 2 * math.pi - math.pi / 2 + rot
            rad = outer if i % 2 == 0 else inner
            pts.append(_clamp_pt(cx + rad * math.cos(a), cy + rad * math.sin(a), w, h))
        pts.append(pts[0][:])
        polylines.append(pts)

    elif t == "ring":
        r = max(1.0, _num(shape.get("r"), 12))
        thick = max(0.5, _num(shape.get("thickness"), 2))
        polylines.append(_circle_pts(cx, cy, r, w, h))
        polylines.append(_circle_pts(cx, cy, max(0.5, r - thick), w, h, n=20))

    elif t == "cross":
        size = max(1.0, _num(shape.get("size"), 12))
        for dx, dy in ((1, 0), (0, 1)):
            x0, y0 = cx - dx * size, cy - dy * size
            x1, y1 = cx + dx * size, cy + dy * size
            polylines.append([
                _clamp_pt(*_rotate(x0, y0, cx, cy, rot), w, h),
                _clamp_pt(*_rotate(x1, y1, cx, cy, rot), w, h),
            ])

    elif t == "wave":
        bw = max(4.0, _num(shape.get("w"), 40))
        bh = max(2.0, _num(shape.get("h"), 8))
        n = max(8, int(bw / 3))
        pts = []
        for i in range(n):
            frac = i / max(1, n - 1)
            x = cx - bw / 2 + bw * frac
            y = cy + bh * math.sin(frac * 4 * math.pi + rot)
            pts.append(_clamp_pt(*_rotate(x, y, cx, cy, rot), w, h))
        polylines.append(pts)

    return [p for p in polylines if len(p) >= 2]


def extract_compose_plan(parsed) -> str:
    if isinstance(parsed, dict):
        for key in ("compose_plan", "plan", "thinking", "intent"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:4000]
    return ""


def extract_shape_plan(parsed: Any) -> list[dict] | None:
    if isinstance(parsed, dict) and isinstance(parsed.get("shapes"), list):
        return [s for s in parsed["shapes"] if isinstance(s, dict) and s.get("type")]
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and parsed[0].get("type"):
        return [s for s in parsed if isinstance(s, dict) and s.get("type")]
    return None


def extract_curated_layers(parsed: Any) -> list[dict] | None:
    """{"layers":[{"painter":"painter-1","color":"#ffb000","shapes":[...]}, ...]}"""
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("layers")
    if not isinstance(raw, list):
        return None
    layers = []
    for layer in raw:
        if not isinstance(layer, dict):
            continue
        shapes = layer.get("shapes")
        if not isinstance(shapes, list):
            continue
        pid = layer.get("painter") or layer.get("painter_id") or layer.get("agent")
        color = layer.get("color", "")
        valid = [s for s in shapes if isinstance(s, dict) and s.get("type")]
        if valid and pid:
            layers.append({"painter_id": str(pid), "color": str(color), "shapes": valid})
    return layers or None


def shapes_to_polylines(
    shapes: list[dict], w: int, h: int, *, max_shapes: int = 80, fill_spacing: float = 2.2,
) -> list:
    out: list = []
    for shape in shapes[:max_shapes]:
        t = str(shape.get("type", "")).lower()
        if t not in SHAPE_TYPES:
            continue
        want_fill = bool(shape.get("fill"))
        want_outline = shape.get("outline", not want_fill)
        polys = _render_one(shape, w, h)
        for poly in polys:
            if len(poly) < 2:
                continue
            if want_fill:
                closed = _poly_closed_coords(poly)
                if closed:
                    out.extend(_scanline_fill(closed, fill_spacing, w, h))
                elif len(poly) >= 2:
                    # открытая линия — утолщаем параллельными сдвигами
                    for off in (-1.5, 0, 1.5):
                        out.append([
                            _clamp_pt(poly[0][0], poly[0][1] + off, w, h),
                            _clamp_pt(poly[-1][0], poly[-1][1] + off, w, h),
                        ])
            if want_outline or not want_fill:
                out.append(poly)
    return out


def procedural_shape_plan(w: int, h: int, seed: int, technique: str) -> list[dict]:
    """Mock / fallback: varied recognizable composition from shape primitives."""
    rng = random.Random(seed)
    pool = list(SHAPE_TYPES)
    if technique == "long_strokes":
        pool = ["line", "wave", "arc", "polyline"]
    elif technique == "soft_bands":
        pool = ["wave", "ellipse", "circle", "rect"]
    elif technique == "hatching":
        pool = ["line", "cross", "triangle", "rect"]
    elif technique == "stippling":
        pool = ["circle", "ring", "cross", "star"]

    n = 10 + seed % 14
    shapes: list[dict] = []
    for i in range(n):
        t = pool[(seed + i) % len(pool)]
        cx = rng.uniform(w * 0.12, w * 0.88)
        cy = rng.uniform(h * 0.12, h * 0.88)
        rot = rng.uniform(0, 2 * math.pi)
        scale = 0.6 + rng.random() * 1.4
        base = {"type": t, "cx": round(cx, 1), "cy": round(cy, 1),
                "rotation": round(rot, 3), "fill": t in ("circle", "ellipse", "rect", "square", "triangle")}
        if t in ("circle", "ring"):
            base["r"] = round((8 + rng.random() * 22) * scale, 1)
        elif t == "ring":
            base["thickness"] = round(1 + rng.random() * 3, 1)
        elif t in ("rect", "square"):
            base["size"] = round((12 + rng.random() * 28) * scale, 1)
        elif t == "ellipse":
            base["rx"] = round((10 + rng.random() * 20) * scale, 1)
            base["ry"] = round((6 + rng.random() * 14) * scale, 1)
        elif t == "triangle":
            base["size"] = round((10 + rng.random() * 24) * scale, 1)
        elif t == "star":
            base["outer"] = round((10 + rng.random() * 18) * scale, 1)
            base["inner"] = round(base["outer"] * 0.4, 1)
            base["points"] = 5 + (seed + i) % 4
        elif t == "line":
            base["from"] = [round(cx - 15 * scale, 1), round(cy, 1)]
            base["to"] = [round(cx + 15 * scale, 1), round(cy + rng.uniform(-8, 8), 1)]
        elif t == "wave":
            base["w"] = round((25 + rng.random() * 50) * scale, 1)
            base["h"] = round((4 + rng.random() * 10) * scale, 1)
        elif t == "arc":
            base["r"] = round((10 + rng.random() * 20) * scale, 1)
            base["start"] = rng.uniform(0, 180)
            base["end"] = base["start"] + rng.uniform(60, 200)
        elif t == "cross":
            base["size"] = round((6 + rng.random() * 14) * scale, 1)
        elif t == "polyline":
            k = 3 + rng.randint(0, 4)
            base["points"] = [
                [round(cx + rng.uniform(-20, 20) * scale, 1),
                 round(cy + rng.uniform(-20, 20) * scale, 1)]
                for _ in range(k)
            ]
        shapes.append(base)
    return shapes
