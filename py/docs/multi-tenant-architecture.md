# Multi-Tenant & Multi-Coworker Architecture

This document explains the design of NanoClaw's multi-tenant, multi-coworker architecture — the reasoning behind the decisions, the trade-offs considered, and the final design.

## Background

NanoClaw started as a single-user personal AI assistant: one person, one agent, one chat group. The architecture was simple — a `RegisteredGroup` mapped 1:1 to a chat context, the agent ran in a container, and module-level Python globals tracked everything.

As the project evolved toward a general-purpose **AI Coworker platform**, we needed to support:

- Multiple **organizations** (tenants) sharing the same infrastructure
- Multiple **AI coworkers** per tenant (operations AI, customer service AI, etc.)
- Multiple coworkers of the same type (one per product line, one per region, etc.)
- Each instance interacting with users across **multiple chat channels** simultaneously
- **Multiple human users** within an organization, each with appropriate access

This document describes how we got from the single-user design to the multi-tenant architecture.

## Core Concepts

### The Entity Hierarchy

```
Tenant (organization)
│
├── Coworker (AI agent)
│   ├── Carries its own config: system prompt, tools, skills, LLM backend
│   ├── Has its own workspace (files, logs)
│   ├── Identified by independent bot identity per channel
│   └── Can operate in multiple chat groups simultaneously
│
├── Conversation (a Coworker's context in a specific chat group)
│   ├── Has independent session/memory
│   └── requires_trigger flag (on for group chats, off for DMs / Web UI)
│
└── User (human team member)
    └── Can interact with multiple Coworkers
```

### Why These Specific Entities?

**Tenant** is straightforward — organizational isolation boundary.

**Coworker** is the central entity — an AI agent with its own identity, configuration (system prompt, tools, skills, LLM backend), workspace, and concurrency limits. We considered splitting this into a "Role template + Coworker instance" model (where Role defines the shared config and Coworker inherits it), but found it to be over-engineering for the current stage: no code uses template reuse, every coworker creation would require a role to exist first, and the extra table/JOIN/CRUD adds complexity with zero benefit. If template reuse is needed later, adding a `roles` table with a FK is a straightforward addition.

**Conversation** emerged from a specific realization: when the same Coworker operates in multiple Telegram groups, the **file workspace should be shared** (same product data, same codebase) but the **conversation memory should be independent** (different groups discuss different topics).

## Design Decisions

### 1. Session Scope: Per-Conversation, Not Per-Coworker

**Decision**: Each Conversation (coworker + chat group combination) has its own session.

**Alternatives considered**:

| Approach | Behavior | Problem |
|----------|----------|---------|
| Per-coworker session | All groups share memory | Group A's discussion leaks into Group B |
| Per-conversation session | Each group has independent memory | Correct isolation ✓ |
| Per-user session | Each human gets their own thread | Breaks group collaboration |

The key insight: a Coworker is like a human employee who works in multiple Slack channels. They remember what was said in each channel separately, but they access the same files and databases regardless of which channel they're in.

```
Coworker "Ops AI - Product Line A"
├── workspace/                    ← shared across all conversations
│   ├── reports/
│   └── data/
├── sessions/
│   ├── {tg_group_1_conv_id}/    ← "In this group we discussed ad campaigns"
│   └── {slack_channel_conv_id}/ ← "Here we talked about inventory"
```

This pairs with Decision 6 (Workspace Isolation) to form the complete sharing model. Together they answer: **"When the same Coworker operates in multiple chat groups, what is shared and what is isolated?"**

| Resource | Scope | Why |
|----------|-------|-----|
| **Workspace files** (code, data, reports) | Per-coworker (shared) | Same coworker manages the same product line regardless of which chat group the request came from |
| **Session/memory** (conversation history) | Per-conversation (isolated) | Different groups discuss different topics; mixing them would confuse the agent |
| **Logs** | Per-coworker (shared) | Operational visibility across all conversations |
| **Shared knowledge** (SOPs, manuals) | Per-tenant (read-only) | All coworkers in a tenant access the same reference materials |

This is a deliberate asymmetry. A common mistake would be to make everything per-conversation (full isolation) or everything per-coworker (full sharing). The split reflects how a human employee actually works: they remember conversations separately, but their desk and files are the same no matter who they're talking to.

### 2. Bot Identity: Per-Coworker, Not Per-Tenant

**Decision**: Each Coworker has its own bot identity per channel type (e.g., its own Telegram bot).

**Why not one bot per tenant?** If a tenant has 3 coworkers (Ops AI, CS AI, Logistics AI) sharing one Telegram bot, users in a group would see one bot and need to use keywords like `@ops help` vs `@cs help` to route messages. This is:
- Confusing for users (which command was it again?)
- Fragile (typos break routing)
- Missing visual identity (no distinct avatar/name per coworker)

With per-coworker bots, users see `@acme_ops_bot` and `@acme_cs_bot` as separate entities in their group. They `@mention` the one they want to talk to, just like mentioning a human colleague. In Telegram specifically, creating bots is nearly free (one BotFather command per bot).

**Trade-off**: More bots to manage. But this is a configuration problem, not an architectural one — the Channel Gateway pattern handles it cleanly.

**Important caveat — token deduplication**: While the *conceptual* model is "one bot per coworker", during migration from single-tenant NanoClaw, multiple coworkers may share the same bot token. The Gateway must deduplicate by token: **one token = one polling connection**, with messages fanned out to all associated bindings. Creating multiple polling instances for the same token causes platform API conflicts (e.g., Telegram's `Conflict: terminated by other getUpdates request`).

### 3. Channel Gateway Pattern

**Decision**: One Gateway per channel type, managing multiple bot instances.

**Problem**: With N coworkers × M channel types, we could have dozens of bot connections. Managing them as individual Channel instances (the original NanoClaw approach) doesn't scale.

**Solution**: A Gateway is a manager object for one channel type:

```
TelegramGateway
├── Bot @acme_ops_bot    (coworker A)
├── Bot @acme_cs_bot     (coworker B)
└── Bot @acme_logistics_bot (coworker C)

SlackGateway
├── App for coworker A
└── App for coworker B
```

The Gateway handles:
- **Token deduplication**: Same token shared by multiple bindings → one polling connection, messages dispatched to all associated bindings
- Connection lifecycle (start/stop/reconnect bots)
- Unified message callback (all bots route through one handler)
- Shared error handling and rate limiting

Individual bots are lightweight — they're just a token + connection. The Gateway owns the complexity.

### 4. OrchestratorState: Structured State Over Globals

**Decision**: Replace module-level global variables with a structured `OrchestratorState` class.

**Before** (single-tenant):
```python
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_queue: GroupQueue = GroupQueue()
_channels: list[Channel] = []
```

These globals work for single-tenant because every key is unique. In multi-tenant, a `group_folder` like `"main"` could exist in every tenant. Flat dicts break.

**After** (multi-tenant):
```python
class OrchestratorState:
    tenants: dict[str, Tenant]
    coworkers: dict[str, CoworkerState]     # coworker_id → state

@dataclass
class CoworkerState:
    config: CoworkerConfig
    conversations: dict[str, ConversationState]
```

Everything is keyed by IDs, indexed by tenant and coworker. No ambiguity, no collisions.

### 5. Three-Level Concurrency Control

**Decision**: Container scheduling enforces limits at three levels: global, per-tenant, and per-coworker.

**Why three levels?**

- **Global limit**: Hardware has finite resources. 20 simultaneous containers might be the server's capacity.
- **Per-tenant limit**: One tenant shouldn't monopolize the platform. If Tenant A has 100 coworkers all active, they shouldn't starve Tenant B.
- **Per-coworker limit**: Within a tenant, one heavily-messaged coworker shouldn't starve the others.

```
Can this container start?
  ├── global_active < global_limit?          (platform capacity)
  ├── tenant_active[tid] < tenant.max?       (fair sharing)
  └── coworker_active[cid] < coworker.max?   (internal fairness)
```

Messages that can't start immediately are queued per-coworker with exponential backoff retry, matching the original NanoClaw `GroupQueue` behavior.

### 6. Workspace Isolation Model

**Decision**: Filesystem isolation at three levels:

```
data/tenants/{tenant_id}/           ← tenant boundary (never crosses)
├── coworkers/{folder}/
│   ├── workspace/                  ← coworker workspace (shared across conversations)
│   ├── sessions/{conversation_id}/ ← per-conversation session data
│   └── logs/                       ← execution logs
├── shared/                         ← cross-coworker read-only knowledge base
└── env/                            ← API credentials
```

Container mounts:
- `/workspace/group` → coworker workspace (read-write)
- `/workspace/shared` → tenant shared knowledge (read-only)
- `/workspace/sessions` → conversation-specific session (read-write)

**Why not per-conversation workspace?** If the same coworker manages ad campaigns from both a Telegram group and a Slack channel, the underlying ad data and scripts are the same. Duplicating the workspace per conversation would cause drift and confusion.

**Why read-only shared space?** The shared knowledge base (SOPs, product manuals, market data) is curated content that coworkers read but shouldn't modify. Write access would create conflicts between coworkers.

### 7. Coworker Configuration

**Decision**: Each Coworker carries its own complete configuration directly — no template indirection.

```python
@dataclass
class CoworkerConfig:
    """Runtime config loaded from coworkers table."""
    name: str                   # coworker's display name (trigger derived from this)
    folder: str                 # workspace path
    system_prompt: str | None   # prompt for the LLM
    tools: list[str]            # tool allowlist
    skills: list[str]           # skill names
    agent_backend: str          # "claude-code" or "pi-mono"
    max_concurrent: int         # concurrency limit
    container_config: dict      # resource overrides (memory, CPU)
    is_admin: bool              # legacy from is_main
```

All fields live on the `coworkers` table. No join, no merge, no template layer. If multiple coworkers need the same config, they're configured independently — duplication is acceptable at this scale and is easier to reason about than a template inheritance system.

**Why not a Role template layer?** We initially designed one (`roles` table with FK from `coworkers`), then removed it because: no code used the template-reuse capability, every coworker creation required a role to exist first, and the extra table added complexity with no current benefit. If template reuse becomes necessary (e.g., a management UI for "create 5 operations AIs from the same template"), adding it back is straightforward.

## How It Evolved from Single-Tenant

The mapping from original NanoClaw concepts:

| Original | Multi-Tenant | What Changed |
|----------|-------------|--------------|
| `RegisteredGroup` | `Coworker` + `Conversation` | Split: "who" separated from "where" |
| `group.folder` | `coworker.folder` | Path: `groups/x/` → `tenants/{tid}/coworkers/x/` |
| `group.trigger` | Derived from `coworker.name` | Trigger text = coworker name; `conversation.requires_trigger` controls on/off |
| `chatJid` | `conversation.channel_chat_id` | 1:N instead of 1:1 |
| `session` (per group) | `session` (per conversation) | Scope narrowed |
| `ASSISTANT_NAME` | `coworker.name` | Global constant → per-entity config |
| `TRIGGER_PATTERN` | From `coworker.name` | Derived from coworker identity |
| `GroupQueue` | Three-level scheduler | Added tenant + coworker limits |
| `Channel` singleton | `ChannelGateway` | One manager per type, multiple bots |
| Module globals | `OrchestratorState` | Structured, indexed by ID |

The migration preserves backward compatibility through:
- `DEFAULT_TENANT = "default"` as parameter default values
- `RegisteredGroup` converter function for legacy code paths
- Existing `tenant_id` columns (added in the PostgreSQL migration step)

## What This Architecture Does NOT Do

These are explicitly deferred to future steps:

- **Approval workflows** (L1/L2/L3 authorization with human-in-the-loop)
- **A2A collaboration** (coworkers delegating tasks to each other)
- **PostgreSQL Row-Level Security** (tenant isolation is currently at application level via WHERE clauses; RLS is a future safety net)
- **Coworker management UI** (entities are managed via SQL or migration scripts, not a web dashboard)
- **Fine-grained user permissions** (the `users` table exists but only basic tenant membership is enforced)

Correspondingly, the database schema does NOT include columns for unimplemented features. Fields like `authorization`, `a2a_config`, or `config_overrides` are added when their features are built, not as placeholders. This keeps the schema honest — every column has code that reads and writes it.

## Data Model

```
                    ┌──────────┐
                    │  Tenant  │
                    └────┬─────┘
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
       ┌───────┐   ┌───────────┐  ┌───────┐
       │ User  │   │ Coworker  │  │Shared │
       └───────┘   │(config +  │  │Space  │
                   │ workspace)│  └───────┘
                   └─────┬─────┘
                         │
               ┌─────────┼──────────┐
               ▼                    ▼
       ┌───────────────┐   ┌──────────────┐
       │ChannelBinding │   │ScheduledTask │
       │(bot identity) │   └──────────────┘
       └───────┬───────┘
               │
               ▼
       ┌──────────────┐
       │ Conversation │─── session (independent memory)
       └──────┬───────┘
              │
              ▼
       ┌──────────────┐
       │   Messages   │
       └──────────────┘
```

### Key Relationships

- **Tenant → Coworker**: One-to-many. Each coworker carries its own complete config (prompt, tools, backend).
- **Coworker → ChannelBinding**: One-to-many (one per channel type). Each binding has bot credentials.
- **ChannelBinding → Conversation**: One-to-many. One bot in multiple chat groups.
- **Conversation → Session**: One-to-one. Independent conversation memory.
- **Conversation → Messages**: One-to-many.
- **Coworker → Workspace**: One-to-one. Shared filesystem across all conversations.

### Database Tables

The schema below includes only columns with defined purpose — no placeholder fields for unimplemented features.

**New multi-tenant tables:**

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `tenants` | `id`, `name`, `max_concurrent_containers`, `last_message_cursor` | Organizational boundary and limits |
| `users` | `id`, `tenant_id`, `name`, `role`, `channel_ids` | Human users (reserved for future permission control) |
| `coworkers` | `id`, `tenant_id`, `name`, `folder`, `agent_backend`, `system_prompt`, `tools`, `skills`, `is_admin`, `container_config`, `max_concurrent` | AI agents with full config |
| `channel_bindings` | `id`, `coworker_id`, `channel_type`, `credentials`, `bot_display_name` | Per-coworker bot identities |
| `conversations` | `id`, `coworker_id`, `channel_binding_id`, `channel_chat_id`, `requires_trigger`, `last_agent_invocation` | Per-chat contexts with independent session; trigger text derived from `coworker.name` |

**Rewritten existing tables (consistent UUID + TIMESTAMPTZ types):**

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `sessions` | `conversation_id` (PK), `tenant_id`, `coworker_id`, `session_id` | Claude/PPI session mapping per conversation |
| `messages` | `id`, `tenant_id`, `conversation_id`, `sender`, `content`, `timestamp` | Chat message history |
| `scheduled_tasks` | `id`, `tenant_id`, `coworker_id`, `conversation_id`, `prompt`, `schedule_type`, `schedule_value`, `next_run` | Cron/interval/once tasks |
| `task_run_logs` | `id`, `task_id`, `run_at`, `duration_ms`, `status`, `result` (truncated), `error` | Task execution history |

**Dropped legacy tables:** `router_state` (replaced by `tenants.last_message_cursor` + `conversations.last_agent_invocation`), `registered_groups` (replaced by coworkers + channel_bindings + conversations), `chats` (replaced by conversations).

## Message Flow

```
1. User sends message in Telegram group
   │
2. TelegramGateway receives via bot @acme_ops_bot
   │  (one token = one polling instance; messages fan out to all associated bindings)
   │
3. Route: binding_id → coworker_id + tenant_id
   │       (binding_id, channel_chat_id) → conversation_id
   │       NOTE: conversation_id (UUID) is the internal routing key,
   │             NOT channel_chat_id (which can collide across coworkers in DMs)
   │
4. Inbound filtering (multi-bot groups):
   │  If requires_trigger and message doesn't match @coworker.name → DROP
   │  (prevents storing @CS_Bot messages in Ops_Bot's conversation)
   │
5. Store message in DB (tenant_id + conversation_id)
   │  + immediately enqueue for processing (event-driven, no polling cursor)
   │
6. Three-level concurrency check:
   │  global_active < 20?
   │  tenant_active < tenant.max_concurrent?
   │  coworker_active < coworker.max_concurrent?
   │
7. Start container:
   │  - Mount coworker workspace (shared)
   │  - Mount conversation session dir (independent)
   │  - Mount tenant shared knowledge (read-only)
   │  - Pass coworker's config (prompt, tools, backend)
   │
8. Agent executes, results flow back via NATS
   │  - send_message MCP tool → immediate delivery (Channel 4)
   │  - ResultMessage → final result (Channel 2)
   │  - Dedup: if same text was already sent via send_message, skip in results stream
   │
9. Gateway sends response via the originating coworker's binding
   │  (routes by coworker_id, NOT by scanning all bindings for matching chat_id)
```

### Critical Routing Rules (learned from implementation)

1. **`conversation_id` is the internal routing key**, not `channel_chat_id`. In Telegram private chats, the same user talking to 3 different bots produces the same `chat_id` (user ID). Only `conversation_id` (UUID) is globally unique.

2. **Inbound filtering before storage**: In multi-bot groups, every bot receives ALL messages. Each bot must filter — only store messages that match its own trigger pattern. Without this, coworkers accumulate irrelevant messages and activate on triggers meant for other coworkers.

3. **Event-driven, not poll-driven**: Inbound messages directly trigger `enqueue_message_check(conversation_id)` after storage. The system does NOT rely on a polling cursor to discover new messages — per-tenant cursors cause race conditions when multiple apps receive the same message at slightly different times.

4. **IPC reply routing by coworker**: When the agent sends a message via `send_message` MCP tool, the reply is routed through the source coworker's own binding, not by scanning all bindings for a matching `chat_id`. This prevents replies going through the wrong bot in private chat scenarios.

5. **Deduplication between output channels**: The agent has two output paths — `send_message` (immediate, via IPC) and the results stream (final, via NATS). The orchestrator tracks texts sent via IPC and skips duplicates in the results stream.

## Container Lifecycle

Containers are **ephemeral** — started on demand, stopped after completion. They are not tied to any specific conversation or binding.

```
Message arrives for Coworker X, Conversation Y
  │
  ├─ Write AgentInitData to NATS KV (includes conversation's session_id)
  ├─ Start container with Coworker X's workspace mounted
  ├─ Container reads KV, resumes session Y's history
  ├─ Agent executes (may span multiple tool calls, minutes of work)
  ├─ Results published to NATS JetStream
  ├─ Container exits
  │
  └─ Session Y updated on disk, ready for next invocation

If another message arrives for Coworker X, Conversation Z (different group):
  │
  ├─ Same workspace mounted (shared files)
  ├─ Different session loaded (Conversation Z's history)
  └─ Queued if coworker concurrency limit reached
```

## Concurrency Model

```
                    ┌─────────────────────────┐
                    │   Global Limit (20)      │
                    │                         │
                    │  ┌───────────────────┐  │
                    │  │ Tenant A (max: 5)  │  │
                    │  │                   │  │
                    │  │  Coworker 1 (2)   │  │
                    │  │  Coworker 2 (2)   │  │
                    │  │  Coworker 3 (1)   │  │
                    │  └───────────────────┘  │
                    │                         │
                    │  ┌───────────────────┐  │
                    │  │ Tenant B (max: 3)  │  │
                    │  │                   │  │
                    │  │  Coworker 4 (2)   │  │
                    │  │  Coworker 5 (1)   │  │
                    │  └───────────────────┘  │
                    └─────────────────────────┘
```

When a container slot is requested:
1. Check global capacity → reject if full
2. Check tenant capacity → queue if tenant is at limit
3. Check coworker capacity → queue if coworker is at limit
4. Start container → increment all three counters
5. Container exits → decrement all three, drain queued work

Queued messages follow exponential backoff with jitter, matching the original NanoClaw retry behavior.

## Operational Considerations

### NATS Startup Cleanup

On process start, the Orchestrator must clean up stale NATS state from previous runs:
- **Delete stale durable consumers** — after an unclean exit, old consumers block new subscriptions
- **Purge stale messages from streams** — old messages replayed on restart would flood users with historical responses

### Schema Migration

When upgrading from single-tenant NanoClaw (Step 4) to multi-tenant (Step 5), the migration script must handle:
- **Existing tables with incompatible schemas** — `CREATE TABLE IF NOT EXISTS` won't modify existing columns; migration must detect the legacy schema, read data, drop old tables, create new tables, and re-insert
- **Coworker name semantics** — legacy `group.name` (e.g., `"all-rolemesh"`) is the group display name, not the assistant name. Migration should use the global `ASSISTANT_NAME` as `coworker.name` so trigger patterns (`@Andy`) continue to work
- **Session data migration** — both `groups/{folder}/` workspace AND `data/sessions/{folder}/.claude/` session state must be moved to the new directory structure; missing session data causes container startup failures
