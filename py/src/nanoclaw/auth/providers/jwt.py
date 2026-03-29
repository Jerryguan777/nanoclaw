"""JWT identity provider for Web Dashboard users.

Validates tokens issued by an external IdP (Auth0, Clerk, WorkOS, etc.).
The JWKS endpoint is used to verify token signatures without sharing secrets.
"""

from __future__ import annotations

from typing import Any

from nanoclaw.auth.context import Subject
from nanoclaw.auth.middleware import RawCredentials
from nanoclaw.core.logger import get_logger

logger = get_logger()


class JwtProvider:
    """Verify Bearer JWT tokens from external identity providers."""

    def __init__(self, jwks_url: str, audience: str | None = None, issuer: str | None = None) -> None:
        self._jwks_url = jwks_url
        self._audience = audience
        self._issuer = issuer
        # JWKS key cache populated on first verify
        self._jwks_cache: dict[str, Any] | None = None

    def can_handle(self, creds: RawCredentials) -> bool:
        return creds.type == "bearer_jwt" and creds.token is not None

    async def verify(self, creds: RawCredentials) -> Subject | None:
        """Decode and validate the JWT, return Subject or None.

        TODO: Implement with PyJWT or python-jose:
        1. Fetch JWKS from self._jwks_url (cache keys)
        2. Decode token with RS256
        3. Validate exp, iss, aud
        4. Extract: sub (user ID), tenant_id (from custom claim or org metadata)
        5. Return Subject(id=sub, type="user", tenant_id=tenant_id)
        """
        if creds.token is None:
            return None

        logger.debug("JWT verification not yet implemented")
        return None
