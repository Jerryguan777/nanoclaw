# NATS-Based IPC Architecture

This document describes how NanoClaw's Orchestrator and container Agents communicate using NATS. It covers the problem with the original approach, why NATS was chosen, the 6-channel protocol design, and the NATS primitives used for each channel.

## Background: Why Not Files or stdin/stdout?

The original NanoClaw used three separate mechanisms for Orchestrator-Agent communication:

1. **stdin** — Orchestrator piped initial JSON to the container's standard input
2. **stdout markers** — Agent wrote results between `---NANOCLAW_OUTPUT_START---` / `---NANOCLAW_OUTPUT_END---` markers on standard output
3. **File-based IPC** — Agent wrote JSON files to shared directories, Orchestrator polled them every second

This worked for a single-user tool, but had fundamental problems:

| Mechanism | Problem |
|-----------|---------|
| stdin JSON | Kubernetes Jobs don't support stdin piping. Agent must be able to start and pull its own input. |
| stdout markers | Fragile parsing. Any unexpected output (library warnings, debug prints) breaks the marker detection. |
| File polling | 1-second latency floor. `readdir` on every group directory every second doesn't scale. File system race conditions with `.tmp` + `rename` workaround. Requires shared volumes (ReadWriteMany PVC) in Kubernetes, which is slow and unreliable. |

All three mechanisms also share a deeper problem: they **couple the Orchestrator and Agent to the same host**. The Agent container must be on the same machine to share stdin/stdout pipes and filesystem mounts. This prevents scheduling Agent containers across a Kubernetes cluster.

## Why NATS?

We evaluated three alternatives:

| Option | Pros | Cons |
|--------|------|------|
| **Redis Streams** | Mature, widely deployed, supports consumer groups | Another stateful service to operate; no native KV with TTL in the same system |
| **gRPC** | Strong typing, bidirectional streaming | Requires generating protobuf stubs; heavy for simple JSON messages; Agent container needs a gRPC server |
| **NATS** | Single binary, zero config; JetStream for durability + KV Store + request-reply in one system; Kubernetes-native; <10MB memory | Less widely known than Redis |

NATS won because it **replaces all three original mechanisms with one system** and provides exactly the primitives we need:

- **KV Store** — for initial input and snapshots (point-read semantics)
- **JetStream** — for streaming results, messages, and tasks (ordered, durable, ack'd)
- **Request-reply** — for close signals (confirmed delivery)

A single NATS server binary with `--jetstream` flag covers everything. Local development: `docker run nats:latest --jetstream`. No configuration files, no clustering setup.

## The 6-Channel Protocol

The Orchestrator and Agent communicate over exactly 6 channels. Each channel has a clear direction, purpose, and NATS primitive:

```
 Orchestrator                                Agent (container)
 ────────────                                ──────────────────
                  Channel 1: Initial Input
           ──── KV Store (agent-init) ────→
                  Orch writes before start,
                  Agent reads on startup

                  Channel 2: Streaming Results
           ←─── JetStream (results) ──────
                  Agent publishes result blocks,
                  Orch subscribes

                  Channel 3: Follow-ups + Close
           ──── JetStream (input) ────────→   follow-up messages
           ──── Request-Reply (close) ────→   close signal (confirmed)

                  Channel 4: Agent Messages
           ←─── JetStream (messages) ─────
                  Agent sends messages to users

                  Channel 5: Task Operations
           ←─── JetStream (tasks) ────────
                  Agent creates/manages tasks

                  Channel 6: Snapshots
           ──── KV Store (snapshots) ─────→
                  Orch writes before start,
                  Agent reads via MCP tools
```

### Channel 1: Initial Input

**Direction**: Orchestrator → Agent
**NATS primitive**: KV Store, bucket `agent-init`
**Key**: `{job_id}`

Before starting the container, the Orchestrator writes the Agent's initial configuration:

```json
{
  "prompt": "Analyze my ad performance for ASIN B09XXX",
  "group_folder": "main",
  "chat_jid": "tg:12345",
  "is_main": true,
  "session_id": "abc-123-or-null",
  "is_scheduled_task": false,
  "assistant_name": "Andy",
  "system_prompt": null,
  "role_config": null
}
```

The Agent reads this once on startup, then begins execution.

**Why KV Store instead of stdin?** The Agent container might start on a different node in Kubernetes. It can't receive a stdin pipe across the network. KV is pull-based — the container starts, reads its config, begins work. The Orchestrator writes the KV entry *before* creating the container, so there's no race condition.

**TTL**: 1 hour. Entries are cleaned up automatically even if the container crashes without reading them.

### Channel 2: Streaming Results

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.results`

Each result block is a JSON message:

```json
{
  "status": "success",
  "result": "Here is the ad performance analysis...",
  "newSessionId": "session-uuid-for-resume",
  "error": null
}
```

The Agent publishes multiple result blocks during execution (streaming). The Orchestrator subscribes and forwards each to the user in real-time. The last message before container exit is the definitive final result.

**Why JetStream instead of stdout markers?** JetStream messages are structured JSON — no marker parsing, no corruption from stray output. Messages are ordered and acknowledged. If the Orchestrator restarts mid-stream, it can replay unacknowledged messages.

**Activity-based timeout**: Each received result resets the Orchestrator's timeout timer. If no result arrives within the timeout period, the container is stopped.

### Channel 3: Follow-up Messages + Close Signal

**Direction**: Orchestrator → Agent
**NATS primitives**: JetStream + Request-Reply

This channel handles two distinct needs:

#### Follow-up messages (JetStream)

**Subject**: `agent.{job_id}.input`

When a user sends additional messages while the Agent is still running (idle-waiting for input), the Orchestrator publishes them as JetStream messages:

```json
{"type": "input", "text": "Also check ASIN B08YYY"}
```

The Agent subscribes to this subject. In the Claude Code backend, follow-up messages are fed into the `MessageStream` that the SDK's `query()` function consumes. The Agent sees them as continuation of the conversation.

#### Close signal (Request-Reply)

**Subject**: `agent.{job_id}.close`

When the Orchestrator decides to close an idle container (timeout, preemption by a higher-priority task), it needs confirmed delivery:

```python
# Orchestrator side:
response = await nc.request(f"agent.{job_id}.close", b"close", timeout=5.0)
# response confirms Agent received and is shutting down

# Agent side:
async def handle_close(msg):
    await msg.respond(b"ack")
    # initiate graceful shutdown — end the message stream, let current work finish
```

**Why request-reply for close?** A JetStream publish is fire-and-forget from the sender's perspective — the Orchestrator wouldn't know if the Agent actually received the close signal. With request-reply, the Orchestrator waits for acknowledgment before cleaning up resources.

### Channel 4: Agent Messages to Users

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.messages`

The Agent can proactively send messages to users (progress updates, notifications) via the `send_message` MCP tool:

```json
{
  "type": "message",
  "chatJid": "tg:12345",
  "text": "Found 3 underperforming campaigns. Analyzing each...",
  "groupFolder": "main",
  "timestamp": "2026-03-28T10:00:00+00:00",
  "sender": null
}
```

The Orchestrator subscribes to `agent.*.messages` (wildcard for all job IDs) with a durable consumer `orch-messages`. It validates authorization (main group can message any chat; non-main groups only their own) and routes the message to the appropriate channel (Telegram, Slack, etc.).

### Channel 5: Task Operations

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.tasks`

The Agent can create and manage scheduled tasks via MCP tools:

```json
{
  "type": "schedule_task",
  "taskId": "task-1711612800000-a1b2c3",
  "prompt": "Daily ad performance check",
  "schedule_type": "cron",
  "schedule_value": "0 8 * * *",
  "context_mode": "group",
  "targetJid": "tg:12345",
  "groupFolder": "main"
}
```

Supported operations: `schedule_task`, `pause_task`, `resume_task`, `cancel_task`, `update_task`, `refresh_groups`, `register_group`.

The Orchestrator subscribes to `agent.*.tasks` with durable consumer `orch-tasks`. Authorization is enforced: main group can manage any task, non-main groups only their own.

### Channel 6: Snapshots

**Direction**: Orchestrator → Agent
**NATS primitive**: KV Store, bucket `snapshots`
**Keys**: `{group_folder}.tasks`, `{group_folder}.groups`

Before starting a container, the Orchestrator writes current state snapshots:

- **Tasks snapshot**: All scheduled tasks (main sees all; non-main sees only their own)
- **Groups snapshot**: Available groups for activation (main only)

The Agent reads these via the `list_tasks` and `list_groups` MCP tools. The data is point-in-time — not a live stream. This is appropriate because the Agent needs to query current state, not subscribe to changes.

**Why KV Store instead of JetStream?** Snapshots are "what is the current state right now?" — latest-value-wins semantics. JetStream is for event streams where order and history matter. KV is simpler and semantically correct for this use case.

## Subject Naming Convention

```
agent.{job_id}.results     # Channel 2
agent.{job_id}.input       # Channel 3 (follow-ups)
agent.{job_id}.close       # Channel 3 (close signal)
agent.{job_id}.messages    # Channel 4
agent.{job_id}.tasks       # Channel 5
```

**Why `job_id` instead of `group_id`?** A single coworker (group) might have multiple concurrent containers — one handling messages, another running a scheduled task. `job_id` is unique per container invocation, ensuring precise routing. It's generated as `{group_folder}-{uuid_hex[:12]}` at container creation time.

The JetStream stream `agent-ipc` captures all subjects matching `agent.*.(results|input|messages|tasks)`. The close signal uses Core NATS (not JetStream) because request-reply doesn't need persistence.

## NATS Infrastructure

### JetStream Stream

```python
StreamConfig(
    name="agent-ipc",
    subjects=["agent.*.results", "agent.*.input", "agent.*.messages", "agent.*.tasks"],
    max_age=3600.0,  # 1 hour TTL — auto-cleanup
)
```

Uses LIMITS retention (not WorkQueue) because both the Orchestrator and Agent subscribe to different subjects within the same stream. WorkQueue would only allow one consumer per subject.

### KV Buckets

```python
KeyValueConfig(bucket="agent-init", ttl=3600.0)   # Channel 1
KeyValueConfig(bucket="snapshots",  ttl=3600.0)   # Channel 6
```

1-hour TTL on both. Entries self-clean even if the consumer crashes.

### Durable Consumers

The Orchestrator creates two durable JetStream consumers:

- `orch-messages` — subscribes to `agent.*.messages` (Channel 4)
- `orch-tasks` — subscribes to `agent.*.tasks` (Channel 5)

Durable consumers survive Orchestrator restarts. Unprocessed messages are replayed on reconnection.

Channels 2 (results) and 3 (input) use ephemeral subscriptions scoped to a specific `job_id` — created when a container starts, unsubscribed when it exits. These don't need durability because they're tied to a single container's lifecycle.

## Container Environment Variables

The Orchestrator passes two environment variables to every container:

| Variable | Example | Purpose |
|----------|---------|---------|
| `NATS_URL` | `nats://host.docker.internal:4222` | NATS server address |
| `JOB_ID` | `main-a1b2c3d4e5f6` | Unique per container invocation |

The Agent uses `NATS_URL` to connect and `JOB_ID` as the routing key for all 6 channels.

`host.docker.internal` resolves to the Docker host's IP, allowing the container to reach the NATS server running on the host. This follows the same pattern used for the credential proxy.

## How Each Side Connects

### Orchestrator Side (`NatsTransport`)

```python
class NatsTransport:
    async def connect(self) -> None:
        self._nc = await nats.connect(url)
        self._js = self._nc.jetstream()
        # Create stream and KV buckets (idempotent)
        await self._js.add_stream(StreamConfig(name="agent-ipc", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="agent-init", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="snapshots", ...))
```

Initialized once at startup, shared across all container invocations.

### Agent Side (in `agent_runner/main.py`)

```python
nc = await nats.connect(NATS_URL)
js = nc.jetstream()

# Channel 1: Read initial input
kv = await js.key_value("agent-init")
entry = await kv.get(JOB_ID)
init_data = AgentInitData.deserialize(entry.value)

# Channel 3: Subscribe to follow-ups + close
input_sub = await js.subscribe(f"agent.{JOB_ID}.input")
close_sub = await nc.subscribe(f"agent.{JOB_ID}.close", cb=handle_close)

# Channels 4, 5: Publish via MCP tools
# (fire-and-forget, using asyncio.ensure_future for non-blocking)
```

Each container creates its own NATS connection on startup and closes it on exit.

## MCP Tools as the Agent-Side IPC Interface

The Agent doesn't directly call NATS publish functions. Instead, IPC operations are exposed as **MCP tools** that the LLM can invoke:

| MCP Tool | Channel | NATS Subject |
|----------|---------|-------------|
| `send_message` | 4 | `agent.{job_id}.messages` |
| `schedule_task` | 5 | `agent.{job_id}.tasks` |
| `pause_task` | 5 | `agent.{job_id}.tasks` |
| `resume_task` | 5 | `agent.{job_id}.tasks` |
| `cancel_task` | 5 | `agent.{job_id}.tasks` |
| `update_task` | 5 | `agent.{job_id}.tasks` |
| `register_group` | 5 | `agent.{job_id}.tasks` |
| `list_tasks` | 6 | KV `snapshots.{group}.tasks` |

These tools are registered as an **in-process MCP server** using Claude Agent SDK's `create_sdk_mcp_server()`. This means:
- No separate process for the MCP server
- Tool calls are direct Python function calls
- The LLM sees them as regular tools with JSON Schema parameters

This design keeps the NATS communication logic contained in one place (`ipc_mcp.py`) while the LLM interacts with clean, documented tool interfaces.

## Authorization Model

Not all Agent containers have the same permissions. The Orchestrator enforces authorization based on the `is_main` flag and `group_folder`:

| Operation | Main group | Non-main group |
|-----------|-----------|---------------|
| Send message to any chat | ✅ | Only to own chat |
| Schedule task for any group | ✅ | Only for own group |
| Pause/resume/cancel any task | ✅ | Only own group's tasks |
| Register new groups | ✅ | ❌ |
| Refresh group metadata | ✅ | ❌ |
| Read all tasks (via snapshot) | ✅ All tasks | Only own tasks |
| Read all groups (via snapshot) | ✅ All groups | ❌ Empty list |

Authorization is enforced on the **Orchestrator side** when processing incoming Channel 4 and 5 messages. The Agent container is not trusted — even if it sends a `register_group` request, the Orchestrator rejects it unless `is_main` is true.

## Error Handling and Reliability

### Container crashes

If a container crashes without publishing results, the Orchestrator's timeout fires (default: 5 minutes). The container is cleaned up and the error is reported to the user.

KV entries (`agent-init`, `snapshots`) have 1-hour TTL — they self-clean even without explicit deletion.

### NATS server restart

If NATS restarts, the Orchestrator reconnects (3 retry attempts with 1-second wait). JetStream streams and KV data are persisted to disk, so no messages are lost.

Durable consumers (`orch-messages`, `orch-tasks`) resume from their last acknowledged position after reconnection.

### Orchestrator restart

If the Orchestrator restarts while containers are running:
- Running containers continue (they're independent Docker processes)
- Unacknowledged messages in `orch-messages` and `orch-tasks` are replayed
- Orphan containers are cleaned up on next startup via `DockerRuntime.cleanup_orphans("nanoclaw-")`
- Channel 2 (results) subscriptions for in-flight jobs are lost — those containers will time out

### Message ordering

JetStream preserves message order within a subject. Channel 2 results arrive in the order the Agent published them. Channel 4 and 5 messages are processed in order per durable consumer.

## Development Setup

```yaml
# docker-compose.dev.yml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"   # Client connections
      - "8222:8222"   # HTTP monitoring dashboard
    command: ["--jetstream", "--store_dir=/data"]
    volumes:
      - nats-data:/data

volumes:
  nats-data:
```

The monitoring dashboard at `http://localhost:8222` shows active connections, streams, consumers, and KV buckets — useful for debugging IPC issues.

Environment variable: `NATS_URL=nats://localhost:4222` (default, no configuration needed for local development).
