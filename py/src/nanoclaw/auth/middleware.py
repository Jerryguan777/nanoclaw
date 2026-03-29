"""Authentication middleware — verifies credentials and builds RequestContext.

Business code never uses this module directly. It is wired in ``main.py``
(the composition root) and injected into HTTP middleware, channel gateways,
and NATS IPC handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nanoclaw.auth.context import RequestContext, Subject
from nanoclaw.auth.errors import AuthenticationError
from nanoclaw.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class RawCredentials:
    """Transport-level credentials before validation.

    Exactly one of the optional fields will be set, depending on the
    transport that produced the credentials.
    """

    type: str  # "bearer_jwt" | "api_key" | "bot_token" | "nats_job" | "internal"

    # Bearer JWT (Web Dashboard)
    token: str | None = None

    # API Key (programmatic access)
    api_key: str | None = None

    # Bot token (Telegram/Slack webhook)
    bot_token: str | None = None

    # NATS job ID (container agent IPC)
    job_id: str | None = None

    # Internal: direct coworker/user ID (scheduled tasks, system operations)
    subject_id: str | None = None
    tenant_id: str | None = None


class IdentityProvider(Protocol):
    """Pluggable identity verifier.

    Implementations live in ``nanoclaw.auth.providers.*``.
    Adding a new login method (e.g. SAML SSO) means adding a new provider
    and registering it in ``main.py`` — zero business code changes.
    """

    def can_handle(self, creds: RawCredentials) -> bool: ...
    async def verify(self, creds: RawCredentials) -> Subject | None: ...


class RoleLoader(Protocol):
    """Loads roles for a subject. Backed by the DB layer."""

    async def load_roles(self, subject: Subject) -> list[str]: ...


class AuthMiddleware:
    """Tries each registered provider in order, returns RequestContext.

    Usage::

        auth = AuthMiddleware(providers=[JwtProvider(...), ApiKeyProvider()], ...)
        ctx = await auth.authenticate(creds)
    """

    def __init__(
        self,
        providers: list[IdentityProvider],
        role_loader: RoleLoader,
    ) -> None:
        self._providers = providers
        self._role_loader = role_loader

    async def authenticate(self, creds: RawCredentials) -> RequestContext:
        """Authenticate credentials and return a RequestContext.

        Raises ``AuthenticationError`` if no provider accepts the credentials.
        """
        for provider in self._providers:
            if not provider.can_handle(creds):
                continue
            subject = await provider.verify(creds)
            if subject is not None:
                roles = await self._role_loader.load_roles(subject)
                logger.debug(
                    "Authenticated",
                    subject_id=subject.id,
                    subject_type=subject.type,
                    tenant=subject.tenant_id,
                    roles=roles,
                )
                return RequestContext(subject=subject, roles=frozenset(roles))

        raise AuthenticationError("no valid credentials")

    async def authenticate_with_delegation(
        self,
        creds: RawCredentials,
        on_behalf_of: Subject | None = None,
    ) -> RequestContext:
        """Authenticate and attach delegation context (for coworker actions)."""
        ctx = await self.authenticate(creds)
        if on_behalf_of is not None:
            return RequestContext(
                subject=ctx.subject,
                roles=ctx.roles,
                on_behalf_of=on_behalf_of,
            )
        return ctx
