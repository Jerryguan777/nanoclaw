"""Authorization service — permission check engine.

Pure policy lookup. No business logic, no side effects beyond audit logging.
All state comes from the database via the ``GrantLoader`` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Protocol

from nanoclaw.auth.context import RequestContext, Subject
from nanoclaw.auth.errors import ApprovalRequired, PermissionDenied
from nanoclaw.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class PermissionGrant:
    """A single role_permissions row."""

    permission: str
    scope: dict[str, Any]


@dataclass(frozen=True)
class ApprovalPolicy:
    """A single approval_policies row."""

    permission: str
    approval_type: str  # "auto" | "human_required" | "conditional"
    condition: dict[str, Any] | None
    approver_role: str | None


class GrantLoader(Protocol):
    """Loads permission grants for a set of roles. Backed by DB."""

    async def load_grants(self, role_names: frozenset[str], tenant_id: str) -> list[PermissionGrant]: ...


class ApprovalPolicyLoader(Protocol):
    """Loads approval policies for a coworker. Backed by DB."""

    async def load_policies(self, coworker_id: str, tenant_id: str) -> list[ApprovalPolicy]: ...


class AuditLogger(Protocol):
    """Records authorization decisions for audit trail."""

    async def log(
        self,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        on_behalf_of_id: str | None,
        action: str,
        resource: str | None,
        result: str,
        context: dict[str, Any] | None,
    ) -> None: ...


class AuthzService:
    """Stateless authorization engine.

    Check flow:
    1. Admin bypass — admins pass all checks.
    2. Direct subject check — the executor must have the permission with matching scope.
    3. Delegation check — if ``on_behalf_of`` is set, the delegator must ALSO have
       the permission (prevents privilege escalation through coworker proxying).
    4. Approval check — for coworker subjects, check if a human approval policy applies.
    """

    def __init__(
        self,
        grant_loader: GrantLoader,
        approval_loader: ApprovalPolicyLoader | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._grant_loader = grant_loader
        self._approval_loader = approval_loader
        self._audit_logger = audit_logger

    async def check(
        self,
        ctx: RequestContext,
        permission: str,
        resource: dict[str, Any] | None = None,
    ) -> None:
        """Pass silently if allowed; raise ``PermissionDenied`` or ``ApprovalRequired``."""
        resource = resource or {}
        result = "allowed"
        try:
            await self._do_check(ctx, permission, resource)
        except (PermissionDenied, ApprovalRequired) as exc:
            result = "denied" if isinstance(exc, PermissionDenied) else "pending_approval"
            raise
        finally:
            if self._audit_logger is not None:
                await self._audit_logger.log(
                    tenant_id=ctx.tenant_id,
                    subject_type=ctx.subject.type,
                    subject_id=ctx.subject.id,
                    on_behalf_of_id=ctx.on_behalf_of.id if ctx.on_behalf_of else None,
                    action=permission,
                    resource=str(resource) if resource else None,
                    result=result,
                    context=resource,
                )

    async def _do_check(
        self,
        ctx: RequestContext,
        permission: str,
        resource: dict[str, Any],
    ) -> None:
        # 1. Admin bypass
        if ctx.is_admin:
            return

        # 2. Direct subject must have permission
        await self._check_subject(ctx.subject, ctx.roles, permission, resource)

        # 3. Delegation check (privilege escalation prevention)
        if ctx.on_behalf_of is not None:
            delegator_roles = await self._load_roles_for_subject(ctx.on_behalf_of)
            await self._check_subject(ctx.on_behalf_of, delegator_roles, permission, resource)

        # 4. Coworker approval policies
        if ctx.subject.type == "coworker" and self._approval_loader is not None:
            await self._check_approval(ctx.subject, permission, resource)

    async def _check_subject(
        self,
        subject: Subject,
        roles: frozenset[str],
        permission: str,
        resource: dict[str, Any],
    ) -> None:
        grants = await self._grant_loader.load_grants(roles, subject.tenant_id)
        for grant in grants:
            if grant.permission == permission and _scope_matches(grant.scope, resource):
                return
        raise PermissionDenied(subject.id, permission)

    async def _check_approval(
        self,
        subject: Subject,
        permission: str,
        resource: dict[str, Any],
    ) -> None:
        assert self._approval_loader is not None
        policies = await self._approval_loader.load_policies(subject.id, subject.tenant_id)
        for policy in policies:
            if policy.permission != permission:
                continue
            if policy.approval_type == "auto":
                continue
            if policy.approval_type == "human_required":
                raise ApprovalRequired(subject.id, permission, policy.approver_role or "admin")
            if policy.approval_type == "conditional" and policy.condition:
                if _condition_triggers(policy.condition, resource):
                    raise ApprovalRequired(subject.id, permission, policy.approver_role or "admin")

    async def _load_roles_for_subject(self, subject: Subject) -> frozenset[str]:
        """Load roles for a subject that isn't the current request's direct subject."""
        # This goes through the grant loader's backing store (DB).
        # In a real implementation, the RoleLoader from middleware would be shared.
        # For now, we load grants for all possible roles and extract unique role names.
        # TODO: inject a shared RoleLoader when wiring in main.py
        return frozenset()

    # -- Decorator for API routes ------------------------------------------

    def require(self, permission: str, **scope_mapping: str) -> Any:
        """Decorator that checks permission before calling the handler.

        ``scope_mapping`` maps scope field names to handler kwarg names::

            @authz.require("tasks.create", department="department")
            async def create_task(ctx: RequestContext, department: str, ...):
                ...
        """

        def decorator(fn: Any) -> Any:
            @wraps(fn)
            async def wrapper(ctx: RequestContext, *args: Any, **kwargs: Any) -> Any:
                resource = {k: kwargs.get(v, v) for k, v in scope_mapping.items()}
                await self.check(ctx, permission, resource)
                return await fn(ctx, *args, **kwargs)

            return wrapper

        return decorator


# ---------------------------------------------------------------------------
# Scope matching helpers
# ---------------------------------------------------------------------------


def _scope_matches(scope: dict[str, Any], context: dict[str, Any]) -> bool:
    """Check if a permission scope matches the resource context.

    Empty scope ``{}`` means unrestricted (matches everything).
    Otherwise, every field in the scope must match the context.

    Supports suffix operators:
    - ``_gt``: context value must be > scope value
    - ``_lt``: context value must be < scope value
    - ``_in``: context value must be in scope list
    """
    if not scope:
        return True
    for key, required in scope.items():
        if key.endswith("_gt"):
            field = key[:-3]
            if context.get(field, 0) <= required:
                return False
        elif key.endswith("_lt"):
            field = key[:-3]
            if context.get(field, 0) >= required:
                return False
        elif key.endswith("_in"):
            field = key[:-3]
            if context.get(field) not in required:
                return False
        elif context.get(key) != required:
            return False
    return True


def _condition_triggers(condition: dict[str, Any], resource: dict[str, Any]) -> bool:
    """Check if an approval policy condition is triggered.

    Uses the same matching logic as scope, but semantics are inverted:
    the condition describes WHEN approval is needed, not when it's allowed.
    """
    return _scope_matches(condition, resource)
