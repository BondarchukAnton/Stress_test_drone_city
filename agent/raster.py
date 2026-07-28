"""Stdlib canvas rasterizer: shape/stroke events -> RGB pixels -> PNG bytes.

Мини-двойник frontend/src/Canvas.tsx и scripts/render_canvas.py, но без PIL
(агентский образ — чистый stdlib): композитинг по глобальному z, заливка
полигонов чёт-нечет, альфа-смешение, линии Брезенхэмом. Используется
VLM-критиком (roles/critic.py) как «камера над холстом» в симуляции и как
источник эвристики, когда VLM недоступна.
"""
from __future__ import annotations

import struct
import zlib

BG = (0x0E, 0x14, 0x20)  # фон студийного холста (как в Canvas.tsx)


def hex2rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#cccccc").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (204, 204, 204)


def _blend(px, col, alpha: float):
    a = max(0.0, min(1.0, alpha))
    return (int(px[0] * (1 - a) + col[0] * a),
            int(px[1] * (1 - a) + col[1] * a),
            int(px[2] * (1 - a) + col[2] * a))


def _plot(img, x: int, y: int, col, alpha: float, thick: int = 1):
    h = len(img)
    w = len(img[0])
    for dy in range(-(thick // 2), thick - thick // 2):
        for dx in range(-(thick // 2), thick - thick // 2):
            xx, yy = x + dx, y + dy
            if 0 <= xx < w and 0 <= yy < h:
                img[yy][xx] = _blend(img[yy][xx], col, alpha)


def _line(img, p0, p1, col, alpha: float, thick: int = 1):
    x0, y0 = int(round(p0[0])), int(round(p0[1]))
    x1, y1 = int(round(p1[0])), int(round(p1[1]))
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        _plot(img, x0, y0, col, alpha, thick)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _fill_poly(img, pts, col, alpha: float):
    """Чёт-нечет заливка по строкам."""
    if len(pts) < 3:
        return
    h = len(img)
    w = len(img[0])
    ys = [p[1] for p in pts]
    y_min = max(0, int(min(ys)))
    y_max = min(h - 1, int(max(ys)) + 1)
    n = len(pts)
    for y in range(y_min, y_max + 1):
        xs = []
        yy = y + 0.5
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            if (y0 <= yy < y1) or (y1 <= yy < y0):
                t = (yy - y0) / (y1 - y0)
                xs.append(x0 + t * (x1 - x0))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            for x in range(max(0, int(xs[i])), min(w - 1, int(xs[i + 1])) + 1):
                img[y][x] = _blend(img[y][x], col, alpha)


def render_events(events: list[dict], canvas_w: int = 120, canvas_h: int = 120,
                  px: int = 240) -> list[list[tuple[int, int, int]]]:
    """shape/stroke события -> пиксельная матрица px×px (композитинг по z)."""
    s = px / max(1, canvas_w)
    img = [[BG for _ in range(px)] for _ in range(int(canvas_h * s))]
    shapes = [e for e in events if e.get("kind") == "shape"]
    shapes.sort(key=lambda e: e.get("z", 0))
    order: list[dict] = shapes + [e for e in events if e.get("kind") == "stroke"]
    for e in order:
        col = hex2rgb(e.get("color"))
        alpha = float(e.get("alpha", 1.0) or 1.0)
        if e.get("kind") == "stroke":
            pts = [(p[0] * s, p[1] * s) for p in (e.get("points") or [])
                   if isinstance(p, (list, tuple)) and len(p) >= 2]
            for a, b in zip(pts, pts[1:]):
                _line(img, a, b, col, alpha, thick=2)
            continue
        for poly in e.get("polys") or []:
            pts = [(p[0] * s, p[1] * s) for p in poly
                   if isinstance(p, (list, tuple)) and len(p) >= 2]
            if not pts:
                continue
            if e.get("fill") and len(pts) >= 3:
                _fill_poly(img, pts, col, alpha)
            if e.get("outline", not e.get("fill")) or not e.get("fill"):
                for a, b in zip(pts, pts[1:]):
                    _line(img, a, b, col, alpha, thick=1)
    return img


def canvas_stats(img) -> dict:
    """Метрики для эвристической оценки: доля закрашенного, разнообразие
    цветов, грубый баланс по квадрантам."""
    h = len(img)
    w = len(img[0]) if h else 0
    painted = 0
    colors: set = set()
    quad = [0, 0, 0, 0]
    for y in range(h):
        for x in range(w):
            p = img[y][x]
            if p != BG:
                painted += 1
                colors.add((p[0] // 32, p[1] // 32, p[2] // 32))
                quad[(1 if x >= w // 2 else 0) + (2 if y >= h // 2 else 0)] += 1
    total = max(1, w * h)
    q_max = max(quad) or 1
    balance = min(quad) / q_max if painted else 0.0
    return {"coverage": painted / total, "colors": len(colors),
            "balance": balance, "painted_px": painted}


def png_bytes(img) -> bytes:
    """RGB-матрица -> PNG (без зависимостей)."""
    h = len(img)
    w = len(img[0]) if h else 0
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in img)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
