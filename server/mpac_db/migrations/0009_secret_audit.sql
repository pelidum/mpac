CREATE TABLE secret_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backend_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete', 'decrypt', 'rewrap')),
    actor TEXT NOT NULL,
    key_version INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX secret_audit_backend_idx ON secret_audit (backend_id, id);
