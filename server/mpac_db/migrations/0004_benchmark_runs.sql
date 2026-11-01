CREATE TABLE IF NOT EXISTS benchmark_runs (
    benchmark_id TEXT NOT NULL,
    round INTEGER NOT NULL CHECK (round > 0),
    match_index INTEGER NOT NULL CHECK (match_index >= 0),
    first_model TEXT NOT NULL,
    second_model TEXT,
    run_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (benchmark_id, round, match_index),
    CHECK ((second_model IS NULL) = (run_id IS NULL))
);
