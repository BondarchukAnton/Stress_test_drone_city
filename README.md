# Stress Test Drone City

Мультиагентная система для роя дронов. Два режима запуска:

```bash
make city-sverk        # реальные дроны (sverk_interfaces)
make city-sverk-sim    # симулятор (sverk_interfaces + ROS2 в Docker)
```

## Управление дронами — только sverk_interfaces

Дроны управляются **строго через `sverk_interfaces`**. Никаких HTTP-мостов.
При запуске `make` всегда `USE_SVERK=1`.

Если `sverk_interfaces` недоступен (не установлен пакет) — методы дрона
становятся no-op с предупреждением в лог, агент продолжает работу в режиме
«только VLM» (анализ снимков из map.json для отладки логики).

## Сквозной пайплайн (4 стадии)

```
[STAGE 1/4] Agents debating flight script (observer vs seeker)...
[STAGE 2/4] drone-1: executing flight script observer.py...
[STAGE 3/4] Agents selecting target cell from VLM results...
[STAGE 4/4] Rover executing firefighting loop (count=N)...
[FINISHED] Mission completed successfully!
```

## Режим 1: реальные дроны (`make city-sverk`)

Агенты дронов запускаются **на Raspberry Pi каждого дрона**.
Координатор и хаб — на центральном ПК (в Docker).

### Что нужно настроить

1. **На каждом дроне (Raspberry Pi):**
   - ROS2 + `sverk_interfaces` установлен
   - Клонирован репозиторий в `~/stress_test_drone_city/`
   - SSH-доступ по ключу с центрального ПК

2. **На центральном ПК:**
   ```bash
   cp .env.sverk.example .env.sverk
   # Отредактировать: DRONE1_HOST..DRONE4_HOST, SVERK_API_KEY
   nano .env.sverk
   ```

### Запуск

```bash
make city-sverk
```

Скрипт `city_sverk.sh`:
1. Проверяет SSH-доступ ко всем дронам
2. Копирует `agent/`, `souls/`, `test_fixtures/` на каждый дрон через rsync
3. Запускает `agent/loop.py` на каждом дроне (nohup)
4. Запускает coordinator + hub + rover локально в Docker
5. Дашборд на http://localhost:8095

### Остановка

```bash
make down                             # coordinator + hub + rover (локально)
# На каждом дроне:
ssh pi@192.168.1.10 "pkill -f 'agent/loop.py'"
```

## Режим 2: симулятор (`make city-sverk-sim`)

Все агенты в Docker, подключаются к симулятору на хосте через ROS2 DDS.

### Что нужно настроить

1. **На хосте запущен симулятор** (PX4 SITL + Gazebo с ROS2)
2. **Docker-образ с ROS2 + sverk_interfaces** (см. `agent/Dockerfile.sverk`)
3. `.env` с ключами LLM:
   ```bash
   cp .env.example .env
   # Отредактировать: SVERK_API_KEY
   ```

### Запуск

```bash
make city-sverk-sim
```

Агенты используют `network_mode: host` для прямого доступа к ROS2 на хосте.
ROS2 discovery через `ROS_DOMAIN_ID` (по умолчанию 0).

### Docker-образ с ROS2 для симулятора

Для `make city-sverk-sim` нужен образ с ROS2 + sverk_interfaces.
Создай `agent/Dockerfile.sverk` на основе `osrf/ros:jazzy-desktop`:

```dockerfile
FROM osrf/ros:jazzy-desktop
RUN apt-get update && apt-get install -y python3-pip
RUN pip3 install sverk_interfaces
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/agent
WORKDIR /app
COPY agent/ /app/agent/
COPY souls/ /app/souls/
COPY test_fixtures/ /app/test_fixtures/
CMD ["python3", "/app/agent/loop.py"]
```

Затем в `docker-compose.sverk-sim.yml` укажи `build: { dockerfile: agent/Dockerfile.sverk }`.

## Параметры

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `BODY_TAKEOFF` | `2.0` | Высота взлёта в body-фрейме (м) |
| `ARUCO_LOCK_SEC` | `18` | Пауза для захвата ArUco (сек) |
| `OBSERVER_HOVER_SEC` | `20` | Зависание observer (сек) |
| `CELL_SIZE_M` | `0.8` | Размер клетки (м) |
| `FIELD_ORIGIN_X/Y` | `-2.0` | Смещение поля в ArUco-координатах |
| `ROS_DOMAIN_ID` | `0` | ROS2 domain (для симулятора) |
| `MODEL_PROVIDER` | `sverk` | LLM-провайдер |
| `MODEL_VISION` | `gemma4-vlm` | VLM-модель для анализа фото |

## Карта города

`test_fixtures/city-1/map.json`:

```json
{
  "grid": [[0,0,0,0,0,0], [0,1,0,1,1,0], [0,0,0,0,1,0],
           [0,0,0,0,0,0], [0,1,1,0,1,0], [0,0,0,0,0,0]],
  "cell_size_m": 0.8,
  "charge_zone": [3, 3],
  "water_tower": [1, 3],
  "fire": {"cell": [4, 2], "level": 2}
}
```

## Структура проекта

```
agent/
├── loop.py                     # главный цикл агента
├── brain.py                    # LLM + VLM клиент
├── bb.py                       # общая доска (Blackboard)
├── drone_api.py                # sverk_interfaces (единственный способ управления)
├── rover_control_client.py     # HTTP-клиент API ровера (:8767)
├── observer.py                 # облёт (наблюдатель, ArUco home_cell)
├── seeker.py                   # облёт (ищейка, 3×3 по спирали)
├── rover_executor.py           # ровер (башня → огонь → старт)
└── roles/
    ├── coordinator.py          # фазовая машина
    └── city_missions.py        # CHAT_SCRIPT → FLIGHT → CHAT_TARGET → ROVER → DONE

.env.sverk                       # хосты дронов + ключи (реальный режим)
.env                             # ключи LLM (режим симулятора)
city_sverk.sh                    # деплой на реальные дроны
Makefile                         # city-sverk / city-sverk-sim
```