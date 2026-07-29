# Stress Test Drone City

Мультиагентная система для роя дронов. Агенты обсуждают план в чате,
приходят к консенсусу, затем координатор запускает централизованный скрипт
`city_mission.py`, который поднимает все 4 дрона, делает снимки, анализирует
их через VLM (gemma4-vlm) и отправляет ровер тушить пожар.

## Как это работает

```
Агенты (host PC)                 Мост (дрон)               Дрон
─────────────                    ──────────                ────
coordinator ──┐
drone-1 ──────┤                 bridge/mock.py             PX4
   │          ├── blackboard ──── (или реальный мост) ──── камера
   │          │   (общая доска)   :9000                    Nav2
   │          │
   │  Фаза CHAT: агенты обсуждают план
   │
   │  Фаза EXECUTE: coordinator запускает city_mission.py
   │    ├─ takeoff всех дронов на HOVER_ALTITUDE (по умолчанию 2м)
   │    ├─ photograph каждой зоны
   │    ├─ brain.see(VLM_PROMPT, PNG) → gemma4-vlm
   │    ├─ определение клетки с огнём
   │    └─ ровер: старт → водонапорная башня → огонь → старт
```

## Три режима запуска

| Команда | Мозг агентов | VLM (анализ фото) | Мосты | Для чего |
|---------|-------------|-------------------|-------|----------|
| `make city-mock` | шаблоны | map.json | `mock.py` | проверить логику |
| `make city-sverk` | gemma4-vlm | map.json | `mock.py` | отладка координации |
| `make city-real` | gemma4-vlm | **gemma4-vlm** (brain.see) | реальный мост | **полёт на железе** |

## Что нужно

- Docker
- API-ключ к Sverk (или другому провайдеру)
- Реальные дроны: Raspberry Pi с PX4, ROS2, камерой, HTTP-мост на :9000
- Опционально: ровер для тушения

## Быстрый старт (разработка)

```bash
make city-mock       # без ключа, мок-мозги + мок-дроны
make city-sverk      # с LLM + мок-дроны
make down            # остановить
make reset           # сбросить состояние
```

Дашборд: http://localhost:8095

## Запуск с реальными дронами

```bash
# 1. Настроить IP дронов
cp .env.real.example .env.real
# отредактировать .env.real:
#   DRONE1_URL=http://192.168.1.10:9000
#   SVERK_API_KEY=sk-...

# 2. Запустить (с проверкой доступности дронов)
make city-real
```

Скрипт `city_real.sh` проверит `/healthz` каждого дрона. Координатор получит
все URL для централизованного управления через `city_mission.py`.

## Параметры

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `HOVER_ALTITUDE` | `2.0` | Высота зависания дронов (метры) |
| `VLM_FIRE_PROMPT` | встроенный | Промпт для VLM-детекции огня |
| `BRIDGE_TIMEOUT` | `120` | Таймаут HTTP-запросов к мосту (сек) |

## Карта города: city-1/map.json

`test_fixtures/city-1/map.json` — цифровой двойник полигона.

### Обязательные поля

| Поле | Тип | Пример | Описание |
|------|-----|--------|----------|
| `grid` | `int[][]` | `[[0,0,1,...]]` | карта: 0 = дорога, 1 = здание. `grid[y][x]` |

### Опциональные поля

| Поле | Тип | Описание |
|------|-----|----------|
| `charge_zone` | `[x, y]` | стартовая позиция ровера |
| `water_tower` | `[x, y]` | клетка водонапорной башни |
| `fire` | `{cell, level}` | ground truth для мок-режима |
| `drone_pads` | `[[x,y], ...]` | стартовые позиции дронов |

### Пример

```json
{
  "grid": [
    [0, 0, 0, 0, 0, 0],
    [0, 1, 0, 1, 1, 0],
    [0, 0, 0, 0, 1, 0],
    [0, 0, 0, 0, 0, 0],
    [0, 1, 1, 0, 1, 0],
    [0, 0, 0, 0, 0, 0]
  ],
  "charge_zone": [3, 3],
  "water_tower": [1, 3],
  "fire": {"cell": [4, 2], "level": 2}
}
```

## Структура проекта

```
├── .env / .env.example            # ключи LLM
├── .env.real / .env.real.example  # IP дронов
├── city_real.sh                   # проверка дронов + запуск
├── Makefile                       # команды city-mock / city-sverk / city-real
│
├── docker-compose.yml             # основной стек
├── docker-compose.city.yml        # оверлей: city_missions
├── docker-compose.egress.yml      # оверлей: выход в интернет (LLM API)
├── docker-compose.real.yml        # оверлей: агенты → реальные дроны
├── docker-compose.simcity.yml     # оверлей: 1 реальный дрон + мок-ровер
│
├── agent/                         # код агентов
│   ├── Dockerfile
│   ├── loop.py                    # главный цикл агента
│   ├── brain.py                   # LLM + VLM (brain.see для анализа фото)
│   ├── bb.py                      # общая доска (blackboard)
│   ├── bridge_client.py           # HTTP-клиент к мосту дрона
│   ├── city_mission.py            # центральный скрипт миссии
│   └── roles/
│       ├── __init__.py
│       ├── coordinator.py         # фазовая машина
│       ├── city_missions.py       # CHAT → EXECUTE (city_mission.py)
│       ├── city_world.py          # модель города (A* и т.д.)
│       ├── city_executor.py       # исполнение миссий (симуляция)
│       ├── scout.py / rover.py    # базовые роли
│       └── ...
│
├── bridge/                        # мост агент ↔ дрон
│   ├── mock.py                    # мок-мост для разработки
│   └── Dockerfile
│
├── hub/                           # дашборд
│   ├── Dockerfile
│   ├── server.py
│   └── static/index.html
│
├── onboard/                       # образ для Raspberry Pi
│
├── souls/                         # промпты агентов
│   ├── coordinator.md
│   ├── drone-1.md .. drone-4.md
│   └── rover.md
│
├── test_fixtures/
│   └── city-1/
│       └── map.json
│
└── docs/
```

## FAQ

**Q: В мок-режиме VLM вызывается?**
A: В режиме `city-sverk` VLM вызывается на реальные снимки от мок-мостов.
   В `city-mock` — используется ground truth из map.json (мозг `mock`).

**Q: Где задать высоту дронов?**
A: `HOVER_ALTITUDE=2.0` в `.env` или docker-compose.

**Q: Сколько дронов можно?**
A: От 1 до N. Укажи IP в `.env.real`, `city_real.sh` проверит доступность.