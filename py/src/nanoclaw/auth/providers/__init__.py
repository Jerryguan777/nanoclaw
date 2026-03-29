"""Identity provider implementations.

Each provider handles one authentication method. New login methods
(e.g. SAML SSO) are added by creating a new file here and registering
the provider in ``main.py`` — zero business code changes.
"""

from nanoclaw.auth.providers.api_key import ApiKeyProvider
from nanoclaw.auth.providers.bot_token import BotTokenProvider
from nanoclaw.auth.providers.internal import InternalProvider
from nanoclaw.auth.providers.jwt import JwtProvider
from nanoclaw.auth.providers.nats_job import NatsJobProvider

__all__ = [
    "ApiKeyProvider",
    "BotTokenProvider",
    "InternalProvider",
    "JwtProvider",
    "NatsJobProvider",
]
