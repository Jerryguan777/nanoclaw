"""Structured runtime state, replacing module-level globals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanoclaw.core.types import (
        ChannelBinding,
        Conversation,
        Coworker,
        Role,
        Tenant,
    )


@dataclass
class CoworkerConfig:
    """Runtime config, merged from Role + Coworker tables."""

    id: str
    tenant_id: str
    name: str
    folder: str
    system_prompt: str | None
    trigger_pattern: re.Pattern[str] | None
    agent_backend: str
    container_image: str | None
    max_concurrent: int
    role_config: dict[str, object]
    tools: list[str]
    skills: list[str]
    is_admin: bool = False

    @staticmethod
    def from_role_and_coworker(role: Role, coworker: Coworker) -> CoworkerConfig:
        """Merge Role template + Coworker instance into runtime config."""
        trigger: re.Pattern[str] | None = None
        # Build trigger pattern from coworker name
        if coworker.name:
            trigger = re.compile(rf"^@{re.escape(coworker.name)}\b", re.IGNORECASE)

        return CoworkerConfig(
            id=coworker.id,
            tenant_id=coworker.tenant_id,
            name=coworker.name,
            folder=coworker.folder,
            system_prompt=role.system_prompt,
            trigger_pattern=trigger,
            agent_backend=role.agent_backend,
            container_image=None,  # from config_overrides if needed
            max_concurrent=coworker.max_concurrent,
            role_config=role.config_overrides,
            tools=list(role.tools),
            skills=list(role.skills),
            is_admin=coworker.is_admin,
        )


@dataclass
class ConversationState:
    """Per-conversation runtime state."""

    conversation: Conversation
    session_id: str | None = None
    last_agent_timestamp: str = ""


@dataclass
class CoworkerState:
    """Per-coworker runtime state."""

    config: CoworkerConfig
    conversations: dict[str, ConversationState] = field(default_factory=dict)  # channel_chat_id -> state
    channel_bindings: dict[str, ChannelBinding] = field(default_factory=dict)  # channel_type -> binding


class OrchestratorState:
    """All runtime state, structured by tenant and coworker.

    Replaces module-level globals (_registered_groups, _sessions,
    _last_agent_timestamp, _channels, etc.).
    """

    def __init__(self, global_limit: int = 20) -> None:
        self.tenants: dict[str, Tenant] = {}  # tenant_id -> Tenant
        self.coworkers: dict[str, CoworkerState] = {}  # coworker_id -> state

        # Three-level scheduling counters
        self.global_active: int = 0
        self.global_limit: int = global_limit
        self.tenant_active: dict[str, int] = {}
        self.coworker_active: dict[str, int] = {}

    def can_start_container(self, tenant_id: str, coworker_id: str) -> bool:
        """Check all three concurrency levels."""
        if self.global_active >= self.global_limit:
            return False

        tenant = self.tenants.get(tenant_id)
        if tenant and self.tenant_active.get(tenant_id, 0) >= tenant.max_concurrent_containers:
            return False

        cw = self.coworkers.get(coworker_id)
        return not (cw and self.coworker_active.get(coworker_id, 0) >= cw.config.max_concurrent)

    def increment_active(self, tenant_id: str, coworker_id: str) -> None:
        """Increment all three concurrency counters."""
        self.global_active += 1
        self.tenant_active[tenant_id] = self.tenant_active.get(tenant_id, 0) + 1
        self.coworker_active[coworker_id] = self.coworker_active.get(coworker_id, 0) + 1

    def decrement_active(self, tenant_id: str, coworker_id: str) -> None:
        """Decrement all three concurrency counters."""
        self.global_active = max(0, self.global_active - 1)
        self.tenant_active[tenant_id] = max(0, self.tenant_active.get(tenant_id, 0) - 1)
        self.coworker_active[coworker_id] = max(0, self.coworker_active.get(coworker_id, 0) - 1)

    def find_coworker_for_conversation(self, channel_chat_id: str) -> tuple[CoworkerState, ConversationState] | None:
        """Look up coworker and conversation state by channel_chat_id."""
        for cw in self.coworkers.values():
            conv = cw.conversations.get(channel_chat_id)
            if conv is not None:
                return cw, conv
        return None

    def find_coworker_by_binding(self, binding_id: str) -> CoworkerState | None:
        """Find the coworker that owns a specific channel binding."""
        for cw in self.coworkers.values():
            for binding in cw.channel_bindings.values():
                if binding.id == binding_id:
                    return cw
        return None
