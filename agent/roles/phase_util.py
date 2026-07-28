"""Shared phase-machine helpers: time, deadlines, transitions, stable tie-breaks.

Extracted from coordinator.py and moderator.py, which each carried a drifted
private copy of the same primitives (the audit's duplicated-helpers finding).
Any role that owns a phase machine (coordinator, moderator, future engines)
uses these; the deadline table lives in ctx.config["deadlines"].
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime(ISO_FMT)


def parse_iso(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def deadline_passed(ctx) -> bool:
    dt = parse_iso(ctx.phase.get("deadline"))
    return bool(dt and now() > dt)


def deadline_in(ctx, key: str, default_secs: int) -> str:
    """Deadline `key` seconds from now, from ctx.config['deadlines'] with fallback."""
    secs = ctx.config.get("deadlines", {}).get(key, default_secs)
    return iso(now() + timedelta(seconds=secs if secs else default_secs))


def transition(ctx, phase: str, round_: int, default_secs: int, key: str | None = None) -> None:
    """Write the new phase (single-writer: only the phase-machine owner calls this)
    and emit the phase event the dashboards key their timelines on."""
    ctx.bb.write_phase(phase, round_, deadline_in(ctx, key or phase.lower(), default_secs))
    ctx.emit({"kind": "phase", "phase": phase, "round": round_})


def stable_key(*parts) -> int:
    """Process-stable tie-break — reproducible by seed, unlike salted hash()."""
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:6], 16)
