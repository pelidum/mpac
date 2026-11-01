CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL,
    run_id TEXT,
    benchmark_id TEXT,
    backend_id TEXT,
    owner TEXT,
    payload BYTEA NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'leased', 'done', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    run_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_until TIMESTAMPTZ,
    leased_by TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((status = 'leased') = (leased_until IS NOT NULL AND leased_by IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_jobs_queued
ON jobs (run_after, created_at) WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_jobs_leased_until
ON jobs (leased_until) WHERE status = 'leased';

CREATE INDEX IF NOT EXISTS idx_jobs_leased_backend
ON jobs (backend_id) WHERE status = 'leased';

CREATE INDEX IF NOT EXISTS idx_jobs_run_id
ON jobs (run_id) WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_benchmark_id
ON jobs (benchmark_id) WHERE benchmark_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_finished
ON jobs (updated_at) WHERE status IN ('done', 'failed', 'cancelled');

CREATE TABLE IF NOT EXISTS run_counters (
    run_id TEXT PRIMARY KEY,
    total INTEGER NOT NULL CHECK (total >= 0),
    remaining INTEGER NOT NULL CHECK (remaining >= 0 AND remaining <= total),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
