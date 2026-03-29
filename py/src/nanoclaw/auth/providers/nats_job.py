"""NATS job identity provider for container agent IPC.

When an agent running inside a container publishes to
``agent.{job_id}.messages`` or ``agent.{job_id}.tasks``, the orchestrator
extracts the ``job_id`` from the NATS subject and resolves it to the
coworker identity and the original human trigger (``on_behalf_of``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nanoclaw.auth.context import Subject
from nanoclaw.auth.middleware import RawCredentials
from nanoclaw.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class JobMetadata:
    """Metadata stored when a container job is created."""

    job_id: str
    coworker_id: str
    tenant_id: str
    on_behalf_of: Subject | None  # original human trigger


class JobMetadataStore(Protocol):
    """Looks up job metadata. Backed by NATS KV or in-memory map."""

    async def get_job_metadata(self, job_id: str) -> JobMetadata | None: ...


class NatsJobProvider:
    """Resolve NATS job_id → coworker identity."""

    def __init__(self, store: JobMetadataStore) -> None:
        self._store = store

    def can_handle(self, creds: RawCredentials) -> bool:
        return creds.type == "nats_job" and creds.job_id is not None

    async def verify(self, creds: RawCredentials) -> Subject | None:
        if creds.job_id is None:
            return None

        meta = await self._store.get_job_metadata(creds.job_id)
        if meta is None:
            logger.warning("Unknown NATS job_id", job_id=creds.job_id)
            return None

        return Subject(
            id=meta.coworker_id,
            type="coworker",
            tenant_id=meta.tenant_id,
        )
