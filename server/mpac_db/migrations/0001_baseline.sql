CREATE TABLE IF NOT EXISTS attachments (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS backends (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS benchmarks (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS test_items (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS test_runs (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS test_run_answers (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tests (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    proto_jsonb JSONB,
    proto_bytes BYTEA,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_testitems_parent_id
ON test_items ((proto_jsonb ->> 'parent_id'));

CREATE INDEX IF NOT EXISTS idx_testitems_test_id
ON test_items ((proto_jsonb ->> 'test_id'));

CREATE INDEX IF NOT EXISTS idx_users_api_key_hash
ON users ((proto_jsonb -> 'api_key' ->> 'hash'));

CREATE INDEX IF NOT EXISTS idx_backends_type
ON backends ((proto_jsonb ->> 'backend_type'));

CREATE INDEX IF NOT EXISTS idx_backends_enabled
ON backends ((proto_jsonb ->> 'enabled'));

CREATE INDEX IF NOT EXISTS idx_test_runs_owner
ON test_runs ((proto_jsonb ->> 'owner'));

CREATE INDEX IF NOT EXISTS idx_benchmarks_status
ON benchmarks ((proto_jsonb ->> 'status'));

CREATE INDEX IF NOT EXISTS idx_tests_owner
ON tests ((proto_jsonb ->> 'owner'));

CREATE INDEX IF NOT EXISTS idx_tests_visibility_type
ON tests ((proto_jsonb -> 'visibility' ->> 'type'));

CREATE INDEX IF NOT EXISTS idx_attachments_owner
ON attachments ((proto_jsonb ->> 'owner'));

CREATE INDEX IF NOT EXISTS idx_attachments_visibility_type
ON attachments ((proto_jsonb -> 'visibility' ->> 'type'));

CREATE INDEX IF NOT EXISTS idx_benchmarks_owner
ON benchmarks ((proto_jsonb ->> 'owner'));

ALTER TABLE tests
ADD COLUMN IF NOT EXISTS item_count INTEGER NOT NULL DEFAULT 0;

UPDATE tests t
SET item_count = sub.cnt
FROM (
    SELECT proto_jsonb ->> 'test_id' AS test_id, COUNT(*) AS cnt
    FROM test_items
    GROUP BY 1
) sub
WHERE t.id = sub.test_id
  AND t.item_count = 0;

ALTER TABLE test_runs
ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS user_tpm_windows (
    user_id TEXT NOT NULL,
    window_minute BIGINT NOT NULL,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, window_minute)
);
