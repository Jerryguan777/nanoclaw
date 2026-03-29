"""API key identity provider for programmatic access.

Used by external systems (CRM, CI/CD) and inter-service calls.
Keys are stored as bcrypt hashes in the ``api_keys`` table.
"""

from __future__ import annotations

from typing import Protocol

from nanoclaw.auth.context import Subject
from nanoclaw.auth.middleware import RawCredentials
from nanoclaw.core.logger import get_logger

logger = get_logger()


class ApiKeyStore(Protocol):
    """Looks up API key metadata from the database."""

    async def lookup_by_key(self, raw_key: str) -> ApiKeyRecord | None: ...


class ApiKeyRecord:
    """Resolved API key metadata."""

    def __init__(
        self,
        *,
        id: str,
        tenant_id: str,
        user_id: str | None,
        coworker_id: str | None,
        scopes: list[str],
    ) -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.coworker_id = coworker_id
        self.scopes = scopes


class ApiKeyProvider:
    """Verify API keys via hash lookup in the database."""

    def __init__(self, store: ApiKeyStore) -> None:
        self._store = store

    def can_handle(self, creds: RawCredentials) -> bool:
        return creds.type == "api_key" and creds.api_key is not None

    async def verify(self, creds: RawCredentials) -> Subject | None:
        """Look up the API key hash, return Subject or None.

        The Subject type is "api_key". The authorization layer uses the
        api_key's scopes as additional restrictions on top of role-based
        permissions.
        """
        if creds.api_key is None:
            return None

        record = await self._store.lookup_by_key(creds.api_key)
        if record is None:
            logger.warning("Invalid API key attempted")
            return None

        return Subject(
            id=record.id,
            type="api_key",
            tenant_id=record.tenant_id,
        )
