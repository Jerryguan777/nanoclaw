"""Request context — the ONLY auth type that business code imports.

All other auth internals (JWT validation, API key hashing, RBAC lookups)
stay behind the middleware/authz boundary. Business logic receives a
``RequestContext`` and never needs to know how it was built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Subject:
    """An authenticated principal — human or AI."""

    id: str
    type: Literal["user", "coworker", "api_key"]
    tenant_id: str

    def serialize(self) -> dict[str, str]:
        return {"id": self.id, "type": self.type, "tenant_id": self.tenant_id}

    @classmethod
    def deserialize(cls, data: dict[str, str]) -> Subject:
        return cls(id=data["id"], type=data["type"], tenant_id=data["tenant_id"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class RequestContext:
    """Flows through the entire request lifecycle.

    ``subject`` is the direct executor (user clicking a button, or coworker
    running in a container).

    ``on_behalf_of`` is the original human trigger when a coworker acts on
    someone's behalf. This enables the *effective permission* check:
    the operation is allowed only if **both** the coworker and the original
    human have the required permission, preventing privilege escalation.
    """

    subject: Subject
    roles: frozenset[str]
    on_behalf_of: Subject | None = None

    @property
    def tenant_id(self) -> str:
        return self.subject.tenant_id

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles
