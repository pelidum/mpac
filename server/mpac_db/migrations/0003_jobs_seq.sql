ALTER TABLE jobs ADD COLUMN IF NOT EXISTS seq BIGINT GENERATED ALWAYS AS IDENTITY;

CREATE INDEX IF NOT EXISTS idx_jobs_queued_seq
ON jobs (run_after, seq) WHERE status = 'queued';

DROP INDEX IF EXISTS idx_jobs_queued;
