# NanoClaw Python Rewrite Plan

## Goal
Rewrite NanoClaw from TypeScript/Node.js to Python, removing WhatsApp dependency, using CLI and HTTP API as input channels. Keep the core architecture: isolated container agents, per-group memory, IPC, scheduled tasks.

---

## Architecture Overview

```
nanoclaw-py/
├── pyproject.toml              # Project config (uv/pip)
├── nanoclaw/
│   ├── __init__.py
│   ├── main.py                 # Entry point, startup, shutdown
│   ├── config.py               # Config & env vars
│   ├── models.py               # Pydantic models (types)
│   ├── db.py                   # SQLite operations
│   ├── router.py               # Message formatting
│   ├── container_runner.py     # Spawn containers, stream parsing
│   ├── group_queue.py          # Concurrency queue
│   ├── ipc.py                  # File-based IPC watcher
│   ├── task_scheduler.py       # Scheduled tasks
│   ├── logger.py               # Structured logging (structlog)
│   └── channels/
│       ├── __init__.py
│       ├── base.py             # Channel protocol/ABC
│       ├── cli.py              # CLI channel (stdin/stdout)
│       └── http.py             # HTTP API channel (FastAPI)
├── container/
│   ├── Dockerfile              # Python-based container
│   ├── agent_runner/
│   │   ├── pyproject.toml
│   │   ├── agent_runner/
│   │   │   ├── __init__.py
│   │   │   ├── main.py         # Agent runner (Claude Agent SDK)
│   │   │   └── ipc_mcp.py      # MCP server (schedule/send tools)
├── groups/                     # Per-group isolated filesystems
│   ├── main/
│   └── global/
├── store/                      # SQLite database
└── data/                       # IPC, sessions, env
```

---

## Phase 1: Foundation (models, config, db, router)

### Step 1.1 — Project scaffolding
- Create `nanoclaw-py/` directory alongside existing code
- `pyproject.toml` with dependencies:
  ```
  python = ">=3.12"
  pydantic = "^2.0"
  structlog = "^24.0"
  aiosqlite = "^0.20"    # async SQLite (or use stdlib sqlite3 + run_in_executor)
  fastapi = "^0.115"
  uvicorn = "^0.34"
  croniter = "^5.0"       # cron expression parsing (replaces cron-parser)
  ```

### Step 1.2 — `config.py`
Port from `src/config.ts`. Direct translation:
- `ASSISTANT_NAME`, `POLL_INTERVAL`, `SCHEDULER_POLL_INTERVAL`
- Path resolution: `PROJECT_ROOT`, `STORE_DIR`, `GROUPS_DIR`, `DATA_DIR`
- Container config: `CONTAINER_IMAGE`, `CONTAINER_TIMEOUT`, `IDLE_TIMEOUT`
- `TRIGGER_PATTERN` regex

**Mapping**: `process.env` → `os.environ`, `path.join` → `pathlib.Path`

### Step 1.3 — `models.py`
Port from `src/types.ts`. Use Pydantic:
```python
class RegisteredGroup(BaseModel):
    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool = True

class NewMessage(BaseModel):
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False

class ScheduledTask(BaseModel): ...
class ContainerInput(BaseModel): ...
class ContainerOutput(BaseModel): ...
```

### Step 1.4 — `db.py`
Port from `src/db.ts` (~585 lines). Use stdlib `sqlite3` (synchronous, matching current behavior):
- Same schema (chats, messages, scheduled_tasks, task_run_logs, router_state, sessions, registered_groups)
- Same function signatures: `init_database()`, `store_message()`, `get_new_messages()`, etc.
- JSON migration logic from `data/` files

**Key decision**: Use sync `sqlite3` (simpler, matches original) and wrap in `asyncio.to_thread()` where needed by the async caller. Don't use `aiosqlite` — it adds complexity for no real benefit given the microsecond-level DB ops.

### Step 1.5 — `router.py`
Port from `src/router.ts` (~47 lines). Trivial:
- `escape_xml()`, `format_messages()`, `strip_internal_tags()`, `format_outbound()`

---

## Phase 2: Container Runner & Process Management

### Step 2.1 — `container_runner.py`
Port from `src/container-runner.ts` (~658 lines). This is the **hardest part**.

Core function: `async def run_container_agent(group, input, on_process, on_output):`

**Key mapping**:
| Node.js | Python |
|---------|--------|
| `spawn('container', args, { stdio: ['pipe','pipe','pipe'] })` | `asyncio.create_subprocess_exec('container', *args, stdin=PIPE, stdout=PIPE, stderr=PIPE)` |
| `container.stdout.on('data', callback)` | `asyncio.create_task(read_stdout_stream(proc.stdout))` |
| `container.stderr.on('data', callback)` | `asyncio.create_task(read_stderr_stream(proc.stderr))` |
| `container.stdin.write(json); container.stdin.end()` | `proc.stdin.write(json_bytes); proc.stdin.close()` |
| `setTimeout(killOnTimeout, ms)` | `asyncio.get_event_loop().call_later(s, kill_fn)` or `asyncio.wait_for()` |
| Promise chain `outputChain.then(...)` | `asyncio.Queue` + consumer task |

**Streaming output parsing** (marker-delimited JSON):
```python
async def _read_stdout(proc, on_output, output_queue):
    buffer = ""
    async for chunk in proc.stdout:
        buffer += chunk.decode()
        while (start := buffer.find(OUTPUT_START_MARKER)) != -1:
            end = buffer.find(OUTPUT_END_MARKER, start)
            if end == -1:
                break
            json_str = buffer[start + len(OUTPUT_START_MARKER):end].strip()
            buffer = buffer[end + len(OUTPUT_END_MARKER):]
            parsed = ContainerOutput.model_validate_json(json_str)
            await output_queue.put(parsed)
```

**Volume mount building** (`build_volume_mounts`): Direct port, replace `fs.mkdirSync` with `Path.mkdir(parents=True, exist_ok=True)`.

**Deadlock prevention**: Must read stdout AND stderr concurrently. Use two `asyncio.Task`s, NOT sequential reads.

### Step 2.2 — `group_queue.py`
Port from `src/group-queue.ts` (~302 lines). Replace Node callbacks with asyncio:

| Node.js | Python |
|---------|--------|
| `setTimeout(fn, delay)` | `asyncio.get_event_loop().call_later()` or `asyncio.sleep()` + task |
| `ChildProcess` reference | `asyncio.subprocess.Process` reference |
| Callback-based drain | `asyncio.Event` / `asyncio.Queue` based drain |

Key methods: `enqueue_message_check()`, `enqueue_task()`, `register_process()`, `send_message()`, `close_stdin()`, `shutdown()`

---

## Phase 3: Input Channels (CLI + HTTP)

### Step 3.1 — `channels/base.py`
Abstract base class replacing `Channel` interface from `src/types.ts`:
```python
class Channel(ABC):
    name: str
    @abstractmethod
    async def send_message(self, jid: str, text: str) -> None: ...
    @abstractmethod
    def is_connected(self) -> bool: ...
    @abstractmethod
    def owns_jid(self, jid: str) -> bool: ...
```

### Step 3.2 — `channels/cli.py` (replaces WhatsApp)
Simple stdin/stdout channel for direct interaction:
```python
class CLIChannel(Channel):
    """Interactive CLI — reads from stdin, writes to stdout."""

    async def start(self):
        # asyncio stdin reader
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, input)
            # Create message, store in DB, trigger processing

    async def send_message(self, jid: str, text: str):
        print(f"\n{text}\n")
```

- JID format: `cli@local` (single user, single "group")
- `requires_trigger = False` (always responds)
- No typing indicator needed

### Step 3.3 — `channels/http.py` (REST API)
FastAPI-based HTTP channel for programmatic access:

```python
app = FastAPI()

@app.post("/messages")
async def send_message(req: MessageRequest):
    """Send a message to a group, returns immediately."""
    # Store message in DB, trigger processing
    return {"status": "queued", "message_id": msg_id}

@app.get("/messages/{group}")
async def get_messages(group: str, since: str = ""):
    """Poll for new messages from a group."""
    return {"messages": [...]}

@app.post("/messages/stream")
async def send_message_stream(req: MessageRequest):
    """Send a message and stream the response via SSE."""
    # Server-Sent Events for real-time output

@app.get("/groups")
async def list_groups():
    """List registered groups."""

@app.post("/groups")
async def register_group(req: GroupRequest):
    """Register a new group."""

@app.get("/tasks")
async def list_tasks(): ...

@app.post("/tasks")
async def create_task(req: TaskRequest): ...
```

Run with: `uvicorn nanoclaw.channels.http:app --port 8080`

---

## Phase 4: IPC & Task Scheduler

### Step 4.1 — `ipc.py`
Port from `src/ipc.ts` (~382 lines). File polling with `os.scandir()`:
- Same structure: poll `data/ipc/{group}/messages/` and `data/ipc/{group}/tasks/`
- Same authorization model: main group has full access, others restricted
- Process types: `message`, `schedule_task`, `pause_task`, `resume_task`, `cancel_task`, `refresh_groups`, `register_group`

```python
async def start_ipc_watcher(deps: IpcDeps):
    while True:
        for group_folder in ipc_base_dir.iterdir():
            if not group_folder.is_dir():
                continue
            await process_messages(group_folder, deps)
            await process_tasks(group_folder, deps)
        await asyncio.sleep(IPC_POLL_INTERVAL / 1000)
```

### Step 4.2 — `task_scheduler.py`
Port from `src/task-scheduler.ts` (~218 lines). Use `croniter` for cron expressions:
```python
from croniter import croniter

async def start_scheduler_loop(deps):
    while True:
        due_tasks = get_due_tasks()
        for task in due_tasks:
            deps.queue.enqueue_task(task.chat_jid, task.id, lambda: run_task(task, deps))
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL / 1000)
```

---

## Phase 5: Container Agent Runner (Inside Container)

### Step 5.1 — `container/agent_runner/main.py`
Port from `container/agent-runner/src/index.ts` (~533 lines).

**Critical dependency**: `claude-code-sdk` (Python package for Claude Agent SDK)

```python
from claude_code_sdk import query, ClaudeCodeOptions

async def main():
    container_input = json.loads(sys.stdin.read())

    async for message in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            cwd="/workspace/group",
            resume=session_id,
            allowed_tools=[...],
            permission_mode="bypassPermissions",
            mcp_servers={"nanoclaw": {...}},
        )
    ):
        if message.type == "result":
            write_output(ContainerOutput(status="success", result=message.result))
```

**MessageStream** (push-based async iterable): Rewrite using `asyncio.Queue`:
```python
class MessageStream:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._done = False

    def push(self, text: str): ...
    def end(self): ...

    async def __aiter__(self): ...
```

**IPC polling** (waitForIpcMessage, drainIpcInput, shouldClose): Direct port with `pathlib` and `os.scandir()`.

### Step 5.2 — `container/agent_runner/ipc_mcp.py`
Port from `container/agent-runner/src/ipc-mcp-stdio.ts` (~280 lines).

Use Python MCP SDK: `mcp` package:
```python
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("nanoclaw")

@server.tool()
async def send_message(text: str, sender: str = None): ...

@server.tool()
async def schedule_task(prompt: str, schedule_type: str, ...): ...

@server.tool()
async def list_tasks(): ...
```

### Step 5.3 — `container/Dockerfile`
New Python-based Dockerfile:
```dockerfile
FROM python:3.12-slim

# System deps (Chromium for browser automation)
RUN apt-get update && apt-get install -y chromium ...

# Install claude-code CLI and agent-browser
RUN npm install -g agent-browser @anthropic-ai/claude-code

# Install Python deps
COPY agent_runner/pyproject.toml ./
RUN pip install .

# Copy source
COPY agent_runner/ ./

ENTRYPOINT ["python", "-m", "agent_runner.main"]
```

**Note**: Container still needs Node.js for `agent-browser` and `claude-code` CLI. The Dockerfile becomes a multi-runtime image (Python + Node.js). This adds ~200MB. Alternative: use `python:3.12-slim` + install node via `nvm` or `nodesource`.

---

## Phase 6: Main Entry Point & Orchestration

### Step 6.1 — `main.py`
Port from `src/index.ts` (~517 lines):

```python
import asyncio
import signal
from nanoclaw.config import *
from nanoclaw.db import init_database, ...
from nanoclaw.channels.cli import CLIChannel
from nanoclaw.channels.http import HTTPChannel
from nanoclaw.container_runner import run_container_agent
from nanoclaw.group_queue import GroupQueue
from nanoclaw.ipc import start_ipc_watcher
from nanoclaw.task_scheduler import start_scheduler_loop

async def main():
    init_database()
    load_state()

    queue = GroupQueue()
    queue.set_process_messages_fn(process_group_messages)

    # Start channels
    cli = CLIChannel(on_message=store_and_route)
    http = HTTPChannel(on_message=store_and_route)

    # Start subsystems as concurrent tasks
    await asyncio.gather(
        cli.start(),
        http.start(),
        start_ipc_watcher(deps),
        start_scheduler_loop(deps),
        message_loop(queue),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

### Step 6.2 — Graceful shutdown
```python
loop = asyncio.get_event_loop()
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
```

---

## Phase 7: Testing & Container Integration

### Step 7.1 — Unit tests
Port existing tests from `src/*.test.ts` to `pytest`:
- `test_router.py` — message formatting
- `test_ipc_auth.py` — IPC authorization
- `test_container_runner.py` — container spawning (mock subprocess)
- `test_group_queue.py` — queue state management
- `test_db.py` — database operations (in-memory SQLite)

### Step 7.2 — Docker support
Replace Apple Container CLI with Docker:
- `container run` → `docker run`
- `container stop` → `docker stop`
- `container ls --format json` → `docker ps --format json`
- `container system start/status` → `docker info`
- Volume mount syntax is compatible (both use `-v host:container`)

This is a **separate concern** from the Python rewrite but unlocks Linux deployment.

### Step 7.3 — Integration test
End-to-end test with CLI channel:
1. Start nanoclaw-py
2. Send message via CLI
3. Verify container spawns, agent responds
4. Verify response appears on stdout

---

## Implementation Order & Estimates

| Phase | What | Files | Lines (approx) |
|-------|------|-------|-----------------|
| 1 | Foundation (models, config, db, router) | 5 | ~500 |
| 2 | Container runner + queue | 2 | ~400 |
| 3 | CLI + HTTP channels | 3 | ~250 |
| 4 | IPC + scheduler | 2 | ~250 |
| 5 | Container agent runner + MCP | 3 | ~350 |
| 6 | Main entry + orchestration | 1 | ~150 |
| 7 | Tests + Docker | ~6 | ~300 |
| **Total** | | **~22** | **~2200** |

Original TypeScript codebase: ~2800 lines across ~12 files. Python version is slightly smaller due to less boilerplate.

---

## Key Drawbacks to Track

1. **Container image size**: Python + Node.js + Chromium ≈ 1.5GB (vs Node.js + Chromium ≈ 1GB)
2. **Claude Agent SDK Python**: `claude-code-sdk` is less mature than JS `@anthropic-ai/claude-agent-sdk`. Verify feature parity for: `query()`, `resume`, `resumeSessionAt`, `permissionMode`, `hooks`, `mcpServers`, `allowedTools`
3. **asyncio deadlock risk**: stdout/stderr must be read concurrently. Forgetting this will deadlock on large outputs
4. **MCP Python SDK**: Verify `mcp` Python package supports stdio transport and Zod-equivalent schema validation
5. **No WhatsApp fallback**: Reintroducing WhatsApp later would require a Node.js microservice bridge
6. **Type safety regression**: Python type hints are not enforced at runtime (consider `beartype` or rely on Pydantic validation at boundaries)

---

## What NOT to Port

- `src/channels/whatsapp.ts` — replaced by CLI + HTTP
- `src/mount-security.ts` — simplify for single-user CLI use (optional, port later if needed)
- `src/whatsapp-auth.ts` — not needed
- WhatsApp-specific JID handling (`@s.whatsapp.net`, `@g.us`)
- QR code authentication
- Baileys dependency and all its transitive deps
