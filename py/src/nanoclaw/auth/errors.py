"""Auth error types."""

from __future__ import annotations


class AuthenticationError(Exception):
    """Raised when credentials are missing or invalid (→ HTTP 401)."""

    def __init__(self, reason: str = "authentication failed"):
        self.reason = reason
        super().__init__(reason)


class PermissionDenied(Exception):
    """Raised when an authenticated subject lacks the required permission (→ HTTP 403)."""

    def __init__(self, subject_id: str, permission: str, reason: str = "scope mismatch"):
        self.subject_id = subject_id
        self.permission = permission
        self.reason = reason
        super().__init__(f"{subject_id} denied {permission}: {reason}")


class ApprovalRequired(Exception):
    """Raised when an operation needs human approval before proceeding."""

    def __init__(self, subject_id: str, permission: str, approver_role: str):
        self.subject_id = subject_id
        self.permission = permission
        self.approver_role = approver_role
        super().__init__(f"{subject_id} needs approval from {approver_role} for {permission}")
