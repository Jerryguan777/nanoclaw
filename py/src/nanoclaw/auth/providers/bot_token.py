"""Bot token identity provider for channel webhooks.

When Telegram/Slack sends an update to our webhook, the request carries
the bot token that identifies which coworker this message is for.
"""

from __future__ import annotations

from typing import Protocol

from nanoclaw.auth.context import Subject
from nanoclaw.auth.middleware import RawCredentials
from nanoclaw.core.logger import get_logger

logger = get_logger()


class ChannelBindingStore(Protocol):
    """Looks up channel binding metadata from the database."""

    async def lookup_by_bot_token(self, bot_token: str) -> BindingRecord | None: ...


class BindingRecord:
    """Resolved channel binding metadata."""

    def __init__(self, *, coworker_id: str, tenant_id: str, channel_type: str) -> None:
        self.coworker_id = coworker_id
        self.tenant_id = tenant_id
        self.channel_type = channel_type


class BotTokenProvider:
    """Resolve bot token → coworker identity via channel_bindings table."""

    def __init__(self, store: ChannelBindingStore) -> None:
        self._store = store

    def can_handle(self, creds: RawCredentials) -> bool:
        return creds.type == "bot_token" and creds.bot_token is not None

    async def verify(self, creds: RawCredentials) -> Subject | None:
        if creds.bot_token is None:
            return None

        record = await self._store.lookup_by_bot_token(creds.bot_token)
        if record is None:
            logger.warning("Unknown bot token")
            return None

        return Subject(
            id=record.coworker_id,
            type="coworker",
            tenant_id=record.tenant_id,
        )
