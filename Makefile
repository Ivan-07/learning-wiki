.PHONY: schemas check-schemas test lint fmt typecheck build install

# 从 domain/contracts.py 导出 JSON Schema 到 schemas/
schemas:
	uv run python scripts/export_schemas.py

# CI 用：重新导出与仓库内 schemas/ 比对，漂移即失败
check-schemas:
	uv run python scripts/export_schemas.py --check

test:
	uv run pytest

lint:
	uv run ruff check python tests scripts
	uv run ruff format --check python tests scripts

fmt:
	uv run ruff check --fix python tests scripts
	uv run ruff format python tests scripts

typecheck:
	uv run mypy

install:
	uv sync
