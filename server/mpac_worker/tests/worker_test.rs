#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use mpac_blob::{BlobStore, blob_key, upload_key};
use mpac_db::crud::{self, Table};
use mpac_proto::file_attachment::UploadState;
use mpac_proto::mpac::jobs::BenchmarkStep;
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::{
    Backend, BackendType, Benchmark, BenchmarkMatch, FileAttachment, FileModality, Model,
    ResponseCode, Test, TestItem, TestRun, TestType, User, UserRateLimits,
};
use mpac_queue::{ClaimedJob, NewJob};
use mpac_testing::TestDb;
use mpac_worker::blobs::prune_stale_uploads;
use mpac_worker::runs::{NewRun, cancel_benchmark_runs, create_run};
use mpac_worker::worker::WorkerConfig;
use mpac_worker::{Context, HandlerResult, Outcome, answer_item, benchmark, finalize};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use tokio::sync::oneshot;
use wiremock::matchers::{header, method};
use wiremock::{Mock, MockServer, ResponseTemplate};

const LEASE: Duration = Duration::from_secs(60);
const OWNER: &str = "owner@example.com";
const KNOW_IT_ALL: &str = "debug/know_it_all";
const RANDOM: &str = "debug/debug_random_model_1";

async fn put<M: prost::Message + prost::Name>(pool: &PgPool, table: Table, id: &str, message: &M) {
    let mut conn = pool.acquire().await.unwrap();
    crud::upsert_in(&mut conn, table, id, message)
        .await
        .unwrap();
}

fn model(id: &str, backend_id: &str) -> Model {
    Model {
        id: id.into(),
        backend_id: backend_id.into(),
        ..Model::default()
    }
}

async fn seed_test(pool: &PgPool, items: usize) {
    put(
        pool,
        Table::Backends,
        "debug",
        &Backend {
            id: "debug".into(),
            backend_type: BackendType::DebugRandom.into(),
            ..Backend::default()
        },
    )
    .await;
    put(
        pool,
        Table::Tests,
        "t",
        &Test {
            id: "t".into(),
            owner: OWNER.into(),
            r#type: TestType::Evaluation.into(),
            ..Test::default()
        },
    )
    .await;
    for index in 0..items {
        let id = format!("i{index}");
        put(
            pool,
            Table::TestItems,
            &id,
            &TestItem {
                id: id.clone(),
                test_id: "t".into(),
                question: format!("Is {index} even?"),
                choices: vec!["yes".into(), "no".into()],
                answer: "yes".into(),
                ..TestItem::default()
            },
        )
        .await;
    }
}

async fn start(pool: &PgPool, models: Vec<Model>, items: usize) -> TestRun {
    create_run(
        pool,
        NewRun {
            test_id: "t".into(),
            owner: OWNER.into(),
            models,
            sample_size: i64::try_from(items).unwrap(),
            ..NewRun::default()
        },
    )
    .await
    .unwrap()
}

async fn claim_one(pool: &PgPool, kind: &str) -> ClaimedJob {
    let jobs = mpac_queue::claim(pool, "test-worker", 100, LEASE)
        .await
        .unwrap();
    let mut wanted = None;
    for job in jobs {
        if wanted.is_none() && job.kind.name() == kind {
            wanted = Some(job);
        } else {
            mpac_queue::reschedule(pool, &job, Duration::ZERO)
                .await
                .unwrap();
        }
    }
    wanted.unwrap_or_else(|| panic!("no {kind} job was claimable"))
}

async fn dispatch(ctx: &Context, job: &ClaimedJob) -> HandlerResult {
    match &job.kind {
        Kind::AnswerItem(work) => answer_item::handle(ctx, job, work).await,
        Kind::FinalizeRun(work) => finalize::handle(ctx, job, &work.run_id).await,
        Kind::BenchmarkStep(work) => benchmark::handle(ctx, job, &work.benchmark_id).await,
    }
}

async fn drain(ctx: &Context) {
    for _ in 0..100 {
        let jobs = mpac_queue::claim(&ctx.pool, "test-worker", 100, LEASE)
            .await
            .unwrap();
        if jobs.is_empty() {
            return;
        }
        for job in jobs {
            match dispatch(ctx, &job).await.unwrap() {
                Outcome::Done => {
                    mpac_queue::complete(&ctx.pool, &job).await.unwrap();
                }
                Outcome::Committed => {}
                other => panic!("unexpected outcome {other:?}"),
            }
        }
    }
    panic!("the queue never drained");
}

async fn run(pool: &PgPool, id: &str) -> TestRun {
    crud::get_unchecked(pool, Table::TestRuns, id)
        .await
        .unwrap()
        .unwrap()
}

async fn scalar(pool: &PgPool, sql: &str, id: &str) -> i64 {
    sqlx::query_scalar(sqlx::AssertSqlSafe(sql.to_owned()))
        .bind(id)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn answers(pool: &PgPool, run_id: &str) -> i64 {
    scalar(
        pool,
        "SELECT COUNT(*) FROM test_run_answers WHERE proto_jsonb->>'run_id' = $1",
        run_id,
    )
    .await
}

async fn remaining(pool: &PgPool, run_id: &str) -> i64 {
    scalar(
        pool,
        "SELECT remaining::bigint FROM run_counters WHERE run_id = $1",
        run_id,
    )
    .await
}

async fn status(pool: &PgPool, job: &ClaimedJob) -> String {
    sqlx::query_scalar("SELECT status FROM jobs WHERE id = $1")
        .bind(job.id)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn reject_inserts(pool: &PgPool, table: &str) {
    sqlx::raw_sql(sqlx::AssertSqlSafe(format!(
        "CREATE FUNCTION reject_{table}() RETURNS trigger LANGUAGE plpgsql AS
         $$ BEGIN RAISE EXCEPTION 'rejected by test'; END $$;
         CREATE TRIGGER reject_{table} BEFORE INSERT ON {table}
         FOR EACH ROW EXECUTE FUNCTION reject_{table}();"
    )))
    .execute(pool)
    .await
    .unwrap();
}

fn answer_work(job: &ClaimedJob) -> &mpac_proto::mpac::jobs::AnswerItem {
    match &job.kind {
        Kind::AnswerItem(work) => work,
        other => panic!("expected an answer job, got {}", other.name()),
    }
}

#[tokio::test]
async fn rejected_answer_write_fails_the_job_instead_of_completing_it() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    let run = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 1).await;
    reject_inserts(&db.pool, "test_run_answers").await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    assert!(
        answer_item::handle(&ctx, &job, answer_work(&job))
            .await
            .is_err()
    );

    assert_eq!(status(&db.pool, &job).await, "leased");
    assert_eq!(remaining(&db.pool, &run.id).await, 1);
    assert_eq!(answers(&db.pool, &run.id).await, 0);
}

#[tokio::test]
async fn answer_from_a_lost_lease_is_discarded() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    let run = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 1).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;
    sqlx::query("UPDATE jobs SET leased_by = 'other-worker/lease' WHERE id = $1")
        .bind(job.id)
        .execute(&db.pool)
        .await
        .unwrap();

    let outcome = answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap();

    assert_eq!(outcome, Outcome::LeaseLost);
    assert_eq!(answers(&db.pool, &run.id).await, 0);
    assert_eq!(remaining(&db.pool, &run.id).await, 1);
}

#[tokio::test]
async fn failed_token_accounting_rolls_back_the_answer() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    put(
        &db.pool,
        Table::Users,
        OWNER,
        &User {
            id: OWNER.into(),
            rate_limits: Some(UserRateLimits {
                tokens_per_minute: 1_000_000,
                ..UserRateLimits::default()
            }),
            ..User::default()
        },
    )
    .await;
    let run = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 1).await;
    reject_inserts(&db.pool, "user_tpm_windows").await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    assert!(
        answer_item::handle(&ctx, &job, answer_work(&job))
            .await
            .is_err()
    );

    assert_eq!(status(&db.pool, &job).await, "leased");
    assert_eq!(answers(&db.pool, &run.id).await, 0);
}

#[tokio::test]
async fn finalize_preserves_a_cancellation_that_lands_mid_finalize() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 2).await;
    let created = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 2).await;
    let ctx = Arc::new(Context::new(db.pool.clone()).unwrap());
    for _ in 0..2 {
        let job = claim_one(&db.pool, "answer_item").await;
        assert_eq!(dispatch(&ctx, &job).await.unwrap(), Outcome::Committed);
    }
    let job = claim_one(&db.pool, "finalize_run").await;

    let mut lock_holder = db.pool.begin().await.unwrap();
    let mut cancelled_run: TestRun =
        crud::get_for_update(&mut lock_holder, Table::TestRuns, &created.id)
            .await
            .unwrap()
            .unwrap();
    let finalizer = tokio::spawn({
        let ctx = Arc::clone(&ctx);
        let job = job.clone();
        async move { dispatch(&ctx, &job).await }
    });
    tokio::time::timeout(Duration::from_secs(10), async {
        while scalar(
            &db.pool,
            "SELECT COUNT(*) FROM pg_stat_activity
             WHERE datname = $1 AND wait_event_type = 'Lock'",
            &db.name,
        )
        .await
            == 0
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("finalize never waited on the run row");
    cancelled_run.status = ResponseCode::Cancelled.into();
    crud::update_in(
        &mut lock_holder,
        Table::TestRuns,
        &created.id,
        &cancelled_run,
    )
    .await
    .unwrap();
    lock_holder.commit().await.unwrap();

    assert_eq!(finalizer.await.unwrap().unwrap(), Outcome::Committed);
    let stored = run(&db.pool, &created.id).await;
    assert_eq!(stored.status, i32::from(ResponseCode::Cancelled));
    assert_eq!(stored.metrics.len(), 1);
}

async fn seed_benchmark(pool: &PgPool) -> Context {
    seed_benchmark_with(
        pool,
        vec![model(KNOW_IT_ALL, "debug"), model(RANDOM, "debug")],
    )
    .await
}

async fn seed_benchmark_with(pool: &PgPool, models: Vec<Model>) -> Context {
    seed_test(pool, 2).await;
    put(
        pool,
        Table::Benchmarks,
        "b",
        &Benchmark {
            id: "b".into(),
            test_id: "t".into(),
            owner: OWNER.into(),
            status: ResponseCode::InProgress.into(),
            models,
            sample_size: 2,
            ..Benchmark::default()
        },
    )
    .await;
    let mut conn = pool.acquire().await.unwrap();
    mpac_queue::enqueue(
        &mut conn,
        &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep {
            benchmark_id: "b".into(),
            round: 0,
        }))
        .benchmark("b")],
    )
    .await
    .unwrap();
    let ctx = Context::new(pool.clone()).unwrap();
    let job = claim_one(pool, "benchmark_step").await;
    assert_eq!(dispatch(&ctx, &job).await.unwrap(), Outcome::Committed);
    ctx
}

async fn match_run(pool: &PgPool) -> String {
    sqlx::query_scalar(
        "SELECT run_id FROM benchmark_runs WHERE benchmark_id = 'b' AND run_id IS NOT NULL",
    )
    .fetch_one(pool)
    .await
    .unwrap()
}

async fn benchmark_status(pool: &PgPool) -> Benchmark {
    crud::get_unchecked(pool, Table::Benchmarks, "b")
        .await
        .unwrap()
        .unwrap()
}

#[tokio::test]
async fn cancelling_a_benchmark_cancels_and_finalizes_its_match_runs() {
    let db = TestDb::create().await;
    let ctx = seed_benchmark(&db.pool).await;
    let run_id = match_run(&db.pool).await;
    let mut benchmark = benchmark_status(&db.pool).await;
    benchmark.status = ResponseCode::Cancelled.into();
    put(&db.pool, Table::Benchmarks, "b", &benchmark).await;

    let mut tx = db.pool.begin().await.unwrap();
    assert_eq!(cancel_benchmark_runs(&mut tx, "b").await.unwrap(), 1);
    tx.commit().await.unwrap();
    drain(&ctx).await;

    let child = run(&db.pool, &run_id).await;
    assert_eq!(child.status, i32::from(ResponseCode::Cancelled));
    assert_eq!(child.metrics.len(), 2);
    assert_eq!(remaining(&db.pool, &run_id).await, 0);
    assert_eq!(
        benchmark_status(&db.pool).await.status,
        i32::from(ResponseCode::Cancelled)
    );
    assert_eq!(
        scalar(
            &db.pool,
            "SELECT COUNT(*) FROM jobs WHERE benchmark_id = $1 AND status IN ('queued', 'leased')",
            "b",
        )
        .await,
        0
    );
}

#[tokio::test]
async fn cancelling_the_last_match_still_finishes_the_bracket() {
    let db = TestDb::create().await;
    let ctx = seed_benchmark(&db.pool).await;
    let run_id = match_run(&db.pool).await;
    let mut tx = db.pool.begin().await.unwrap();
    let mut child: TestRun = crud::get_for_update(&mut tx, Table::TestRuns, &run_id)
        .await
        .unwrap()
        .unwrap();
    child.status = ResponseCode::Cancelled.into();
    crud::update_in(&mut tx, Table::TestRuns, &run_id, &child)
        .await
        .unwrap();
    mpac_queue::cancel_run(&mut tx, &run_id).await.unwrap();
    tx.commit().await.unwrap();

    drain(&ctx).await;

    let finished = benchmark_status(&db.pool).await;
    assert_eq!(finished.status, i32::from(ResponseCode::Success));
    assert!([KNOW_IT_ALL, RANDOM].contains(&finished.winner.as_str()));
}

#[tokio::test]
async fn workers_send_the_decrypted_key_and_clear_it_when_it_changes() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    let provider = MockServer::start().await;
    Mock::given(method("POST"))
        .and(header("authorization", "Bearer sk-decrypted"))
        .respond_with(ResponseTemplate::new(400).set_body_string("seen"))
        .expect(1..)
        .mount(&provider)
        .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&provider)
        .await;
    put(
        &db.pool,
        Table::Backends,
        "keyed",
        &Backend {
            id: "keyed".into(),
            backend_type: BackendType::Vllm.into(),
            base_url: format!("{}/v1", provider.uri()),
            is_local: true,
            ..Backend::default()
        },
    )
    .await;
    let key_path = std::env::temp_dir().join(format!("mpac-worker-key-{}", std::process::id()));
    std::fs::write(&key_path, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=").unwrap();
    let secrets = Arc::new(
        mpac_secrets::SecretStore::open(&format!("file://{}", key_path.display())).unwrap(),
    );
    let mut conn = db.pool.acquire().await.unwrap();
    secrets
        .store_in(&mut conn, "keyed", "sk-decrypted", "test")
        .await
        .unwrap();
    drop(conn);
    start(&db.pool, vec![model("key-model", "keyed")], 1).await;
    let ctx = Context::new(db.pool.clone())
        .unwrap()
        .with_secrets(Some(secrets));
    let job = claim_one(&db.pool, "answer_item").await;

    assert_eq!(
        answer_item::handle(&ctx, &job, answer_work(&job))
            .await
            .unwrap(),
        Outcome::Committed
    );
    provider.verify().await;
}

#[tokio::test]
async fn workers_without_a_master_key_fail_jobs_for_keyed_backends() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    put(
        &db.pool,
        Table::Backends,
        "keyed",
        &Backend {
            id: "keyed".into(),
            backend_type: BackendType::Vllm.into(),
            ..Backend::default()
        },
    )
    .await;
    sqlx::query(
        "INSERT INTO backend_secrets (backend_id, ciphertext, wrapped_key, master_key_id)
         VALUES ('keyed', '', '', 'file:x')",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    start(&db.pool, vec![model("key-model", "keyed")], 1).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    let error = answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap_err();

    assert!(!error.retryable(), "{error}");
}

#[tokio::test]
async fn deleting_the_last_match_run_still_finishes_the_bracket() {
    let db = TestDb::create().await;
    let ctx = seed_benchmark(&db.pool).await;
    let run_id = match_run(&db.pool).await;
    let mut tx = db.pool.begin().await.unwrap();
    mpac_queue::cancel_run(&mut tx, &run_id).await.unwrap();
    sqlx::query("DELETE FROM test_runs WHERE id = $1")
        .bind(&run_id)
        .execute(&mut *tx)
        .await
        .unwrap();
    tx.commit().await.unwrap();

    drain(&ctx).await;

    let finished = benchmark_status(&db.pool).await;
    assert_eq!(finished.status, i32::from(ResponseCode::Success));
    assert!([KNOW_IT_ALL, RANDOM].contains(&finished.winner.as_str()));
}

async fn set_limits(pool: &PgPool, limits: UserRateLimits) {
    put(
        pool,
        Table::Users,
        OWNER,
        &User {
            id: OWNER.into(),
            rate_limits: Some(limits),
            ..User::default()
        },
    )
    .await;
}

#[tokio::test]
async fn exhausted_tokens_per_minute_reschedule_without_spending_an_attempt() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    set_limits(
        &db.pool,
        UserRateLimits {
            tokens_per_minute: 10,
            ..UserRateLimits::default()
        },
    )
    .await;
    sqlx::query(
        "INSERT INTO user_tpm_windows (user_id, window_minute, tokens_used)
         VALUES ($1, EXTRACT(EPOCH FROM NOW())::bigint / 60, 100)",
    )
    .bind(OWNER)
    .execute(&db.pool)
    .await
    .unwrap();
    let created = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 1).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    let Outcome::Reschedule(delay) = answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap()
    else {
        panic!("expected a reschedule");
    };

    assert!(delay <= Duration::from_secs(61));
    mpac_queue::reschedule(&db.pool, &job, delay).await.unwrap();
    assert_eq!(answers(&db.pool, &created.id).await, 0);
    let attempts: i32 = sqlx::query_scalar("SELECT attempts FROM jobs WHERE id = $1")
        .bind(job.id)
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(attempts, 0);
}

#[tokio::test]
async fn concurrent_answers_count_every_token_toward_the_minute_window() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 8).await;
    set_limits(
        &db.pool,
        UserRateLimits {
            tokens_per_minute: 1_000_000,
            ..UserRateLimits::default()
        },
    )
    .await;
    start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 8).await;
    let ctx = Arc::new(Context::new(db.pool.clone()).unwrap());
    let jobs = mpac_queue::claim(&db.pool, "w", 8, LEASE).await.unwrap();
    assert_eq!(jobs.len(), 8);

    let mut tasks = Vec::new();
    for job in jobs {
        let ctx = Arc::clone(&ctx);
        tasks.push(tokio::spawn(async move {
            answer_item::handle(&ctx, &job, answer_work(&job))
                .await
                .unwrap();
        }));
    }
    for task in tasks {
        task.await.unwrap();
    }

    let used: i64 = sqlx::query_scalar("SELECT SUM(tokens_used)::bigint FROM user_tpm_windows")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(used, 8 * 600);
}

#[tokio::test]
async fn reaching_a_spend_limit_cancels_the_run() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 2).await;
    set_limits(
        &db.pool,
        UserRateLimits {
            daily_spend: 1,
            ..UserRateLimits::default()
        },
    )
    .await;
    put(
        &db.pool,
        Table::TestRuns,
        "earlier",
        &TestRun {
            id: "earlier".into(),
            owner: OWNER.into(),
            total_cost_usd: 1.0,
            ..TestRun::default()
        },
    )
    .await;
    let created = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 2).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap();

    assert_eq!(
        run(&db.pool, &created.id).await.status,
        i32::from(ResponseCode::Cancelled)
    );
}

#[tokio::test]
async fn updated_limits_apply_after_the_cache_is_invalidated() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    set_limits(&db.pool, UserRateLimits::default()).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    assert!(
        !ctx.limits
            .spend_exceeded(&db.pool, OWNER, 5.0)
            .await
            .unwrap()
    );

    set_limits(
        &db.pool,
        UserRateLimits {
            daily_spend: 1,
            ..UserRateLimits::default()
        },
    )
    .await;
    assert!(
        !ctx.limits
            .spend_exceeded(&db.pool, OWNER, 5.0)
            .await
            .unwrap()
    );

    ctx.limits.invalidate(OWNER);
    assert!(
        ctx.limits
            .spend_exceeded(&db.pool, OWNER, 5.0)
            .await
            .unwrap()
    );
}

#[tokio::test]
async fn oversized_attachments_store_a_bad_request_answer_and_finish_the_run() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    put(
        &db.pool,
        Table::Attachments,
        "huge",
        &FileAttachment {
            id: "huge".into(),
            name: "huge.mp4".into(),
            modality: FileModality::Video.into(),
            file_size: 2 * 1024 * 1024,
            uri: "blobs/huge".into(),
            upload_state: UploadState::Ready.into(),
            ..FileAttachment::default()
        },
    )
    .await;
    put(
        &db.pool,
        Table::TestItems,
        "i0",
        &TestItem {
            id: "i0".into(),
            test_id: "t".into(),
            question: "Q?".into(),
            choices: vec!["yes".into(), "no".into()],
            answer: "yes".into(),
            attachment_id: "huge".into(),
            ..TestItem::default()
        },
    )
    .await;
    let created = start(&db.pool, vec![model(KNOW_IT_ALL, "debug")], 1).await;
    let ctx = Context::new(db.pool.clone()).unwrap();

    drain(&ctx).await;

    let stored = run(&db.pool, &created.id).await;
    assert_eq!(stored.status, i32::from(ResponseCode::Success));
    let (status, error): (String, String) = sqlx::query_as(
        "SELECT proto_jsonb->>'status', proto_jsonb->>'error' FROM test_run_answers
         WHERE proto_jsonb->>'run_id' = $1",
    )
    .bind(&created.id)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(status, "BAD_REQUEST");
    assert!(error.contains("64 MiB"), "{error}");
}

async fn finish_match(pool: &PgPool, ctx: &Context, run_id: &str) {
    let mut child = run(pool, run_id).await;
    child.status = ResponseCode::Success.into();
    put(pool, Table::TestRuns, run_id, &child).await;
    let mut conn = pool.acquire().await.unwrap();
    mpac_queue::enqueue(
        &mut conn,
        &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep {
            benchmark_id: "b".into(),
            round: 0,
        }))
        .benchmark("b")],
    )
    .await
    .unwrap();
    let job = claim_one(pool, "benchmark_step").await;
    assert_eq!(dispatch(ctx, &job).await.unwrap(), Outcome::Committed);
}

#[tokio::test]
async fn each_match_is_recorded_when_it_finishes_not_when_the_round_does() {
    let db = TestDb::create().await;
    let models = (1..=5)
        .map(|i| model(&format!("debug/debug_random_model_{i}"), "debug"))
        .collect();
    let ctx = seed_benchmark_with(&db.pool, models).await;
    let run_ids: Vec<String> = sqlx::query_scalar(
        "SELECT run_id FROM benchmark_runs
         WHERE benchmark_id = 'b' AND round = 1 AND run_id IS NOT NULL ORDER BY match_index",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    assert_eq!(run_ids.len(), 2);

    let opened = benchmark_status(&db.pool).await;
    assert_eq!(
        opened
            .match_history
            .iter()
            .map(|m| m.id)
            .collect::<Vec<_>>(),
        [3],
        "the BYE is visible as soon as the round opens"
    );

    finish_match(&db.pool, &ctx, &run_ids[1]).await;

    let partial = benchmark_status(&db.pool).await;
    assert_eq!(
        partial
            .match_history
            .iter()
            .map(|m| m.id)
            .collect::<Vec<_>>(),
        [2, 3],
        "a later match can finish first and keeps its bracket position"
    );
    let round_two_started: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM benchmark_runs WHERE round = 2")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(round_two_started, 0);

    finish_match(&db.pool, &ctx, &run_ids[1]).await;
    assert_eq!(benchmark_status(&db.pool).await.match_history.len(), 2);

    finish_match(&db.pool, &ctx, &run_ids[0]).await;

    let complete = benchmark_status(&db.pool).await;
    assert_eq!(
        complete
            .match_history
            .iter()
            .filter(|m| m.round == 1)
            .map(|m| m.id)
            .collect::<Vec<_>>(),
        [1, 2, 3]
    );
    let round_two: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM benchmark_runs WHERE round = 2")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(round_two, 2);
}

#[tokio::test]
async fn odd_brackets_give_each_bye_to_a_different_model() {
    let db = TestDb::create().await;
    let models = (1..=9)
        .map(|i| model(&format!("debug/debug_random_model_{i}"), "debug"))
        .collect();
    let ctx = seed_benchmark_with(&db.pool, models).await;

    drain(&ctx).await;

    let finished = benchmark_status(&db.pool).await;
    assert_eq!(finished.status, i32::from(ResponseCode::Success));
    assert_eq!(finished.match_history.len(), 11);
    let byes: Vec<&BenchmarkMatch> = finished
        .match_history
        .iter()
        .filter(|m| m.responses.len() == 1)
        .collect();
    let mut holders: Vec<&str> = byes.iter().map(|m| m.winner.as_str()).collect();
    holders.sort_unstable();
    holders.dedup();
    assert_eq!(byes.len(), 3);
    assert_eq!(holders.len(), 3);
    for bye in byes {
        let round_matches = finished
            .match_history
            .iter()
            .filter(|m| m.round == bye.round)
            .count();
        assert_eq!(bye.id, i64::try_from(round_matches).unwrap());
    }
}

#[tokio::test]
async fn shutdown_returns_a_slow_job_to_the_queue_without_spending_an_attempt() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 1).await;
    let provider = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_delay(Duration::from_secs(120)))
        .mount(&provider)
        .await;
    put(
        &db.pool,
        Table::Backends,
        "slow",
        &Backend {
            id: "slow".into(),
            backend_type: BackendType::Vllm.into(),
            base_url: format!("{}/v1", provider.uri()),
            is_local: true,
            ..Backend::default()
        },
    )
    .await;
    let created = start(&db.pool, vec![model("slow-model", "slow")], 1).await;
    let config = WorkerConfig {
        poll: Duration::from_millis(100),
        shutdown_grace: Duration::from_millis(200),
        ..WorkerConfig::new(1)
    };
    let (stop, stopped) = oneshot::channel::<()>();
    let worker = tokio::spawn(mpac_worker::worker::run(
        Arc::new(Context::new(db.pool.clone()).unwrap()),
        config,
        async move {
            stopped.await.ok();
        },
    ));
    tokio::time::timeout(Duration::from_secs(10), async {
        while provider
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty()
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("the worker never called the provider");

    stop.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(10), worker)
        .await
        .expect("shutdown was not bounded")
        .unwrap()
        .unwrap();

    let (status, attempts, leased_by): (String, i32, Option<String>) = sqlx::query_as(
        "SELECT status, attempts, leased_by FROM jobs WHERE run_id = $1 AND kind = 'answer_item'",
    )
    .bind(&created.id)
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!((status.as_str(), attempts, leased_by), ("queued", 0, None));
}

async fn stored_blob(pool: &PgPool, blobs: &BlobStore, id: &str, content: &[u8]) {
    let digest = hex::encode(Sha256::digest(content));
    blobs
        .put(&blob_key(&digest), content.to_vec().into())
        .await
        .unwrap();
    put(
        pool,
        Table::Attachments,
        id,
        &FileAttachment {
            id: id.into(),
            name: "clip.wav".into(),
            modality: FileModality::Audio.into(),
            mime: "audio/x-wav".into(),
            uri: blob_key(&digest),
            sha256: digest,
            upload_state: UploadState::Ready.into(),
            ..FileAttachment::default()
        },
    )
    .await;
}

#[tokio::test]
async fn abandoned_uploads_are_pruned_with_their_temporary_objects() {
    let db = TestDb::create().await;
    let blobs = BlobStore::in_memory();
    for id in ["old", "new"] {
        put(
            &db.pool,
            Table::Attachments,
            id,
            &FileAttachment {
                id: id.into(),
                uri: upload_key(id),
                upload_state: UploadState::Pending.into(),
                ..FileAttachment::default()
            },
        )
        .await;
        blobs
            .put(&upload_key(id), bytes::Bytes::from_static(b"partial"))
            .await
            .unwrap();
    }
    sqlx::query("UPDATE attachments SET created_at = NOW() - interval '2 days' WHERE id = 'old'")
        .execute(&db.pool)
        .await
        .unwrap();

    let pruned = prune_stale_uploads(&db.pool, &blobs, Duration::from_hours(24))
        .await
        .unwrap();

    assert_eq!(pruned, 1);
    assert!(blobs.size(&upload_key("old")).await.is_err());
    assert!(blobs.size(&upload_key("new")).await.is_ok());
    assert_eq!(
        scalar(
            &db.pool,
            "SELECT COUNT(*) FROM attachments WHERE id = $1",
            "new"
        )
        .await,
        1
    );
}

async fn run_with_attachment(pool: &PgPool, attachment_id: &str) -> TestRun {
    seed_test(pool, 0).await;
    put(
        pool,
        Table::TestItems,
        "with-attachment",
        &TestItem {
            id: "with-attachment".into(),
            test_id: "t".into(),
            question: "What is this?".into(),
            choices: vec!["yes".into(), "no".into()],
            answer: "yes".into(),
            attachment_id: attachment_id.into(),
            ..TestItem::default()
        },
    )
    .await;
    start(pool, vec![model(KNOW_IT_ALL, "debug")], 1).await
}

#[tokio::test]
async fn answers_load_blob_backed_attachments() {
    let db = TestDb::create().await;
    let blobs = BlobStore::in_memory();
    stored_blob(&db.pool, &blobs, "a", b"RIFF audio").await;
    let run = run_with_attachment(&db.pool, "a").await;
    let ctx = Context::new(db.pool.clone())
        .unwrap()
        .with_blobs(Some(blobs));
    let job = claim_one(&db.pool, "answer_item").await;

    assert_eq!(
        answer_item::handle(&ctx, &job, answer_work(&job))
            .await
            .unwrap(),
        Outcome::Committed
    );

    let stored: mpac_proto::TestRunAnswer = crud::get_unchecked(
        &db.pool,
        Table::TestRunAnswers,
        &job.id.simple().to_string(),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(stored.run_id, run.id);
    assert_eq!(stored.attachment_type, i32::from(FileModality::Audio));
    assert!(ctx.blob_cache.len_bytes() > 0);
}

#[tokio::test]
async fn blob_backed_attachments_fail_jobs_on_workers_without_blob_storage() {
    let db = TestDb::create().await;
    let blobs = BlobStore::in_memory();
    stored_blob(&db.pool, &blobs, "a", b"RIFF audio").await;
    run_with_attachment(&db.pool, "a").await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    let job = claim_one(&db.pool, "answer_item").await;

    let error = answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap_err();

    assert!(!error.retryable(), "{error}");
}

#[tokio::test]
async fn cleanup_retries_and_keeps_referenced_content() {
    let db = TestDb::create().await;
    let blobs = BlobStore::in_memory();
    stored_blob(&db.pool, &blobs, "a", b"shared content").await;
    let attachment: FileAttachment = crud::get_unchecked(&db.pool, Table::Attachments, "a")
        .await
        .unwrap()
        .unwrap();
    let mut tx = mpac_db::attachments::lock(&db.pool).await.unwrap();
    mpac_db::attachments::queue(&mut tx, &attachment.uri)
        .await
        .unwrap();
    mpac_db::attachments::queue(&mut tx, "blobs/orphan")
        .await
        .unwrap();
    tx.commit().await.unwrap();
    blobs
        .put("blobs/orphan", b"orphan".as_slice().into())
        .await
        .unwrap();

    mpac_worker::blobs::cleanup(&db.pool, &blobs).await.unwrap();
    assert_eq!(blobs.get(&attachment.uri).await.unwrap(), "shared content");
    assert!(blobs.get("blobs/orphan").await.is_err());
    mpac_worker::blobs::cleanup(&db.pool, &blobs).await.unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM blob_cleanup")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}

#[tokio::test]
async fn oversized_blob_inference_fails_non_retryably_without_caching() {
    let db = TestDb::create().await;
    let blobs = BlobStore::in_memory();
    blobs
        .put("blobs/oversized", vec![0; 64 * 1024 * 1024 + 1].into())
        .await
        .unwrap();
    put(
        &db.pool,
        Table::Attachments,
        "large",
        &FileAttachment {
            id: "large".into(),
            uri: "blobs/oversized".into(),
            upload_state: UploadState::Ready.into(),
            ..FileAttachment::default()
        },
    )
    .await;
    run_with_attachment(&db.pool, "large").await;
    let ctx = Context::new(db.pool.clone())
        .unwrap()
        .with_blobs(Some(blobs));
    let job = claim_one(&db.pool, "answer_item").await;
    let error = answer_item::handle(&ctx, &job, answer_work(&job))
        .await
        .unwrap_err();
    assert!(!error.retryable(), "{error}");
    assert_eq!(ctx.blob_cache.len_bytes(), 0);
}

#[tokio::test]
async fn responder_metrics_publish_before_other_responders_finish() {
    let db = TestDb::create().await;
    seed_test(&db.pool, 3).await;
    let first = model(KNOW_IT_ALL, "debug");
    let mut second = first.clone();
    second.responder_id = format!("{KNOW_IT_ALL}#2");
    let created = start(&db.pool, vec![first, second], 100).await;
    let ctx = Context::new(db.pool.clone()).unwrap();
    for index in 0..3 {
        let jobs = mpac_queue::claim(&db.pool, "one-slot-worker", 1, LEASE)
            .await
            .unwrap();
        assert_eq!(jobs.len(), 1);
        let Kind::AnswerItem(work) = &jobs[0].kind else {
            panic!("expected first responder answer");
        };
        assert_eq!(work.responder_id, KNOW_IT_ALL);
        assert_eq!(dispatch(&ctx, &jobs[0]).await.unwrap(), Outcome::Committed);
        assert_eq!(
            run(&db.pool, &created.id).await.metrics.len(),
            0,
            "published before item {index} completed the responder"
        );
    }
    let jobs = mpac_queue::claim(&db.pool, "one-slot-worker", 1, LEASE)
        .await
        .unwrap();
    let Kind::FinalizeRun(work) = &jobs[0].kind else {
        panic!("responder finalization must precede the next model's queued answers");
    };
    assert_eq!(work.responder_id, KNOW_IT_ALL);
    assert_eq!(dispatch(&ctx, &jobs[0]).await.unwrap(), Outcome::Committed);
    let partial = run(&db.pool, &created.id).await;
    assert_eq!(partial.status, i32::from(ResponseCode::InProgress));
    assert_eq!(partial.completed_at_utc, None);
    assert_eq!(partial.metrics.len(), 1);
    assert_eq!(partial.metrics[0].responder_id, KNOW_IT_ALL);
    assert_eq!(partial.metrics[0].total, 3);
    assert_eq!(partial.metrics[0].num_correct, 3);
    assert_eq!(
        scalar(
            &db.pool,
            "SELECT remaining::bigint FROM run_counters WHERE run_id = $1",
            &created.id
        )
        .await,
        3
    );
    drain(&ctx).await;
    let completed = run(&db.pool, &created.id).await;
    assert_eq!(completed.status, i32::from(ResponseCode::Success));
    assert_eq!(completed.metrics.len(), 2);
    assert_eq!(completed.metrics[0].responder_id, KNOW_IT_ALL);
    assert_eq!(
        completed.metrics[1].responder_id,
        format!("{KNOW_IT_ALL}#2")
    );
    assert!(completed.completed_at_utc.is_some());
}
