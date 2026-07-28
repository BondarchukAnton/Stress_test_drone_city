"""Pure-logic core of the `city_missions` scenario (Город дронов, Архипелаг 2026).

Deterministic, dependency-free, fully unit-testable WITHOUT any flight/bridge:
this is the "critical mass" the compliance audit calls out — the two-mission
model, energy planning, and the multi-step rover plan. The agent roles
(city_coordinator / city_scout / city_rover / safety_drone) drive these; the LLM
proposes and explains, but every plan is compiled and checked here.

Contains:
  * grid helpers (4-connected road pathfinding over the 6×6 city);
  * WorldModel + Observation/Detection/Action/Evidence schemas (audit §7);
  * EnergyLedger (rover energy: 0 start, +1 per confirmed stationary second in the
    charge zone, -1 per cell transition, movement blocked at 0);
  * FireRoute / DeliveryRoute compilers (level-N fire = N× tower→3s dwell→fire;
    delivery = 5s load dwell + escort, no path through an un-extinguished fire);
  * MissionPlanner (orders missions by a transparent score);
  * dwell validators for the regulation's exact timings (3 s / 5 s).

Frames/units: field is a grid of cells; regulation cell size is 0.8 m (config
`cell_size_m`). Cells are [x, y] with grid[y][x] (0 = road, 1 = building/forbidden).
"""
from __future__ import annotations

import heapq
import os
from dataclasses import dataclass, field
from typing import Optional

from .survey_common import cell_key, grid_size

# regulation-fixed dwell requirements (seconds)
WATER_DWELL_S = 3.0     # § tower: остановка ровно не менее 3 секунд
LOAD_DWELL_S = 5.0      # § delivery: погрузка ровно 5 секунд
ESCORT_MAX_LAG = 2      # § escort: не отставать более чем на 2 клетки
# how many fire cycles one tank of water covers. Regulation: the rover carries 1
# unit of water and drives back to the tower to refill (WATER_CAPACITY=1 => one
# tower trip per fire cycle). Raise it (env) to carry more water = fewer trips.
WATER_CAPACITY = max(1, int(os.environ.get("WATER_CAPACITY", "1")))
MAX_ALT_M = 4.0         # § safety: высота полёта не более 4 м


class RouteBlocked(Exception):
    """No legal path (e.g. the only route to dropoff crosses an active fire)."""


class EnergyError(Exception):
    """Movement attempted with insufficient rover energy."""


# ---- grid / pathfinding -----------------------------------------------------
def is_road(grid, cell) -> bool:
    """A cell the rover may occupy: inside the grid and not a building (1)."""
    x, y = int(cell[0]), int(cell[1])
    if y < 0 or y >= len(grid) or x < 0 or x >= len(grid[0]):
        return False
    return int(grid[y][x]) == 0


def neighbors4(cell, w: int, h: int):
    x, y = int(cell[0]), int(cell[1])
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h:
            yield [nx, ny]


def astar(grid, start, goal, *, blocked=None) -> Optional[list]:
    """Shortest 4-connected road path start..goal (inclusive), avoiding buildings
    and any cells in `blocked`. Returns the cell list or None if unreachable.
    `start`/`goal` are used even if they are buildings (endpoints like a pad), but
    intermediate cells must be roads."""
    h = len(grid)
    w = len(grid[0]) if h else 0
    start = [int(start[0]), int(start[1])]
    goal = [int(goal[0]), int(goal[1])]
    blocked = {cell_key(c) for c in (blocked or [])}
    if cell_key(goal) in blocked:
        return None
    start_k, goal_k = cell_key(start), cell_key(goal)
    pq = [(_manh(start, goal), 0, start_k, start)]
    came: dict[str, Optional[list]] = {start_k: None}
    best = {start_k: 0}
    while pq:
        _, g, k, cell = heapq.heappop(pq)
        if k == goal_k:
            path = []
            cur = cell
            while cur is not None:
                path.append(cur)
                cur = came[cell_key(cur)]
            return path[::-1]
        for nb in neighbors4(cell, w, h):
            nk = cell_key(nb)
            if nk in blocked:
                continue
            if nk != goal_k and not is_road(grid, nb):
                continue
            ng = g + 1
            if ng < best.get(nk, 1 << 30):
                best[nk] = ng
                came[nk] = cell
                heapq.heappush(pq, (ng + _manh(nb, goal), ng, nk, nb))
    return None


def _manh(a, b) -> int:
    return abs(int(a[0]) - int(b[0])) + abs(int(a[1]) - int(b[1]))


def path_moves(path) -> int:
    """Number of cell transitions in a path (energy cost = one per transition)."""
    return max(0, len(path) - 1) if path else 0


# ---- world model + schemas (audit §7) ---------------------------------------
@dataclass
class Detection:
    type: str                       # fire | person_in_window | delivery_pickup | ...
    confidence: float = 0.0
    level: Optional[int] = None     # fire level (number on the marker)
    aruco_id: Optional[int] = None
    label: str = ""


@dataclass
class Observation:
    observation_id: str
    agent_id: str
    cell: list
    detections: list                # list[Detection]
    pose: Optional[dict] = None      # {x,y,z,frame}
    image: str = ""
    confidence: float = 0.0
    timestamp: str = ""


@dataclass
class Action:
    action_id: str
    do: str                          # navigate | dwell | charge | led | escort | done
    cell: Optional[list] = None
    to: Optional[list] = None
    seconds: float = 0.0
    led: str = ""                    # "" | on | off | blink
    preconditions: list = field(default_factory=list)
    success_evidence: list = field(default_factory=list)
    timeout: float = 30.0

    def as_dict(self) -> dict:
        d = {"action_id": self.action_id, "do": self.do}
        for k in ("cell", "to", "seconds", "led", "preconditions",
                  "success_evidence", "timeout"):
            v = getattr(self, k)
            if v not in (None, "", 0.0, []):
                d[k] = v
        return d


@dataclass
class WorldModel:
    """Parsed, mutable model of the field for a city_missions attempt."""
    grid: list
    w: int
    h: int
    cell_size_m: float = 0.8
    charge_zone: Optional[list] = None
    water_tower: Optional[list] = None
    drone_pads: list = field(default_factory=list)
    districts: dict = field(default_factory=dict)
    fire: Optional[dict] = None         # {cell, level, confirmed_by, extinguished}
    person: Optional[dict] = None       # {cell, window, confidence}
    delivery: Optional[dict] = None     # {pickup, dropoff}
    missions: list = field(default_factory=list)

    @property
    def fire_active(self) -> bool:
        return bool(self.fire and self.fire.get("cell") is not None
                    and not self.fire.get("extinguished"))

    @property
    def fire_cell(self):
        return (self.fire or {}).get("cell")


def load_world(scenario_map: dict) -> WorldModel:
    w, h = grid_size(scenario_map)
    grid = scenario_map.get("grid") or [[0] * w for _ in range(h)]
    fire = scenario_map.get("fire")
    if isinstance(fire, list):                 # allow bare [x,y] (level defaults 1)
        fire = {"cell": fire, "level": 1}
    elif isinstance(fire, dict):
        fire = dict(fire)
    return WorldModel(
        grid=grid, w=w, h=h,
        cell_size_m=float(scenario_map.get("cell_size_m", 0.8)),
        charge_zone=scenario_map.get("charge_zone") or scenario_map.get("start"),
        water_tower=scenario_map.get("water_tower"),
        drone_pads=scenario_map.get("drone_pads") or [],
        districts=scenario_map.get("districts") or {},
        fire=fire,
        person=scenario_map.get("person"),
        delivery=scenario_map.get("delivery"),
        missions=scenario_map.get("missions") or [],
    )


# ---- energy ledger ----------------------------------------------------------
class EnergyLedger:
    """Rover energy per regulation §2.7: start 0; +1 per confirmed stationary
    second in the charge zone; -1 per cell transition; movement blocked at 0."""

    def __init__(self, energy: int = 0):
        self.energy = int(energy)
        self.log: list = []

    def charge(self, seconds: float) -> int:
        """Credit energy for CONFIRMED stationary seconds in the charge zone.
        Only whole confirmed seconds count."""
        gained = int(seconds)
        if gained > 0:
            self.energy += gained
            self.log.append(("charge", gained, self.energy))
        return self.energy

    def can_move(self, cells: int = 1) -> bool:
        return self.energy >= cells

    def spend_move(self) -> int:
        if self.energy <= 0:
            raise EnergyError("движение заблокировано: заряд ровера = 0")
        self.energy -= 1
        self.log.append(("move", -1, self.energy))
        return self.energy

    @staticmethod
    def cost_of(moves: int) -> int:
        return int(moves)


# ---- dwell validators (exact regulation timings) ----------------------------
def water_dwell_valid(seconds: float, *, moved: bool = False,
                      in_zone: bool = True, led_on: bool = True) -> bool:
    """Water pickup counts only if stationary ≥3 s in the tower cell with LED on.
    2.9 s, movement, wrong cell, or LED off => not counted."""
    return (not moved) and in_zone and led_on and float(seconds) >= WATER_DWELL_S


def load_dwell_valid(seconds: float, *, moved: bool = False,
                     in_zone: bool = True, led_on: bool = True) -> bool:
    """Delivery loading counts only if stationary ≥5 s at pickup with LED on."""
    return (not moved) and in_zone and led_on and float(seconds) >= LOAD_DWELL_S


# ---- mission route compilers ------------------------------------------------
def fire_approach(world: WorldModel) -> list:
    """The ROAD cell the rover parks on to fight the fire. The fire is in a house
    (a building) — regulation says approach to ONE cell away, do NOT drive into the
    fire. Return the road neighbour of the burning house closest to the water tower
    (shortest cycles). Falls back to the fire cell itself if it has no road
    neighbour (e.g. a fire placed on a road in older configs)."""
    fc = world.fire_cell
    cands = [c for c in neighbors4([int(fc[0]), int(fc[1])],
                                   len(world.grid[0]) if world.grid else 6,
                                   len(world.grid) if world.grid else 6)
             if is_road(world.grid, c)]
    if not cands:
        return list(fc)
    ref = world.water_tower or world.charge_zone or list(fc)
    cands.sort(key=lambda c: _manh(c, ref))
    return cands[0]


def fire_route(world: WorldModel) -> tuple[list, int]:
    """Compile the level-N fire mission into atomic rover actions and return
    (actions, total_moves). N = fire level = number of water cycles. Each cycle:
    tower → 3 s LED dwell → APPROACH (1 cell from the burning house) → extinguish
    dwell. Rover starts at charge_zone and never enters the fire cell."""
    if not (world.fire and world.water_tower and world.charge_zone):
        raise RouteBlocked("нет пожара / башни / зоны зарядки в конфигурации")
    level = int(world.fire.get("level", 1))
    cap = WATER_CAPACITY
    tower, approach = world.water_tower, fire_approach(world)
    actions: list = []
    moves = 0
    pos = list(world.charge_zone)
    water = 0                                  # units in the tank (0 => must refill)
    for cyc in range(1, level + 1):
        if water == 0:                         # tank empty -> drive to the tower, refill
            leg1 = astar(world.grid, pos, tower)
            if leg1 is None:
                raise RouteBlocked(f"нет пути к башне из {pos}")
            moves += path_moves(leg1)
            water = min(cap, level - cyc + 1)  # only load what is still needed
            actions.append(Action(f"fire-{cyc}-to-water", "navigate", to=list(tower),
                                  timeout=60).as_dict())
            actions.append(Action(
                f"fire-{cyc}-water-dwell", "dwell", cell=list(tower),
                seconds=WATER_DWELL_S, led="blink",
                preconditions=["in_water_zone", "speed_zero"],
                success_evidence=["pose_stable_3s", "led_on_3s", f"water_loaded_{water}"],
                timeout=8).as_dict())
            pos = list(tower)
        leg2 = astar(world.grid, pos, approach)
        if leg2 is None:
            raise RouteBlocked(f"нет пути к пожару (подъезд {approach}) из {pos}")
        moves += path_moves(leg2)
        actions.append(Action(f"fire-{cyc}-to-fire", "navigate", to=list(approach),
                              timeout=60).as_dict())
        water -= 1                             # spend one unit on this fire cycle
        actions.append(Action(
            f"fire-{cyc}-extinguish", "dwell", cell=list(approach), seconds=1.0,
            led="on", preconditions=["adjacent_to_fire", "speed_zero", "has_water"],
            success_evidence=["fire_cycle_done", f"water_left_{water}"], timeout=8).as_dict())
        pos = list(approach)
    actions.append(Action("fire-done", "done", cell=list(approach),
                          success_evidence=["fire_extinguished"]).as_dict())
    return actions, moves


def delivery_route(world: WorldModel, *, avoid_fire: bool = True) -> tuple[list, int]:
    """Compile the delivery mission: charge_zone → pickup (5 s LED load) → dropoff.
    If the fire is still active and `avoid_fire`, the dropoff leg must not pass
    through the fire cell — raises RouteBlocked if that removes every path."""
    if not (world.delivery and world.charge_zone):
        raise RouteBlocked("нет доставки / зоны зарядки в конфигурации")
    pickup = world.delivery.get("pickup")
    dropoff = world.delivery.get("dropoff")
    if pickup is None or dropoff is None:
        raise RouteBlocked("не заданы клетки старта/финиша доставки")
    blocked = []
    if avoid_fire and world.fire_active:
        blocked = [world.fire_cell]
    leg1 = astar(world.grid, world.charge_zone, pickup)
    if leg1 is None:
        raise RouteBlocked(f"нет пути к погрузке {pickup}")
    leg2 = astar(world.grid, pickup, dropoff, blocked=blocked)
    if leg2 is None:
        raise RouteBlocked(
            "маршрут доставки заблокирован непотушенным пожаром"
            if blocked else f"нет пути к финишу {dropoff}")
    moves = path_moves(leg1) + path_moves(leg2)
    actions = [
        Action("del-to-pickup", "navigate", to=list(pickup), timeout=60).as_dict(),
        Action("del-load-dwell", "dwell", cell=list(pickup), seconds=LOAD_DWELL_S,
               led="blink", preconditions=["in_pickup_zone", "speed_zero"],
               success_evidence=["pose_stable_5s", "led_on_5s"], timeout=8).as_dict(),
        Action("del-start-escort", "escort", cell=list(pickup),
               success_evidence=["escort_launched", "beacon_on"]).as_dict(),
        Action("del-to-dropoff", "navigate", to=list(dropoff), led="blink",
               preconditions=["beacon_on"], timeout=120).as_dict(),
        Action("del-done", "done", cell=list(dropoff),
               success_evidence=["delivery_arrived"]).as_dict(),
    ]
    return actions, moves


# ---- mission planner --------------------------------------------------------
def plan_energy_for(world: WorldModel, order: list) -> int:
    """Total rover moves for a mission order (fire before delivery reuses the
    running position implicitly via each compiler starting from charge_zone —
    a conservative upper bound that always suffices)."""
    total = 0
    for m in order:
        if m == "fire":
            _, mv = fire_route(world)
        elif m == "delivery":
            _, mv = delivery_route(world, avoid_fire=False)
        else:
            continue
        total += mv
    return total


def rank_missions(world: WorldModel) -> tuple[list, dict]:
    """Choose the mission order. Transparent score per candidate:
    score = danger*10 - energy_cost - deadline_penalty. Fire is a hard predecessor
    of delivery whenever the delivery route would otherwise cross the fire cell
    (delivery через непотушенный пожар запрещён). Returns (order, reasons)."""
    have = [m.get("id") or m.get("type") for m in world.missions] or []
    if not have:
        have = [m for m in ("fire", "delivery")
                if (m == "fire" and world.fire) or (m == "delivery" and world.delivery)]
    danger = {m.get("id") or m.get("type"): float(m.get("danger", 1))
              for m in world.missions}
    reasons = {}

    # hard constraint: if delivery must cross the fire cell, fire goes first
    fire_blocks_delivery = False
    if "fire" in have and "delivery" in have and world.fire_active:
        try:
            delivery_route(world, avoid_fire=True)
        except RouteBlocked:
            fire_blocks_delivery = True
            reasons["constraint"] = "доставка не проходит мимо пожара — сначала тушим"

    def score(m):
        s = danger.get(m, 1.0) * 10.0
        try:
            s -= plan_energy_for(world, [m])
        except RouteBlocked:
            s -= 100.0
        return s

    order = sorted(have, key=lambda m: -score(m))
    if fire_blocks_delivery:
        order = ["fire"] + [m for m in order if m != "fire"]
    for m in have:
        reasons[m] = {"danger": danger.get(m, 1.0), "score": round(score(m), 1)}
    return order, reasons
