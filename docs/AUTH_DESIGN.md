# Authentication & Authorization Design

Multi-tenant auth system for NanoClaw Agent-as-a-Service platform.

## Overview

Two types of principals (subjects): **human users** and **AI coworkers**. Both go through the same authorization pipeline. Business code never touches auth details — it only sees `RequestContext`.

```
Transport (HTTP/WebSocket/NATS/Telegram)
    │
    ▼
Auth Middleware (verify JWT / API Key / Bot Token)
    │ outputs RequestContext
    ▼
Authz Guard (check permission + scope)
    │ pass → continue; deny → 403
    ▼
Business Logic (only sees ctx.tenant_id, ctx.subject_id)
```

---

## 1. Entity Model

### 1.1 Multi-Tenant Hierarchy

```
Tenant (company)
 ├── User (human employee) ×N
 ├── AgentBlueprint (agent type/template) ×N
 │    e.g. "ops agent", "finance agent"
 │    defines: system_prompt, CLAUDE.md, tools, model_backend
 │
 ├── Coworker (blueprint instance) ×N
 │    e.g. "ops-xiaowang" (blueprint=ops_agent, name="小王")
 │    ├── ChannelBinding ×N (Telegram bot, Slack workspace, Web)
 │    └── Conversation ×N (≈ current RegisteredGroup)
 │
 └── ScheduledTask ×N (bound to coworker + conversation)
```

### 1.2 Key Distinction: Blueprint vs Coworker

- **AgentBlueprint** defines what the agent CAN do (capabilities, tools, persona)
- **Coworker** defines WHO the agent IS (name, trigger pattern, channel bindings)
- Same blueprint can have multiple coworker instances (e.g. two ops bots for different teams)

---

## 2. Database Schema

### 2.1 Tenant & Identity Tables

```sql
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT DEFAULT 'free',           -- free | pro | enterprise
    max_coworkers INT DEFAULT 10,
    max_concurrent INT DEFAULT 5,       -- platform-wide container limit for this tenant
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    email TEXT,
    role TEXT DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_blueprints (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,                  -- "ops_agent", "finance_agent"
    description TEXT,
    system_prompt TEXT,
    claude_md TEXT,                      -- mounted into container as CLAUDE.md
    role_config JSONB,
    model_backend TEXT DEFAULT 'claude-code',
    container_image TEXT,
    container_timeout INT DEFAULT 1800000,
    tools JSONB DEFAULT '[]',           -- available MCP servers / skills
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE coworkers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    blueprint_id TEXT NOT NULL REFERENCES agent_blueprints(id),
    name TEXT NOT NULL,                  -- replaces global ASSISTANT_NAME
    display_name TEXT,
    system_prompt_override TEXT,         -- NULL = use blueprint default
    trigger_pattern TEXT,               -- per-instance trigger regex
    max_concurrent INT DEFAULT 2,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE channel_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,          -- 'telegram' | 'slack' | 'web' | 'discord'
    credentials JSONB NOT NULL,         -- encrypted: { bot_token, webhook_url, ... }
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT NOT NULL REFERENCES coworkers(id),
    channel_binding_id TEXT NOT NULL REFERENCES channel_bindings(id),
    jid TEXT NOT NULL,                   -- external platform group/channel ID
    name TEXT NOT NULL,
    folder TEXT NOT NULL,                -- isolated filesystem path
    requires_trigger BOOLEAN DEFAULT TRUE,
    is_main BOOLEAN DEFAULT FALSE,
    container_config JSONB,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (coworker_id, channel_binding_id, jid),
    UNIQUE (tenant_id, folder)
);
```

### 2.2 Auth & Identity Tables

```sql
-- External identity provider mappings (Auth0 / Google / Azure AD / SAML)
CREATE TABLE auth_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,             -- 'auth0' | 'google' | 'azure_ad' | 'saml_okta'
    provider_user_id TEXT NOT NULL,     -- sub / nameID from IdP
    email TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_user_id)
);

-- API keys for programmatic access (external systems, CI/CD, coworker-to-coworker)
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    user_id TEXT REFERENCES users(id),          -- who created it (audit trail)
    coworker_id TEXT REFERENCES coworkers(id),  -- NULL = not coworker-scoped
    key_hash TEXT NOT NULL,                     -- bcrypt hash, never store plaintext
    name TEXT NOT NULL,                         -- human-readable label
    scopes JSONB DEFAULT '[]',                  -- additional restrictions: ["messages.send"]
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.3 RBAC Tables

```sql
-- System-defined permissions
CREATE TABLE permissions (
    id TEXT PRIMARY KEY,                -- 'conversations.read', 'tools.finance', etc.
    resource TEXT NOT NULL,             -- 'conversations', 'tasks', 'tools', ...
    action TEXT NOT NULL,               -- 'read', 'create', 'update', 'delete', 'execute'
    description TEXT,
    is_system BOOLEAN DEFAULT TRUE,     -- system-defined permissions cannot be deleted
    UNIQUE (resource, action)
);

-- Roles (per-tenant, system roles auto-created for new tenants)
CREATE TABLE roles (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,                 -- 'admin', 'ops_manager', 'ops_agent', ...
    description TEXT,
    is_system BOOLEAN DEFAULT FALSE,
    assignable_to TEXT DEFAULT 'both',  -- 'user' | 'coworker' | 'both'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- Role → Permission mapping with resource scope
CREATE TABLE role_permissions (
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id TEXT NOT NULL REFERENCES permissions(id),
    scope JSONB DEFAULT '{}',           -- e.g. {"department":"ops"}, {} = unrestricted
    PRIMARY KEY (role_id, permission_id)
);

-- User → Role assignment
CREATE TABLE user_roles (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by TEXT,
    PRIMARY KEY (user_id, role_id)
);

-- Coworker → Role assignment
CREATE TABLE coworker_roles (
    coworker_id TEXT NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (coworker_id, role_id)
);

-- Approval policies for coworker sensitive operations
CREATE TABLE approval_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT REFERENCES coworkers(id),  -- NULL = tenant-wide
    permission_id TEXT NOT NULL REFERENCES permissions(id),
    approval_type TEXT NOT NULL,         -- 'auto' | 'human_required' | 'conditional'
    condition JSONB,                    -- {"amount_gt": 10000}
    approver_role_id TEXT REFERENCES roles(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.4 Audit Log

```sql
CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,          -- 'user' | 'coworker' | 'api_key'
    subject_id TEXT NOT NULL,
    on_behalf_of_id TEXT,               -- original human trigger (for coworker actions)
    action TEXT NOT NULL,               -- 'tools.finance.execute'
    resource TEXT,                       -- 'invoice:INV-2026-001'
    result TEXT NOT NULL,               -- 'allowed' | 'denied' | 'pending_approval'
    context JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_tenant_ts ON audit_logs(tenant_id, created_at DESC);
```

### 2.5 Row-Level Security (defense in depth)

```sql
-- Even if application code has a bug that forgets WHERE tenant_id = ...,
-- RLS prevents cross-tenant data leakage at the database level.

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE scheduled_tasks ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON messages
    USING (tenant_id = current_setting('app.tenant_id'));
CREATE POLICY tenant_isolation ON conversations
    USING (tenant_id = current_setting('app.tenant_id'));
CREATE POLICY tenant_isolation ON scheduled_tasks
    USING (tenant_id = current_setting('app.tenant_id'));

-- Usage: each DB connection sets tenant context before queries
-- SET app.tenant_id = 'tenant-abc';
```

---

## 3. System Permissions (seed data)

```sql
INSERT INTO permissions (id, resource, action, description) VALUES
-- Conversations
('conversations.read',       'conversations', 'read',    'View conversation content'),
('conversations.create',     'conversations', 'create',  'Create new conversations'),
('conversations.delete',     'conversations', 'delete',  'Delete conversations'),

-- Messages
('messages.send',            'messages',      'execute', 'Send messages'),
('messages.read_history',    'messages',      'read',    'Read message history'),

-- Scheduled tasks
('tasks.create',             'tasks',         'create',  'Create scheduled tasks'),
('tasks.manage',             'tasks',         'update',  'Update/delete tasks'),
('tasks.execute',            'tasks',         'execute', 'Execute scheduled tasks'),

-- Coworker management
('coworkers.create',         'coworkers',     'create',  'Create coworker instances'),
('coworkers.configure',      'coworkers',     'update',  'Modify coworker configuration'),
('coworkers.delete',         'coworkers',     'delete',  'Delete coworkers'),

-- Blueprint management
('blueprints.create',        'blueprints',    'create',  'Create agent blueprints'),
('blueprints.configure',     'blueprints',    'update',  'Modify blueprint configuration'),

-- Channel management
('channels.manage',          'channels',      'update',  'Configure channel bindings'),

-- Tenant administration
('tenant.manage',            'tenant',        'update',  'Modify tenant settings'),
('tenant.billing',           'tenant',        'execute', 'Manage billing'),
('users.manage',             'users',         'update',  'Manage users and roles'),

-- IPC (coworker-to-coworker communication)
('ipc.send_message',         'ipc',           'execute', 'Send IPC messages to other groups'),
('ipc.create_task',          'ipc',           'execute', 'Create tasks via IPC'),

-- External tools
('tools.finance',            'tools',         'execute', 'Use finance tools'),
('tools.data_query',         'tools',         'execute', 'Use data query tools'),
('tools.external_api',       'tools',         'execute', 'Call external APIs');
```

---

## 4. Role Templates

Example permission matrix:

```
                    admin  ops_mgr  ops_staff  fin_mgr  fin_staff  ops_agent  fin_agent
tenant.manage        Y
users.manage         Y      ops                fin
blueprints.*         Y
coworkers.create     Y      ops                fin
coworkers.configure  Y      ops                fin
channels.manage      Y
conversations.read   Y      ops       ops      fin       fin       ops        fin
messages.send        Y      ops       ops      fin       fin       ops        fin
tasks.create         Y      ops       ops      fin       fin       ops        fin
tasks.manage         Y      ops                fin
tools.finance        Y                         Y       <10000                  Y
tools.data_query     Y      Y        Y                            Y
ipc.send_message     Y                                            ops->ops
ipc.create_task      Y      Y                  Y                  Y
```

Scope column values (e.g. "ops") mean `{"department": "ops"}` in the `role_permissions.scope` JSONB field.

---

## 5. Core Types

```python
# nanoclaw/auth/context.py

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Subject:
    """An authenticated principal — human or AI."""
    id: str
    type: Literal["user", "coworker", "api_key"]
    tenant_id: str


@dataclass(frozen=True)
class RequestContext:
    """Flows through the entire request lifecycle.

    This is the ONLY type that business code imports from the auth module.
    """
    subject: Subject
    roles: frozenset[str]
    on_behalf_of: Subject | None = None  # for delegated execution

    @property
    def tenant_id(self) -> str:
        return self.subject.tenant_id

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles
```

---

## 6. Authentication (who are you?)

### 6.1 Pluggable Identity Providers

```python
# nanoclaw/auth/middleware.py

class IdentityProvider(Protocol):
    """Pluggable identity verifier."""
    def can_handle(self, creds: RawCredentials) -> bool: ...
    async def verify(self, creds: RawCredentials) -> Subject | None: ...


class AuthMiddleware:
    """Tries each provider in order, returns RequestContext on first match."""

    def __init__(self, providers: list[IdentityProvider]):
        self._providers = providers

    async def authenticate(self, creds: RawCredentials) -> RequestContext:
        for provider in self._providers:
            if not provider.can_handle(creds):
                continue
            subject = await provider.verify(creds)
            if subject is not None:
                roles = await self._load_roles(subject)
                return RequestContext(subject=subject, roles=frozenset(roles))
        raise AuthenticationError("no valid credentials")
```

### 6.2 Provider Implementations

| Provider | Handles | Used by |
|----------|---------|---------|
| `JwtProvider` | Bearer JWT from Auth0/Clerk/WorkOS | Web Dashboard users |
| `ApiKeyProvider` | `X-API-Key` header | External systems, CI/CD |
| `BotTokenProvider` | Bot token → coworker lookup | Telegram/Slack webhooks |
| `InternalNatsProvider` | NATS job_id → coworker lookup | Container agent IPC |

Adding a new login method (e.g. SAML SSO) = add one provider file + register in main.py. Zero business code changes.

### 6.3 Enterprise Login Priority

1. **Google OAuth + email/password** (covers 90% of early users)
2. **SAML SSO** via Auth0/WorkOS (when enterprise customers require it)
3. **API Keys** from day one (needed for integrations)

---

## 7. Authorization (can you do this?)

### 7.1 AuthzService

```python
# nanoclaw/auth/authz.py

class AuthzService:
    """Pure policy lookup — no business logic."""

    async def check(
        self, ctx: RequestContext, permission: str, resource: dict | None = None,
    ) -> None:
        """Pass silently or raise PermissionDenied."""
        if ctx.is_admin:
            return

        # 1. Direct executor must have permission
        await self._check_subject(ctx.subject, ctx.roles, permission, resource or {})

        # 2. If delegated, delegator must ALSO have permission (prevent privilege escalation)
        if ctx.on_behalf_of is not None:
            delegator_roles = await self._load_roles(ctx.on_behalf_of)
            await self._check_subject(ctx.on_behalf_of, delegator_roles, permission, resource or {})

        # 3. If coworker, check approval policies
        if ctx.subject.type == "coworker":
            await self._check_approval_policy(ctx, permission, resource or {})

    async def _check_subject(
        self, subject: Subject, roles: frozenset[str], permission: str, resource: dict,
    ) -> None:
        grants = await self._load_grants(roles)
        for grant in grants:
            if grant.permission == permission and self._scope_matches(grant.scope, resource):
                return
        raise PermissionDenied(subject.id, permission)

    def _scope_matches(self, scope: dict, context: dict) -> bool:
        """Empty scope {} = unrestricted. Otherwise match each field."""
        if not scope:
            return True
        for key, required in scope.items():
            if key.endswith("_gt"):
                if context.get(key[:-3], 0) <= required:
                    return False
            elif key.endswith("_lt"):
                if context.get(key[:-3], 0) >= required:
                    return False
            elif context.get(key) != required:
                return False
        return True
```

### 7.2 Decorator for API Routes

```python
    def require(self, permission: str, **scope_mapping: str):
        """Decorator for API endpoints."""
        def decorator(fn):
            @wraps(fn)
            async def wrapper(ctx: RequestContext, *args, **kwargs):
                resource = {k: kwargs.get(v, v) for k, v in scope_mapping.items()}
                await self.check(ctx, permission, resource)
                return await fn(ctx, *args, **kwargs)
            return wrapper
        return decorator

# Usage:
@router.post("/tasks")
@authz.require("tasks.create", department="department")
async def create_task(ctx: RequestContext, req: CreateTaskRequest):
    ...  # business logic only
```

---

## 8. Effective Permission (Privilege Escalation Prevention)

### 8.1 Problem

A low-privilege user can instruct a high-privilege coworker to perform actions the user cannot:

```
User "小赵" (ops_staff, no data_query permission)
    → tells coworker "小王" (ops_agent, has data_query permission)
    → "帮我查VIP客户消费记录"
    → 小王 executes query, returns data to 小赵
    → 小赵 sees data they shouldn't have access to
```

### 8.2 Solution: Dual Permission Check

When a coworker acts on behalf of a human, the effective permission is the **intersection** of both:

```
effective_permission = coworker_permissions ∩ trigger_user_permissions
```

Implementation via `on_behalf_of` field in `RequestContext`:

```python
# Agent startup — attach trigger user's identity
output = await executor.execute(
    AgentInput(
        prompt=prompt,
        on_behalf_of=resolve_sender_identity(trigger_msg.sender, tenant_id),
        ...
    ),
)

# IPC handler — dual check
ctx = RequestContext(
    subject=coworker_subject,
    roles=coworker_roles,
    on_behalf_of=original_human_subject,  # restored from job metadata
)
await authz.check(ctx, "tools.data_query", {"department": "ops"})
# This checks BOTH coworker AND human have the permission
```

### 8.3 on_behalf_of Rules

| Scenario | on_behalf_of | Permission check |
|----------|-------------|------------------|
| Human via Web Dashboard | N/A (human is the subject) | user only |
| Human triggers coworker via message | message sender | coworker ∩ user |
| Scheduled task executes | task creator (`created_by_user_id`) | coworker ∩ creator |
| Coworker autonomous action | None | coworker only |
| Coworker chain (A → B → C) | original human trigger | current_coworker ∩ original_user |

For coworker chains, always propagate the **original human trigger**, never an intermediate coworker.

---

## 9. Integration Points

### 9.1 HTTP API (Web Dashboard)

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    creds = extract_credentials(request)
    ctx = await auth.authenticate(creds)
    request.state.ctx = ctx
    # Set PG RLS context
    await db.execute(f"SET app.tenant_id = '{ctx.tenant_id}'")
    return await call_next(request)
```

### 9.2 Channel Messages (Telegram/Slack)

```python
class TelegramGateway:
    async def _on_update(self, update):
        # Auth: bot token → coworker identity
        ctx = await self._auth.authenticate(
            RawCredentials(type="bot_token", token=self._bot_token)
        )
        # Authz: can sender interact with this coworker?
        sender_ctx = await self._resolve_sender(update.message.from_user, ctx.tenant_id)
        await self._authz.check(sender_ctx, "messages.send", {"coworker_id": ctx.subject.id})
        # Pass to business logic
        self._on_message(ctx, update.message)
```

### 9.3 NATS IPC (Container Agent)

```python
async def _handle_tool_call(msg):
    job_id = msg.subject.split(".")[1]
    job_meta = await get_job_metadata(job_id)
    ctx = RequestContext(
        subject=job_meta.coworker_subject,
        roles=job_meta.coworker_roles,
        on_behalf_of=job_meta.on_behalf_of,
    )
    await authz.check(ctx, ...)
```

### 9.4 Scheduled Tasks

```python
async def _execute_task(task: ScheduledTask):
    ctx = RequestContext(
        subject=Subject(task.coworker_id, "coworker", task.tenant_id),
        roles=await load_coworker_roles(task.coworker_id),
        on_behalf_of=Subject(task.created_by_user_id, "user", task.tenant_id),
    )
    await authz.check(ctx, "tasks.execute", {"task_id": task.id})
```

---

## 10. Module Structure

```
nanoclaw/
├── auth/                           # Independent auth module
│   ├── __init__.py                 # exports RequestContext, AuthMiddleware, AuthzService
│   ├── context.py                  # RequestContext, Subject (the ONLY thing business code imports)
│   ├── middleware.py               # AuthMiddleware + IdentityProvider protocol
│   ├── authz.py                    # AuthzService (permission check engine)
│   ├── errors.py                   # AuthenticationError, PermissionDenied
│   └── providers/                  # Pluggable identity verifiers
│       ├── __init__.py
│       ├── jwt.py                  # JwtProvider (Web Dashboard)
│       ├── api_key.py              # ApiKeyProvider (programmatic access)
│       ├── bot_token.py            # BotTokenProvider (Telegram/Slack)
│       └── nats_job.py             # InternalNatsProvider (container IPC)
│
├── core/                           # Business logic — does NOT import auth internals
│   ├── types.py                    # only imports auth.context.RequestContext
│   └── ...
│
├── api/                            # HTTP layer — thin, assembles auth + business
│   ├── app.py                      # FastAPI app + auth middleware registration
│   └── routes/
│       ├── tasks.py
│       └── conversations.py
│
└── main.py                         # Composition root — wires everything together
```

Dependency direction:

```
providers/ → auth/middleware.py → auth/context.py ← core/
                                                  ← api/
                                                  ← channels/
                                                  ← main.py
```

`core/` depends only on `auth/context.py` (a single dataclass file), not on any auth implementation.

---

## 11. Composition Root (main.py)

```python
async def main():
    ...
    # 1. Build auth layer (pluggable providers)
    auth = AuthMiddleware([
        JwtProvider(jwks_url=AUTH0_JWKS_URL),
        ApiKeyProvider(),
        BotTokenProvider(),
        InternalNatsProvider(transport=transport),
    ])

    # 2. Build authz layer
    authz = AuthzService()

    # 3. Inject into consumers
    for channel_name in get_registered_channel_names():
        channel = create_channel(channel_name, auth=auth, authz=authz, ...)

    ipc_handler = IpcHandler(auth=auth, authz=authz, deps=ipc_deps)
    api_app = create_api_app(auth=auth, authz=authz)
    ...
```

---

## 12. Implementation Phases

| Phase | Work | Risk | Business impact |
|-------|------|------|-----------------|
| 1 | Create `auth/context.py` with `RequestContext`, `Subject` | None | None — just types |
| 2 | Create DB tables (tenants, users, roles, permissions, etc.) | Low | None — additive |
| 3 | Implement `AuthMiddleware` + `JwtProvider` + `ApiKeyProvider` | Low | None — new code |
| 4 | Implement `AuthzService.check()` with scope matching | Low | None — new code |
| 5 | Add `@authz.require()` to HTTP API routes | Low | API now requires auth |
| 6 | Add auth/authz to NATS IPC handlers (replace `is_main` check) | Medium | Core change |
| 7 | Add `on_behalf_of` propagation for privilege escalation prevention | Medium | Requires job metadata changes |
| 8 | Add `approval_policies` for coworker sensitive operations | Low | New feature |
| 9 | Add audit logging | Low | Observability |
| 10 | Add SAML SSO via Auth0/WorkOS (when enterprise customers need it) | Low | New provider only |
