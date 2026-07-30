#!/usr/bin/env python3
"""
Единый скрипт миссии:
  1. Запуск дронов (fly_photo_land_pw.py) — взлёт, фото каждые 3с, посадка
  2. VLM-анализ снимков (scan_fire_kletki.py) — поиск огня
  3. Фиксация результата (клетка + уровень пожара + время) в лог / blackboard
  4. Отправка координат роверу (mission3_zachet.py)
"""

import subprocess
import sys
import os
import json
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MISSION_FLY = os.path.join(SCRIPT_DIR, "fly_photo_land_pw.py")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

DRONES = [
    ("192.168.1.110", "sverk", "drone110_2_5.jpg"),
    ("192.168.1.124", "sverk", "drone124_5_5.jpg"),
    ("192.168.1.116", "sverk", "drone116_2_2.jpg"),
    ("192.168.1.111", "sverk", "drone111_5_2.jpg"),
]

os.makedirs(LOG_DIR, exist_ok=True)

BB = None
bb_now_iso = None
try:
    sys.path.insert(0, os.path.join(os.path.dirname(SCRIPT_DIR), "agent"))
    from bb import Blackboard, now_iso as _bb_now_iso
    bb_now_iso = _bb_now_iso
    BB = Blackboard()
    BB.ensure_layout()
    print("[BB] Blackboard подключён")
except Exception as e:
    print(f"[BB] Blackboard недоступен (локальный режим): {e}")


LOG_FILE = os.path.join(LOG_DIR, f"mission_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def bb_event(kind, data=None):
    if BB is None:
        return
    try:
        event = {"kind": kind, "source": "mission_unified"}
        if data:
            event.update(data)
        BB.append_event(event)
    except Exception as e:
        log(f"BB event error: {e}")


def bb_post_message(msg_type, body, payload=None):
    if BB is None:
        return
    try:
        BB.write_message({
            "from": "mission_unified",
            "to": "rover",
            "type": msg_type,
            "body": body,
            "payload": payload or {},
            "ts": bb_now_iso() if bb_now_iso else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    except Exception as e:
        log(f"BB message error: {e}")


def dedup_fires(fires):
    """Группирует пожары по клетке, берёт максимальную уверенность и сумму count."""
    by_cell = {}
    for f in fires:
        key = (f["cell"][0], f["cell"][1])
        if key not in by_cell:
            by_cell[key] = f
        else:
            prev = by_cell[key]
            prev["count"] = max(prev["count"], f["count"])
            prev["confidence"] = max(prev["confidence"], f["confidence"] or 0)
            prev["summary"] = f["summary"] or prev["summary"]
            prev["image"] = f["image"]
            prev["ts"] = f["ts"]
    return list(by_cell.values())


log("=" * 60)
log("МИССИЯ ЗАПУЩЕНА")
log("=" * 60)

# ── Фаза 1: дроны ────────────────────────────────────────────────────────
log("ФАЗА 1: ЗАПУСК ДРОНОВ (взлёт, фото каждые 3с, посадка)")

procs = {}
out_files = {}
for ip, pw, fname in DRONES:
    cmd = [sys.executable, MISSION_FLY, ip, pw, fname]
    log(f"  [{ip}] запуск... ({fname})")
    out_f = open(os.path.join(LOG_DIR, f"drone_{ip.replace('.', '_')}.log"), "w")
    out_files[ip] = out_f
    procs[ip] = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.STDOUT, text=True)

log("Ожидание завершения дронов...")
for ip, proc in procs.items():
    proc.wait(timeout=300)
    status = "OK" if proc.returncode == 0 else f"FAIL (rc={proc.returncode})"
    log(f"  [{ip}] {status}")
    out_files[ip].close()

log("ФАЗА 1 ЗАВЕРШЕНА")

# ── Фаза 2: VLM-анализ ───────────────────────────────────────────────────
log("ФАЗА 2: АНАЛИЗ СНИМКОВ (VLM)")

from scan_fire_kletki import scan_folder, now_iso

fires = scan_folder(SCRIPT_DIR)
log(f"Обнаружено кадров с огнём: {len(fires)}")

# ── Фаза 3: дедупликация и фиксация ──────────────────────────────────────
log("ФАЗА 3: ФИКСАЦИЯ РЕЗУЛЬТАТОВ")

fires = dedup_fires(fires)
log(f"Уникальных клеток с огнём: {len(fires)}")

detection_ts = now_iso()

for f in fires:
    cx, cy = f["cell"]
    log(f"  КЛЕТКА ({cx};{cy})  count={f['count']}  "
        f"confidence={f['confidence']}  dir={f['direction']}  img={f['image']}")

if not fires:
    log("ОГОНЬ НЕ ОБНАРУЖЕН. Миссия завершена.")
    log("=" * 60)
    log("МИССИЯ ЗАВЕРШЕНА (без пожара)")
    log("=" * 60)
    sys.exit(0)

# Фиксация в общей системе ДО отправки ровера
bb_event("fire_detected", {
    "fires": fires,
    "detection_ts": detection_ts,
    "total_fires": len(fires),
})

for f in fires:
    bb_post_message("FIRE_TARGET", f"Пожар в клетке ({f['cell'][0]};{f['cell'][1]})", {
        "cell": f["cell"],
        "count": f["count"],
        "confidence": f["confidence"],
        "detected_at": detection_ts,
    })

# Сохраняем результат в JSON
result_path = os.path.join(SCRIPT_DIR, "fire_detection_result.json")
with open(result_path, "w", encoding="utf-8") as rf:
    json.dump({
        "detection_ts": detection_ts,
        "fires": fires,
    }, rf, indent=2, ensure_ascii=False)
log(f"Результат сохранён: {result_path}")

# ── Фаза 4: ровер ────────────────────────────────────────────────────────
log("ФАЗА 4: ОТПРАВКА КООРДИНАТ РОВЕРУ")

from mission3_zachet import run_command

# Берём первый (единственный) очаг — или все последовательно
primary = fires[0]
fire_x, fire_y = primary["cell"]
fire_count = primary["count"]

log(f"Целевая клетка: ({fire_x};{fire_y}), очагов: {fire_count}")
log("Запуск ровера...")

run_command(fire_x, fire_y, fire_count)

log("Ровер завершил миссию.")

# ── Финал ─────────────────────────────────────────────────────────────────
log("=" * 60)
log("МИССИЯ ЗАВЕРШЕНА УСПЕШНО")
log("=" * 60)