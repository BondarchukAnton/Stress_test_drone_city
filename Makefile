# Makefile

.PHONY: hub agents

hub:
	docker-compose -f docker-compose.agents.yml up -d hub_server

agents:
	docker-compose -f docker-compose.agents.yml up -d agent_loop bridge_node
