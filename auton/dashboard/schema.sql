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
