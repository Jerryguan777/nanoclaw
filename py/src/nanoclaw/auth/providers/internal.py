"""Internal identity provider for system operations.

Used when the system itself needs to act as a subject — scheduled task
execution, startup recovery, etc. No external credentials involved;
the caller directly provides the subject identity.
"""

from __future__ import annotations

from nanoclaw.auth.context import Subject
from nanoclaw.auth.middleware import RawCredentials


class InternalProvider:
    """Resolve internal subject_id + tenant_id to a Subject.

    Trusted — only used for system-initiated operations where the caller
    already knows the identity (e.g. scheduled task with ``created_by_user_id``).
    """

    def can_handle(self, creds: RawCredentials) -> bool:
        return creds.type == "internal" and creds.subject_id is not None and creds.tenant_id is not None

    async def verify(self, creds: RawCredentials) -> Subject | None:
        if creds.subject_id is None or creds.tenant_id is None:
            return None

        # Determine type from ID prefix convention, or default to "user"
        subject_type = "coworker" if creds.subject_id.startswith("cw-") else "user"

        return Subject(
            id=creds.subject_id,
            type=subject_type,  # type: ignore[arg-type]
            tenant_id=creds.tenant_id,
        )
