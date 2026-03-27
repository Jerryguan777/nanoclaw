"""NATS transport for Orchestrator-side IPC.

Manages JetStream streams and KV buckets used for all 6 IPC channels
between the Orchestrator and container Agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nats
from nats.js.api import KeyValueConfig, RetentionPolicy, StreamConfig

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.client import JetStreamContext

logger = get_logger()

# Stream max age: 1 hour in seconds (nats-py converts to nanoseconds internally)
_STREAM_MAX_AGE_S = 3600.0

# KV TTL: 1 hour in seconds
_KV_TTL_SECONDS = 3600.0


class NatsTransport:
    """NATS transport for Orchestrator-side IPC.

    Provides JetStream and KV access after connect().
    """

    def __init__(self, url: str = "nats://localhost:4222") -> None:
        self._url = url
        self._nc: Client | None = None
        self._js: JetStreamContext | None = None

    async def connect(self) -> None:
        """Connect to NATS and create JetStream stream + KV buckets."""
        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()

        # Create JetStream stream for agent communication
        await self._js.add_stream(
            StreamConfig(
                name="agent-ipc",
                subjects=["agent.*.results", "agent.*.input", "agent.*.messages", "agent.*.tasks"],
                retention=RetentionPolicy.WORK_QUEUE,
                max_age=_STREAM_MAX_AGE_S,
            )
        )

        # Create KV buckets
        await self._js.create_key_value(config=KeyValueConfig(bucket="agent-init", ttl=_KV_TTL_SECONDS))
        await self._js.create_key_value(config=KeyValueConfig(bucket="snapshots", ttl=_KV_TTL_SECONDS))

        logger.info("NATS connected", url=self._url)

    @property
    def nc(self) -> Client:
        """Return the raw NATS client. Raises if not connected."""
        assert self._nc is not None, "NatsTransport not connected"
        return self._nc

    @property
    def js(self) -> JetStreamContext:
        """Return the JetStream context. Raises if not connected."""
        assert self._js is not None, "NatsTransport not connected"
        return self._js

    async def close(self) -> None:
        """Close the NATS connection."""
        if self._nc:
            await self._nc.close()
            self._nc = None
            self._js = None
            logger.info("NATS connection closed")
