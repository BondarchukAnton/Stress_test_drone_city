# Stress Test Drone City

Мультиагентная система координации дронов. Агенты на базе LLM (Sverk/Gemma)
принимают решения, обследуют город, тушат пожары, доставляют грузы.

## Архитектура

```
┌─────────────────────────────────────────┐      ┌──────────────────────────────┐
│  Хост-ПК (этот проект)                  │      │  Симулятор / реальное железо  │
│                                         │      │                              │
│  coordinator ──┐                        │      │  PX4 + Gazebo + ROS2          │
│  drone-1..4 ───┤  HTTP   ┌──────────┐   │      │  или                         │
│  rover ────────┼────────▶│ bridge   │───┼──────│  Raspberry Pi + камера + Nav2 │
│                │         │ (mock.py)│   │      │                              │
│  hub ─── порт  │         └──────────┘   │      └──────────────────────────────┘
│  :8095 (дашборд)                        │
└─────────────────────────────────────────┘
```

- **Агенты** (agent/) — принимают решения через LLM, пишут в общую доску (blackboard)
- **Мосты** (bridge/) — HTTP-интерфейс к дрону: сфотографировать, полететь, проанализировать
  - `bridge/mock.py` — мок для разработки (без железа и симулятора)
  - `bridge/ros2/bridge_node.py` — реальный мост для PX4/ROS2 (симулятор или железо)
- **Хаб** (hub/) — дашборд + SSE-стрим событий
- **Борт** (onboard/) — образ для Raspberry Pi дрона (автономный запуск)

## Быстрый старт (разработка, без симулятора)

### 1. Мок-мозги (без API-ключа)

```bash
make city-mock
```

Дашборд: http://localhost:8095

### 2. С реальным LLM (Sverk/Gemma)

```bash
# Настрой .env (уже сделано):
#   MODEL_PROVIDER=sverk
#   MODEL=gemma4-vlm
#   SVERK_API_KEY=sk-...

make city-sverk
```

### 3. Остановка и сброс

```bash
make down     # остановить контейнеры
make reset    # удалить blackboard (сброс состояния)
make logs     # смотреть логи
```

## Запуск с симулятором

Симулятор запускается отдельно из `/home/workerfit/PycharmProjects/simulation/`:

```bash
# 1. Запустить симулятор (в отдельном терминале)
cd /home/workerfit/PycharmProjects/simulation/compose
./sim.sh up novnc

# 2. Запустить агентов (в этом проекте)
make city-sverk
```

Для связи агентов с симулятором нужно заменить мок-мосты на ROS2-мост:
- `bridge/ros2/bridge_node.py` подключается к ROS2-графу симулятора
- В docker-compose нужно указать `BRIDGE_IMAGE=openclaw-bridge-ros2` и
  добавить контейнер в сеть `sverk_sitl`

## Развёртывание на реальный дрон

1. Скопировать `onboard/` на Raspberry Pi дрона
2. Настроить `onboard/.env.drone`:
   ```bash
   HANDLER_ID=team-7
   FLEET=city
   HUB_URL=http://<IP_хаба>:8080
   MODEL_PROVIDER=sverk
   MODEL=gemma4-vlm
   SVERK_API_KEY=sk-...
   ```
3. Запустить:
   ```bash
   docker compose -f docker-compose.drone.yml --env-file onboard/.env.drone up -d
   ```

## Сценарии

| Сценарий | TASK | Описание |
|----------|------|----------|
| `city-1` | `city_missions` | Город 6×6: пожар в доме + доставка между районами |
| `scenario-1` | `safe_passage` | Базовый облёт поля скаутами |
| `painters-1` | `painting` | Дроны-художники рисуют картину |

Сменить сценарий: `SCENARIO=scenario-1 TASK=safe_passage make city-mock`

## Файлы проекта

| Файл | Назначение |
|------|-----------|
| `docker-compose.yml` | Базовый стек (coordinator + дроны + мосты + хаб) |
| `docker-compose.city.yml` | Оверлей для city_missions |
| `docker-compose.egress.yml` | Оверлей: выход агентов в интернет (для real LLM) |
| `.env` | Настройки LLM и сценария (НЕ коммитить) |
| `.env.example` | Шаблон .env |
| `agent/` | Код агентов (loop, brain, роли) |
| `bridge/mock.py` | Мок-мост для разработки |
| `bridge/ros2/` | ROS2-мост для симулятора/железа |
| `hub/` | Дашборд и хаб-сервер |
| `onboard/` | Бортовой образ для дрона |
| `souls/` | Персонажи агентов (YAML frontmatter) |
| `test_fixtures/` | Карты сценариев |
