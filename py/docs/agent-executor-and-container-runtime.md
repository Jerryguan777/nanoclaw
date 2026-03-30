# Container Runtime & Agent Executor Architecture

This document describes the two abstraction layers that sit between the Orchestrator and the actual container processes: **ContainerRuntime** (how containers are started and managed) and **AgentExecutor** (how agent work is dispatched). It covers the problems these abstractions solve, the design decisions behind them, and how they enable swapping both the container backend (Docker → Kubernetes) and the agent backend (Claude Code → pi-mono) independently.

## The Problem: Two Things Tangled Together

The original NanoClaw had a single 340-line function, `run_container_agent()`, that did everything:

1. Build volume mount lists
2. Construct `docker run` CLI arguments as string arrays
3. Call `asyncio.create_subprocess_exec("docker", "run", ...)`
4. Write initial input to NATS KV
5. Subscribe to NATS JetStream for streaming results
6. Read stderr from the subprocess pipe
7. Manage timeout with activity-based reset
8. Write execution logs to disk
9. Return the final result

This mixed two unrelated concerns:

- **Container lifecycle** (steps 1-3): How to start, stop, and monitor a container — the mechanics of Docker commands, volume mounts, environment variables, process management
- **Agent orchestration** (steps 4-9): What to do with the container once it's running — write the prompt, subscribe to results, handle timeouts, collect output

These need to change independently:
- Switching from Docker to Kubernetes changes the container lifecycle but not the agent orchestration
- Switching from Claude Code to pi-mono changes the agent inside the container but not how the container itself is managed

## The Solution: Two Layers

```
                        Orchestrator (main.py)
                              │
                    ┌─────────▼──────────┐
                    │   AgentExecutor     │  "What work to do"
                    │   Protocol          │
                    ├────────────────────┤
                    │ContainerAgent-      │  Writes KV, subscribes NATS,
                    │  Executor           │  manages timeout, collects output
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │  ContainerRuntime   │  "How to run containers"
                    │  Protocol           │
                    ├────────────────────┤
                    │  DockerRuntime      │  Docker Engine API (aiodocker)
                    │  (K8sRuntime)       │  Kubernetes Jobs (future)
                    └─────────┬──────────┘
                              │
                         Container
                    (Claude Code or pi-mono)
```

## Layer 1: ContainerRuntime

### What It Does

ContainerRuntime is the low-level layer. It knows how to:
- Check if Docker/Kubernetes is available (`ensure_available`)
- Start a container from a specification (`run`)
- Stop a running container (`stop`)
- Find and clean up orphaned containers from previous runs (`cleanup_orphans`)

It does NOT know about agents, prompts, NATS, sessions, or any business logic.

### The Protocol

```python
class ContainerRuntime(Protocol):
    @property
    def name(self) -> str: ...

    async def ensure_available(self) -> None: ...
    async def run(self, spec: ContainerSpec) -> ContainerHandle: ...
    async def stop(self, name: str, timeout: int = 1) -> None: ...
    async def cleanup_orphans(self, prefix: str) -> list[str]: ...
    async def close(self) -> None: ...
```

### ContainerSpec: What to Run

A pure data object describing everything needed to start a container:

```python
@dataclass(frozen=True)
class ContainerSpec:
    name: str                                    # "nanoclaw-main-1711612800000"
    image: str                                   # "nanoclaw-agent:latest"
    mounts: list[VolumeMount]                    # [{host_path, container_path, readonly}]
    env: dict[str, str]                          # {"NATS_URL": "...", "JOB_ID": "..."}
    user: str | None                             # "1000:1000" (match host uid/gid)
    memory_limit: str | None                     # "512m"
    cpu_limit: float | None                      # 1.0
    extra_hosts: dict[str, str]                  # {"host.docker.internal": "host-gateway"}
    remove_on_exit: bool                         # True (clean up after exit)
    entrypoint: list[str] | None                 # Override image entrypoint
```

The spec is built by pure functions (`build_volume_mounts()`, `build_container_spec()`) in `container/runner.py` — they compute the spec from the coworker's configuration without any I/O.

### ContainerHandle: What You Get Back

A handle to a running container. Intentionally minimal:

```python
class ContainerHandle(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def pid(self) -> int: ...

    async def wait(self) -> int: ...         # wait for exit, return exit code
    async def stop(self, timeout: int) -> None: ...
    def read_stderr(self) -> AsyncIterator[bytes]: ...
```

**Why no stdin/stdout on the handle?** Because NanoClaw's NATS-based IPC (see [nats-ipc-architecture.md](nats-ipc-architecture.md)) replaced stdin/stdout communication. The container reads its input from NATS KV and publishes results to NATS JetStream. The only thing the Orchestrator needs from the container process is:
- Wait for it to exit (`wait`)
- Stop it if needed (`stop`)
- Read stderr for logging (`read_stderr`)

This simplification is a direct consequence of the NATS migration. In the original design, ContainerHandle would have needed `read_stdout_line()` and `write_stdin()` — methods that are hard to implement correctly across Docker API and Kubernetes.

### DockerRuntime: The Current Implementation

Uses `aiodocker` (async Docker Engine API client) instead of subprocess calls:

```python
class DockerRuntime:
    async def ensure_available(self) -> None:
        self._client = aiodocker.Docker()
        await self._client.system.info()    # verify daemon is running

    async def run(self, spec: ContainerSpec) -> DockerContainerHandle:
        config = self._spec_to_config(spec)  # convert to Docker API format
        container = await self._client.containers.create_or_replace(name=spec.name, config=config)
        await container.start()
        return DockerContainerHandle(container, spec.name)

    async def stop(self, name: str, timeout: int = 1) -> None:
        # Idempotent — no error if already stopped
        container = self._client.containers.container(name)
        suppress(DockerError): await container.stop(t=timeout)
        suppress(DockerError): await container.delete(force=True)
```

**Why aiodocker instead of subprocess?**

| Approach | Problem |
|----------|---------|
| `subprocess.run(["docker", "run", ...])` | String-based argument construction is fragile. No structured error handling. Can't stream stderr without pipe management. |
| `asyncio.create_subprocess_exec(...)` | Better, but still string args. Process management is manual. Can't easily translate to Kubernetes. |
| `aiodocker` (Docker Engine API) | Structured config dicts. Native async. Proper error types. Same API shape as Kubernetes client. |

**AutoRemove quirk**: Docker's `AutoRemove` flag (equivalent to `--rm`) races with `container.wait()` — by the time you read the exit code, the container might already be deleted. We skip `AutoRemove` and delete explicitly in `ContainerHandle.stop()`.

### K8sRuntime: The Future

When deploying to Kubernetes, each agent invocation becomes a K8s Job instead of a `docker run`. The `ContainerSpec` maps naturally:

| ContainerSpec field | Docker | Kubernetes Job |
|--------------------:|--------|---------------|
| `name` | `--name` | `metadata.name` |
| `image` | `Image` | `spec.containers[0].image` |
| `mounts` | `HostConfig.Binds` | `spec.volumes` + `volumeMounts` |
| `env` | `Env` | `spec.containers[0].env` |
| `memory_limit` | `HostConfig.Memory` | `resources.limits.memory` |
| `entrypoint` | `Entrypoint` | `spec.containers[0].command` |

The `K8sRuntime` would use `kubernetes-asyncio` to create Jobs, watch for completion, and stream logs. The `ContainerAgentExecutor` above it wouldn't change at all.

Currently `K8sRuntime` is a stub that raises `NotImplementedError`. It will be implemented when Kubernetes deployment is needed.

### Runtime Selection

```python
def get_runtime(runtime_name: str | None = None) -> ContainerRuntime:
    name = runtime_name or os.environ.get("CONTAINER_RUNTIME", "docker")
    if name == "docker":
        return DockerRuntime()
    if name == "k8s":
        raise NotImplementedError("K8sRuntime not yet implemented")
    raise ValueError(f"Unknown runtime: {name}")
```

Set `CONTAINER_RUNTIME=docker` (default) or `CONTAINER_RUNTIME=k8s`. The Orchestrator calls `get_runtime()` once at startup and passes the instance to everything that needs it.

## Layer 2: AgentExecutor

### What It Does

AgentExecutor is the high-level layer. It knows how to:
- Write the agent's initial input to NATS KV
- Start a container (via ContainerRuntime)
- Subscribe to NATS JetStream for streaming results
- Manage activity-based timeout
- Read and log stderr
- Return structured output

It does NOT know how containers are started or stopped — that's ContainerRuntime's job.

### The Protocol

```python
class AgentExecutor(Protocol):
    @property
    def name(self) -> str: ...

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[ContainerHandle, str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput: ...
```

**`on_process` callback**: Called when the container starts. The Orchestrator's scheduler uses this to track active containers for concurrency control and lifecycle management. It receives `(handle, container_name, job_id)`.

**`on_output` callback**: Called for each streaming result block. The Orchestrator forwards these to the user in real-time.

### Why a Single Implementation, Not Claude Code / pi-mono Classes

We analyzed pi-mono (ppi) and found that the Orchestrator-side logic is **identical** for all agent backends:

1. Build volume mounts
2. Build container spec
3. Write initial input to NATS KV
4. Start container
5. Subscribe to NATS results
6. Manage timeout
7. Read stderr
8. Return output

The only differences are:
- Container image name
- Entrypoint command
- Volume mounts (pi-mono doesn't need `.claude/` session directory)
- Extra environment variables

These are all configuration, not logic. So instead of:

```
❌  ClaudeCodeExecutor (copy-pasted orchestration logic)
❌  PiMonoExecutor     (same logic, different config)
```

We have:

```
✅  ContainerAgentExecutor (one class, configured via AgentBackendConfig)
```

### AgentBackendConfig

```python
@dataclass(frozen=True)
class AgentBackendConfig:
    name: str                          # "claude-code" or "pi-mono"
    image: str                         # Docker image
    entrypoint: list[str] | None       # Override entrypoint
    extra_mounts: list[tuple]          # Backend-specific mounts
    extra_env: dict[str, str]          # Backend-specific env vars
    skip_claude_session: bool          # Don't mount .claude/ directory

# Presets
CLAUDE_CODE_BACKEND = AgentBackendConfig(
    name="claude-code",
    image="nanoclaw-agent:latest",
)

PIMONO_BACKEND = AgentBackendConfig(
    name="pi-mono",
    image="ppi-agent:latest",
    entrypoint=["python", "-m", "ppi.coding_agent", "--mode", "nanoclaw"],
    skip_claude_session=True,
)
```

Switching the agent backend is a one-line config change:

```python
# In main.py at startup:
executor = ContainerAgentExecutor(CLAUDE_CODE_BACKEND, runtime, transport, get_groups)
# or:
executor = ContainerAgentExecutor(PIMONO_BACKEND, runtime, transport, get_groups)
```

### How AgentBackendConfig Flows Through

The config affects two pure functions in `container/runner.py`:

**`build_volume_mounts(group, is_main, backend_config)`**:
- If `skip_claude_session` is true, the `.claude/` session mount is excluded
- `extra_mounts` are appended to the mount list

**`build_container_spec(mounts, name, job_id, backend_config)`**:
- `image` comes from config instead of the global `CONTAINER_IMAGE` constant
- `entrypoint` is set if config provides one
- `extra_env` is merged into the environment variables

## How the Two Layers Work Together

A complete agent invocation:

```
1. Orchestrator receives message for a group
        │
2. ContainerAgentExecutor.execute(AgentInput(...))
        │
        ├── build_volume_mounts(group, is_main, backend_config)
        │     → list[VolumeMount]
        │
        ├── build_container_spec(mounts, name, job_id, backend_config)
        │     → ContainerSpec
        │
        ├── Write AgentInitData to NATS KV "agent-init.{job_id}"
        │
        ├── runtime.run(spec)                    ← ContainerRuntime layer
        │     → ContainerHandle
        │
        ├── on_process(handle, name, job_id)     ← scheduler tracks this
        │
        ├── Subscribe to agent.{job_id}.results  ← NATS JetStream
        │
        ├── Start timeout watcher task
        │
        ├── Start stderr reader task             ← handle.read_stderr()
        │
        ├── Wait for container exit              ← handle.wait()
        │     (meanwhile: results arrive via NATS, timeout resets per result)
        │
        ├── Cancel subscriptions and tasks
        │
        ├── Write execution log to disk
        │
        └── Return AgentOutput(status, result, new_session_id)
```

The separation is clean: steps involving `runtime.*` or `handle.*` are the ContainerRuntime layer. Everything else (NATS, timeout, logging, output parsing) is the AgentExecutor layer.

## Platform Helpers

Two platform-specific concerns live as module-level functions in `runtime.py`, independent of any runtime implementation:

### Proxy Bind Host

The credential proxy runs on the host machine. Containers access it via `host.docker.internal`. But what IP should the proxy bind to?

| Platform | Bind to | Reason |
|----------|---------|--------|
| macOS (Docker Desktop) | `127.0.0.1` | Docker Desktop VM routes `host.docker.internal` to the host's loopback |
| WSL (Docker Desktop) | `127.0.0.1` | Same as macOS |
| Linux (native Docker) | docker0 bridge IP | No VM — need the actual bridge interface IP (typically `172.17.0.1`) |
| Fallback | `0.0.0.0` | If docker0 detection fails |

Detection uses `fcntl.ioctl` with `SIOCGIFADDR` on the `docker0` interface — a low-level but reliable approach.

### Host Gateway

On Linux, `host.docker.internal` doesn't resolve by default. Containers need an `--add-host` entry:

```python
def get_host_gateway_extra_hosts() -> dict[str, str]:
    if platform.system() == "Linux":
        return {"host.docker.internal": "host-gateway"}
    return {}  # macOS/Windows: built-in
```

This is injected into every `ContainerSpec.extra_hosts`.

## Design Trade-offs

### Why Protocol, Not ABC?

Python's `typing.Protocol` enables structural subtyping — a class satisfies the protocol if it has the right methods, without inheriting from it. This means:

- `DockerRuntime` doesn't need `class DockerRuntime(ContainerRuntime)` — it just implements the methods
- Tests can use simple mock objects without complex inheritance
- The runtime module doesn't import the implementation modules

ABCs would force inheritance, import dependencies, and registration boilerplate for no practical benefit.

### Why Not a Container Orchestration Library?

Libraries like `docker-py` (synchronous) or full orchestration frameworks (Kubernetes Operator SDK) add complexity we don't need. Our requirements are simple:

- Start a container with some config
- Wait for it to exit
- Read stderr
- Stop it
- Clean up orphans

`aiodocker` gives us exactly this with async support, in ~190 lines of `DockerRuntime`.

### Why Separate build_volume_mounts / build_container_spec from the Executor?

These are **pure functions** — given inputs, they produce outputs with no side effects (aside from creating directories, which is a known wart). Keeping them separate from the executor class means:

- They're easily testable without mocking Docker or NATS
- They can be reused by other code (e.g., dry-run mode, container spec preview)
- The executor class focuses on orchestration flow, not configuration computation

### Why Not stdin/stdout on ContainerHandle?

The original design for ContainerHandle included `read_stdout_line()` and `write_stdin()`. These were removed because:

1. **NATS replaced stdin/stdout** — The agent reads initial input from KV and publishes results to JetStream. No pipe communication needed.
2. **Docker API stdin/stdout is complex** — `aiodocker` requires WebSocket attach for interactive I/O, with its own buffering and framing. Getting reliable line-by-line reading across Docker API and future Kubernetes is harder than it looks.
3. **Simpler handle = easier K8s port** — Kubernetes Jobs don't have a concept of stdin pipe. By not requiring it, the K8sRuntime implementation becomes straightforward.

The one remaining I/O method, `read_stderr()`, is simple log streaming — no framing, no bidirectional communication, just a byte stream for diagnostics.

## Container Naming and Orphan Cleanup

Container names follow the pattern: `nanoclaw-{safe_group_folder}-{epoch_ms}`

Example: `nanoclaw-main-1711612800000`

On startup, the Orchestrator calls `runtime.cleanup_orphans("nanoclaw-")` to find and remove any containers left over from a previous crash. The prefix-based filter catches all NanoClaw containers regardless of which group or job created them.

## Dependency Graph

```
main.py
  │
  ├── get_runtime() → DockerRuntime
  │
  ├── ContainerAgentExecutor(backend_config, runtime, transport, get_groups)
  │     │
  │     ├── runner.build_volume_mounts()     ← pure function
  │     ├── runner.build_container_spec()    ← pure function
  │     ├── runtime.run(spec)               ← ContainerRuntime
  │     └── transport.js.*                  ← NATS (IPC layer)
  │
  └── scheduler.GroupQueue(transport, runtime)
        │
        ├── runtime.stop(name)              ← for shutdown
        └── transport.nc.request(...)       ← for close signal
```

The ContainerRuntime is injected into both the executor (for starting containers) and the scheduler (for stopping them during shutdown). Neither depends on a specific implementation — they program against the Protocol.
