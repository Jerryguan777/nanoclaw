# NanoClaw Python Rewrite

This directory contains the Python rewrite of NanoClaw (originally TypeScript).

## Conventions

- **Python 3.12+** — use modern syntax (type unions with `|`, `match` statements where appropriate)
- **Type hints everywhere** — `mypy --strict` must pass
- **dataclasses** for data models, **Protocol** for interfaces
- **asyncio** for concurrency (polling loops, subprocess, HTTP proxy)
- **sqlite3 stdlib** for database (synchronous, matching the TS better-sqlite3 model)
- **structlog** for logging
- **pathlib.Path** for all file path operations
- Code comments and docstrings in **English only**
- **src layout** — all packages live under `src/`, install with `uv pip install -e ".[dev]"` for development
- Run `ruff check . && ruff format --check . && mypy --strict src/nanoclaw && pytest --cov --cov-fail-under=80` before committing

## Architecture

See `PLAN.md` for the full rewrite plan and design decisions.

The Python version preserves the same architecture as the TypeScript original:
- Single process with asyncio event loop
- Channel self-registration pattern
- Container-isolated agent execution (Docker)
- File-based IPC with JSON protocol
- SQLite persistence
