# Stress Test Drone City — Makefile
#
# make city-sverk        — реальные дроны (sverk_interfaces)
# make city-sverk-sim    — симулятор (drone-агенты на хосте)
# make down              — остановить
# make reset             — сбросить blackboard
# make logs              — логи

.PHONY: city-sverk city-sverk-sim down reset logs help

city-sverk:
	@test -f .env.sverk || { echo "-> Создай .env.sverk из .env.sverk.example"; exit 1; }
	./city_sverk.sh .env.sverk

city-sverk-sim:
	./city_sverk_sim.sh

down:
	-docker compose -f docker-compose.yml -f docker-compose.city.yml \
	                -f docker-compose.egress.yml \
	                -f docker-compose.sverk-sim.yml down --remove-orphans 2>/dev/null
	pkill -f 'agent/loop.py' 2>/dev/null || true

reset:
	rm -rf blackboard/

logs:
	@echo "=== Coordinator ===" && docker compose -f docker-compose.yml -f docker-compose.city.yml logs --tail=20 coordinator 2>/dev/null
	@echo "=== Drone-1 ===" && tail -20 /tmp/drone-agent-drone-1.log 2>/dev/null || echo "(нет лога)"

help:
	@echo "make city-sverk        — реальные дроны через sverk_interfaces"
	@echo "make city-sverk-sim    — симулятор (drone-агенты на хосте)"
	@echo "make down              — остановить всё"
	@echo "make reset             — сбросить blackboard"
	@echo "make logs              — логи"