-- Auton dashboard schema: PostgreSQL + Keycloak

CREATE TABLE users (
  id UUID PRIMARY KEY,  -- Keycloak user UUID
  email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  full_name TEXT,
  country TEXT,
  organization TEXT,
  title TEXT,
  credits INTEGER DEFAULT 50 NOT NULL,
  tier INTEGER DEFAULT 0 NOT NULL,
  stripe_customer_id TEXT,
  tos_confirmed BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE INDEX idx_users_email ON users(email);

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER on_users_update
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- API keys for programmatic access
CREATE TABLE api_keys (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_hash TEXT UNIQUE NOT NULL,
  key_prefix TEXT NOT NULL,
  name TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
  last_used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  is_active BOOLEAN DEFAULT TRUE NOT NULL
);

CREATE INDEX idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX idx_api_keys_key_hash ON api_keys(key_hash);

-- RLS helper
CREATE OR REPLACE FUNCTION current_user_id()
RETURNS UUID AS $$
BEGIN
    RETURN current_setting('app.user_id', true)::uuid;
EXCEPTION
    WHEN OTHERS THEN
        RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Row Level Security
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY users_select_own ON users
    FOR SELECT USING (id = current_user_id());

CREATE POLICY users_update_own ON users
    FOR UPDATE USING (id = current_user_id())
    WITH CHECK (id = current_user_id());

CREATE POLICY api_keys_all_own ON api_keys
    USING (user_id = current_user_id())
    WITH CHECK (user_id = current_user_id());

-- ---------------------------------------------------------------------------
-- Agent runtime tables (consolidated from SQLite)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    parent_path TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    spec_json JSONB NOT NULL,
    state TEXT NOT NULL,
    health_json JSONB NOT NULL,
    restart_count INTEGER DEFAULT 0,
    idle_reason TEXT,
    profile_json JSONB,  -- non-null = persistent "employee" agent
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_profile ON agents(id) WHERE profile_json IS NOT NULL;

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    checkpoint_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_agent_id ON checkpoints(agent_id);

CREATE TABLE IF NOT EXISTS spec_templates (
    name TEXT PRIMARY KEY,
    spec_json JSONB NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);


CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    agent_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'text/markdown',
    description TEXT NOT NULL DEFAULT '',
    tags JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'expected',
    file_size INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_agent_id ON artifacts(agent_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
