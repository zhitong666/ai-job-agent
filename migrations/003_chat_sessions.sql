CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_sub TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);