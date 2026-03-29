-- Migration 001: Multi-tenant entity model + authentication & authorization
--
-- Adds: tenants, users, agent_blueprints, coworkers, channel_bindings,
--        conversations, auth_identities, api_keys, RBAC tables, audit_logs
--
-- Prerequisites: existing tables (chats, messages, scheduled_tasks, etc.)
--                already have tenant_id columns from the SQLite→PG migration.

BEGIN;

-- =============================================
-- 1. Core multi-tenant entities
-- =============================================

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT DEFAULT 'free',
    max_coworkers INT DEFAULT 10,
    max_concurrent INT DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    email TEXT,
    role TEXT DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS agent_blueprints (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    description TEXT,
    system_prompt TEXT,
    claude_md TEXT,
    role_config JSONB,
    model_backend TEXT DEFAULT 'claude-code',
    container_image TEXT,
    container_timeout INT DEFAULT 1800000,
    tools JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS coworkers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    blueprint_id TEXT NOT NULL REFERENCES agent_blueprints(id),
    name TEXT NOT NULL,
    display_name TEXT,
    system_prompt_override TEXT,
    trigger_pattern TEXT,
    max_concurrent INT DEFAULT 2,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_coworkers_tenant ON coworkers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_coworkers_blueprint ON coworkers(blueprint_id);

CREATE TABLE IF NOT EXISTS channel_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,
    credentials JSONB NOT NULL,
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_bindings_coworker ON channel_bindings(coworker_id);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT NOT NULL REFERENCES coworkers(id),
    channel_binding_id TEXT NOT NULL REFERENCES channel_bindings(id),
    jid TEXT NOT NULL,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    requires_trigger BOOLEAN DEFAULT TRUE,
    is_main BOOLEAN DEFAULT FALSE,
    container_config JSONB,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (coworker_id, channel_binding_id, jid),
    UNIQUE (tenant_id, folder)
);
CREATE INDEX IF NOT EXISTS idx_conversations_coworker ON conversations(coworker_id);
CREATE INDEX IF NOT EXISTS idx_conversations_jid ON conversations(tenant_id, jid);

-- =============================================
-- 2. Authentication tables
-- =============================================

-- External IdP identity mappings (Auth0, Google, Azure AD, SAML)
CREATE TABLE IF NOT EXISTS auth_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    email TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_auth_identities_user ON auth_identities(user_id);

-- API keys for programmatic access
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    user_id TEXT REFERENCES users(id),
    coworker_id TEXT REFERENCES coworkers(id),
    key_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    scopes JSONB DEFAULT '[]',
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- =============================================
-- 3. RBAC tables
-- =============================================

-- System-defined permissions
CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT,
    is_system BOOLEAN DEFAULT TRUE,
    UNIQUE (resource, action)
);

-- Roles (per-tenant)
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    description TEXT,
    is_system BOOLEAN DEFAULT FALSE,
    assignable_to TEXT DEFAULT 'both',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- Role → Permission with scope
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id TEXT NOT NULL REFERENCES permissions(id),
    scope JSONB DEFAULT '{}',
    PRIMARY KEY (role_id, permission_id)
);

-- User → Role
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by TEXT,
    PRIMARY KEY (user_id, role_id)
);

-- Coworker → Role
CREATE TABLE IF NOT EXISTS coworker_roles (
    coworker_id TEXT NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (coworker_id, role_id)
);

-- Approval policies for coworker sensitive operations
CREATE TABLE IF NOT EXISTS approval_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    coworker_id TEXT REFERENCES coworkers(id),
    permission_id TEXT NOT NULL REFERENCES permissions(id),
    approval_type TEXT NOT NULL,
    condition JSONB,
    approver_role_id TEXT REFERENCES roles(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approval_policies_coworker ON approval_policies(coworker_id);

-- =============================================
-- 4. Audit log
-- =============================================

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    on_behalf_of_id TEXT,
    action TEXT NOT NULL,
    resource TEXT,
    result TEXT NOT NULL,
    context JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_logs(subject_id, created_at DESC);

-- =============================================
-- 5. Seed system permissions
-- =============================================

INSERT INTO permissions (id, resource, action, description) VALUES
    ('conversations.read',       'conversations', 'read',    'View conversation content'),
    ('conversations.create',     'conversations', 'create',  'Create new conversations'),
    ('conversations.delete',     'conversations', 'delete',  'Delete conversations'),
    ('messages.send',            'messages',      'execute', 'Send messages'),
    ('messages.read_history',    'messages',      'read',    'Read message history'),
    ('tasks.create',             'tasks',         'create',  'Create scheduled tasks'),
    ('tasks.manage',             'tasks',         'update',  'Update/delete tasks'),
    ('tasks.execute',            'tasks',         'execute', 'Execute scheduled tasks'),
    ('coworkers.create',         'coworkers',     'create',  'Create coworker instances'),
    ('coworkers.configure',      'coworkers',     'update',  'Modify coworker configuration'),
    ('coworkers.delete',         'coworkers',     'delete',  'Delete coworkers'),
    ('blueprints.create',        'blueprints',    'create',  'Create agent blueprints'),
    ('blueprints.configure',     'blueprints',    'update',  'Modify blueprint configuration'),
    ('channels.manage',          'channels',      'update',  'Configure channel bindings'),
    ('tenant.manage',            'tenant',        'update',  'Modify tenant settings'),
    ('tenant.billing',           'tenant',        'execute', 'Manage billing'),
    ('users.manage',             'users',         'update',  'Manage users and roles'),
    ('ipc.send_message',         'ipc',           'execute', 'Send IPC messages to other groups'),
    ('ipc.create_task',          'ipc',           'execute', 'Create tasks via IPC'),
    ('tools.finance',            'tools',         'execute', 'Use finance tools'),
    ('tools.data_query',         'tools',         'execute', 'Use data query tools'),
    ('tools.external_api',       'tools',         'execute', 'Call external APIs')
ON CONFLICT (id) DO NOTHING;

-- =============================================
-- 6. Row-Level Security (defense in depth)
-- =============================================

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE scheduled_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE coworkers ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_bindings ENABLE ROW LEVEL SECURITY;

-- RLS policies: filter by app.tenant_id session variable
-- Usage: SET app.tenant_id = 'tenant-abc' on each connection
DO $$
BEGIN
    -- Only create policies if they don't exist
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename = 'messages') THEN
        EXECUTE 'CREATE POLICY tenant_isolation ON messages USING (tenant_id = current_setting(''app.tenant_id'', true))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename = 'conversations') THEN
        EXECUTE 'CREATE POLICY tenant_isolation ON conversations USING (tenant_id = current_setting(''app.tenant_id'', true))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename = 'scheduled_tasks') THEN
        EXECUTE 'CREATE POLICY tenant_isolation ON scheduled_tasks USING (tenant_id = current_setting(''app.tenant_id'', true))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename = 'coworkers') THEN
        EXECUTE 'CREATE POLICY tenant_isolation ON coworkers USING (tenant_id = current_setting(''app.tenant_id'', true))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename = 'channel_bindings') THEN
        EXECUTE 'CREATE POLICY tenant_isolation ON channel_bindings USING (tenant_id = current_setting(''app.tenant_id'', true))';
    END IF;
END $$;

-- =============================================
-- 7. Add coworker_id to existing tables
-- =============================================

-- scheduled_tasks: bind to coworker + track creator for on_behalf_of
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'scheduled_tasks' AND column_name = 'coworker_id') THEN
        ALTER TABLE scheduled_tasks ADD COLUMN coworker_id TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'scheduled_tasks' AND column_name = 'created_by_user_id') THEN
        ALTER TABLE scheduled_tasks ADD COLUMN created_by_user_id TEXT;
    END IF;
END $$;

-- sessions: add coworker_id dimension
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'sessions' AND column_name = 'coworker_id') THEN
        ALTER TABLE sessions ADD COLUMN coworker_id TEXT DEFAULT 'default';
    END IF;
END $$;

COMMIT;
