"""Привязка дрона к хендлеру (hub) с сохранением в постоянную память.

Несколько команд работают в одной локальной сети → у каждого хаба есть
HANDLER_ID (номер/имя команды) и FLEET (painter | city). Дрон хранит свою
привязку в файле на несъёмной памяти Raspberry (BIND_FILE, по умолчанию
/data/binding.json — volume в docker-compose.drone.yml), поэтому переживает
перезагрузку и смену DHCP-адресов:

    {"handler_id": "team-7", "hub_url": "http://10.0.0.2:8080",
     "agent_id": "drone-3", "fleet": "city", "bound": true, "ts": "..."}

Порядок разрешения конфигурации на дроне (env всегда может переопределить):
  1. env (HUB_URL / HANDLER_ID / FLEET / AGENT_ID) — профиль проекта,
     записываемый на все дроны;
  2. сохранённая привязка (после успешной LED-регистрации через /fleet);
  3. без обоих — дрон не знает, чей он: регистрируется как unbound-кандидат
     и ждёт, пока оператор мигнёт его лентой и нажмёт «привязать».

Чистые функции — покрыты tests/test_binding.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FLEETS = ("painter", "city")


def bind_file() -> Path:
    return Path(os.environ.get("BIND_FILE", "/data/binding.json"))


def load_binding(path: Path | None = None) -> dict:
    p = path or bind_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_binding(binding: dict, path: Path | None = None) -> bool:
    """Атомарная запись привязки; False, если носитель недоступен (не роняем
    агент — привязка тогда живёт до перезагрузки)."""
    p = path or bind_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(binding, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def resolve(env: dict, stored: dict) -> dict:
    """env-профиль + сохранённая привязка -> действующая конфигурация.

    env приоритетнее (оператор явно переписал профиль); из stored берём то,
    чего в env нет. bound=True только если привязка подтверждена и её
    handler_id не противоречит env."""
    handler_env = (env.get("HANDLER_ID") or "").strip()
    handler_stored = str(stored.get("handler_id") or "").strip()
    conflict = bool(handler_env and handler_stored
                    and handler_env != handler_stored)
    out = {
        "handler_id": handler_env or handler_stored,
        "hub_url": (env.get("HUB_URL") or "").strip() or str(stored.get("hub_url") or ""),
        "agent_id": (env.get("AGENT_ID") or "").strip() or str(stored.get("agent_id") or ""),
        "fleet": normalize_fleet(env.get("FLEET") or stored.get("fleet")),
        # конфликт handler'ов = привязка недействительна (дрон переехал в
        # другую команду по env) — идём на перерегистрацию
        "bound": bool(stored.get("bound")) and not conflict,
        "stored_conflict": conflict,
    }
    return out


def normalize_fleet(v) -> str:
    v = str(v or "").strip().lower()
    aliases = {"painter": "painter", "painters": "painter", "художники": "painter",
               "city": "city", "город": "city", "survey": "city",
               "safe_passage": "city"}
    return aliases.get(v, "")


def fleet_for_task(task: str) -> str:
    """Флот, который подразумевает задача хаба (для валидации регистраций)."""
    return "painter" if task == "painting" else ("city" if task in
                                                 ("safe_passage", "survey") else "")


def registration_meta(cfg: dict, extra: dict | None = None) -> dict:
    """Тело регистрации на хабе: хендлер/флот едут с каждым дроном, чтобы
    чужой хаб отказал сразу и команды не путались, чей это дрон."""
    return {**(extra or {}),
            "handler_id": cfg.get("handler_id") or "",
            "fleet": cfg.get("fleet") or "",
            "bound": bool(cfg.get("bound"))}


def accept_registration(hub_handler: str, hub_fleet: str, body: dict) -> str | None:
    """Хаб-сторона: None = принять, иначе причина отказа.

    Пустой handler_id у дрона допустим (кандидат на привязку — его ещё не
    привязали); НЕпустой чужой — жёсткий отказ."""
    d_handler = str(body.get("handler_id") or "").strip()
    d_fleet = normalize_fleet(body.get("fleet"))
    if hub_handler and d_handler and d_handler != hub_handler:
        return f"drone is bound to handler '{d_handler}', this hub is '{hub_handler}'"
    if hub_fleet and d_fleet and d_fleet != hub_fleet:
        return f"drone fleet '{d_fleet}' does not match hub fleet '{hub_fleet}'"
    return None
