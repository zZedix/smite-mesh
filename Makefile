.PHONY: help install-panel install-node build-panel build-node build-frontend up down logs status

help:
	@echo "Smite - Tunneling Control Panel"
	@echo ""
	@echo "Commands:"
	@echo "  make install-panel    - Install panel dependencies"
	@echo "  make install-node     - Install node dependencies"
	@echo "  make build-panel      - Build panel Docker image"
	@echo "  make build-node       - Build node Docker image"
	@echo "  make build-frontend   - Build frontend"
	@echo "  make up               - Start all services"
	@echo "  make down             - Stop all services"
	@echo "  make logs             - Show logs"
	@echo "  make status           - Show status"

install-panel:
	cd panel && pip install -r requirements.txt

install-node:
	cd node && pip install -r requirements.txt

build-panel:
	DOCKER_BUILDKIT=1 docker compose build sm-panel

build-node:
	cd node && DOCKER_BUILDKIT=1 docker compose build

build-frontend:
	cd frontend && npm ci --prefer-offline --no-audit --no-fund && npm run build

build-all:
	DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 docker compose build --parallel

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

status:
	smite status || echo "Install CLI: bash cli/install_cli.sh"

