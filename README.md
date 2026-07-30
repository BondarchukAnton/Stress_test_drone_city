# Stress Test Drone City

Мультиагентная система: дроны ищут пожар, ровер тушит.

```bash
make city-sverk-sim    # симулятор (4 дрона + ровер в Gazebo)
make city-sverk        # реальные дроны (Raspberry Pi)
```

**Подробная инструкция по запуску и переключению между режимами:** [`Guide.md`](Guide.md)

## Управление дронами — только sverk_interfaces

Дроны управляются строго через `sverk_interfaces` (ROS2 + ArUco).
Если библиотека недоступна — логируется предупреждение, агент работает в режиме VLM-only.

## Сквозной пайплайн

```
[STAGE 1/4] Agents debating flight script (observer vs seeker)...
[STAGE 2/4] drone-1: executing flight script observer.py...
[STAGE 3/4] Agents selecting target cell from VLM results...
[STAGE 4/4] Rover executing firefighting loop...
[FINISHED] Mission completed!
```

## Быстрый старт

```bash
# Симулятор (нужен pip3 install sverk_interfaces Pillow + ROS2 Humble)
make city-sverk-sim

# Реальные дроны (нужен .env.sverk с IP дронов)
make city-sverk

# Остановка
make down
```

## Структура проекта

```
agent/
├── loop.py               # главный цикл агента
├── brain.py              # LLM + VLM клиент
├── drone_api.py          # sverk_interfaces (единственный способ управления)
├── observer.py           # облёт (наблюдатель, ArUco home_cell)
├── seeker.py             # облёт (ищейка, 3×3 по спирали)
├── rover_executor.py     # ровер (башня → огонь → старт)
├── fallback_cv.py        # CV-детектор при сбое VLM
├── mission_journal.py    # JSONL-журнал миссии
└── roles/
    └── city_missions.py  # фазы: CHAT_SCRIPT → FLIGHT → CHAT_TARGET → ROVER → DONE

Guide.md                  # подробная инструкция по запуску
Makefile                  # city-sverk / city-sverk-sim
city_sverk_sim.sh         # запуск drone-агентов на хосте (симулятор)
city_sverk.sh             # деплой на реальные дроны
.env                      # конфигурация (симулятор по умолчанию)
.env.sverk                # конфигурация реальных дронов
```