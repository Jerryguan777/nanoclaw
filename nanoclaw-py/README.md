# NanoClaw (Python Edition)

Python rewrite of NanoClaw — a personal Claude agent with CLI and HTTP API interfaces.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Run with CLI interface (interactive stdin/stdout)
python -m nanoclaw.main

# Run with HTTP API
python -m nanoclaw.main --mode http

# Run both CLI + HTTP
python -m nanoclaw.main --mode both
```

## Architecture

Same architecture as the TypeScript original, minus WhatsApp:

```
nanoclaw/
├── main.py              # Orchestrator: state, message loop, agent invocation
├── config.py            # Trigger pattern, paths, intervals
├── models.py            # Pydantic models (types)
├── db.py                # SQLite operations
├── router.py            # Message formatting and outbound routing
├── container_runner.py  # Spawns agent containers with mounts
├── group_queue.py       # Per-group concurrency queue
├── ipc.py               # IPC watcher and task processing
├── task_scheduler.py    # Runs scheduled tasks
├── logger.py            # Structured logging (structlog)
└── channels/
    ├── cli.py           # Interactive stdin/stdout channel
    └── http.py          # REST API channel (FastAPI)
```

## Input Channels

### CLI (default)
Interactive terminal — type messages, get responses on stdout.

### HTTP API
REST endpoints at `http://127.0.0.1:8080`:
- `POST /messages` — send a message (returns immediately)
- `POST /messages/stream` — send and stream response (SSE)
- `GET /health` — health check

## Container Runtime

Uses Docker by default (set `CONTAINER_RUNTIME=container` for Apple Container).

```bash
# Build the agent container
docker build -t nanoclaw-agent:latest -f container/Dockerfile container/
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ASSISTANT_NAME` | `Andy` | Bot name and trigger word |
| `CONTAINER_RUNTIME` | `docker` | `docker` or `container` (Apple) |
| `CONTAINER_IMAGE` | `nanoclaw-agent:latest` | Container image name |
| `HTTP_HOST` | `127.0.0.1` | HTTP API bind address |
| `HTTP_PORT` | `8080` | HTTP API port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `ANTHROPIC_API_KEY` | — | API key (passed to container via .env) |

## Tests

```bash
pytest
```
