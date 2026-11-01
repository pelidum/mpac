CREATE TABLE backend_secrets (
    backend_id VARCHAR PRIMARY KEY REFERENCES backends (id) ON DELETE CASCADE,
    ciphertext BYTEA NOT NULL,
    wrapped_key BYTEA NOT NULL,
    master_key_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
