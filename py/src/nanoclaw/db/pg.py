"""PostgreSQL database operations using asyncpg."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from nanoclaw.core.config import DATABASE_URL
from nanoclaw.core.group_folder import is_valid_group_folder
from nanoclaw.core.logger import get_logger
from nanoclaw.core.types import (
    ChannelBinding,
    ContainerConfig,
    Conversation,
    Coworker,
    NewMessage,
    RegisteredGroup,
    Role,
    ScheduledTask,
    TaskRunLog,
    Tenant,
    User,
)

logger = get_logger()

_pool: asyncpg.Pool[asyncpg.Record] | None = None
DEFAULT_TENANT: str = "default"


@dataclass(frozen=True)
class ChatInfo:
    """Chat metadata record."""

    jid: str
    name: str
    last_message_time: str
    channel: str | None
    is_group: bool


def _get_pool() -> asyncpg.Pool[asyncpg.Record]:
    """Return the module-level connection pool, asserting it is initialized."""
    assert _pool is not None, "Database not initialized. Call await init_database() first."
    return _pool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def _create_schema(conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record]) -> None:
    """Create tables and indexes."""

    # --- Multi-tenant core tables ---

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            plan TEXT DEFAULT 'starter',
            config JSONB DEFAULT '{}',
            max_concurrent_containers INT DEFAULT 5,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'member',
            channel_ids JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL,
            role_type TEXT NOT NULL,
            agent_backend TEXT DEFAULT 'claude-code',
            system_prompt TEXT,
            tools JSONB DEFAULT '[]',
            skills JSONB DEFAULT '[]',
            a2a_config JSONB DEFAULT '{}',
            "authorization" JSONB DEFAULT '{}',
            config_overrides JSONB DEFAULT '{}',
            UNIQUE (tenant_id, name)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS coworkers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            role_id UUID NOT NULL REFERENCES roles(id),
            name TEXT NOT NULL,
            folder TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT FALSE,
            container_config JSONB,
            max_concurrent INT DEFAULT 2,
            status TEXT DEFAULT 'active',
            UNIQUE (tenant_id, folder)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_bindings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            channel_type TEXT NOT NULL,
            credentials JSONB NOT NULL DEFAULT '{}',
            bot_display_name TEXT,
            status TEXT DEFAULT 'active',
            UNIQUE (coworker_id, channel_type)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            channel_binding_id UUID NOT NULL REFERENCES channel_bindings(id),
            channel_chat_id TEXT NOT NULL,
            name TEXT,
            trigger_pattern TEXT,
            requires_trigger BOOLEAN DEFAULT TRUE,
            is_main BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (channel_binding_id, channel_chat_id)
        )
    """)

    # --- Legacy / shared tables (with multi-tenant columns) ---

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            jid TEXT NOT NULL,
            name TEXT,
            last_message_time TEXT,
            channel TEXT,
            is_group BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (tenant_id, jid)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            id TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            sender TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT NOT NULL,
            is_from_me BOOLEAN DEFAULT FALSE,
            is_bot_message BOOLEAN DEFAULT FALSE,
            conversation_id UUID,
            coworker_id UUID,
            PRIMARY KEY (tenant_id, id, chat_jid)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(tenant_id, timestamp)")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(tenant_id, conversation_id, timestamp)"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            id TEXT PRIMARY KEY,
            group_folder TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            context_mode TEXT DEFAULT 'isolated',
            next_run TEXT,
            last_run TEXT,
            last_result TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            coworker_id UUID,
            conversation_id UUID
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next ON scheduled_tasks(tenant_id, next_run)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(tenant_id, status)")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS task_run_logs (
            id SERIAL PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
            run_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at)")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS router_state (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (tenant_id, key)
        )
    """)

    # Sessions: per-conversation (new) with legacy group_folder support
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            group_folder TEXT NOT NULL,
            session_id TEXT NOT NULL,
            conversation_id UUID,
            coworker_id UUID,
            PRIMARY KEY (tenant_id, group_folder)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS registered_groups (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            jid TEXT NOT NULL,
            name TEXT NOT NULL,
            folder TEXT NOT NULL,
            trigger_pattern TEXT NOT NULL,
            added_at TEXT NOT NULL,
            container_config JSONB,
            requires_trigger BOOLEAN DEFAULT TRUE,
            is_main BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (tenant_id, jid),
            UNIQUE (tenant_id, folder)
        )
    """)


async def init_database(database_url: str | None = None) -> None:
    """Initialize PostgreSQL connection pool and create schema."""
    global _pool
    url = database_url or DATABASE_URL
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await _create_schema(conn)


async def _init_test_database(database_url: str) -> None:
    """Initialize a test database with a fresh schema."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        # Drop all tables for a clean slate (order matters for FK constraints)
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await conn.execute("DROP TABLE IF EXISTS conversations CASCADE")
        await conn.execute("DROP TABLE IF EXISTS channel_bindings CASCADE")
        await conn.execute("DROP TABLE IF EXISTS coworkers CASCADE")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
        await conn.execute("DROP TABLE IF EXISTS users CASCADE")
        await conn.execute("DROP TABLE IF EXISTS tenants CASCADE")
        await _create_schema(conn)


async def close_database() -> None:
    """Close the connection pool. Call on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


async def create_tenant(
    *,
    slug: str,
    name: str,
    plan: str = "starter",
    config: dict[str, object] | None = None,
    max_concurrent_containers: int = 5,
) -> Tenant:
    """Create a new tenant and return it."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenants (slug, name, plan, config, max_concurrent_containers)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING id, slug, name, plan, config, max_concurrent_containers, created_at
            """,
            slug,
            name,
            plan,
            json.dumps(config or {}),
            max_concurrent_containers,
        )
    assert row is not None
    return _record_to_tenant(row)


async def get_tenant(tenant_id: str) -> Tenant | None:
    """Get a tenant by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1::uuid", tenant_id)
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_tenant_by_slug(slug: str) -> Tenant | None:
    """Get a tenant by slug."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE slug = $1", slug)
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_all_tenants() -> list[Tenant]:
    """Get all tenants."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM tenants ORDER BY created_at")
    return [_record_to_tenant(row) for row in rows]


def _record_to_tenant(row: asyncpg.Record) -> Tenant:
    cfg = row["config"]
    config_dict: dict[str, object] = cfg if isinstance(cfg, dict) else json.loads(cfg) if cfg else {}
    return Tenant(
        id=str(row["id"]),
        slug=row["slug"],
        name=row["name"],
        plan=row["plan"] or "starter",
        config=config_dict,
        max_concurrent_containers=row["max_concurrent_containers"] or 5,
        created_at=str(row["created_at"]) if row["created_at"] else "",
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def create_user(
    *,
    tenant_id: str,
    name: str,
    email: str | None = None,
    role: str = "member",
    channel_ids: dict[str, str] | None = None,
) -> User:
    """Create a new user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, name, email, role, channel_ids)
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
            RETURNING id, tenant_id, name, email, role, channel_ids, created_at
            """,
            tenant_id,
            name,
            email,
            role,
            json.dumps(channel_ids or {}),
        )
    assert row is not None
    return _record_to_user(row)


async def get_users_for_tenant(tenant_id: str) -> list[User]:
    """Get all users for a tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE tenant_id = $1::uuid ORDER BY created_at",
            tenant_id,
        )
    return [_record_to_user(row) for row in rows]


def _record_to_user(row: asyncpg.Record) -> User:
    cids = row["channel_ids"]
    channel_ids: dict[str, str] = cids if isinstance(cids, dict) else json.loads(cids) if cids else {}
    return User(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        email=row["email"],
        role=row["role"] or "member",
        channel_ids=channel_ids,
        created_at=str(row["created_at"]) if row["created_at"] else "",
    )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def create_role(
    *,
    tenant_id: str,
    name: str,
    role_type: str,
    agent_backend: str = "claude-code",
    system_prompt: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    a2a_config: dict[str, object] | None = None,
    authorization: dict[str, object] | None = None,
    config_overrides: dict[str, object] | None = None,
) -> Role:
    """Create a new role."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO roles (tenant_id, name, role_type, agent_backend, system_prompt,
                               tools, skills, a2a_config, "authorization", config_overrides)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb)
            RETURNING *
            """,
            tenant_id,
            name,
            role_type,
            agent_backend,
            system_prompt,
            json.dumps(tools or []),
            json.dumps(skills or []),
            json.dumps(a2a_config or {}),
            json.dumps(authorization or {}),
            json.dumps(config_overrides or {}),
        )
    assert row is not None
    return _record_to_role(row)


async def get_roles_for_tenant(tenant_id: str) -> list[Role]:
    """Get all roles for a tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM roles WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_role(row) for row in rows]


async def get_role(role_id: str) -> Role | None:
    """Get a role by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM roles WHERE id = $1::uuid", role_id)
    if row is None:
        return None
    return _record_to_role(row)


def _record_to_role(row: asyncpg.Record) -> Role:
    def _json_list(val: Any) -> list[str]:
        if isinstance(val, list):
            return list(val)
        return json.loads(val) if val else []

    def _json_dict(val: Any) -> dict[str, object]:
        if isinstance(val, dict):
            return dict(val)
        return json.loads(val) if val else {}

    return Role(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        role_type=row["role_type"],
        agent_backend=row["agent_backend"] or "claude-code",
        system_prompt=row["system_prompt"],
        tools=_json_list(row["tools"]),
        skills=_json_list(row["skills"]),
        a2a_config=_json_dict(row["a2a_config"]),
        authorization=_json_dict(row["authorization"]),
        config_overrides=_json_dict(row["config_overrides"]),
    )


# ---------------------------------------------------------------------------
# Coworkers
# ---------------------------------------------------------------------------


async def create_coworker(
    *,
    tenant_id: str,
    role_id: str,
    name: str,
    folder: str,
    is_admin: bool = False,
    container_config: dict[str, object] | None = None,
    max_concurrent: int = 2,
) -> Coworker:
    """Create a new coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO coworkers (tenant_id, role_id, name, folder, is_admin, container_config, max_concurrent)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7)
            RETURNING *
            """,
            tenant_id,
            role_id,
            name,
            folder,
            is_admin,
            json.dumps(container_config) if container_config else None,
            max_concurrent,
        )
    assert row is not None
    return _record_to_coworker(row)


async def get_coworker(coworker_id: str) -> Coworker | None:
    """Get a coworker by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coworkers WHERE id = $1::uuid", coworker_id)
    if row is None:
        return None
    return _record_to_coworker(row)


async def get_coworkers_for_tenant(tenant_id: str) -> list[Coworker]:
    """Get all coworkers for a tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM coworkers WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_coworker(row) for row in rows]


async def get_all_coworkers() -> list[Coworker]:
    """Get all coworkers across all tenants."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM coworkers ORDER BY name")
    return [_record_to_coworker(row) for row in rows]


def _record_to_coworker(row: asyncpg.Record) -> Coworker:
    cc_raw = row["container_config"]
    cc: ContainerConfig | None = None
    if cc_raw:
        parsed = cc_raw if isinstance(cc_raw, dict) else json.loads(cc_raw)
        if isinstance(parsed, dict):
            cc = ContainerConfig(**parsed)
    return Coworker(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        role_id=str(row["role_id"]),
        name=row["name"],
        folder=row["folder"],
        is_admin=bool(row["is_admin"]),
        container_config=cc,
        max_concurrent=row["max_concurrent"] or 2,
        status=row["status"] or "active",
    )


# ---------------------------------------------------------------------------
# Channel bindings
# ---------------------------------------------------------------------------


async def create_channel_binding(
    *,
    coworker_id: str,
    tenant_id: str,
    channel_type: str,
    credentials: dict[str, str] | None = None,
    bot_display_name: str | None = None,
) -> ChannelBinding:
    """Create a new channel binding."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO channel_bindings (coworker_id, tenant_id, channel_type, credentials, bot_display_name)
            VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5)
            RETURNING *
            """,
            coworker_id,
            tenant_id,
            channel_type,
            json.dumps(credentials or {}),
            bot_display_name,
        )
    assert row is not None
    return _record_to_channel_binding(row)


async def get_channel_binding(binding_id: str) -> ChannelBinding | None:
    """Get a channel binding by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM channel_bindings WHERE id = $1::uuid", binding_id)
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def get_channel_bindings_for_coworker(coworker_id: str) -> list[ChannelBinding]:
    """Get all channel bindings for a coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM channel_bindings WHERE coworker_id = $1::uuid",
            coworker_id,
        )
    return [_record_to_channel_binding(row) for row in rows]


async def get_all_channel_bindings(tenant_id: str | None = None) -> list[ChannelBinding]:
    """Get all channel bindings, optionally filtered by tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        if tenant_id:
            rows = await conn.fetch(
                "SELECT * FROM channel_bindings WHERE tenant_id = $1::uuid",
                tenant_id,
            )
        else:
            rows = await conn.fetch("SELECT * FROM channel_bindings")
    return [_record_to_channel_binding(row) for row in rows]


def _record_to_channel_binding(row: asyncpg.Record) -> ChannelBinding:
    creds = row["credentials"]
    credentials: dict[str, str] = creds if isinstance(creds, dict) else json.loads(creds) if creds else {}
    return ChannelBinding(
        id=str(row["id"]),
        coworker_id=str(row["coworker_id"]),
        tenant_id=str(row["tenant_id"]),
        channel_type=row["channel_type"],
        credentials=credentials,
        bot_display_name=row["bot_display_name"],
        status=row["status"] or "active",
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


async def create_conversation(
    *,
    tenant_id: str,
    coworker_id: str,
    channel_binding_id: str,
    channel_chat_id: str,
    name: str | None = None,
    trigger_pattern: str | None = None,
    requires_trigger: bool = True,
    is_main: bool = False,
) -> Conversation:
    """Create a new conversation."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (tenant_id, coworker_id, channel_binding_id,
                                       channel_chat_id, name, trigger_pattern, requires_trigger, is_main)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            channel_binding_id,
            channel_chat_id,
            name,
            trigger_pattern,
            requires_trigger,
            is_main,
        )
    assert row is not None
    return _record_to_conversation(row)


async def get_conversation(conversation_id: str) -> Conversation | None:
    """Get a conversation by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM conversations WHERE id = $1::uuid", conversation_id)
    if row is None:
        return None
    return _record_to_conversation(row)


async def get_conversations_for_coworker(coworker_id: str) -> list[Conversation]:
    """Get all conversations for a coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM conversations WHERE coworker_id = $1::uuid ORDER BY created_at",
            coworker_id,
        )
    return [_record_to_conversation(row) for row in rows]


async def get_conversation_by_binding_chat(binding_id: str, channel_chat_id: str) -> Conversation | None:
    """Look up a conversation by binding + chat ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations WHERE channel_binding_id = $1::uuid AND channel_chat_id = $2",
            binding_id,
            channel_chat_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


async def get_all_conversations(tenant_id: str | None = None) -> list[Conversation]:
    """Get all conversations, optionally filtered by tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        if tenant_id:
            rows = await conn.fetch(
                "SELECT * FROM conversations WHERE tenant_id = $1::uuid ORDER BY created_at",
                tenant_id,
            )
        else:
            rows = await conn.fetch("SELECT * FROM conversations ORDER BY created_at")
    return [_record_to_conversation(row) for row in rows]


def _record_to_conversation(row: asyncpg.Record) -> Conversation:
    return Conversation(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        channel_binding_id=str(row["channel_binding_id"]),
        channel_chat_id=row["channel_chat_id"],
        name=row["name"],
        trigger_pattern=row["trigger_pattern"],
        requires_trigger=bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True,
        is_main=bool(row["is_main"]) if row["is_main"] is not None else False,
        created_at=str(row["created_at"]) if row["created_at"] else "",
    )


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


async def store_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None = None,
    channel: str | None = None,
    is_group: bool | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Store chat metadata only (no message content)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        if name:
            await conn.execute(
                """
                INSERT INTO chats (tenant_id, jid, name, last_message_time, channel, is_group)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, jid) DO UPDATE SET
                    name = EXCLUDED.name,
                    last_message_time = GREATEST(chats.last_message_time, EXCLUDED.last_message_time),
                    channel = COALESCE(EXCLUDED.channel, chats.channel),
                    is_group = COALESCE(EXCLUDED.is_group, chats.is_group)
                """,
                tenant_id,
                chat_jid,
                name,
                timestamp,
                channel,
                is_group,
            )
        else:
            await conn.execute(
                """
                INSERT INTO chats (tenant_id, jid, name, last_message_time, channel, is_group)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, jid) DO UPDATE SET
                    last_message_time = GREATEST(chats.last_message_time, EXCLUDED.last_message_time),
                    channel = COALESCE(EXCLUDED.channel, chats.channel),
                    is_group = COALESCE(EXCLUDED.is_group, chats.is_group)
                """,
                tenant_id,
                chat_jid,
                chat_jid,
                timestamp,
                channel,
                is_group,
            )


async def update_chat_name(chat_jid: str, name: str, tenant_id: str = DEFAULT_TENANT) -> None:
    """Update chat name without changing timestamp for existing chats."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (tenant_id, jid, name, last_message_time)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET name = EXCLUDED.name
            """,
            tenant_id,
            chat_jid,
            name,
            datetime.now(UTC).isoformat(),
        )


async def get_all_chats(tenant_id: str = DEFAULT_TENANT) -> list[ChatInfo]:
    """Get all known chats, ordered by most recent activity."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT jid, name, last_message_time, channel, is_group
            FROM chats
            WHERE tenant_id = $1
            ORDER BY last_message_time DESC
            """,
            tenant_id,
        )
    return [
        ChatInfo(
            jid=row["jid"],
            name=row["name"],
            last_message_time=row["last_message_time"],
            channel=row["channel"],
            is_group=bool(row["is_group"]) if row["is_group"] is not None else False,
        )
        for row in rows
    ]


async def get_last_group_sync(tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Get timestamp of last group metadata sync."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_message_time FROM chats WHERE tenant_id = $1 AND jid = '__group_sync__'",
            tenant_id,
        )
    if row is None:
        return None
    return row["last_message_time"] or None


async def set_last_group_sync(tenant_id: str = DEFAULT_TENANT) -> None:
    """Record that group metadata was synced."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (tenant_id, jid, name, last_message_time)
            VALUES ($1, '__group_sync__', '__group_sync__', $2)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET last_message_time = EXCLUDED.last_message_time
            """,
            tenant_id,
            now,
        )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def store_message(msg: NewMessage, tenant_id: str = DEFAULT_TENANT) -> None:
    """Store a message with full content."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (tenant_id, id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (tenant_id, id, chat_jid) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            tenant_id,
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            msg.is_from_me,
            msg.is_bot_message,
        )


async def store_message_direct(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    is_bot_message: bool = False,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Store a message directly."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (tenant_id, id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (tenant_id, id, chat_jid) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            tenant_id,
            id,
            chat_jid,
            sender,
            sender_name,
            content,
            timestamp,
            is_from_me,
            is_bot_message,
        )


def _record_to_new_message(row: asyncpg.Record) -> NewMessage:
    """Convert an asyncpg.Record to a NewMessage dataclass."""
    return NewMessage(
        id=row["id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=row["content"],
        timestamp=row["timestamp"],
        is_from_me=bool(row["is_from_me"]),
        is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
    )


async def get_new_messages(
    jids: list[str],
    last_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[list[NewMessage], str]:
    """Get new messages since last_timestamp for the given JIDs.

    Returns (messages, new_timestamp).
    """
    if not jids:
        return [], last_timestamp

    pool = _get_pool()
    async with pool.acquire() as conn:
        # Build numbered placeholders for jids: $3, $4, $5, ...
        jid_placeholders = ", ".join(f"${i + 3}" for i in range(len(jids)))
        sql = f"""
            SELECT * FROM (
                SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
                FROM messages
                WHERE tenant_id = $1 AND timestamp > $2
                    AND chat_jid IN ({jid_placeholders})
                    AND is_bot_message = FALSE
                    AND content NOT LIKE ${len(jids) + 3}
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ${len(jids) + 4}
            ) sub ORDER BY timestamp
        """
        params: list[Any] = [tenant_id, last_timestamp, *jids, f"{bot_prefix}:%", limit]
        rows = await conn.fetch(sql, *params)

    messages = [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]

    new_timestamp = last_timestamp
    for msg in messages:
        if msg.timestamp > new_timestamp:
            new_timestamp = msg.timestamp

    return messages, new_timestamp


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
    tenant_id: str = DEFAULT_TENANT,
) -> list[NewMessage]:
    """Get messages since a timestamp for a specific chat."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM (
                SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
                FROM messages
                WHERE tenant_id = $1 AND chat_jid = $2 AND timestamp > $3
                    AND is_bot_message = FALSE AND content NOT LIKE $4
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT $5
            ) sub ORDER BY timestamp
            """,
            tenant_id,
            chat_jid,
            since_timestamp,
            f"{bot_prefix}:%",
            limit,
        )
    return [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


async def create_task(task: ScheduledTask, tenant_id: str = DEFAULT_TENANT) -> None:
    """Create a new scheduled task (without last_run / last_result)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (tenant_id, id, group_folder, chat_jid, prompt, schedule_type,
                                         schedule_value, context_mode, next_run, status, created_at,
                                         coworker_id, conversation_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid, $13::uuid)
            """,
            tenant_id,
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            task.next_run,
            task.status,
            task.created_at,
            task.coworker_id,
            task.conversation_id,
        )


def _record_to_scheduled_task(row: asyncpg.Record) -> ScheduledTask:
    """Convert an asyncpg.Record to a ScheduledTask dataclass."""
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        coworker_id=str(row["coworker_id"]) if row.get("coworker_id") else None,
        conversation_id=str(row["conversation_id"]) if row.get("conversation_id") else None,
    )


async def get_task_by_id(id: str, tenant_id: str = DEFAULT_TENANT) -> ScheduledTask | None:
    """Get a task by its ID, or None if not found."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            id,
        )
    if row is None:
        return None
    return _record_to_scheduled_task(row)


async def get_tasks_for_group(group_folder: str, tenant_id: str = DEFAULT_TENANT) -> list[ScheduledTask]:
    """Get all tasks for a specific group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 AND group_folder = $2 ORDER BY created_at DESC",
            tenant_id,
            group_folder,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def get_all_tasks(tenant_id: str = DEFAULT_TENANT) -> list[ScheduledTask]:
    """Get all scheduled tasks."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE tenant_id = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task(
    id: str,
    *,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_value: str | None = None,
    next_run: str | None = None,
    status: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Update selected fields on a scheduled task."""
    fields: list[str] = []
    values: list[Any] = [tenant_id]
    param_idx = 2  # $1 is tenant_id

    if prompt is not None:
        fields.append(f"prompt = ${param_idx}")
        values.append(prompt)
        param_idx += 1
    if schedule_type is not None:
        fields.append(f"schedule_type = ${param_idx}")
        values.append(schedule_type)
        param_idx += 1
    if schedule_value is not None:
        fields.append(f"schedule_value = ${param_idx}")
        values.append(schedule_value)
        param_idx += 1
    if next_run is not None:
        fields.append(f"next_run = ${param_idx}")
        values.append(next_run)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return

    values.append(id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE tenant_id = $1 AND id = ${param_idx}",
            *values,
        )


async def delete_task(id: str, tenant_id: str = DEFAULT_TENANT) -> None:
    """Delete a task and its run logs (CASCADE handles task_run_logs)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM scheduled_tasks WHERE tenant_id = $1 AND id = $2",
            tenant_id,
            id,
        )


async def get_due_tasks(tenant_id: str = DEFAULT_TENANT) -> list[ScheduledTask]:
    """Get all active tasks whose next_run is in the past."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM scheduled_tasks
            WHERE tenant_id = $1 AND status = 'active' AND next_run IS NOT NULL AND next_run <= $2
            ORDER BY next_run
            """,
            tenant_id,
            now,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task_after_run(
    id: str,
    next_run: str | None,
    last_result: str,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    """Update task state after execution."""
    pool = _get_pool()
    now = datetime.now(UTC).isoformat()
    new_status = "completed" if next_run is None else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduled_tasks
            SET next_run = $1::text, last_run = $2, last_result = $3,
                status = COALESCE($4::text, status)
            WHERE tenant_id = $5 AND id = $6
            """,
            next_run,
            now,
            last_result,
            new_status,
            tenant_id,
            id,
        )


async def log_task_run(log: TaskRunLog, tenant_id: str = DEFAULT_TENANT) -> None:
    """Insert a task run log entry."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_run_logs (tenant_id, task_id, run_at, duration_ms, status, result, error)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            tenant_id,
            log.task_id,
            log.run_at,
            log.duration_ms,
            log.status,
            log.result,
            log.error,
        )


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


async def get_router_state(key: str, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Get a value from the router_state table."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM router_state WHERE tenant_id = $1 AND key = $2",
            tenant_id,
            key,
        )
    if row is None:
        return None
    return row["value"]  # type: ignore[no-any-return]


async def set_router_state(key: str, value: str, tenant_id: str = DEFAULT_TENANT) -> None:
    """Set a value in the router_state table."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO router_state (tenant_id, key, value) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value
            """,
            tenant_id,
            key,
            value,
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def get_session(group_folder: str, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """Get the session ID for a group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_id FROM sessions WHERE tenant_id = $1 AND group_folder = $2",
            tenant_id,
            group_folder,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session(group_folder: str, session_id: str, tenant_id: str = DEFAULT_TENANT) -> None:
    """Set the session ID for a group folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (tenant_id, group_folder, session_id) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, group_folder) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            tenant_id,
            group_folder,
            session_id,
        )


async def get_all_sessions(tenant_id: str = DEFAULT_TENANT) -> dict[str, str]:
    """Get all session mappings."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT group_folder, session_id FROM sessions WHERE tenant_id = $1",
            tenant_id,
        )
    return {row["group_folder"]: row["session_id"] for row in rows}


async def set_session_new(
    conversation_id: str,
    tenant_id: str,
    coworker_id: str,
    session_id: str,
) -> None:
    """Set session for a conversation (new multi-tenant format)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (tenant_id, group_folder, session_id, conversation_id, coworker_id)
            VALUES ($1, $2, $3, $4::uuid, $5::uuid)
            ON CONFLICT (tenant_id, group_folder) DO UPDATE SET
                session_id = EXCLUDED.session_id,
                conversation_id = EXCLUDED.conversation_id,
                coworker_id = EXCLUDED.coworker_id
            """,
            tenant_id,
            conversation_id,  # use conversation_id as group_folder key
            session_id,
            conversation_id,
            coworker_id,
        )


# ---------------------------------------------------------------------------
# Registered groups (legacy)
# ---------------------------------------------------------------------------


def _parse_registered_group_record(
    row: asyncpg.Record,
) -> tuple[str, RegisteredGroup] | None:
    """Parse a registered_groups row into (jid, RegisteredGroup).

    Returns None if the folder is invalid.
    """
    jid: str = row["jid"]
    folder: str = row["folder"]

    if not is_valid_group_folder(folder):
        logger.warn("Skipping registered group with invalid folder", jid=jid, folder=folder)
        return None

    container_config_raw: dict[str, Any] | str | None = row["container_config"]
    container_config: ContainerConfig | None = None
    if container_config_raw:
        # JSONB is returned as dict by asyncpg
        parsed = container_config_raw if isinstance(container_config_raw, dict) else json.loads(container_config_raw)
        container_config = ContainerConfig(**parsed) if isinstance(parsed, dict) else None

    requires_trigger = bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True
    is_main = bool(row["is_main"]) if row["is_main"] is not None else False

    return jid, RegisteredGroup(
        name=row["name"],
        folder=folder,
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
        is_main=is_main,
    )


async def get_registered_group(jid: str, tenant_id: str = DEFAULT_TENANT) -> RegisteredGroup | None:
    """Get a registered group by JID, or None if not found / invalid folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM registered_groups WHERE tenant_id = $1 AND jid = $2",
            tenant_id,
            jid,
        )
    if row is None:
        return None
    result = _parse_registered_group_record(row)
    if result is None:
        return None
    return result[1]


async def set_registered_group(jid: str, group: RegisteredGroup, tenant_id: str = DEFAULT_TENANT) -> None:
    """Insert or replace a registered group."""
    if not is_valid_group_folder(group.folder):
        raise ValueError(f'Invalid group folder "{group.folder}" for JID {jid}')

    container_config_json: str | None = None
    if group.container_config:
        container_config_json = json.dumps(
            {
                "additional_mounts": [
                    {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                    for m in group.container_config.additional_mounts
                ],
                "timeout": group.container_config.timeout,
            }
        )

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO registered_groups (tenant_id, jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET
                name = EXCLUDED.name,
                folder = EXCLUDED.folder,
                trigger_pattern = EXCLUDED.trigger_pattern,
                added_at = EXCLUDED.added_at,
                container_config = EXCLUDED.container_config,
                requires_trigger = EXCLUDED.requires_trigger,
                is_main = EXCLUDED.is_main
            """,
            tenant_id,
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            container_config_json,
            group.requires_trigger,
            group.is_main,
        )


async def get_all_registered_groups(tenant_id: str = DEFAULT_TENANT) -> dict[str, RegisteredGroup]:
    """Get all registered groups as a dict keyed by JID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM registered_groups WHERE tenant_id = $1",
            tenant_id,
        )
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        parsed = _parse_registered_group_record(row)
        if parsed is not None:
            result[parsed[0]] = parsed[1]
    return result


async def get_all_registered_groups_legacy(tenant_id: str = DEFAULT_TENANT) -> dict[str, RegisteredGroup]:
    """Legacy alias for migration script."""
    return await get_all_registered_groups(tenant_id)
