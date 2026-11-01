pub mod signals;

use std::time::Duration;

use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::mpac::jobs::{BenchmarkStep, FinalizeRun, Job};
use prost::Message;
use sqlx::{PgConnection, PgPool, Row};
use uuid::Uuid;

pub const JOBS_CHANNEL: &str = "mpac_jobs";
pub const CANCEL_CHANNEL: &str = "mpac_cancel";
pub const BACKEND_CHANGED_CHANNEL: &str = "mpac_backend_changed";
pub const DEFAULT_MAX_ATTEMPTS: i32 = 3;
pub const LOCAL_CONCURRENCY_DEFAULT: i32 = 4;
pub const REMOTE_CONCURRENCY_DEFAULT: i32 = 20;

const MAX_BACKOFF_SECS: f64 = 300.0;
const CLAIM_LOCK_KEY: i64 = 0x6d70_6163_636c_6169;
const ANSWER_ITEM: &str = "answer_item";
const FINALIZE_RUN: &str = "finalize_run";
const BENCHMARK_STEP: &str = "benchmark_step";

#[derive(Debug, thiserror::Error)]
pub enum QueueError {
    #[error("queue query failed: {0}")]
    Query(#[from] sqlx::Error),
    #[error("job {0} has an undecodable payload: {1}")]
    Decode(Uuid, #[source] prost::DecodeError),
    #[error("job {0} has no kind")]
    MissingKind(Uuid),
}

#[derive(Debug, Clone, PartialEq)]
pub struct NewJob {
    pub kind: Kind,
    pub run_id: Option<String>,
    pub benchmark_id: Option<String>,
    pub backend_id: Option<String>,
    pub owner: Option<String>,
    pub max_attempts: i32,
    pub delay: Duration,
}

impl NewJob {
    pub fn new(kind: Kind) -> Self {
        Self {
            kind,
            run_id: None,
            benchmark_id: None,
            backend_id: None,
            owner: None,
            max_attempts: DEFAULT_MAX_ATTEMPTS,
            delay: Duration::ZERO,
        }
    }

    #[must_use]
    pub fn run(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    #[must_use]
    pub fn benchmark(mut self, benchmark_id: impl Into<String>) -> Self {
        self.benchmark_id = Some(benchmark_id.into());
        self
    }

    #[must_use]
    pub fn backend(mut self, backend_id: impl Into<String>) -> Self {
        self.backend_id = Some(backend_id.into()).filter(|id: &String| !id.is_empty());
        self
    }

    #[must_use]
    pub fn owner(mut self, owner: impl Into<String>) -> Self {
        self.owner = Some(owner.into());
        self
    }

    #[must_use]
    pub fn max_attempts(mut self, attempts: i32) -> Self {
        self.max_attempts = attempts.max(1);
        self
    }

    #[must_use]
    pub fn delay(mut self, delay: Duration) -> Self {
        self.delay = delay;
        self
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ClaimedJob {
    pub id: Uuid,
    pub lease: String,
    pub kind: Kind,
    pub run_id: Option<String>,
    pub benchmark_id: Option<String>,
    pub backend_id: Option<String>,
    pub owner: Option<String>,
    pub attempts: i32,
    pub max_attempts: i32,
}

impl ClaimedJob {
    pub fn is_last_attempt(&self) -> bool {
        self.attempts >= self.max_attempts
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Completion {
    pub recorded: bool,
    pub finalize_enqueued: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailOutcome {
    Retrying,
    Failed { finalize_enqueued: bool },
    LeaseLost,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ReapReport {
    pub requeued: u64,
    pub failed: u64,
}

pub fn backoff(attempts: i32) -> Duration {
    let exponent = attempts.clamp(0, 16);
    Duration::from_secs_f64(f64::from(1_u32 << exponent).min(MAX_BACKOFF_SECS))
}

fn job_payload(kind: &Kind) -> Vec<u8> {
    Job {
        kind: Some(kind.clone()),
    }
    .encode_to_vec()
}

fn job_model_id(kind: &Kind) -> Option<String> {
    match kind {
        Kind::AnswerItem(work) => work.model.as_ref().map(|model| model.id.clone()),
        _ => None,
    }
    .filter(|id| !id.is_empty())
}

fn seconds(duration: Duration) -> f64 {
    duration.as_secs_f64()
}

pub async fn enqueue(conn: &mut PgConnection, jobs: &[NewJob]) -> Result<Vec<Uuid>, QueueError> {
    if jobs.is_empty() {
        return Ok(Vec::new());
    }
    let kinds: Vec<&str> = jobs.iter().map(|job| job.kind.name()).collect();
    let run_ids: Vec<Option<String>> = jobs.iter().map(|job| job.run_id.clone()).collect();
    let benchmark_ids: Vec<Option<String>> =
        jobs.iter().map(|job| job.benchmark_id.clone()).collect();
    let backend_ids: Vec<Option<String>> = jobs.iter().map(|job| job.backend_id.clone()).collect();
    let owners: Vec<Option<String>> = jobs.iter().map(|job| job.owner.clone()).collect();
    let model_ids: Vec<Option<String>> = jobs.iter().map(|job| job_model_id(&job.kind)).collect();
    let payloads: Vec<Vec<u8>> = jobs.iter().map(|job| job_payload(&job.kind)).collect();
    let attempts: Vec<i32> = jobs.iter().map(|job| job.max_attempts.max(1)).collect();
    let delays: Vec<f64> = jobs.iter().map(|job| seconds(job.delay)).collect();
    let ids = sqlx::query_scalar(
        "INSERT INTO jobs (kind, run_id, benchmark_id, backend_id, owner, payload, max_attempts, run_after, model_id)
         SELECT kind, run_id, benchmark_id, backend_id, owner, payload, max_attempts,
                NOW() + make_interval(secs => delay), model_id
         FROM UNNEST($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::bytea[],
                     $7::int[], $8::float8[], $9::text[])
              WITH ORDINALITY
              AS t(kind, run_id, benchmark_id, backend_id, owner, payload, max_attempts, delay, model_id, ord)
         ORDER BY ord
         RETURNING id",
    )
    .bind(&kinds)
    .bind(&run_ids)
    .bind(&benchmark_ids)
    .bind(&backend_ids)
    .bind(&owners)
    .bind(&payloads)
    .bind(&attempts)
    .bind(&delays)
    .bind(&model_ids)
    .fetch_all(&mut *conn)
    .await?;
    sqlx::query("SELECT pg_notify($1, '')")
        .bind(JOBS_CHANNEL)
        .execute(&mut *conn)
        .await?;
    Ok(ids)
}

pub async fn start_run(
    conn: &mut PgConnection,
    run_id: &str,
    benchmark_id: Option<&str>,
    jobs: &[NewJob],
) -> Result<Vec<Uuid>, QueueError> {
    let total = i32::try_from(jobs.len()).unwrap_or(i32::MAX);
    sqlx::query("INSERT INTO run_counters (run_id, total, remaining) VALUES ($1, $2, $2)")
        .bind(run_id)
        .bind(total)
        .execute(&mut *conn)
        .await?;
    if jobs.is_empty() {
        return enqueue(
            conn,
            &[finalize_job(run_id, benchmark_id.map(str::to_owned))],
        )
        .await;
    }
    enqueue(conn, jobs).await
}

fn finalize_job(run_id: &str, benchmark_id: Option<String>) -> NewJob {
    NewJob {
        benchmark_id,
        ..NewJob::new(Kind::FinalizeRun(FinalizeRun {
            run_id: run_id.to_owned(),
            responder_id: String::new(),
        }))
        .run(run_id)
    }
}

async fn release_answer_slot(
    conn: &mut PgConnection,
    run_id: &str,
    benchmark_id: Option<String>,
) -> Result<bool, QueueError> {
    let remaining: Option<i32> = sqlx::query_scalar(
        "UPDATE run_counters SET remaining = remaining - 1
         WHERE run_id = $1 AND remaining > 0 RETURNING remaining",
    )
    .bind(run_id)
    .fetch_optional(&mut *conn)
    .await?;
    if remaining == Some(0) {
        enqueue(conn, &[finalize_job(run_id, benchmark_id)]).await?;
        return Ok(true);
    }
    Ok(false)
}

async fn recover_undecodable(
    conn: &mut PgConnection,
    row: &sqlx::postgres::PgRow,
) -> Result<(), QueueError> {
    let kind: String = row.try_get("kind")?;
    let run_id: Option<String> = row.try_get("run_id")?;
    let benchmark_id: Option<String> = row.try_get("benchmark_id")?;
    match (kind.as_str(), run_id, benchmark_id) {
        (ANSWER_ITEM, Some(run_id), benchmark_id) => {
            release_answer_slot(conn, &run_id, benchmark_id).await?;
        }
        (FINALIZE_RUN, Some(run_id), benchmark_id) => {
            let remaining: Option<i32> =
                sqlx::query_scalar("SELECT remaining FROM run_counters WHERE run_id = $1")
                    .bind(&run_id)
                    .fetch_optional(&mut *conn)
                    .await?;
            if remaining == Some(0) {
                enqueue(conn, &[finalize_job(&run_id, benchmark_id)]).await?;
            }
        }
        (BENCHMARK_STEP, _, Some(benchmark_id)) => {
            enqueue(
                conn,
                &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep {
                    benchmark_id: benchmark_id.clone(),
                    round: 0,
                }))
                .benchmark(benchmark_id)],
            )
            .await?;
        }
        _ => {}
    }
    Ok(())
}

fn decode_claimed(row: &sqlx::postgres::PgRow) -> Result<ClaimedJob, QueueError> {
    let id: Uuid = row.try_get("id")?;
    let payload: Vec<u8> = row.try_get("payload")?;
    let job = Job::decode(payload.as_slice()).map_err(|error| QueueError::Decode(id, error))?;
    Ok(ClaimedJob {
        id,
        lease: row.try_get("leased_by")?,
        kind: job.kind.ok_or(QueueError::MissingKind(id))?,
        run_id: row.try_get("run_id")?,
        benchmark_id: row.try_get("benchmark_id")?,
        backend_id: row.try_get("backend_id")?,
        owner: row.try_get("owner")?,
        attempts: row.try_get("attempts")?,
        max_attempts: row.try_get("max_attempts")?,
    })
}

const CLAIM_SQL: &str = "WITH capacity AS (
            SELECT id AS backend_id,
                   COALESCE((proto_jsonb->>'is_local')::bool, false) AS is_local,
                   CASE
                     WHEN COALESCE((proto_jsonb->>'max_concurrency')::int, 0) > 0
                       THEN (proto_jsonb->>'max_concurrency')::int
                     WHEN COALESCE((proto_jsonb->>'is_local')::bool, false) THEN $4
                     ELSE $5
                   END AS slots
            FROM backends
        ),
        leased AS (
            SELECT backend_id, COUNT(*)::int AS n
            FROM jobs WHERE status = 'leased' AND backend_id IS NOT NULL
            GROUP BY backend_id
        ),
        ranked AS (
            SELECT id, backend_id, model_id, run_after, seq, kind,
                   ROW_NUMBER() OVER (PARTITION BY backend_id ORDER BY run_after, seq) AS rn
            FROM jobs WHERE status = 'queued' AND run_after <= NOW()
        ),
        active_model AS (
            SELECT DISTINCT ON (backend_id) backend_id, model_id
            FROM (
                SELECT backend_id, model_id, 0 AS source, seq AS position
                FROM jobs WHERE status = 'leased' AND backend_id IS NOT NULL
                UNION ALL
                SELECT backend_id, model_id, 1 AS source, rn AS position
                FROM ranked WHERE rn = 1 AND backend_id IS NOT NULL
            ) candidates
            ORDER BY backend_id, source, position
        ),
        local_barrier AS (
            SELECT r.backend_id, MIN(r.rn) AS first_other_model
            FROM ranked r
            JOIN capacity c ON c.backend_id = r.backend_id AND c.is_local
            JOIN active_model a ON a.backend_id = r.backend_id
            WHERE r.model_id IS DISTINCT FROM a.model_id
            GROUP BY r.backend_id
        ),
        eligible AS (
            SELECT r.id FROM ranked r
            LEFT JOIN capacity c ON c.backend_id = r.backend_id
            LEFT JOIN leased l ON l.backend_id = r.backend_id
            LEFT JOIN local_barrier b ON b.backend_id = r.backend_id
            WHERE r.backend_id IS NULL
               OR (r.rn <= COALESCE(c.slots, $5) - COALESCE(l.n, 0)
                   AND (b.first_other_model IS NULL OR r.rn < b.first_other_model))
            ORDER BY (r.kind = 'finalize_run') DESC, COALESCE(l.n, 0) + r.rn, r.run_after, r.seq
            LIMIT $1
        )
        UPDATE jobs SET status = 'leased', leased_by = $2 || '/' || gen_random_uuid()::text,
                        leased_until = NOW() + make_interval(secs => $3),
                        attempts = attempts + 1, updated_at = NOW()
        WHERE id IN (
            SELECT id FROM jobs
            WHERE id IN (SELECT id FROM eligible) AND status = 'queued'
            FOR UPDATE SKIP LOCKED
        )
          AND status = 'queued'
        RETURNING id, seq, kind, payload, leased_by, run_id, benchmark_id, backend_id, owner,
                  attempts, max_attempts";

pub async fn claim(
    pool: &PgPool,
    worker: &str,
    limit: i64,
    lease: Duration,
) -> Result<Vec<ClaimedJob>, QueueError> {
    let mut tx = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(CLAIM_LOCK_KEY)
        .execute(&mut *tx)
        .await?;
    let rows = sqlx::query(CLAIM_SQL)
        .bind(limit)
        .bind(worker)
        .bind(seconds(lease))
        .bind(LOCAL_CONCURRENCY_DEFAULT)
        .bind(REMOTE_CONCURRENCY_DEFAULT)
        .fetch_all(&mut *tx)
        .await?;
    let mut claimed = Vec::with_capacity(rows.len());
    for row in &rows {
        let seq: i64 = row.try_get("seq")?;
        match decode_claimed(row) {
            Ok(job) => claimed.push((seq, job)),
            Err(error) => {
                let id: Uuid = row.try_get("id")?;
                tracing::error!(job = %id, %error, "failing job with an undecodable payload");
                sqlx::query(
                    "UPDATE jobs SET status = 'failed', leased_by = NULL, leased_until = NULL,
                                     last_error = $2, updated_at = NOW()
                     WHERE id = $1",
                )
                .bind(id)
                .bind(error.to_string())
                .execute(&mut *tx)
                .await?;
                recover_undecodable(&mut tx, row).await?;
            }
        }
    }
    tx.commit().await?;
    claimed.sort_by_key(|(seq, _)| *seq);
    Ok(claimed.into_iter().map(|(_, job)| job).collect())
}

pub async fn extend_lease(
    pool: &PgPool,
    job: &ClaimedJob,
    lease: Duration,
) -> Result<bool, QueueError> {
    let extended = sqlx::query(
        "UPDATE jobs SET leased_until = NOW() + make_interval(secs => $3), updated_at = NOW()
         WHERE id = $1 AND status = 'leased' AND leased_by = $2",
    )
    .bind(job.id)
    .bind(&job.lease)
    .bind(seconds(lease))
    .execute(pool)
    .await?;
    Ok(extended.rows_affected() == 1)
}

async fn finish_in(
    conn: &mut PgConnection,
    job: &ClaimedJob,
    status: &str,
    error: Option<&str>,
) -> Result<Completion, QueueError> {
    let updated = sqlx::query(
        "UPDATE jobs SET status = $3, leased_by = NULL, leased_until = NULL,
                         last_error = $4, updated_at = NOW()
         WHERE id = $1 AND status = 'leased' AND leased_by = $2",
    )
    .bind(job.id)
    .bind(&job.lease)
    .bind(status)
    .bind(error)
    .execute(&mut *conn)
    .await?;
    if updated.rows_affected() == 0 {
        return Ok(Completion {
            recorded: false,
            finalize_enqueued: false,
        });
    }
    let mut finalize_enqueued = false;
    if job.kind.name() == ANSWER_ITEM
        && let Some(run_id) = &job.run_id
    {
        finalize_enqueued = release_answer_slot(conn, run_id, job.benchmark_id.clone()).await?;
    }
    Ok(Completion {
        recorded: true,
        finalize_enqueued,
    })
}

async fn finish(
    pool: &PgPool,
    job: &ClaimedJob,
    status: &str,
    error: Option<&str>,
) -> Result<Completion, QueueError> {
    let mut tx = pool.begin().await?;
    let completion = finish_in(&mut tx, job, status, error).await?;
    tx.commit().await?;
    Ok(completion)
}

pub async fn complete(pool: &PgPool, job: &ClaimedJob) -> Result<Completion, QueueError> {
    finish(pool, job, "done", None).await
}

pub async fn complete_in(
    conn: &mut PgConnection,
    job: &ClaimedJob,
) -> Result<Completion, QueueError> {
    finish_in(conn, job, "done", None).await
}

pub async fn release_worker_leases(pool: &PgPool, worker: &str) -> Result<u64, QueueError> {
    let mut tx = pool.begin().await?;
    let released = sqlx::query(
        "UPDATE jobs SET status = 'queued', leased_by = NULL, leased_until = NULL,
                         attempts = GREATEST(attempts - 1, 0), run_after = NOW(), updated_at = NOW()
         WHERE status = 'leased' AND starts_with(leased_by, $1 || '/')",
    )
    .bind(worker)
    .execute(&mut *tx)
    .await?;
    if released.rows_affected() > 0 {
        sqlx::query("SELECT pg_notify($1, '')")
            .bind(JOBS_CHANNEL)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(released.rows_affected())
}

pub async fn fail(
    pool: &PgPool,
    job: &ClaimedJob,
    error: &str,
    retryable: bool,
) -> Result<FailOutcome, QueueError> {
    if !retryable || job.is_last_attempt() {
        let completion = finish(pool, job, "failed", Some(error)).await?;
        return Ok(if completion.recorded {
            FailOutcome::Failed {
                finalize_enqueued: completion.finalize_enqueued,
            }
        } else {
            FailOutcome::LeaseLost
        });
    }
    let retried = sqlx::query(
        "UPDATE jobs SET status = 'queued', leased_by = NULL, leased_until = NULL,
                         last_error = $3, run_after = NOW() + make_interval(secs => $4),
                         updated_at = NOW()
         WHERE id = $1 AND status = 'leased' AND leased_by = $2",
    )
    .bind(job.id)
    .bind(&job.lease)
    .bind(error)
    .bind(seconds(backoff(job.attempts)))
    .execute(pool)
    .await?;
    Ok(if retried.rows_affected() == 1 {
        FailOutcome::Retrying
    } else {
        FailOutcome::LeaseLost
    })
}

pub async fn reschedule(
    pool: &PgPool,
    job: &ClaimedJob,
    delay: Duration,
) -> Result<bool, QueueError> {
    let rescheduled = sqlx::query(
        "UPDATE jobs SET status = 'queued', leased_by = NULL, leased_until = NULL,
                         attempts = GREATEST(attempts - 1, 0),
                         run_after = NOW() + make_interval(secs => $3), updated_at = NOW()
         WHERE id = $1 AND status = 'leased' AND leased_by = $2",
    )
    .bind(job.id)
    .bind(&job.lease)
    .bind(seconds(delay))
    .execute(pool)
    .await?;
    Ok(rescheduled.rows_affected() == 1)
}

pub async fn reap_expired(pool: &PgPool) -> Result<ReapReport, QueueError> {
    let mut tx = pool.begin().await?;
    let rows = sqlx::query(
        "UPDATE jobs SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                         last_error = 'lease expired', leased_by = NULL, leased_until = NULL,
                         run_after = NOW(), updated_at = NOW()
         WHERE status = 'leased' AND leased_until < NOW()
         RETURNING status, kind, run_id, benchmark_id",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut report = ReapReport::default();
    for row in &rows {
        let status: String = row.try_get("status")?;
        if status == "queued" {
            report.requeued += 1;
            continue;
        }
        report.failed += 1;
        let kind: String = row.try_get("kind")?;
        let run_id: Option<String> = row.try_get("run_id")?;
        if let (ANSWER_ITEM, Some(run_id)) = (kind.as_str(), run_id) {
            release_answer_slot(&mut tx, &run_id, row.try_get("benchmark_id")?).await?;
        }
    }
    if report.requeued > 0 {
        sqlx::query("SELECT pg_notify($1, '')")
            .bind(JOBS_CHANNEL)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(report)
}

async fn cancel_where(conn: &mut PgConnection, column: &str, id: &str) -> Result<u64, QueueError> {
    let cancelled = sqlx::query(sqlx::AssertSqlSafe(format!(
        "UPDATE jobs SET status = 'cancelled', leased_by = NULL, leased_until = NULL,
                         updated_at = NOW()
         WHERE {column} = $1 AND status IN ('queued', 'leased') AND kind <> 'finalize_run'"
    )))
    .bind(id)
    .execute(&mut *conn)
    .await?;
    sqlx::query("SELECT pg_notify($1, $2)")
        .bind(CANCEL_CHANNEL)
        .bind(id)
        .execute(&mut *conn)
        .await?;
    Ok(cancelled.rows_affected())
}

pub async fn cancel_run(conn: &mut PgConnection, run_id: &str) -> Result<u64, QueueError> {
    let cancelled = cancel_where(conn, "run_id", run_id).await?;
    let drained: Option<String> = sqlx::query_scalar(
        "UPDATE run_counters SET remaining = 0 WHERE run_id = $1 AND remaining > 0 RETURNING run_id",
    )
    .bind(run_id)
    .fetch_optional(&mut *conn)
    .await?;
    if drained.is_some() {
        let benchmark_id: Option<String> = sqlx::query_scalar(
            "SELECT benchmark_id FROM jobs
             WHERE run_id = $1 AND benchmark_id IS NOT NULL LIMIT 1",
        )
        .bind(run_id)
        .fetch_optional(&mut *conn)
        .await?;
        enqueue(conn, &[finalize_job(run_id, benchmark_id)]).await?;
    }
    Ok(cancelled)
}

pub async fn cancel_benchmark(
    conn: &mut PgConnection,
    benchmark_id: &str,
) -> Result<u64, QueueError> {
    cancel_where(conn, "benchmark_id", benchmark_id).await
}

pub async fn notify_backend_changed(pool: &PgPool, backend_id: &str) -> Result<(), QueueError> {
    sqlx::query("SELECT pg_notify($1, $2)")
        .bind(BACKEND_CHANGED_CHANNEL)
        .bind(backend_id)
        .execute(pool)
        .await?;
    Ok(())
}

pub async fn prune_finished(pool: &PgPool, older_than: Duration) -> Result<u64, QueueError> {
    let pruned = sqlx::query(
        "DELETE FROM jobs WHERE status IN ('done', 'failed', 'cancelled')
         AND updated_at < NOW() - make_interval(secs => $1)",
    )
    .bind(seconds(older_than))
    .execute(pool)
    .await?;
    Ok(pruned.rows_affected())
}

#[cfg(test)]
mod tests {
    use mpac_proto::mpac::jobs::AnswerItem;

    use super::*;

    #[test]
    fn backoff_doubles_and_caps() {
        assert_eq!(backoff(0), Duration::from_secs(1));
        assert_eq!(backoff(3), Duration::from_secs(8));
        assert_eq!(backoff(40), Duration::from_secs(300));
    }

    #[test]
    fn builder_drops_empty_backend_and_clamps_attempts() {
        let job = NewJob::new(Kind::AnswerItem(AnswerItem::default()))
            .backend("")
            .max_attempts(0);

        assert_eq!(job.backend_id, None);
        assert_eq!(job.max_attempts, 1);
    }

    #[test]
    fn payload_round_trips_kind() {
        let kind = Kind::FinalizeRun(FinalizeRun {
            run_id: "r".into(),
            responder_id: String::new(),
        });

        let job = Job::decode(job_payload(&kind).as_slice()).unwrap();

        assert_eq!(job.kind, Some(kind));
    }
}
