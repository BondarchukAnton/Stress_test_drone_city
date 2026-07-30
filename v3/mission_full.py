#!/usr/bin/env python3
"""
Полный цикл: запуск 4 дронов → ожидание → анализ фото на огонь.
Использование: python3 mission_full.py
"""

import subprocess
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(SCRIPT_DIR, "img")
MISSION = os.path.join(SCRIPT_DIR, "fly_photo_land_pw.py")
SCAN = os.path.join(SCRIPT_DIR, "scan_fire_kletki.py")

DRONES = [
    ("192.168.1.110", "sverk", "drone110_2_5.jpg"),
    ("192.168.1.124", "sverk", "drone124_5_5.jpg"),
    ("192.168.1.116", "sverk", "drone116_2_2.jpg"),
    ("192.168.1.111", "sverk", "drone111_5_2.jpg"),
]

print("=" * 50)
print("ЗАПУСК ДРОНОВ (взлёт 1.7м → фото → висение 10с → посадка)")
print("=" * 50)

procs = {}
for ip, pw, fname in DRONES:
    cmd = [sys.executable, MISSION, ip, pw, fname]
    print(f"  [{ip}] запуск...  ({fname})")
    procs[ip] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

print("\nОжидание завершения всех дронов...\n")

for ip, proc in procs.items():
    stdout, stderr = proc.communicate(timeout=180)
    status = "OK" if proc.returncode == 0 else f"FAIL (rc={proc.returncode})"
    print(f"  [{ip}] {status}")
    if stderr:
        for line in stderr.strip().splitlines():
            print(f"       stderr: {line}")

print("\n" + "=" * 50)
print("АНАЛИЗ ФОТО НА ОГОНЬ (scan_fire_kletki.py)")
print("=" * 50)

r = subprocess.run(
    [sys.executable, SCAN, IMG_DIR],
    capture_output=True, text=True, timeout=120
)
print(r.stdout)
if r.stderr:
    print(r.stderr, file=sys.stderr)

print("\nМИССИЯ ЗАВЕРШЕНА.")