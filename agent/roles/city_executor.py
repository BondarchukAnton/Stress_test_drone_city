"""Deterministic mission executor for city_missions — the verifiable heart of the
scenario (audit §5 RoverPlanCompiler + §7.3 ACTION_COMPLETED evidence).

Runs the compiled fire/delivery routes against a simulated rover + safety drone in
SIM TIME (no real sleeps, so a full 15-minute attempt runs in milliseconds), and
emits a reproducible evidence log that a judge could replay. Every regulation
constraint is enforced HERE, independent of the LLM:
  * energy (0 start, +1/confirmed-second charge, -1/move, blocked at 0);
  * water dwell ≥3 s + LED on, load dwell ≥5 s + LED on (else NOT counted);
  * fire level N = exactly N confirmed water→fire cycles before "extinguished";
  * delivery never routes through an un-extinguished fire cell;
  * continuous beacon from load to dropoff;
  * safety drone: escort lag ≤2 cells (else a violation event), and person search.

The real `city_rover` role drives the SAME steps through the bridge; this module is
the mock/reference implementation and the E2E oracle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .city_world import (
    EnergyLedger, RouteBlocked, WorldModel, astar, path_moves,
    fire_route, delivery_route, fire_approach, rank_missions,
    WATER_DWELL_S, LOAD_DWELL_S, ESCORT_MAX_LAG, WATER_CAPACITY,
    water_dwell_valid, load_dwell_valid,
)

SIM_TIME_BUDGET_S = 15 * 60      # регламент: зачётная попытка ≤ 15 минут
SEC_PER_MOVE = 4.0               # sim seconds a rover spends per cell transition


@dataclass
class SimRover:
    cell: list
    energy: EnergyLedger = field(default_factory=EnergyLedger)
    led: str = "off"
    t: float = 0.0               # sim clock (s)
    trail: list = field(default_factory=list)   # cells visited (for escort lag)

    def set_led(self, mode: str):
        self.led = mode

    def charge(self, seconds: float):
        self.energy.charge(seconds)
        self.t += seconds

    def move_to(self, grid, goal, *, blocked=None):
        """Drive cell-by-cell to goal, spending 1 energy per transition. Returns the
        path or raises RouteBlocked / EnergyError."""
        path = astar(grid, self.cell, goal, blocked=blocked)
        if path is None:
            raise RouteBlocked(f"нет пути {self.cell}->{goal}")
        for nxt in path[1:]:
            self.energy.spend_move()          # raises EnergyError at 0
            self.cell = list(nxt)
            self.trail.append(list(nxt))
            self.t += SEC_PER_MOVE
        return path

    def dwell(self, seconds: float):
        self.t += seconds


@dataclass
class SimSafetyDrone:
    cell: list
    active: bool = False
    max_lag: int = 0             # worst escort lag observed (cells)
    found_person: bool = False

    def escort_step(self, rover_prev_cell, rover_cell):
        """Follow strictly behind: step onto the rover's PREVIOUS cell. Lag = grid
        distance rover->drone; regulation caps it at 2 cells."""
        self.cell = list(rover_prev_cell)
        lag = abs(rover_cell[0] - self.cell[0]) + abs(rover_cell[1] - self.cell[1])
        self.max_lag = max(self.max_lag, lag)
        return lag


def _ev(log, type_, **kw):
    log.append({"type": type_, **kw})


def plan_total_energy(world: WorldModel, order: list) -> int:
    """Moves the rover needs for the WHOLE ordered plan, INCLUDING returning to the
    charge zone between missions (regulation: dispatcher pre-computes the budget /
    keeps a return reserve; the rover must never strand itself at 0). Fire is
    assumed done before delivery, so delivery is costed without the fire block."""
    grid, charge = world.grid, world.charge_zone
    total = 0
    for m in order:
        if m == "fire":
            _, mv = fire_route(world)
            total += mv
            back = astar(grid, fire_approach(world), charge)
            total += path_moves(back) if back else 0
        elif m == "delivery":
            _, mv = delivery_route(world, avoid_fire=False)
            total += mv
    return total


def execute_fire(world: WorldModel, rover: SimRover, log: list) -> bool:
    """Run the level-N fire mission (rover pre-charged for the whole plan). N×
    (tower→3s LED dwell→fire→op). Fire extinguished only after N confirmed cycles."""
    if rover.cell != world.charge_zone:
        rover.move_to(world.grid, world.charge_zone)
    level = int(world.fire["level"])
    cap = WATER_CAPACITY                      # units the tank holds (regulation: 1)
    approach = fire_approach(world)          # park 1 cell from the burning house
    cycles_done = 0
    water = 0
    for cyc in range(1, level + 1):
        if water == 0:                        # tank empty -> drive to the tower, refill
            rover.set_led("off")
            rover.move_to(world.grid, world.water_tower)
            rover.set_led("blink")
            rover.dwell(WATER_DWELL_S)        # 3 s LED dwell = water pickup
            ok = water_dwell_valid(WATER_DWELL_S, in_zone=(rover.cell == world.water_tower),
                                   led_on=(rover.led == "blink"))
            water = min(cap, level - cyc + 1)
            _ev(log, "ACTION_COMPLETED", action_id=f"fire-{cyc}-water",
                agent_id="rover", evidence={"cell": list(rover.cell),
                "stationary_seconds": WATER_DWELL_S, "led": rover.led == "blink",
                "counted": ok, "water_loaded": water})
            if not ok:
                return False
        # -> approach cell (1 from the fire), extinguish; spend one water unit
        rover.move_to(world.grid, approach)
        rover.set_led("on")
        rover.dwell(1.0)
        water -= 1
        cycles_done += 1
        _ev(log, "ACTION_COMPLETED", action_id=f"fire-{cyc}-extinguish",
            agent_id="rover", evidence={"cell": list(rover.cell),
            "cycle": cycles_done, "of": level, "water_left": water})
    world.fire["extinguished"] = cycles_done >= level
    rover.set_led("off")
    _ev(log, "FIRE_EXTINGUISHED", cell=list(world.fire_cell), cycles=cycles_done)
    return world.fire["extinguished"]


def execute_delivery(world: WorldModel, rover: SimRover, drone: SimSafetyDrone,
                     log: list) -> bool:
    """Run the delivery: charge, →pickup, 5 s LED load, launch escort + beacon,
    →dropoff avoiding an active fire, beacon on the whole way. Escort follows
    strictly behind (lag ≤2 or a violation is logged)."""
    delivery_route(world, avoid_fire=True)      # raises RouteBlocked if fire blocks
    if rover.cell != world.charge_zone:
        rover.move_to(world.grid, world.charge_zone)
    pickup = world.delivery["pickup"]
    dropoff = world.delivery["dropoff"]
    # -> pickup
    rover.set_led("off")
    rover.move_to(world.grid, pickup)
    # 5 s LED load dwell
    rover.set_led("blink")
    rover.dwell(LOAD_DWELL_S)
    ok = load_dwell_valid(LOAD_DWELL_S, in_zone=(rover.cell == pickup),
                          led_on=(rover.led == "blink"))
    _ev(log, "ACTION_COMPLETED", action_id="delivery-load", agent_id="rover",
        evidence={"cell": list(rover.cell), "stationary_seconds": LOAD_DWELL_S,
                  "led": rover.led == "blink", "counted": ok})
    if not ok:
        return False
    # launch escort + beacon; beacon stays ON to dropoff
    drone.active = True
    drone.cell = list(pickup)
    _ev(log, "ESCORT_LAUNCHED", drone_cell=list(drone.cell))
    blocked = [world.fire_cell] if world.fire_active else []
    path = astar(world.grid, pickup, dropoff, blocked=blocked)
    if path is None:
        raise RouteBlocked("маршрут доставки через непотушенный пожар запрещён")
    for i in range(1, len(path)):
        prev = path[i - 1]
        rover.energy.spend_move()
        rover.cell = list(path[i]); rover.trail.append(list(path[i]))
        rover.t += SEC_PER_MOVE
        if world.fire_active and rover.cell == world.fire_cell:
            _ev(log, "SAFETY_VIOLATION", reason="проезд через активный пожар",
                cell=list(rover.cell)); return False
        lag = drone.escort_step(prev, rover.cell)
        if lag > ESCORT_MAX_LAG:
            _ev(log, "ESCORT_VIOLATION", lag=lag, cell=list(rover.cell))
    _ev(log, "ACTION_COMPLETED", action_id="delivery-arrived", agent_id="rover",
        evidence={"cell": list(rover.cell), "beacon": rover.led == "blink",
                  "escort_max_lag": drone.max_lag})
    rover.set_led("off")
    _ev(log, "DELIVERY_DONE", cell=list(dropoff), escort_max_lag=drone.max_lag)
    return True


def run_attempt(world: WorldModel, observations: list | None = None) -> dict:
    """Full deterministic city_missions attempt: plan order → execute each mission
    → collect the evidence log + a pass/fail result. `observations` (optional) are
    the confirmed scout detections; by default we trust the world's own config."""
    log: list = []
    order, reasons = rank_missions(world)
    _ev(log, "MISSION_ORDER", order=order, reasons=reasons)
    rover = SimRover(cell=list(world.charge_zone or [0, 0]))
    drone = SimSafetyDrone(cell=list(world.charge_zone or [0, 0]))
    result = {"order": order, "fire_ok": None, "delivery_ok": None,
              "person_found": None, "blocked": None}
    # dispatcher pre-charges the whole plan up front (+2 reserve) — the rover starts
    # at 0 and gains 1 energy per confirmed second stationary in the charge zone
    try:
        budget = plan_total_energy(world, order) + 2
        rover.charge(budget)
        _ev(log, "CHARGED", seconds=budget, energy=rover.energy.energy)
    except RouteBlocked as e:
        result["blocked"] = str(e)
        _ev(log, "REPLAN_NEEDED", reason=str(e))
        result.update(sim_time_s=round(rover.t, 1), within_time=True,
                      final_energy=rover.energy.energy, log=log)
        return result
    try:
        for mission in order:
            if mission == "fire":
                # safety drone searches for a person in the fire district (parallel)
                drone.found_person = bool(world.person)
                result["person_found"] = drone.found_person
                if drone.found_person:
                    _ev(log, "PERSON_FOUND", cell=list(world.person.get("cell")),
                        window=world.person.get("window"), agent_id="safety_drone")
                result["fire_ok"] = execute_fire(world, rover, log)
            elif mission == "delivery":
                result["delivery_ok"] = execute_delivery(world, rover, drone, log)
    except RouteBlocked as e:
        result["blocked"] = str(e)
        _ev(log, "REPLAN_NEEDED", reason=str(e))
    result["sim_time_s"] = round(rover.t, 1)
    result["within_time"] = rover.t <= SIM_TIME_BUDGET_S
    result["final_energy"] = rover.energy.energy
    result["log"] = log
    return result
