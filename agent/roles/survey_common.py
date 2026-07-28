"""Pure survey-mission logic: grid math, zone splits, action-plan validation,
verification quorum. No I/O, no ctx — unit-testable (tests/test_survey.py).

The survey task ("город дронов v2"): N scout drones sweep a W×H cell field
top-down, photograph + analyze every cell to localize cargo, cross-verify a
find one drone at a time, then dispatch the rover to the confirmed cell.

Per-turn output contract (the «джейсонка за ход»):

    {"thinking": "...", "say": "реплика в канал или ''",
     "actions": [
        {"do": "fly_to", "cell": [1, 1]},
        {"do": "wait", "seconds": 20},
        {"do": "photo_analyze"}
     ]}

Verbs: fly_to (any cell — multi-cell hops allowed; the bridge/hardware handles
the path), wait (flight-time model, seconds), photo_analyze (photograph the
CURRENT pose cell and run detection on it immediately).
"""
from __future__ import annotations

VERBS = ("fly_to", "wait", "photo_analyze")


# ---- grid / zones ---------------------------------------------------------
def cell_key(cell) -> str:
    return f"{int(cell[0])},{int(cell[1])}"


def key_cell(key: str) -> list[int]:
    x, y = key.split(",")
    return [int(x), int(y)]


def serpentine(w: int, h: int) -> list[list[int]]:
    """Boustrophedon sweep order: row 0 left→right, row 1 right→left, …
    Adjacent cells in the list are adjacent on the field → efficient flight."""
    out: list[list[int]] = []
    for y in range(h):
        xs = range(w) if y % 2 == 0 else range(w - 1, -1, -1)
        out.extend([x, y] for x in xs)
    return out


def zone_split(w: int, h: int, n: int) -> list[list[list[int]]]:
    """Split the serpentine path into n contiguous runs, sizes as even as
    possible (25 cells / 4 drones → 7,6,6,6). Contiguous runs keep each
    drone's sweep path short."""
    path = serpentine(w, h)
    total = len(path)
    base, extra = divmod(total, max(1, n))
    zones, i = [], 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        zones.append(path[i:i + size])
        i += size
    return zones


def zones_from_map(scenario_map: dict, labels: list[str]) -> dict[str, list[list[int]]]:
    """Zones from the fixture when present, else auto-split the grid."""
    w, h = grid_size(scenario_map)
    raw = scenario_map.get("zones") or {}
    out: dict[str, list[list[int]]] = {}
    for lab in labels:
        cells = raw.get(lab)
        if isinstance(cells, list) and cells:
            out[lab] = [[int(c[0]), int(c[1])] for c in cells]
    if len(out) == len(labels):
        return out
    auto = zone_split(w, h, len(labels))
    return {lab: auto[i] for i, lab in enumerate(labels)}


def grid_size(scenario_map: dict) -> tuple[int, int]:
    gs = scenario_map.get("grid_size")
    if isinstance(gs, list) and len(gs) == 2:
        return int(gs[0]), int(gs[1])
    grid = scenario_map.get("grid")
    if isinstance(grid, list) and grid and isinstance(grid[0], list):
        return len(grid[0]), len(grid)
    return 5, 5


def chebyshev(a, b) -> int:
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


# ---- action plans ----------------------------------------------------------
def flight_wait(dist: int, wait_per_cell: float, wait_max: float) -> float:
    """Flight-time model: «подождать 20 сек чтобы дрон долетел», scaled by
    hop distance and capped (a 4-cell hop shouldn't stall the turn forever)."""
    return round(min(max(dist, 1) * wait_per_cell, wait_max), 1)


def sweep_plan(pose, targets: list[list[int]], *, cells_per_turn: int,
               wait_per_cell: float, wait_max: float) -> list[dict]:
    """Deterministic fallback plan: visit the next `cells_per_turn` targets in
    order. fly_to → wait(flight time) → photo_analyze, repeated."""
    plan: list[dict] = []
    cur = list(pose)
    for cell in targets[:max(1, cells_per_turn)]:
        d = chebyshev(cur, cell)
        plan.append({"do": "fly_to", "cell": [int(cell[0]), int(cell[1])]})
        plan.append({"do": "wait", "seconds": flight_wait(d, wait_per_cell, wait_max)})
        plan.append({"do": "photo_analyze"})
        cur = list(cell)
    return plan


def verify_plan(pose, cell, *, wait_per_cell: float, wait_max: float) -> list[dict]:
    """Verification leg: fly to the candidate cell, settle, close-look photo."""
    d = chebyshev(pose, cell)
    return [
        {"do": "fly_to", "cell": [int(cell[0]), int(cell[1])]},
        {"do": "wait", "seconds": flight_wait(d, wait_per_cell, wait_max)},
        {"do": "photo_analyze", "close_look": True},
    ]


def validate_plan(plan, *, w: int, h: int, allowed_cells=None,
                  max_actions: int = 12, wait_max: float = 60.0) -> tuple[list[dict], list[str]]:
    """Sanitize an LLM-produced plan: whitelist verbs, clamp cells to the grid
    (and to `allowed_cells` when given — a sweeping drone plans only inside its
    own zone), cap wait times and total length. Returns (clean_plan, problems).
    An empty clean_plan means the plan was unusable → caller falls back to the
    deterministic planner."""
    problems: list[str] = []
    if not isinstance(plan, list):
        return [], ["plan is not a list"]
    allowed = {cell_key(c) for c in allowed_cells} if allowed_cells else None
    out: list[dict] = []
    for a in plan[:max_actions]:
        if not isinstance(a, dict):
            problems.append("action is not an object")
            continue
        verb = str(a.get("do") or a.get("action") or "").strip()
        if verb not in VERBS:
            problems.append(f"unknown verb {verb!r}")
            continue
        if verb == "fly_to":
            cell = a.get("cell") or a.get("to")
            if (not isinstance(cell, (list, tuple)) or len(cell) != 2
                    or not all(isinstance(c, (int, float)) for c in cell)):
                problems.append("fly_to without a valid cell")
                continue
            cx = min(max(int(cell[0]), 0), w - 1)
            cy = min(max(int(cell[1]), 0), h - 1)
            if [cx, cy] != [cell[0], cell[1]]:
                problems.append(f"cell {list(cell)} clamped to [{cx},{cy}]")
            if allowed is not None and cell_key([cx, cy]) not in allowed:
                problems.append(f"cell [{cx},{cy}] outside allowed set — dropped")
                continue
            out.append({"do": "fly_to", "cell": [cx, cy]})
        elif verb == "wait":
            try:
                secs = float(a.get("seconds", 0))
            except (TypeError, ValueError):
                problems.append("wait without numeric seconds")
                continue
            out.append({"do": "wait", "seconds": min(max(secs, 0.0), wait_max)})
        else:  # photo_analyze
            act = {"do": "photo_analyze"}
            if a.get("close_look"):
                act["close_look"] = True
            out.append(act)
    if not any(a["do"] == "photo_analyze" for a in out):
        problems.append("no photo_analyze in plan")
        return [], problems
    if len(plan) > max_actions:
        problems.append(f"plan truncated to {max_actions} actions")
    return out, problems


def plan_summary(plan: list[dict]) -> str:
    """Human-readable one-liner for the board: «→[1,1] · 20с · фото+анализ»."""
    parts = []
    for a in plan:
        if a["do"] == "fly_to":
            parts.append(f"→[{a['cell'][0]},{a['cell'][1]}]")
        elif a["do"] == "wait":
            parts.append(f"{a['seconds']:g}с")
        else:
            parts.append("фото+анализ" + (" (вблизи)" if a.get("close_look") else ""))
    return " · ".join(parts)


# ---- verification quorum ---------------------------------------------------
def quorum_state(votes: dict[str, bool], queue: list[str], quorum: int,
                 verify_all: bool) -> str:
    """'confirmed' | 'rejected' | 'pending' for a candidate under verification.

    votes: verifier -> saw-cargo. queue: ALL assigned verifiers. quorum: yes
    votes needed to confirm. verify_all: wait for every verifier before ruling
    («полетели все по очереди»); when False, rule as soon as the outcome is
    mathematically settled."""
    yes = sum(1 for v in votes.values() if v)
    no = sum(1 for v in votes.values() if not v)
    outstanding = len(queue) - len(votes)
    if verify_all and outstanding > 0:
        return "pending"
    if yes >= quorum:
        return "confirmed"
    if yes + outstanding < quorum:
        return "rejected"
    if outstanding == 0:
        return "confirmed" if yes >= quorum else "rejected"
    return "pending"


def next_verifier(queue: list[str], votes: dict[str, bool]) -> str | None:
    for v in queue:
        if v not in votes:
            return v
    return None
