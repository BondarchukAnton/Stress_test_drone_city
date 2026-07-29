# Stress Test Drone City — Makefile
#
# make city          — запустить city_missions на мок-мозгах (без API)
# make city-sverk    — запустить city_missions с реальным LLM (sverk/gemma4-vlm)
# make down          — остановить
# make reset         — сбросить blackboard
# make logs          — логи всех сервисов
# make build         — собрать образы

.PHONY: city city-sverk city-mock down reset logs build hub help

COMPOSE = docker compose -f docker-compose.yml -f docker-compose.city.yml

city: city-mock   # default: без реального LLM

city-mock:
	$(COMPOSE) up -d --build
	@echo "Hub: http://localhost:8095"

city-sverk:
	$(COMPOSE) -f docker-compose.egress.yml up -d --build
	@echo "Hub: http://localhost:8095"

down:
	$(COMPOSE) down --remove-orphans
	-docker compose -f docker-compose.egress.yml down 2>/dev/null

reset:
	rm -rf blackboard/

logs:
	$(COMPOSE) logs -f

build:
	$(COMPOSE) build

help:
	@echo "make city           — запустить city_missions (mock LLM)"
	@echo "make city-sverk     — запустить с реальным Sverk LLM"
	@echo "make down           — остановить и удалить контейнеры"
	@echo "make reset          — удалить blackboard (сброс состояния)"
	@echo "make logs           — логи"
	@echo "make build          — сборка образов"
