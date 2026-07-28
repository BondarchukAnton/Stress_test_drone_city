"""Per-run debug logs under blackboard/runs/<run_id>/.

Each painting session gets a durable folder (survives blackboard reset of messages/events).
Another agent / human can replay llm.jsonl + events.jsonl + copied messages to debug.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from bb import now_iso


def _enabled() -> bool:
    return os.environ.get("RUN_LOG", "1") not in ("0", "false", "no")


_active_dir: Path | None = None
_agent_id: str = ""
_phase: str = ""


def set_agent_context(agent_id: str, phase: str) -> None:
    global _agent_id, _phase
    _agent_id = agent_id or os.environ.get("AGENT_ID", "")
    _phase = phase or ""


def _append_jsonl(path: Path, record: dict) -> None:
    """Single O_APPEND write per record: every agent process appends to the same
    llm.jsonl, and buffered open("a")+write() can interleave 30-80KB lines from
    concurrent writers mid-record. One write() on an O_APPEND fd is serialized
    whole by the kernel on Linux (mirrors bb.append_event)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def start_run(bb, *, meta: dict, paint_seed: int = 0) -> dict:
    """Phase-machine owner: create run dir and pointer file. Returns run info.
    paint_seed is painting-specific; other tasks (debate) just omit it."""
    if not _enabled():
        return {}
    ts = now_iso().replace(":", "").replace("-", "")[:15]
    run_id = f"{ts}-s{paint_seed}"
    run_dir = bb.root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages").mkdir(exist_ok=True)
    info = {
        "run_id": run_id,
        "dir": str(run_dir),
        "started": now_iso(),
        "paint_seed": paint_seed,
        **meta,
    }
    bb.write_json(bb.root / "state" / "current_run.json", info)
    bb.write_json(run_dir / "run.json", info)
    activate(run_dir)
    return info


def refresh_from_bb(bb) -> None:
    """All agents: attach to current run if pointer exists.

    Only meaningful for a co-located FileBoard — an HttpBoard drone has no
    shared run directory (and no .root/.read_json), and this call sits OUTSIDE
    loop.py's resilience try: without the guard every remote drone crash-looped
    on its first cycle (the distributed mode was dead on arrival)."""
    if not _enabled() or not hasattr(bb, "read_json") or not hasattr(bb, "root"):
        return
    info = bb.read_json(bb.root / "state" / "current_run.json", {}) or {}
    d = info.get("dir")
    if d:
        activate(Path(d))


def activate(run_dir: Path) -> None:
    global _active_dir
    _active_dir = Path(run_dir)


def run_dir() -> Path | None:
    return _active_dir


def log_event(event: dict) -> None:
    if not _enabled() or not _active_dir:
        return
    rec = dict(event)
    rec.setdefault("t", now_iso())
    _append_jsonl(_active_dir / "events.jsonl", rec)


def log_llm(
    *,
    context: str = "",
    system: str = "",
    messages: list | None = None,
    response: str = "",
    error: str = "",
    max_tokens: int | None = None,
    duration_ms: float | None = None,
    parsed_ok: bool | None = None,
    extra: dict | None = None,
) -> None:
    if not _enabled() or not _active_dir:
        return
    full = os.environ.get("RUN_LOG_FULL", "1") not in ("0", "false", "no")
    rec = {
        "ts": now_iso(),
        "agent": _agent_id or os.environ.get("AGENT_ID", "?"),
        "phase": _phase,
        "context": context,
        "max_tokens": max_tokens,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "error": error or None,
        "parsed_ok": parsed_ok,
        "response_len": len(response or ""),
    }
    if full:
        rec["system"] = system
        rec["messages"] = messages or []
        rec["response"] = response
    else:
        rec["system_preview"] = (system or "")[:500]
        rec["response_preview"] = (response or "")[:800]
    if extra:
        rec.update(extra)
    _append_jsonl(_active_dir / "llm.jsonl", rec)


def log_parse(context: str, attempt: int, ok: bool, error: str = "") -> None:
    """Per-attempt parse outcome for JSON calls — makes the repair path observable
    (the big log_llm records can't know parsed_ok; the retry loop can)."""
    if not _enabled() or not _active_dir:
        return
    _append_jsonl(_active_dir / "llm.jsonl", {
        "ts": now_iso(),
        "agent": _agent_id or os.environ.get("AGENT_ID", "?"),
        "phase": _phase,
        "kind": "parse",
        "context": context,
        "attempt": attempt,
        "parsed_ok": ok,
        "error": (error or None),
    })


def log_agent_turn(phase: str, task: str, raw: str, out: dict, *, fallback_used: bool) -> None:
    if not _enabled() or not _active_dir:
        return
    _append_jsonl(_active_dir / "turns.jsonl", {
        "ts": now_iso(),
        "agent": _agent_id,
        "phase": phase,
        "task": task[:300],
        "fallback_used": fallback_used,
        "line": (out.get("line") or "")[:400],
        "thinking_len": len(out.get("thinking") or ""),
        "raw_len": len(raw or ""),
    })


def finalize_run(bb, *, summary: dict) -> None:
    """Copy messages + write summary at end of run."""
    if not _enabled() or not _active_dir:
        return
    msg_src = bb.root / "messages"
    msg_dst = _active_dir / "messages"
    msg_dst.mkdir(parents=True, exist_ok=True)
    if msg_src.exists():
        for p in sorted(msg_src.glob("*.json")):
            try:
                shutil.copy2(p, msg_dst / p.name)
            except OSError:
                pass
    for name in ("decision.json", "collaborative.json", "phase.json"):
        src = bb.root / "state" / name
        if src.exists():
            try:
                shutil.copy2(src, _active_dir / name)
            except OSError:
                pass
    summary = {**summary, "finished": now_iso(), "run_id": _active_dir.name}
    with open(_active_dir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
