# Многоагентный координатор дронов — Makefile
#
# make city-mock     — мок-мозги + мок-дроны (разработка без всего)
# make city-sverk    — реальный LLM + мок-дроны (отладка агентов)
# make city-real     — реальный LLM + реальные дроны (полёт!)
# make down          — остановить
# make reset         — сбросить blackboard
# make logs          — логи всех сервисов
# make build         — собрать образы

.PHONY: city city-mock city-sverk city-real down reset logs build hub help

COMPOSE_MOCK = docker compose -f docker-compose.yml -f docker-compose.city.yml
COMPOSE_SVERK = $(COMPOSE_MOCK) -f docker-compose.egress.yml
COMPOSE_REAL = $(COMPOSE_MOCK) -f docker-compose.egress.yml -f docker-compose.real.yml

city: city-mock

city-mock:
	$(COMPOSE_MOCK) up -d --build
	@echo "Hub (мок-мозги + мок-дроны): http://localhost:8095"

city-sverk:
	$(COMPOSE_SVERK) up -d --build
	@echo "Hub (LLM + мок-дроны): http://localhost:8095"

city-real:
	@test -f .env.real || { echo "-> Создай .env.real из .env.real.example и укажи IP дронов"; exit 1; }
	./city_real.sh .env.real

down:
	$(COMPOSE_MOCK) down --remove-orphans 2>/dev/null; true
	-docker compose -f docker-compose.egress.yml down --remove-orphans 2>/dev/null
	-docker compose -f docker-compose.real.yml down --remove-orphans 2>/dev/null

reset:
	rm -rf blackboard/

logs:
	$(COMPOSE_MOCK) logs -f

build:
	$(COMPOSE_MOCK) build

help:
	@echo "make city-mock    — мок-мозги + мок-дроны (без API-ключа, без железа)"
	@echo "make city-sverk   — реальный LLM + мок-дроны (отладка координации)"
	@echo "make city-real    — реальный LLM + реальные дроны (нужен .env.real с IP)"
	@echo "make down         — остановить и удалить контейнеры"
	@echo "make reset        — удалить blackboard (сброс состояния)"
	@echo "make logs         — логи"
	@echo "make build        — сборка образов"