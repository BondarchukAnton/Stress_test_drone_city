# Guide: запуск с реальными дронами и в симуляторе

Две команды. Переключение — правкой `.env`.

```bash
make city-sverk        # реальные дроны (активно сейчас)
make city-sverk-sim    # симулятор
```

---

## Режим: Реальные дроны (`make city-sverk`)

### Архитектура

```
Центральный ПК (твой ноутбук)
├── Docker: coordinator (фазовая машина)
├── Docker: hub (дэшборд :8095)
└── Docker: rover (ждёт команды через HTTP API ровера)

Дрон-1 (Raspberry Pi)           Дрон-2 (Raspberry Pi)
├── agent/loop.py                ├── agent/loop.py
├── sverk_interfaces             ├── sverk_interfaces
└── ROS2 + PX4                   └── ROS2 + PX4

Дрон-3 (Raspberry Pi)           Дрон-4 (Raspberry Pi)
├── agent/loop.py                ├── agent/loop.py
├── sverk_interfaces             ├── sverk_interfaces
└── ROS2 + PX4                   └── ROS2 + PX4

Ровер (Sverk Rover)
└── HTTP API :8767 (initial-cell, goal-cell, clear)
```

### Что нужно настроить перед запуском

#### 1. На каждом дроне (Raspberry Pi)

- ROS2 + `sverk_interfaces` установлены
- Клонирован репозиторий проекта в `~/stress_test_drone_city/`
- SSH-доступ по ключу с центрального ПК:
  ```bash
  ssh-copy-id pi@192.168.1.10   # повторить для каждого дрона
  ```

#### 2. На центральном ПК

**Файл `.env.sverk`** — IP дронов, ровера, ключ LLM:

| Переменная | Что это | Пример |
|-----------|---------|--------|
| `DRONE1_HOST` | IP первого дрона | `192.168.1.10` |
| `DRONE2_HOST` | IP второго дрона | `192.168.1.11` |
| `DRONE3_HOST` | IP третьего дрона | `192.168.1.12` |
| `DRONE4_HOST` | IP четвёртого дрона | `192.168.1.13` |
| `DRONE_USER` | Пользователь на Pi | `pi` |
| `ROVER_API_URL` | HTTP API ровера | `http://192.168.1.201:8767` |
| `HUB_HOST` | IP твоего ПК | `192.168.1.100` |
| `HUB_URL` | URL хаба для дронов | `http://192.168.1.100:8095` |
| `SVERK_API_KEY` | Ключ LLM | `sk-...` |

**Файл `.env`** — общие параметры поля, сценарий:

| Переменная | Назначение | По умолчанию |
|-----------|-----------|-------------|
| `CELL_SIZE_M` | Размер клетки в метрах | `0.8` |
| `FIELD_ORIGIN_X/Y` | Смещение поля в ArUco | `-2.0` |
| `BODY_TAKEOFF` | Высота взлёта (м) | `2.0` |
| `ARUCO_LOCK_SEC` | Пауза захвата ArUco (сек) | `18` |
| `OBSERVER_HOVER_SEC` | Зависание над клеткой (сек) | `20` |
| `SCENARIO` | Сценарий | `city-1` |
| `VIZ_HOST_PORT` | Порт дэшборда | `8095` |

### Запуск

```bash
# 1. Проверить что все IP правильные
nano .env.sverk

# 2. Одна команда
make city-sverk
```

Скрипт `city_sverk.sh` делает:
1. Проверяет SSH-доступ ко всем дронам
2. Копирует код (`agent/`, `souls/`, `test_fixtures/`) на каждый дрон через rsync
3. Запускает `agent/loop.py` на каждом дроне (nohup, логи в `/tmp/drone-agent-*.log`)
4. Поднимает coordinator + hub + rover в Docker локально

### Дашборд

Открыть в браузере: **http://localhost:8095**

На дэшборде видно:
- **Фазу** миссии (CHAT_SCRIPT → EXECUTE_FLIGHT → CHAT_TARGET → ROVER_EXECUTE → DONE)
- **Карточки агентов** — статус каждого дрона (cell, flying, landed, ошибки)
- **Ленту событий** — takeoff, home_cell, vlm_result (FIRE!), rover navigation
- **Stage-индикатор** — выбранный скрипт, целевая клетка

Цвета фаз:
- 🟣 CHAT_SCRIPT — обсуждение скрипта
- 🟠 EXECUTE_FLIGHT — полёт дронов
- 🟢 CHAT_TARGET — выбор цели
- 🔴 ROVER_EXECUTE — ровер едет
- ✅ DONE — завершено

### Остановка

```bash
make down                             # coordinator + hub + rover (локально)

# На каждом дроне:
ssh pi@192.168.1.10 "pkill -f 'agent/loop.py'"
ssh pi@192.168.1.11 "pkill -f 'agent/loop.py'"
ssh pi@192.168.1.12 "pkill -f 'agent/loop.py'"
ssh pi@192.168.1.13 "pkill -f 'agent/loop.py'"
```

### Результаты миссии

После завершения в папке `blackboard/`:
- `mission_journal.jsonl` — полный журнал (решения → команды → действия)
- `mission_report.json` — итоговый отчёт (diagnostics, ошибки)

---

## Переключение на симулятор

В `.env`:
1. Закомментировать `ROVER_API_URL=http://...`
2. Раскомментировать `ROS_DOMAIN_ID=0`

```bash
make city-sverk-sim
```

Симулятор нужно запустить отдельно (см. `Guide.md` раздел Симулятор).

---

## Переключение на реальные дроны (обратно)

В `.env`:
1. Раскомментировать `ROVER_API_URL=http://192.168.1.201:8767`
2. Закомментировать `ROS_DOMAIN_ID=0`

Проверить `.env.sverk` — IP дронов актуальны.

```bash
make city-sverk
```