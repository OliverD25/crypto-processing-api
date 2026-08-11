.DEFAULT_GOAL := help
SHELL := /bin/sh

PY ?= python
VENV ?= .venv
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
else
BIN := $(VENV)/bin
endif

TEST_DB_COMPOSE := deploy/docker-compose.test.yml
REGTEST_COMPOSE := deploy/docker-compose.regtest.yml

.PHONY: help
help:
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

.PHONY: venv
venv: ## Create the local virtualenv
	$(PY) -m venv $(VENV)

.PHONY: install
install: ## Install the package plus dev tooling in editable mode
	$(BIN)/python -m pip install -U pip
	$(BIN)/python -m pip install -e ".[dev]"

.PHONY: lint
lint: ## ruff check + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

.PHONY: fmt
fmt: ## Apply ruff formatting and autofixes
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

.PHONY: typecheck
typecheck: ## mypy --strict over src/
	$(BIN)/mypy

.PHONY: test-unit
test-unit: ## Unit tests only (no containers needed)
	$(BIN)/pytest tests/unit

.PHONY: test-int
test-int: ## Integration tests against the dockerized test Postgres
	$(BIN)/pytest tests/integration -m integration

.PHONY: test
test: test-unit test-int ## Full test suite

.PHONY: contracts
contracts: ## Regenerate the committed OpenAPI, webhook and signature contracts
	$(BIN)/python scripts/export_openapi.py
	$(BIN)/python scripts/export_signature_vectors.py
	$(BIN)/python scripts/generate_event_types.py

.PHONY: sdk-python
sdk-python: ## Regenerate the Python SDK core from the committed spec
	openapi-python-client generate \
	  --path docs/reference/openapi.json \
	  --meta none \
	  --config sdks/python/openapi-python-client.yaml \
	  --output-path sdks/python/crypto_processing_client/_generated \
	  --overwrite

.PHONY: sdk-ts
sdk-ts: ## Regenerate the TypeScript SDK core from the committed spec
	cd sdks/typescript && npm ci && npm run generate

.PHONY: sdks
sdks: contracts sdk-python sdk-ts ## Regenerate every generated file both SDKs contain

.PHONY: cov
cov: ## Ledger coverage gate (the CI floor)
	$(BIN)/pytest --cov=src/crypto_processing_api/ledger --cov-report=term-missing --cov-fail-under=85

.PHONY: db-up
db-up: ## Start the throwaway test Postgres (host port 54329)
	docker compose -f $(TEST_DB_COMPOSE) up -d

.PHONY: db-down
db-down: ## Stop and wipe the test Postgres
	docker compose -f $(TEST_DB_COMPOSE) down -v

.PHONY: migrate
migrate: ## Run alembic upgrade head + idempotent asset seed
	$(BIN)/python -m crypto_processing_api.cli migrate

.PHONY: regtest-up
regtest-up: ## Boot the local BTCPay regtest stack
	docker compose -f $(REGTEST_COMPOSE) up -d

.PHONY: regtest-down
regtest-down: ## Tear down the regtest stack and its volumes
	docker compose -f $(REGTEST_COMPOSE) down -v

.PHONY: bootstrap
bootstrap: ## Configure the regtest BTCPay instance (idempotent)
	$(BIN)/python scripts/bootstrap_btcpay.py

.PHONY: mine
mine: ## Mine N regtest blocks, e.g. make mine N=101
	sh scripts/dev/mine.sh $(N)

.PHONY: docker-build
docker-build: ## Build the API image
	docker build -t crypto-processing-api:dev .

.PHONY: ci
ci: lint typecheck test cov ## Everything CI runs
