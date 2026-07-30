#!/usr/bin/env bash
# city_sverk_sim.sh — запуск drone-агентов на хосте для симулятора.
#
# Требования:
#   - Симулятор уже запущен с network_mode:host (docker-compose.sim.hostnet.yml)
#   - ROS2 Humble + sverk_interfaces установлены на хосте
#   - Coordinator + hub уже запущены в Docker
#
# Используется командой: make city-sverk-sim
set -euo pipefail
cd "$(dirname "$0")"

# Загрузить .env
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# Проверить sverk_interfaces
python3 -c "import sverk_interfaces" 2>/dev/null || {
  echo "! sverk_interfaces не установлен. Выполни: pip3 install sverk_interfaces Pillow"
  exit 1
}

# Очистить старый blackboard
rm -rf blackboard/
mkdir -p blackboard/

# Настройки
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export PYTHONPATH="$PWD/agent"
export BLACKBOARD="$PWD/blackboard"
export FIXTURES="$PWD/test_fixtures"
export SCENARIO="${SCENARIO:-city-1}"
export TASK="${TASK:-city_missions}"
export MODEL_PROVIDER="${MODEL_PROVIDER:-sverk}"
export MODEL="${MODEL:-gemma4-vlm}"
export MODEL_VISION="${MODEL_VISION:-gemma4-vlm}"
export SVERK_API_KEY="${SVERK_API_KEY:-}"
export SVERK_API_BASE="${SVERK_API_BASE:-https://ai.sverk.tech/v1}"

echo "=== Drone City: симулятор ==="
echo "ROS_DOMAIN_ID=$ROS_DOMAIN_ID RMW=$RMW_IMPLEMENTATION"
echo ""

# Coordinator + hub в Docker
docker compose -f docker-compose.yml -f docker-compose.city.yml \
               -f docker-compose.egress.yml \
               -f docker-compose.sverk-sim.yml \
               up -d --build coordinator hub rover

sleep 2

# 4 drone-агента на хосте
echo "Запуск drone-агентов..."
AGENTS=(
  "drone-1:"
  "drone-2:px4_1"
  "drone-3:px4_2"
  "drone-4:px4_3"
)

for entry in "${AGENTS[@]}"; do
  agent_id="${entry%%:*}"
  ns="${entry##*:}"
  log="/tmp/drone-agent-${agent_id}.log"
  echo "  $agent_id (ns=$ns) -> $log"
  ROS_NAMESPACE="$ns" AGENT_ID="$agent_id" ROLE=scout \
    nohup python3 agent/loop.py > "$log" 2>&1 &
done

echo ""
echo "Все агенты запущены."
echo "  Дашборд:   http://localhost:${VIZ_HOST_PORT:-8095}"
echo "  Симуляция: http://localhost:6080/vnc.html"
echo ""
echo "Логи агентов:"
echo "  tail -f /tmp/drone-agent-drone-1.log"
echo "  tail -f /tmp/drone-agent-drone-2.log"
echo "  tail -f /tmp/drone-agent-drone-3.log"
echo "  tail -f /tmp/drone-agent-drone-4.log"
echo ""
echo "Остановка: make down"