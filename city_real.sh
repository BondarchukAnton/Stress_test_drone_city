#!/usr/bin/env bash
# city_real.sh — запуск агентов с реальными дронами.
#
# Проверяет доступность каждого дрона по BRIDGE_URL (HTTP /healthz).
# Если дрон недоступен — спрашивает, продолжать без него или выйти.
# Координатор получает все URL для централизованного управления через city_mission.py.
#
# Использование:
#   ./city_real.sh                  # читает .env.real или спрашивает IP
#   ./city_real.sh .env.real        # явно указать env-файл
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${1:-.env.real}"

# ---- загрузить IP дронов ----
if [ -f "$ENV_FILE" ]; then
  echo "-> Загружаю $ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
fi

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.city.yml -f docker-compose.egress.yml -f docker-compose.real.yml"

DRONE1_URL="${DRONE1_URL:-}"
DRONE2_URL="${DRONE2_URL:-}"
DRONE3_URL="${DRONE3_URL:-}"
DRONE4_URL="${DRONE4_URL:-}"
ROVER_URL="${ROVER_URL:-}"

# ---- список устройств ----
declare -A DEVICES
[ -n "$DRONE1_URL" ] && DEVICES[drone-1]="$DRONE1_URL"
[ -n "$DRONE2_URL" ] && DEVICES[drone-2]="$DRONE2_URL"
[ -n "$DRONE3_URL" ] && DEVICES[drone-3]="$DRONE3_URL"
[ -n "$DRONE4_URL" ] && DEVICES[drone-4]="$DRONE4_URL"
[ -n "$ROVER_URL"  ] && DEVICES[rover]="$ROVER_URL"

if [ ${#DEVICES[@]} -eq 0 ]; then
  echo "-> В .env.real не указано ни одного устройства."
  echo "   Добавь DRONE1_URL=http://<IP>:9000 (и т.д.) или укажи IP сейчас."
  read -rp "   DRONE1_URL (или Enter для пропуска): " url
  [ -n "$url" ] && DEVICES[drone-1]="$url"
  read -rp "   DRONE2_URL: " url
  [ -n "$url" ] && DEVICES[drone-2]="$url"
  read -rp "   DRONE3_URL: " url
  [ -n "$url" ] && DEVICES[drone-3]="$url"
  read -rp "   DRONE4_URL: " url
  [ -n "$url" ] && DEVICES[drone-4]="$url"
  read -rp "   ROVER_URL: " url
  [ -n "$url" ] && DEVICES[rover]="$url"
fi

if [ ${#DEVICES[@]} -eq 0 ]; then
  echo "! Не указано ни одного устройства. Выход."
  exit 1
fi

# ---- проверка доступности каждого устройства ----
echo ""
echo "=== Проверка дронов ==="
UNAVAILABLE=()
AVAILABLE_IDS=()
AVAILABLE_URLS=()

for dev_id in "${!DEVICES[@]}"; do
  url="${DEVICES[$dev_id]}"
  healthz="${url%/}/healthz"
  echo -n "  $dev_id ($url) ... "
  if curl -sf --max-time 5 "$healthz" > /dev/null 2>&1; then
    echo "OK"
    AVAILABLE_IDS+=("$dev_id")
    AVAILABLE_URLS+=("$url")
  else
    echo "НЕДОСТУПЕН"
    UNAVAILABLE+=("$dev_id")
  fi
done

# ---- если есть недоступные — спросить ----
if [ ${#UNAVAILABLE[@]} -gt 0 ]; then
  echo ""
  echo "! Недоступны: ${UNAVAILABLE[*]}"
  echo ""
  echo "  [c] продолжить с доступными (${AVAILABLE_IDS[*]:-нет})"
  echo "  [a] прервать запуск"
  read -rp "  Выбор [c/a]: " choice
  case "$choice" in
    a|A) echo "Прервано."; exit 0 ;;
    *)   echo "Продолжаю с ${AVAILABLE_IDS[*]:-нет}" ;;
  esac
fi

if [ ${#AVAILABLE_IDS[@]} -eq 0 ]; then
  echo "! Нет доступных устройств. Выход."
  exit 1
fi

# ---- сформировать список скаутов и URL ----
SCOUTS=""
ROVER_URL_FINAL=""
DRONE1_URL_FINAL=""
DRONE2_URL_FINAL=""
DRONE3_URL_FINAL=""
DRONE4_URL_FINAL=""

for i in "${!AVAILABLE_IDS[@]}"; do
  id="${AVAILABLE_IDS[$i]}"
  url="${AVAILABLE_URLS[$i]}"
  if [ "$id" = "rover" ]; then
    ROVER_URL_FINAL="$url"
  else
    [ -n "$SCOUTS" ] && SCOUTS="$SCOUTS,"
    SCOUTS="${SCOUTS}${id}"
    case "$id" in
      drone-1) DRONE1_URL_FINAL="$url" ;;
      drone-2) DRONE2_URL_FINAL="$url" ;;
      drone-3) DRONE3_URL_FINAL="$url" ;;
      drone-4) DRONE4_URL_FINAL="$url" ;;
    esac
  fi
done

# ---- запуск ----
echo ""
echo "=== Запуск агентов ==="
echo "  Скауты: $SCOUTS"
echo "  Ровер:  ${ROVER_URL_FINAL:-нет}"
echo ""

export DRONE1_URL="${DRONE1_URL_FINAL:-}"
export DRONE2_URL="${DRONE2_URL_FINAL:-}"
export DRONE3_URL="${DRONE3_URL_FINAL:-}"
export DRONE4_URL="${DRONE4_URL_FINAL:-}"
export ROVER_URL="${ROVER_URL_FINAL:-}"
export SCOUTS="$SCOUTS"

docker compose $COMPOSE_FILES --env-file "$ENV_FILE" up -d --build

echo ""
echo "Дашборд: http://localhost:${VIZ_HOST_PORT:-8095}"
echo "Остановка: docker compose $COMPOSE_FILES down"