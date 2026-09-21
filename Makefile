.PHONY: install install-dev run-collector run-bot run-web migrate backfill digest ratings lint fmt typecheck test check

PY ?= python3

install:
	$(PY) -m pip install -e .

install-dev:
	$(PY) -m pip install -e ".[dev]"

run-collector:
	$(PY) -m src.collector

run-bot:
	$(PY) -m src.bot.main

run-web:
	$(PY) -m src.web.app

migrate:
	$(PY) -m src.db.migrate

login:
	$(PY) -m src.collector.login

backfill:
	$(PY) -m src.collector.backfill --days $(or $(DAYS),30)

digest:
	$(PY) -m src.digest.pipeline --date $(DATE)

ratings:
	$(PY) -m src.jobs.ratings $(if $(DATE),--date $(DATE),) $(if $(DAYS),--backfill $(DAYS),)

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest -q

check: lint typecheck test
