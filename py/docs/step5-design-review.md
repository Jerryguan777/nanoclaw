# Step 5 Design Review: Issue #12 Design Flaws Found During Implementation

This document summarizes the design flaws discovered in GitHub Issue #12 (Multi-tenant Multi-coworker Architecture) during implementation and manual testing.

## Overview

The issue's **data model and concept hierarchy** (Tenant -> Role -> Coworker -> ChannelBinding -> Conversation) was well-designed. The core flaws concentrated in three areas:

1. **Same external ID mapping to multiple internal entities** (same token across bindings, same chat_id across conversations, same group across bots)
2. **Migration only considered data structure, not runtime compatibility** (schema upgrade, session data, coworker name semantics)
3. **Message flow race conditions and duplication** (multi-app timing differences, dual-channel duplicate sends, NATS stale message replay)

---

## Flaw 1: No Upgrade Path for Existing Databases

**Issue assumed**: Clean install or one-step migration.

**Actual problem**: `init_database()` runs on an existing Step 4 database where `messages`, `sessions`, `scheduled_tasks`, `task_run_logs` tables already exist with incompatible schemas. `CREATE TABLE IF NOT EXISTS` won't modify existing tables, but `CREATE INDEX` referencing new columns fails.

**Fix**: Detect legacy tables (check for `chat_jid` column in `messages`), skip new-format table creation until migration script runs. Migration script changed to: read old data -> drop old tables -> create new tables -> write data.

**Design should have included**: Explicit schema version detection and staged upgrade strategy.

---

## Flaw 2: Gateway "One Bot Per Coworker" Model Doesn't Hold

**Issue design**:
> TelegramGateway manages multiple Telegram bots (one per coworker)

**Actual problem**: After migration, multiple coworkers share the same bot token (old system had one global bot). Creating independent polling instances per binding causes Telegram API `Conflict: terminated by other getUpdates request`.

**Fix**: `TelegramGateway` deduplicates by token -- same token gets one `_BotInstance`, messages dispatched to all associated bindings.

**Design should have included**: "One token = one polling instance, messages fan out to all associated bindings" architecture.

---

## Flaw 3: `channel_chat_id` as Routing Key Has Collisions

**Issue design**: `conversations` dict keyed by `channel_chat_id`, message loop and queue also use `chat_id` as scheduling key.

**Actual problem**: In Telegram private chats, same user talking to 3 different bots has the same `chat_id` (user ID, e.g. `8326882447`). Three coworkers' conversation dicts all have the same key. `_process_conversation_messages(chat_id)` iterates and picks only the first matching coworker.

**Fix**: Use `conversation_id` (UUID) as dict key and queue key. `_process_conversation_messages` parameter changed to `conversation_id`.

**Design should have included**: `conversation_id` is the globally unique routing identifier; `channel_chat_id` is only an external identifier, not suitable as internal routing key.

---

## Flaw 4: IPC Message Reply Routing Flaw

**Issue design**: Agent sends messages via `send_message` MCP tool using `chatJid` for routing. Orchestrator's `_send_to_chat(chatJid)` scans all coworkers and picks the first match.

**Actual problem**: When multiple coworkers share the same `chatJid` (private chat scenario), replies go to the wrong bot.

**Fix**: IPC handler uses source coworker's binding directly (`_send_via_coworker`), no more blind scanning.

**Design should have included**: IPC messages should carry `coworkerId`; routing should prefer coworker's own binding, with `chatJid` only as target address.

---

## Flaw 5: Multi-Bot Same-Group Message Fan-out Not Considered

**Issue concept diagram**:
> Coworker "Ops AI" -> tg group 1001
> Coworker "CS AI" -> tg group 1001 (same group, different bot)

**Actual problem**: Every bot in the group receives ALL messages. `_handle_incoming` stored indiscriminately, causing `@CS Bot hello` to be stored in Ops Bot's conversation. Accumulated irrelevant messages with matching triggers from other coworkers caused wrong coworkers to activate.

**Fix**: `_handle_incoming` filters before storing -- for group messages requiring trigger, drop if content doesn't match this coworker's trigger pattern.

**Design should have included**: Explicit "inbound message filtering" strategy -- in multi-bot group scenarios, each bot only stores messages relevant to itself.

---

## Flaw 6: `send_message` MCP Tool and Results Stream Duplicate Delivery

**Issue design**: Did not discuss deduplication between agent's two output paths.

**Actual problem**: Agent sends real-time messages via `send_message` tool (NATS IPC path), and `ResultMessage` also comes through results stream. Same text delivered twice.

**Fix**: Track IPC-sent texts; `_on_output` skips if same text was already sent via IPC.

**Design should have included**: `send_message` is the intermediate message channel, `results stream` is the final result channel, and explicit deduplication mechanism between the two.

---

## Flaw 7: Three Migration Script Omissions

| Omission | Issue Design | Actually Needed |
|----------|-------------|-----------------|
| **Coworker name** | Used `group.name` (e.g. `all-rolemesh`) | Should use global `ASSISTANT_NAME` (e.g. `Adam`), otherwise trigger pattern won't match |
| **`.claude` session data** | Only migrated `groups/{folder}/` -> `workspace/` | Also need to migrate `data/sessions/{folder}/.claude/`, otherwise container reports `.claude.json not found` |
| **`system_prompt` passthrough** | `CoworkerConfig` has `system_prompt` field | `_run_agent` didn't pass it to `AgentInput`, agent container never receives it |

---

## Flaw 8: Per-Tenant Cursor Unsuitable for Multi-App Real-Time Scenarios

**Issue design**: `tenants.last_message_cursor` replaces `router_state`'s `last_timestamp`.

**Actual problem**: Same group has 3 Slack apps (Socket Mode), each receiving messages at microsecond-level differences. One app's newer message advances the cursor, another app's older message gets skipped permanently.

**Fix**: `_handle_incoming` immediately `enqueue_message_check` after storing, not relying on polling cursor to discover new messages.

**Design should have included**: Event-driven (push) over polling (pull) -- inbound messages should directly trigger processing rather than waiting for cursor polling.

---

## Flaw 9: NATS Operations Issues Not Considered

**Issue did not mention**:
- Stale durable consumers after unclean process exit -> new process cannot subscribe
- Old messages in NATS stream replayed on restart -> users receive flood of historical messages

**Fix**: `delete_consumer` + `purge_stream` on startup.

---

## Summary Table

| # | Flaw | Category | Root Cause |
|---|------|----------|------------|
| 1 | Schema upgrade conflict | Migration | No version detection |
| 2 | Telegram token dedup | Gateway | Assumed 1:1 token:coworker |
| 3 | chat_id routing collision | Routing | External ID as internal key |
| 4 | IPC reply to wrong bot | Routing | No coworker context in routing |
| 5 | Multi-bot message fan-out | Message flow | No inbound filtering |
| 6 | Duplicate agent replies | Message flow | Dual output paths |
| 7 | Migration omissions (x3) | Migration | Incomplete scope |
| 8 | Per-tenant cursor skip | Message flow | Polling vs event-driven |
| 9 | NATS stale state | Operations | No startup cleanup |
