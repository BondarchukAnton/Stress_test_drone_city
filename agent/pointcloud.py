"""Rasterize painter stroke events into physical point clouds.

Maps canvas coordinates (from events / fixtures) onto a real wall:
  3.0 m wide × 1.5 m tall, sampled on a 2 cm grid.

Exports per-agent CSV files under blackboard/artifacts/pointcloud/.
"""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path


WIDTH_M = float(os.environ.get("CANVAS_WIDTH_M", "3.0"))
HEIGHT_M = float(os.environ.get("CANVAS_HEIGHT_M", "1.5"))
STEP_M = float(os.environ.get("POINT_STEP_M", "0.02"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def canvas_to_m(x: float, y: float, canvas_w: float, canvas_h: float) -> tuple[float, float]:
    return x / canvas_w * WIDTH_M, y / canvas_h * HEIGHT_M


def _dist_point_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _stroke_radius_m(width_canvas: float, canvas_w: float) -> float:
    """Half spray width in metres."""
    return max(STEP_M * 0.5, (float(width_canvas) / 2.0) * (WIDTH_M / canvas_w))


def rasterize_strokes(
    strokes: list[dict],
    *,
    canvas_w: float = 120,
    canvas_h: float = 120,
    width_m: float = WIDTH_M,
    height_m: float = HEIGHT_M,
    step_m: float = STEP_M,
) -> list[list[float]]:
    """Return [[x_m, y_m], ...] grid points covered by the given strokes."""
    if not strokes:
        return []
    # segment list in metres + per-segment radius
    segments: list[tuple[float, float, float, float, float]] = []
    for s in strokes:
        pts = s.get("points") or []
        if len(pts) < 2:
            continue
        rad = _stroke_radius_m(s.get("width", 2), canvas_w)
        mpts = [canvas_to_m(float(p[0]), float(p[1]), canvas_w, canvas_h) for p in pts]
        for i in range(len(mpts) - 1):
            x1, y1 = mpts[i]
            x2, y2 = mpts[i + 1]
            segments.append((x1, y1, x2, y2, rad))

    if not segments:
        return []

    out: list[list[float]] = []
    x = step_m / 2
    while x < width_m:
        y = step_m / 2
        while y < height_m:
            for x1, y1, x2, y2, rad in segments:
                if _dist_point_segment(x, y, x1, y1, x2, y2) <= rad:
                    out.append([round(x, 3), round(y, 3)])
                    break
            y += step_m
        x += step_m
    return out


def strokes_from_events(
    events_path: Path, canvas_w: float = 120, canvas_h: float = 120,
) -> dict[str, list[dict]]:
    """Group stroke + shape events by agent id (shapes → polylines)."""
    by_agent: dict[str, list[dict]] = {}
    if not events_path.exists():
        return by_agent
    _to_polys = None
    with open(events_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = e.get("kind")
            agent = e.get("by") or e.get("from") or "unknown"
            if kind == "stroke":
                by_agent.setdefault(agent, []).append({
                    "points": e.get("points", []),
                    "color": e.get("color", "#ffffff"),
                    "width": e.get("width", 2),
                })
            elif kind == "shape" and isinstance(e.get("shape"), dict):
                if _to_polys is None:
                    from roles.paint_shapes import shapes_to_polylines as _to_polys
                color = e.get("color", "#ffffff")
                for poly in _to_polys([e["shape"]], int(canvas_w), int(canvas_h)):
                    by_agent.setdefault(agent, []).append({
                        "points": poly, "color": color, "width": e.get("width", 2),
                    })
    return by_agent


def build_pointcloud(
    events_path: Path,
    *,
    canvas_w: float = 120,
    canvas_h: float = 120,
    width_m: float | None = None,
    height_m: float | None = None,
    step_m: float | None = None,
) -> dict:
    width_m = width_m if width_m is not None else WIDTH_M
    height_m = height_m if height_m is not None else HEIGHT_M
    step_m = step_m if step_m is not None else STEP_M
    by_strokes = strokes_from_events(events_path, canvas_w=canvas_w, canvas_h=canvas_h)
    agents = {}
    for agent, strokes in sorted(by_strokes.items()):
        color = next((s.get("color") for s in strokes if s.get("color")), "#ffffff")
        pts = rasterize_strokes(
            strokes, canvas_w=canvas_w, canvas_h=canvas_h,
            width_m=width_m, height_m=height_m, step_m=step_m,
        )
        agents[agent] = {"color": color, "points": pts, "count": len(pts)}
    return {
        "width_m": width_m,
        "height_m": height_m,
        "step_m": step_m,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "agents": agents,
    }


def export_pointcloud(
    bb_root: Path,
    *,
    canvas_w: float = 120,
    canvas_h: float = 120,
    fixtures: Path | None = None,
    scenario: str | None = None,
) -> Path:
    """Write pointcloud.json + per-agent CSVs; return the output directory."""
    root = Path(bb_root)
    events = root / "events.jsonl"
    if fixtures and scenario:
        mp = fixtures / scenario / "map.json"
        if mp.exists():
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                cv = m.get("canvas") or {}
                canvas_w = float(cv.get("w", canvas_w))
                canvas_h = float(cv.get("h", canvas_h))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    cloud = build_pointcloud(events, canvas_w=canvas_w, canvas_h=canvas_h)
    out_dir = root / "artifacts" / "pointcloud"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "pointcloud.json").write_text(
        json.dumps(cloud, separators=(",", ":")), encoding="utf-8")

    for agent, data in cloud["agents"].items():
        csv_path = out_dir / f"{agent}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["agent", "x_m", "y_m"])
            for x, y in data["points"]:
                w.writerow([agent, x, y])

    # combined sheet for convenience
    with open(out_dir / "all.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["agent", "x_m", "y_m"])
        for agent, data in cloud["agents"].items():
            for x, y in data["points"]:
                w.writerow([agent, x, y])

    return out_dir


def csv_bytes(agent: str, points: list[list[float]]) -> bytes:
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["agent", "x_m", "y_m"])
    for x, y in points:
        w.writerow([agent, x, y])
    return buf.getvalue().encode("utf-8")
