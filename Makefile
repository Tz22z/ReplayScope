.PHONY: install lint test integration validate up down

install:
	python -m pip install -e '.[dev]'

lint:
	ruff check src tests scripts
	ruff format --check src tests scripts

test:
	pytest -q

integration:
	REPLAYSCOPE_INTEGRATION=1 pytest -q tests/integration

validate:
	python scripts/validate_claims.py

up:
	docker compose up -d --build postgres migrate --wait

down:
	docker compose down -v

