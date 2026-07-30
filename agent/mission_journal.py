#!/usr/bin/env python3
"""mission_journal.py — структурированный JSONL-журнал миссии.

При старте старый журнал удаляется. Каждая запись связывает:
  решение агента → отправленную команду → фактическое действие аппарата.

Запись останавливается когда ровер прибывает в клетку пожара
(событие rover_reached_fire).

Формат записи (одна строка JSON на событие):
  {"ts":"...", "seq":N, "type":"...", "agent":"...", "phase":"...", ...}

Типы записей:
  phase_transition  — смена фазы
  agent_thought     — мысль агента
  agent_message     — сообщение в чат/blackboard
  command_sent      — отправленная команда дрону/роверу
  action_result     — результат выполнения команды
  vlm_detection     — результат VLM-анализа
  telemetry         — телеметрия (ArUco)
  rover_navigation  — навигация ровера
  rover_reached_fire — ровер прибыл к пожару (→ остановка журнала)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MissionJournal:
    def __init__(self, bb_root: str = ""):
        root = Path(bb_root or os.environ.get("BLACKBOARD", "./blackboard"))
        self._path = root / "mission_journal.jsonl"
        self._seq = 0
        self._frozen = False

    def reset(self) -> None:
        """Очистить старый журнал перед новым запуском."""
        self._seq = 0
        self._frozen = False
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass

    def freeze(self) -> None:
        """Остановить запись (ровер достиг пожара)."""
        if not self._frozen:
            self._frozen = True
            self._write({"type": "journal_frozen",
                         "reason": "rover_reached_fire_cell"})

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def record(self, entry_type: str, **fields) -> None:
        if self._frozen:
            return
        self._seq += 1
        rec = {"ts": _now_iso(), "seq": self._seq, "type": entry_type, **fields}
        self._write(rec)

    def _write(self, rec: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except Exception:
            pass


_journal: MissionJournal | None = None


def init_journal(bb_root: str = "") -> MissionJournal:
    global _journal
    _journal = MissionJournal(bb_root)
    _journal.reset()
    return _journal


def get_journal() -> MissionJournal:
    global _journal
    if _journal is None:
        _journal = MissionJournal()
    return _journal


def journal_reset() -> None:
    get_journal().reset()


def journal_freeze() -> None:
    get_journal().freeze()


def journal_record(entry_type: str, **fields) -> None:
    get_journal().record(entry_type, **fields)