# Stress Test Drone City — Makefile
#
# make city-sverk        — реальные дроны (sverk_interfaces)
# make city-sverk-sim    — симулятор (drone-агенты на хосте)
# make clean             — полная очистка перед новым запуском
# make down              — остановить Docker-контейнеры
# make reset             — сбросить blackboard
# make logs              — логи

.PHONY: city-sverk city-sverk-sim clean down reset logs help

city-sverk:
	@test -f .env.sverk || { echo "-> Создай .env.sverk из .env.sverk.example"; exit 1; }
	./city_sverk.sh .env.sverk

city-sverk-sim:
	./city_sverk_sim.sh

clean:
	@echo "=== Остановка Docker ==="
	docker compose -f docker-compose.yml -f docker-compose.city.yml \
	                -f docker-compose.egress.yml down --remove-orphans -v 2>/dev/null || true
	@echo "=== Остановка агентов на дронах ==="
	@for ip in $(shell grep DRONE._HOST .env.sverk 2>/dev/null | cut -d= -f2); do \
		user=$(shell grep DRONE_USER .env.sverk 2>/dev/null | cut -d= -f2); \
		echo "  $$user@$$ip"; \
		ssh -o ConnectTimeout=3 -n $$user@$$ip "pkill -f 'agent/loop.py'" 2>/dev/null || true; \
	done
	@echo "=== Очистка blackboard ==="
	rm -rf blackboard/
	@echo "Готово. Теперь: make city-sverk"

down:
	-docker compose -f docker-compose.yml -f docker-compose.city.yml \
	                -f docker-compose.egress.yml down --remove-orphans 2>/dev/null
	pkill -f 'agent/loop.py' 2>/dev/null || true

reset:
	rm -rf blackboard/

logs:
	@echo "=== Coordinator ===" && docker compose -f docker-compose.yml -f docker-compose.city.yml logs --tail=20 coordinator 2>/dev/null
	@echo "=== Drone-1 ===" && tail -20 /tmp/drone-agent-drone-1.log 2>/dev/null || echo "(нет лога)"

help:
	@echo "make clean             — полная очистка (остановка всего + сброс)"
	@echo "make city-sverk        — реальные дроны"
	@echo "make city-sverk-sim    — симулятор"
	@echo "make logs              — логи"