"""Blackboard I/O for the multi-agent stack.

Implements the directory layout, single-writer state files, append-only
message log, append-only events.jsonl feed, and the novelty gate from
agent-coordination-spec.md (§1-§4, §6.4, §8).

Concurrency rules honored here:
  * state files are single-writer (coordinator owns phase/decision/assignments/
    world; each agent owns state/progress/<id>.json -> per-agent file is a
    stricter form of the spec's "one writer per file" rule than a shared
    progress.json would be).
  * messages/ carry a BOARD-GLOBAL monotonic seq (flock-guarded counter file)
    so consumers get true append order even when several agents write within
    the same second; filenames are unique per (seq, ts, agent, type).
  * events.jsonl lines are written with a single O_APPEND write() call;
    O_APPEND writes to a regular file are serialized by the kernel on Linux,
    so concurrent appends interleave whole-line (lines may exceed PIPE_BUF —
    that limit applies to pipes, not regular files).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Blackboard:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.environ.get("BLACKBOARD", "./blackboard")).resolve()
        self.messages = self.root / "messages"
        self.state = self.root / "state"
        self.progress = self.state / "progress"
        self.artifacts = self.root / "artifacts"
        self.agents = self.root / "agents"
        self.events_path = self.root / "events.jsonl"
        self._seq_path = self.root / ".seq"

    # ---- scaffold -------------------------------------------------------
    def ensure_layout(self) -> None:
        for d in (self.messages, self.state, self.progress, self.artifacts, self.agents):
            d.mkdir(parents=True, exist_ok=True)
        if not self.events_path.exists():
            self.events_path.touch()

    def reset_runtime(self, keep_registry: bool = True) -> None:
        """Wipe a previous run's runtime so a fresh demo starts clean.

        Removes messages, per-agent progress, all coordinator-owned state files
        (phase/decision/assignments/world), produced artifacts, and truncates
        events.jsonl. Without this, a persisted blackboard (the Docker named
        volume, or a re-`make up`) carries a prior run's phase=DONE and old
        messages -> agents look like they "already talked" before the run began.

        The agent registry (agents/) is preserved by default so a drone that
        registered moments earlier in a distributed run is not dropped; agents
        re-register on boot anyway. Called once, by the coordinator, at startup.
        """
        import shutil
        self.ensure_layout()
        for d in (self.messages, self.progress):
            for p in d.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        # commerce_cursor принадлежит payments/blackboard_bridge.py и раньше
        # переживал сброс. Последствие видно на демо: доску вытерли, курсор
        # остался на последнем перенесённом событии, наблюдатель считал, что
        # переносить нечего, и виджет коммерции показывал ноль сделок. Курсор
        # обязан умирать вместе с доской, которую он описывает. Файл читается
        # только при старте наблюдателя — после сброса его надо перезапустить
        # (это делает `make mission-rerun`).
        for name in ("phase", "decision", "assignments", "world", "collaborative",
                     "current_run", "pause", "commerce_cursor"):
            (self.state / f"{name}.json").unlink(missing_ok=True)
        self._seq_path.unlink(missing_ok=True)  # fresh run counts from 1
        for p in self.artifacts.glob("*"):
            try:
                (shutil.rmtree(p) if p.is_dir() else p.unlink())
            except OSError:
                pass
        if not keep_registry and self.agents.exists():
            for p in self.agents.glob("*"):
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                except OSError:
                    pass
        # truncate the event feed (keep the file so tailers/SSE stay attached)
        try:
            with open(self.events_path, "w", encoding="utf-8"):
                pass
        except OSError:
            pass

    # ---- generic json (atomic last-write-wins) --------------------------
    @staticmethod
    def read_json(path: Path, default=None):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def write_json(path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        os.replace(tmp, path)  # atomic on same filesystem

    # ---- phase / decision / assignments (coordinator-owned) -------------
    def read_phase(self) -> dict:
        return self.read_json(self.state / "phase.json", {"phase": "INIT", "round": 0})

    def write_phase(self, phase: str, round_: int = 0, deadline: str | None = None) -> dict:
        obj = {
            "phase": phase,
            "round": round_,
            "updated_by": "coordinator",
            "ts": now_iso(),
            "deadline": deadline,
        }
        self.write_json(self.state / "phase.json", obj)
        return obj

    def read_decision(self) -> dict:
        return self.read_json(self.state / "decision.json", {})

    def write_decision(self, obj: dict) -> None:
        self.write_json(self.state / "decision.json", obj)

    def read_assignments(self) -> dict:
        return self.read_json(self.state / "assignments.json", {})

    def write_assignments(self, obj: dict) -> None:
        self.write_json(self.state / "assignments.json", obj)

    def read_world(self) -> dict:
        return self.read_json(self.state / "world.json", {})

    def write_world(self, obj: dict) -> None:
        self.write_json(self.state / "world.json", obj)

    # ---- pause (operator-owned: hub/viz POST /pause or make pause) ------
    # Глобальная пауза сценария (замена аккумуляторов): дроны садятся и ждут,
    # фазовая машина замирает, дедлайны сдвигаются на длительность паузы.
    def read_pause(self) -> dict:
        return self.read_json(self.state / "pause.json", {"paused": False})

    def write_pause(self, obj: dict) -> None:
        self.write_json(self.state / "pause.json", obj)

    # ---- progress (per-agent single-writer file) ------------------------
    def write_progress(self, agent_id: str, entry: dict) -> None:
        self.write_json(self.progress / f"{agent_id}.json", entry)

    def read_all_progress(self) -> dict:
        out: dict = {}
        if self.progress.exists():
            for p in sorted(self.progress.glob("*.json")):
                data = self.read_json(p)
                if data is not None:
                    out[p.stem] = data
        return out

    # ---- registry (who is connected; powers discovery + dashboard) ------
    def write_registry(self, agent_id: str, meta: dict) -> None:
        d = self.agents / agent_id
        d.mkdir(parents=True, exist_ok=True)
        self.write_json(d / "meta.json", {**meta, "id": agent_id, "ts": now_iso()})

    def read_registry(self) -> dict:
        out: dict = {}
        if self.agents.exists():
            for m in sorted(self.agents.glob("*/meta.json")):
                data = self.read_json(m)
                if data is not None:
                    out[m.parent.name] = data
        return out

    # ---- messages (append-only, unique filename, board-global order) ----
    def _next_seq(self) -> int:
        """Board-global monotonic counter, shared by EVERY process on this board.

        The old per-process `self._seq` broke ordering two ways: same-second
        writes from different agents sorted arbitrarily (non-causal transcripts),
        and a restarted process reset to 0 (message-id collisions — a reissued
        debate floor id could look already answered). One flock-guarded counter
        file fixes both; it is wiped by reset_runtime so runs start at 1, and
        re-seeds above any surviving message file when RESET_ON_START=0.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._seq_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 32).decode("ascii", "replace").strip()
            cur = int(raw) if raw.isdigit() else self._seed_seq()
            nxt = cur + 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(nxt).encode("ascii"))
            return nxt
        finally:
            os.close(fd)  # closing the fd releases the flock

    def _seed_seq(self) -> int:
        best = 0
        if self.messages.exists():
            for p in self.messages.glob("*.json"):
                m = re.match(r"^(\d+)-", p.name)
                if m:
                    best = max(best, int(m.group(1)))
        return best

    def write_message(self, msg: dict) -> dict:
        seq = self._next_seq()
        ts = msg.get("ts") or now_iso()
        msg.setdefault("ts", ts)
        msg["seq"] = seq
        # filename components must never carry path separators (hub messages are
        # network input; the hub also validates, this is defense in depth)
        safe_from = re.sub(r"[^A-Za-z0-9_.-]", "_", str(msg.get("from", "?")))[:64]
        safe_type = re.sub(r"[^A-Za-z0-9_.-]", "_", str(msg.get("type", "?")))[:32]
        msg.setdefault("id", f"msg-{safe_from}-{seq:06d}")
        safe_ts = ts.replace(":", "").replace("-", "")
        fname = f"{seq:06d}-{safe_ts}-{safe_from}-{safe_type}.json"
        self.write_json(self.messages / fname, msg)
        return msg

    def list_messages(self) -> list[dict]:
        out = []
        for p in sorted(self.messages.glob("*.json")):
            data = self.read_json(p)
            if data is not None:
                out.append(data)
        # true append order: seq is authoritative; ts breaks ties for any
        # legacy files that predate the global counter
        out.sort(key=lambda m: (m.get("seq") is not None, m.get("seq") or 0,
                                m.get("ts") or ""))
        return out

    def messages_since(self, cursor: float) -> tuple[list[dict], float]:
        """Return messages whose file mtime > cursor, plus the new cursor."""
        out = []
        newest = cursor
        for p in sorted(self.messages.glob("*.json")):
            mt = p.stat().st_mtime
            if mt > cursor:
                data = self.read_json(p)
                if data is not None:
                    out.append(data)
                newest = max(newest, mt)
        return out, newest

    # ---- events.jsonl (append-only feed for viz, §8) --------------------
    def append_event(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("t", now_iso())
        line = json.dumps(event, separators=(",", ":")) + "\n"
        # single atomic O_APPEND write (line < PIPE_BUF=4096)
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


class HttpBoard:
    """Network-backed board for a drone/rover on its own host.

    Same method surface as FileBoard, but every read/write goes to the central
    hub over HTTP (the brief §2 swap: shared volume -> coordinator HTTP API).
    Reads tolerate transient hub errors (return defaults; the loop retries next
    cycle); writes/events are best-effort so a flaky link never crashes a drone.
    """

    def __init__(self, hub_url: str, token: str | None = None, agent_id: str | None = None):
        self.base = hub_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        # ordering: the hub's FileBoard stamps the board-global seq server-side

    def _req(self, method: str, path: str, body=None, timeout: float = 10.0):
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}

    def _get(self, path: str, default):
        try:
            out = self._req("GET", path)
            return out if out not in (None, {}) or isinstance(default, dict) else default
        except Exception:
            return default

    # -- scaffold / discovery --
    def ensure_layout(self) -> None:
        pass

    def healthz(self) -> bool:
        try:
            return bool(self._req("GET", "/healthz", timeout=4).get("ok"))
        except Exception:
            return False

    def register(self, role: str, meta: dict | None = None) -> None:
        try:
            self._req("POST", "/register",
                      {"id": self.agent_id, "role": role, **(meta or {})})
        except Exception:
            pass

    # -- reads (default-on-error) --
    def read_phase(self) -> dict:
        return self._get("/state/phase", {"phase": "INIT", "round": 0}) or {"phase": "INIT", "round": 0}

    def read_decision(self) -> dict:
        return self._get("/state/decision", {})

    def read_assignments(self) -> dict:
        return self._get("/state/assignments", {})

    def read_world(self) -> dict:
        return self._get("/state/world", {})

    def read_all_progress(self) -> dict:
        return self._get("/progress", {})

    def read_pause(self) -> dict:
        # осторожный дефолт: обрыв связи с хабом НЕ должен выглядеть как
        # «пауза снята» или «пауза включена» — вернём последнее известное?
        # У HttpBoard нет состояния; дефолт paused=False, а дрон, потерявший
        # хаб, всё равно не получит новых заданий (read-петля пустая).
        return self._get("/state/pause", {"paused": False}) or {"paused": False}

    def list_messages(self) -> list:
        out = self._get("/messages", [])
        return out if isinstance(out, list) else []

    # -- writes (best-effort) --
    def write_progress(self, agent_id: str, entry: dict) -> None:
        try:
            self._req("POST", f"/progress/{agent_id}", entry)
        except Exception:
            pass

    def write_message(self, msg: dict) -> dict:
        try:
            return self._req("POST", "/messages", msg) or msg
        except Exception:
            return msg

    def append_event(self, event: dict) -> None:
        try:
            self._req("POST", "/events", event)
        except Exception:
            pass


def make_board(agent_id: str | None = None):
    """FileBoard for co-located agents (coordinator on the orchestrator, or the
    single-host dev demo); HttpBoard for a drone/rover when HUB_URL is set."""
    hub = os.environ.get("HUB_URL")
    if hub:
        return HttpBoard(hub, os.environ.get("HUB_TOKEN"), agent_id)
    return Blackboard()


# ---- novelty gate (§6.4) ------------------------------------------------
def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t}


def novelty(candidate: str, existing: list[str]) -> float:
    """Information gain in [0,1] = 1 - max Jaccard similarity to prior text.

    A blank candidate has zero novelty. With nothing on the board, novelty=1.
    """
    cand = _tokens(candidate)
    if not cand:
        return 0.0
    best = 0.0
    for ex in existing:
        ex_t = _tokens(ex)
        if not ex_t:
            continue
        inter = len(cand & ex_t)
        union = len(cand | ex_t)
        if union:
            best = max(best, inter / union)
    return round(1.0 - best, 3)


def wait_for_change(paths: list[Path], prev_sig: tuple, interval: float = 0.5):
    """Block until any watched path's (count, max-mtime) signature changes."""
    while True:
        sig = _signature(paths)
        if sig != prev_sig:
            return sig
        time.sleep(interval)


def _signature(paths: list[Path]) -> tuple:
    parts = []
    for p in paths:
        if p.is_dir():
            files = list(p.glob("*"))
            mt = max((f.stat().st_mtime for f in files), default=0.0)
            parts.append((len(files), mt))
        elif p.exists():
            parts.append((1, p.stat().st_mtime))
        else:
            parts.append((0, 0.0))
    return tuple(parts)
