#!/usr/bin/env bash
# city_sverk.sh — запуск с реальными дронами через sverk_interfaces.
#
# Разворачивает агентов на Raspberry Pi каждого дрона. Координатор и хаб
# запускаются локально в Docker (или на центральном ПК).
#
# Требования к каждому дрону:
#   - Raspberry Pi с ROS2 + sverk_interfaces
#   - SSH-доступ по ключу
#   - Клонированный репозиторий в ~/stress_test_drone_city/
#
# Использование:
#   cp .env.sverk.example .env.sverk   # указать IP + ключи
#   ./city_sverk.sh .env.sverk
#
# Или одной командой:
#   make city-sverk
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${1:-.env.sverk}"

if [ -f "$ENV_FILE" ]; then
  echo "-> Загружаю $ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
fi

# === Конфигурация дронов ===
DRONE1_HOST="${DRONE1_HOST:-}"
DRONE2_HOST="${DRONE2_HOST:-}"
DRONE3_HOST="${DRONE3_HOST:-}"
DRONE4_HOST="${DRONE4_HOST:-}"
DRONE_USER="${DRONE_USER:-pi}"

declare -A DRONES
[ -n "$DRONE1_HOST" ] && DRONES[drone-1]="$DRONE1_HOST"
[ -n "$DRONE2_HOST" ] && DRONES[drone-2]="$DRONE2_HOST"
[ -n "$DRONE3_HOST" ] && DRONES[drone-3]="$DRONE3_HOST"
[ -n "$DRONE4_HOST" ] && DRONES[drone-4]="$DRONE4_HOST"

if [ ${#DRONES[@]} -eq 0 ]; then
  echo "! В .env.sverk не указан ни один хост дрона (DRONE1_HOST=...)"
  exit 1
fi

SCOUTS=""
for id in "${!DRONES[@]}"; do
  [ -n "$SCOUTS" ] && SCOUTS="$SCOUTS,"
  SCOUTS="${SCOUTS}${id}"
done
echo "Дроны: $SCOUTS"

# === Проверка доступности ===
echo ""
echo "=== Проверка дронов ==="
AVAILABLE_DRONES=()
UNAVAILABLE_DRONES=()
for id in "${!DRONES[@]}"; do
  host="${DRONES[$id]}"
  echo -n "  $id ($host) ... "
  if ssh -o ConnectTimeout=5 -o BatchMode=yes "${DRONE_USER}@${host}" "echo ok" >/dev/null 2>&1; then
    echo "OK"
    AVAILABLE_DRONES+=("$id")
  else
    echo "НЕДОСТУПЕН — пропускаю"
    UNAVAILABLE_DRONES+=("$id")
  fi
done

if [ ${#UNAVAILABLE_DRONES[@]} -gt 0 ]; then
  echo ""
  echo "! Недоступны: ${UNAVAILABLE_DRONES[*]}"
  echo "  Миссия продолжается с доступными дронами."
fi

if [ ${#AVAILABLE_DRONES[@]} -eq 0 ]; then
  echo "! Ни один дрон не доступен. Выход."
  exit 1
fi

# пересобираем DRONES — только доступные
declare -A DRONES_AVAILABLE
for id in "${AVAILABLE_DRONES[@]}"; do
  DRONES_AVAILABLE[$id]="${DRONES[$id]}"
done
# заменяем исходный массив
unset DRONES
declare -A DRONES
for id in "${!DRONES_AVAILABLE[@]}"; do
  DRONES[$id]="${DRONES_AVAILABLE[$id]}"
done

# === Деплой на каждый дрон ===
echo ""
echo "=== Деплой агентов на дроны ==="
REPO_DIR="${DRONE_REPO_DIR:-~/stress_test_drone_city}"
for id in "${!DRONES[@]}"; do
  host="${DRONES[$id]}"
  echo "  $id → $host"

  # копируем agent/ и souls/
  ssh "${DRONE_USER}@${host}" "mkdir -p ${REPO_DIR}/agent ${REPO_DIR}/souls ${REPO_DIR}/test_fixtures"

  rsync -az --delete agent/ "${DRONE_USER}@${host}:${REPO_DIR}/agent/"
  rsync -az --delete souls/ "${DRONE_USER}@${host}:${REPO_DIR}/souls/"
  rsync -az --delete test_fixtures/ "${DRONE_USER}@${host}:${REPO_DIR}/test_fixtures/"

  # запускаем агента (в фоне, логи в файл)
  ssh -n -o StrictHostKeyChecking=no "${DRONE_USER}@${host}" "
    cd ${REPO_DIR} && \
    PYTHONPATH=${REPO_DIR}/agent \
    BLACKBOARD=${REPO_DIR}/blackboard \
    FIXTURES=${REPO_DIR}/test_fixtures \
    AGENT_ID=${id} ROLE=scout TASK=city_missions SCENARIO=city-1 \
    HUB_URL=${HUB_URL:-http://${HUB_HOST:-localhost}:8080} \
    MODEL_PROVIDER=${MODEL_PROVIDER:-sverk} \
    MODEL=${MODEL:-gemma4-vlm} \
    MODEL_VISION=${MODEL_VISION:-gemma4-vlm} \
    SVERK_API_KEY=${SVERK_API_KEY} \
    SVERK_API_BASE=${SVERK_API_BASE:-https://ai.sverk.tech/v1} \
    nohup python3 agent/loop.py > /tmp/drone-agent-${id}.log 2>&1 &
    echo \"pid=\$!\"
  " </dev/null &
done

echo ""
echo "=== Запуск координатора и хаба локально ==="
export SCOUTS="$SCOUTS"
docker compose -f docker-compose.yml -f docker-compose.city.yml -f docker-compose.egress.yml \
  up -d coordinator hub rover

echo ""
echo "Дашборд: http://localhost:8095"
echo ""
echo "Остановка:"
echo "  make down"
echo "  # И на каждом дроне: pkill -f 'agent/loop.py'"