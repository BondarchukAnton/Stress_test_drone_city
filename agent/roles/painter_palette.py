"""Shared palette + session context for painter agents.

The roster is no longer hardcoded: the coordinator publishes a per-run roster
(seed-derived personas, soul-overridable) into ``config.yaml.json`` at INIT, and
everyone reads it from there so all containers agree on every painter's name,
colour and technique. See ``personas.py``.
"""
from __future__ import annotations

from .personas import persona, roster as build_roster


def _cfg(ctx) -> dict:
    return ctx.bb.read_json(ctx.bb.root / "config.yaml.json", {}) or {}


def _seed(ctx) -> int:
    return int(_cfg(ctx).get("paint_seed", 0))


def _painters(ctx) -> list[str]:
    cfg = _cfg(ctx)
    return cfg.get("painters") or ctx.config.get("painters") or [ctx.agent_id]


def roster(ctx) -> list[dict]:
    """Active cast, published by the coordinator (else derived on the fly)."""
    pub = _cfg(ctx).get("roster")
    if isinstance(pub, list) and pub:
        return pub
    return build_roster(_painters(ctx), _seed(ctx))


def roster_by_id(ctx, agent_id: str) -> dict | None:
    for p in roster(ctx):
        if p.get("id") == agent_id:
            return p
    return None


def my_entry(ctx) -> dict:
    """My persona — published roster entry, merged with my own soul flavour."""
    base = roster_by_id(ctx, ctx.agent_id)
    if base is None:
        base = persona(ctx.agent_id, _painters(ctx), _seed(ctx), ctx.soul)
    # let my own soul tint temperament/leaning even if roster is pre-published
    out = dict(base)
    out["name"] = ctx.soul.get("name", out.get("name", ctx.agent_id))
    if ctx.soul.get("temperament"):
        out["temperament"] = ctx.soul["temperament"]
    if ctx.soul.get("leaning"):
        out["leaning"] = ctx.soul["leaning"]
    out.setdefault("hex", out.get("color", ctx.soul.get("color", "#ffffff")))
    out.setdefault("label", out["hex"])
    return out


def palette_context(ctx) -> str:
    """Who paints in which colour — for negotiation + composition prompts."""
    lines = ["ПАЛИТРА ХОЛСТА (у каждого дрона свой ИМЕНОВАННЫЙ пигмент, других нет):"]
    for p in roster(ctx):
        lines.append(
            f"  • {p.get('name', p['id'])} ({p['id']}): "
            f"{p.get('color_name', p.get('hex', '?'))} ({p.get('hex', '?')})"
            f" — {p.get('technique_ru', p.get('technique', ''))}"
        )
    me = my_entry(ctx)
    lines.append(
        f"ТВОЙ пигмент: {me.get('color_name', me.get('hex'))} ({me.get('hex')}). "
        f"Ты рисуешь ИМЕННО этим цветом — учитывай его смысл "
        f"(напр. синим хорошо рисовать воду/небо, зелёным — листву/траву, "
        f"жёлтым/янтарным — солнце, красным — акценты). "
        "Называй настроение цветами, которые реально есть в палитре выше."
    )
    return "\n".join(lines)


def palette_rules_short() -> str:
    return (
        "Правило палитры: на холсте только пигменты из общего списка дронов. "
        "Не предлагай настроение, где главный цвет отсутствует у всей команды. "
        "Настроение = чувство, которое ВЫ сможете передать СВОИМИ пигментами."
    )


def subject_color_feasible(subject: str) -> bool:
    """Palette is per-run dynamic now — any subject is colour-feasible."""
    return True


def sanitize_subject(subject: str, fallback: str) -> str:
    s = (subject or "").strip()[:80]
    return s or fallback


def canvas_state_summary(ctx) -> str:
    """Кто уже закончил и сколько мазков — для осознанного наложения."""
    lines = []
    order = []
    if ctx.assignments:
        order = sorted(
            ctx.assignments.keys(),
            key=lambda pid: (
                ctx.assignments[pid].get("paint_order", 99)
                if isinstance(ctx.assignments.get(pid), dict)
                else 99
            ),
        )
    if not order:
        order = [p["id"] for p in roster(ctx)]

    for pid in order:
        pr = ctx.progress.get(pid, {})
        entry = roster_by_id(ctx, pid) or {"name": pid, "hex": "?"}
        if pr.get("status") == "done":
            plan = pr.get("compose_plan", "")
            extra = f" — замысел: {plan}" if plan else ""
            lines.append(
                f"  ✓ {entry.get('name', pid)}: {pr.get('strokes', 0)} фигур "
                f"{entry.get('hex', '?')} — уже на холсте{extra}"
            )
        elif pid == ctx.agent_id:
            lines.append(f"  → {entry.get('name', pid)}: ты рисуешь СЕЙЧАС ({entry.get('hex', '?')})")
        else:
            lines.append(f"  · {entry.get('name', pid)}: ждёт очереди ({entry.get('hex', '?')})")
    if not lines:
        return "Ты первый на холсте — холст пуст."
    return "СОСТОЯНИЕ ХОЛСТА:\n" + "\n".join(lines)


def technique_hint(technique: str) -> str:
    from .personas import TECHNIQUES
    return dict(TECHNIQUES).get(technique, "Распыляй фигуры по всему холсту.")
