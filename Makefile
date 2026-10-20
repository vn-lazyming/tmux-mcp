.PHONY: dev lint format test

dev:
	uv sync --all-groups

lint:
	uv run ruff format --check . && uv run ruff check . && uv run ty check server.py test_server.py

format:
	uv run ruff format .

test:
	uv run python test_server.py
