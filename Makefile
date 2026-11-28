.PHONY: up down db migrate test lint backfill api web
COMPOSE = docker compose -f infra/docker-compose.yml --env-file .env

up:        ; $(COMPOSE) up -d --build
down:      ; $(COMPOSE) down
db:        ; $(COMPOSE) up -d db
migrate:   ; cd py && uv run fairline db migrate
test:      ; cd py && uv run pytest
lint:      ; cd py && uv run ruff check . && uv run ruff format --check .
backfill:  ; cd py && uv run fairline ingest fdcuk --backfill
api:       ; cd py && uv run uvicorn fairline.api.app:app --port 8000 --reload
web:       ; cd web && npm run dev
