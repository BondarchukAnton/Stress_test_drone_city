# Stress Test Drone City — Makefile
#
# make city-sverk        — реальные дроны (sverk_interfaces)
# make city-sverk-sim    — симулятор (sverk_interfaces + ROS2 в Docker)
# make down              — остановить
# make reset             — сбросить blackboard
# make logs              — логи

.PHONY: city-sverk city-sverk-sim down reset logs help

COMPOSE_BASE = -f docker-compose.yml -f docker-compose.city.yml -f docker-compose.egress.yml
COMPOSE_SVERK_SIM = $(COMPOSE_BASE) -f docker-compose.sverk-sim.yml

city-sverk:
	@test -f .env.sverk || { echo "-> Создай .env.sverk из .env.sverk.example"; exit 1; }
	./city_sverk.sh .env.sverk

city-sverk-sim:
	$(COMPOSE_SVERK_SIM) up -d --build
	@echo "Hub (симулятор): http://localhost:8095"

down:
	-docker compose $(COMPOSE_SVERK_SIM) down --remove-orphans 2>/dev/null
	-docker compose -f docker-compose.yml -f docker-compose.city.yml down --remove-orphans 2>/dev/null

reset:
	rm -rf blackboard/

logs:
	$(COMPOSE_SVERK_SIM) logs -f

help:
	@echo "make city-sverk        — реальные дроны через sverk_interfaces"
	@echo "make city-sverk-sim    — симулятор (ROS2 в Docker → sverk_interfaces)"
	@echo "make down              — остановить"
	@echo "make reset             — сбросить blackboard"
	@echo "make logs              — логи"