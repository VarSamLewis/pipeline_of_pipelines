.PHONY: check fmt lint typecheck test

check: lint fmt typecheck test

lint:
	uv run ruff check backend/src/ tests/

fmt:
	uv run ruff format --check backend/src/ tests/

typecheck:
	uv run mypy backend/src/

test:
	uv run pytest
