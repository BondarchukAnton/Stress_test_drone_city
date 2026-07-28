"""Role behaviors: one decide() per (role, phase).

Each role's step(ctx) is idempotent against the board -- it inspects what is
already posted/recorded before acting, so the polling loop (§7) can call it
repeatedly without duplicating messages or robot actions.

step(ctx) returns:
  {
    "thought":  str,            # rationale for this cycle (-> thought event)
    "messages": [msg, ...],     # candidate messages (loop applies novelty gate)
    "idle":     bool,           # nothing to do this cycle
  }
"""
from __future__ import annotations


# ---- shared helpers used by role modules (defined first to avoid the ----
# ---- circular import: submodules below do `from . import has_posted`)  ----
def has_posted(ctx, type_, phase=None, round_=None) -> bool:
    for m in ctx.messages:
        if m.get("from") != ctx.agent_id or m.get("type") != type_:
            continue
        if phase is not None and m.get("phase") != phase:
            continue
        if round_ is not None and m.get("payload", {}).get("round") != round_:
            continue
        return True
    return False


def messages_of(ctx, type_=None, phase=None):
    out = []
    for m in ctx.messages:
        if type_ is not None and m.get("type") != type_:
            continue
        if phase is not None and m.get("phase") != phase:
            continue
        out.append(m)
    return out


def make_msg(ctx, type_, to, phase, body, payload=None, ref=None) -> dict:
    return {
        "from": ctx.agent_id,
        "to": to,
        "phase": phase,
        "type": type_,
        "ref": ref,
        "body": body,
        "payload": payload or {},
    }


from . import coordinator, scout, rover, painter, debate, moderator, critic  # noqa: E402 (after helpers, on purpose)
from . import city_missions  # noqa: E402  (city_missions safety_drone role)
from . import scan_debate  # noqa: E402  (test-3x3 single-drone scan + dual-pilot dispute)

_ROLES = {"coordinator": coordinator, "scout": scout, "rover": rover,
          "painter": painter, "debater": debate, "moderator": moderator,
          "critic": critic, "safety_drone": city_missions}

# Роли, которым loop даёт ОДИН дополнительный шаг на phase=DONE (обычные агенты
# на DONE сразу выходят): критику нужен финальный вердикт по готовой картине.
STEP_AT_DONE = {"critic"}


def step(ctx):
    # task-aware routing: scan_debate owns all its roles (coordinator/controller/
    # flyer) — keeps the generic ROLE->module map (city, painting, debate) intact
    # while the test-3x3 scenario reuses ROLE=coordinator for the phase machine.
    if ctx.config.get("task") == "scan_debate":
        return scan_debate.step(ctx)
    mod = _ROLES.get(ctx.role)
    if mod is None:
        return {"thought": f"unknown role {ctx.role}", "messages": [], "idle": True}
    return mod.step(ctx)
