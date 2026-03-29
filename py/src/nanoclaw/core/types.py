"""Core type definitions for NanoClaw."""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Mount / container config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdditionalMount:
    """Mount configuration for additional directories in containers."""

    host_path: str
    container_path: str | None = None
    readonly: bool = True


@dataclass(frozen=True)
class AllowedRoot:
    """An allowed root directory for mount validation."""

    path: str
    allow_read_write: bool = False
    description: str | None = None


@dataclass(frozen=True)
class MountAllowlist:
    """Security configuration for additional mounts.

    Stored at ~/.config/nanoclaw/mount-allowlist.json,
    NOT mounted into any container (tamper-proof from agents).
    """

    allowed_roots: list[AllowedRoot] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    non_main_read_only: bool = True


@dataclass(frozen=True)
class ContainerConfig:
    """Per-group container configuration."""

    additional_mounts: list[AdditionalMount] = field(default_factory=list)
    timeout: int = 300_000


# ---------------------------------------------------------------------------
# Multi-tenant entities
# ---------------------------------------------------------------------------


@dataclass
class Tenant:
    """An organization / workspace."""

    id: str
    slug: str
    name: str
    plan: str = "starter"
    config: dict[str, object] = field(default_factory=dict)
    max_concurrent_containers: int = 5
    created_at: str = ""


@dataclass
class User:
    """A human user within a tenant."""

    id: str
    tenant_id: str
    name: str
    email: str | None = None
    role: str = "member"  # admin / manager / member
    channel_ids: dict[str, str] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class Role:
    """An AI agent template (prompt + tools + skills + backend)."""

    id: str
    tenant_id: str
    name: str
    role_type: str  # "operations" / "logistics" / "cs" / "general"
    agent_backend: str = "claude-code"
    system_prompt: str | None = None
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    a2a_config: dict[str, object] = field(default_factory=dict)
    authorization: dict[str, object] = field(default_factory=dict)
    config_overrides: dict[str, object] = field(default_factory=dict)


@dataclass
class Coworker:
    """A role instance with its own workspace and identity."""

    id: str
    tenant_id: str
    role_id: str
    name: str
    folder: str
    is_admin: bool = False
    container_config: ContainerConfig | None = None
    max_concurrent: int = 2
    status: str = "active"


@dataclass
class ChannelBinding:
    """Bot credentials: per-coworker per-channel-type."""

    id: str
    coworker_id: str
    tenant_id: str
    channel_type: str  # "telegram" / "slack" / "web"
    credentials: dict[str, str] = field(default_factory=dict)
    bot_display_name: str | None = None
    status: str = "active"


@dataclass
class Conversation:
    """Per-coworker per-chat context."""

    id: str
    tenant_id: str
    coworker_id: str
    channel_binding_id: str
    channel_chat_id: str
    name: str | None = None
    trigger_pattern: str | None = None
    requires_trigger: bool = True
    is_main: bool = False
    created_at: str = ""


# ---------------------------------------------------------------------------
# Legacy types (backward compatibility)
# ---------------------------------------------------------------------------


@dataclass
class RegisteredGroup:
    """A registered group with its configuration.

    .. deprecated::
        Use Coworker + Conversation instead.  Kept for backward-compat
        during migration.  Will be removed in a future release.
    """

    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool = True
    is_main: bool = False


def registered_group_to_coworker(
    jid: str,
    group: RegisteredGroup,
    tenant_id: str,
    role_id: str,
    coworker_id: str = "",
    binding_id: str = "",
    conversation_id: str = "",
) -> tuple[Coworker, ChannelBinding, Conversation]:
    """Convert a RegisteredGroup into the new Coworker + ChannelBinding + Conversation triple.

    Caller must supply IDs (typically UUIDs).  Channel type is inferred from JID prefix.
    """
    warnings.warn(
        "registered_group_to_coworker is a migration helper and will be removed",
        DeprecationWarning,
        stacklevel=2,
    )
    channel_type = "telegram" if jid.startswith("tg:") else "slack" if jid.startswith("slack:") else "unknown"
    chat_id = jid.split(":", 1)[1] if ":" in jid else jid

    coworker = Coworker(
        id=coworker_id,
        tenant_id=tenant_id,
        role_id=role_id,
        name=group.name,
        folder=group.folder,
        is_admin=group.is_main,
        container_config=group.container_config,
    )
    binding = ChannelBinding(
        id=binding_id,
        coworker_id=coworker_id,
        tenant_id=tenant_id,
        channel_type=channel_type,
        credentials={},
    )
    conversation = Conversation(
        id=conversation_id,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        channel_binding_id=binding_id,
        channel_chat_id=chat_id,
        name=group.name,
        trigger_pattern=group.trigger,
        requires_trigger=group.requires_trigger,
        is_main=group.is_main,
        created_at=group.added_at,
    )
    return coworker, binding, conversation


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewMessage:
    """An inbound message from a channel."""

    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False
    is_bot_message: bool = False


@dataclass
class ScheduledTask:
    """A scheduled task configuration."""

    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"]
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""
    coworker_id: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class TaskRunLog:
    """Log entry for a task execution."""

    task_id: str
    run_at: str
    duration_ms: int
    status: Literal["success", "error"]
    result: str | None = None
    error: str | None = None


# --- Channel abstraction ---


@runtime_checkable
class Channel(Protocol):
    """Protocol for messaging channel implementations."""

    name: str

    async def connect(self) -> None: ...

    async def send_message(self, jid: str, text: str) -> None: ...

    def is_connected(self) -> bool: ...

    def owns_jid(self, jid: str) -> bool: ...

    async def disconnect(self) -> None: ...


class TypingChannel(Protocol):
    """Channel that supports typing indicators."""

    async def set_typing(self, jid: str, is_typing: bool) -> None: ...


class SyncableChannel(Protocol):
    """Channel that supports group/chat name syncing."""

    async def sync_groups(self, force: bool) -> None: ...


# Callback types
OnInboundMessage = Callable[[str, "NewMessage"], None]
OnChatMetadata = Callable[[str, str, str | None, str | None, bool | None], "Awaitable[None]"]
