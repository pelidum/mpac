#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::Duration;

use mpac_db::crud::{self, Table};
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::mpac::jobs::{AnswerItem, BenchmarkStep, FinalizeRun};
use mpac_proto::{Backend, Model};
use mpac_queue::signals::{Signal, Signals};
use mpac_queue::{
    ClaimedJob, FailOutcome, NewJob, ReapReport, cancel_run, claim, complete, enqueue,
    extend_lease, fail, prune_finished, reap_expired, release_worker_leases, reschedule, start_run,
};
use mpac_testing::TestDb;
use sqlx::PgPool;

const LEASE: Duration = Duration::from_secs(60);

fn answer(run_id: &str, item: usize) -> NewJob {
    NewJob::new(Kind::AnswerItem(AnswerItem {
        run_id: run_id.into(),
        item_id: format!("item-{item}"),
        ..AnswerItem::default()
    }))
    .run(run_id)
}

fn model_answer(run_id: &str, item: usize, model_id: &str, backend: &str) -> NewJob {
    NewJob::new(Kind::AnswerItem(AnswerItem {
        run_id: run_id.into(),
        item_id: format!("item-{item}"),
        model: Some(Model {
            id: model_id.into(),
            ..Model::default()
        }),
        ..AnswerItem::default()
    }))
    .run(run_id)
    .backend(backend)
}

fn model_of(job: &ClaimedJob) -> &str {
    match &job.kind {
        Kind::AnswerItem(work) => work.model.as_ref().map_or("", |model| model.id.as_str()),
        _ => "",
    }
}

async fn add(pool: &PgPool, jobs: &[NewJob]) {
    let mut conn = pool.acquire().await.unwrap();
    enqueue(&mut conn, jobs).await.unwrap();
}

async fn begin_run(pool: &PgPool, run_id: &str, jobs: &[NewJob]) {
    let mut tx = pool.begin().await.unwrap();
    start_run(&mut tx, run_id, None, jobs).await.unwrap();
    tx.commit().await.unwrap();
}

async fn count(pool: &PgPool, sql: &str) -> i64 {
    sqlx::query_scalar(sqlx::AssertSqlSafe(sql.to_owned()))
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn finalize_jobs(pool: &PgPool, run_id: &str) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'finalize_run' AND run_id = $1")
        .bind(run_id)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn live_finalizers(pool: &PgPool, run_id: &str) -> (i64, i64) {
    sqlx::query_as(
        "SELECT COUNT(*) FILTER (WHERE status IN ('queued', 'leased', 'done')),
                COUNT(*) FILTER (WHERE status IN ('cancelled', 'failed'))
         FROM jobs WHERE kind = 'finalize_run' AND run_id = $1",
    )
    .bind(run_id)
    .fetch_one(pool)
    .await
    .unwrap()
}

async fn job_status(pool: &PgPool, id: uuid::Uuid) -> String {
    sqlx::query_scalar("SELECT status FROM jobs WHERE id = $1")
        .bind(id)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn add_backend(pool: &PgPool, id: &str, max_concurrency: i32, is_local: bool) {
    let backend = Backend {
        id: id.into(),
        max_concurrency,
        is_local,
        enabled: true,
        ..Backend::default()
    };
    crud::create(pool, Table::Backends, id, &backend, false)
        .await
        .unwrap();
}

#[tokio::test]
async fn enqueue_and_claim_in_order_with_payloads() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0), answer("r", 1), answer("r", 2)]).await;

    let first = claim(&db.pool, "w1", 2, LEASE).await.unwrap();
    let second = claim(&db.pool, "w1", 2, LEASE).await.unwrap();
    let third = claim(&db.pool, "w1", 2, LEASE).await.unwrap();

    assert_eq!((first.len(), second.len(), third.len()), (2, 1, 0));
    let Kind::AnswerItem(item) = &first[0].kind else {
        panic!("unexpected kind");
    };
    assert_eq!(item.item_id, "item-0");
    assert!(
        first
            .iter()
            .all(|job| job.attempts == 1 && job.run_id.as_deref() == Some("r"))
    );
}

#[tokio::test]
async fn claims_follow_enqueue_order_within_a_batch() {
    let db = TestDb::create().await;
    let jobs: Vec<NewJob> = (0..50).map(|i| answer("r", i)).collect();
    add(&db.pool, &jobs).await;

    let mut order = Vec::new();
    for _ in 0..50 {
        let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
        let Kind::AnswerItem(item) = job.kind else {
            panic!("unexpected kind");
        };
        order.push(item.item_id);
    }

    let expected: Vec<String> = (0..50).map(|i| format!("item-{i}")).collect();
    assert_eq!(order, expected);
}

#[tokio::test]
async fn undecodable_payloads_fail_without_poisoning_the_batch() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0)]).await;
    sqlx::query("INSERT INTO jobs (kind, payload) VALUES ('finalize_run', '\\x120a0a08')")
        .execute(&db.pool)
        .await
        .unwrap();
    add(&db.pool, &[answer("r", 1)]).await;

    let claimed = claim(&db.pool, "w1", 10, LEASE).await.unwrap();

    assert_eq!(claimed.len(), 2);
    let failed: Vec<(String, Option<String>)> =
        sqlx::query_as("SELECT kind, last_error FROM jobs WHERE status = 'failed'")
            .fetch_all(&db.pool)
            .await
            .unwrap();
    assert_eq!(failed.len(), 1);
    assert!(
        failed[0]
            .1
            .as_deref()
            .unwrap()
            .contains("undecodable payload")
    );
}

#[tokio::test]
async fn undecodable_finalizers_are_replaced_once_a_run_has_no_work_left() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "finished", &[]).await;
    claim(&db.pool, "w1", 10, LEASE).await.unwrap();
    begin_run(&db.pool, "active", &[answer("active", 0)]).await;
    sqlx::query(
        "INSERT INTO jobs (kind, run_id, payload) VALUES
         ('finalize_run', 'finished', '\\x120a0a08'), ('finalize_run', 'active', '\\x120a0a08')",
    )
    .execute(&db.pool)
    .await
    .unwrap();

    claim(&db.pool, "w1", 10, LEASE).await.unwrap();

    let queued = |run: &'static str| {
        sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM jobs
             WHERE kind = 'finalize_run' AND run_id = $1 AND status = 'queued'",
        )
        .bind(run)
    };
    assert_eq!(queued("finished").fetch_one(&db.pool).await.unwrap(), 1);
    assert_eq!(queued("active").fetch_one(&db.pool).await.unwrap(), 0);
}

#[tokio::test]
async fn undecodable_benchmark_steps_are_replaced() {
    let db = TestDb::create().await;
    sqlx::query(
        "INSERT INTO jobs (kind, benchmark_id, payload)
         VALUES ('benchmark_step', 'b', '\\x120a0a08')",
    )
    .execute(&db.pool)
    .await
    .unwrap();

    claim(&db.pool, "w1", 10, LEASE).await.unwrap();

    let queued: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM jobs
         WHERE kind = 'benchmark_step' AND benchmark_id = 'b' AND status = 'queued'",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(queued, 1);
}

#[tokio::test]
async fn undecodable_answer_jobs_still_release_their_run_slot() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0)]).await;
    sqlx::query("UPDATE jobs SET payload = '\\x120a0a08' WHERE run_id = 'r'")
        .execute(&db.pool)
        .await
        .unwrap();

    assert_eq!(claim(&db.pool, "w1", 10, LEASE).await.unwrap(), Vec::new());

    assert_eq!(
        count(
            &db.pool,
            "SELECT remaining::bigint FROM run_counters WHERE run_id = 'r'"
        )
        .await,
        0
    );
    assert_eq!(finalize_jobs(&db.pool, "r").await, 1);
}

#[tokio::test]
async fn shutdown_releases_only_this_workers_leases_without_spending_attempts() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0), answer("r", 1), answer("r", 2)]).await;
    let mine = claim(&db.pool, "w1", 2, LEASE).await.unwrap();
    let theirs = claim(&db.pool, "w10", 1, LEASE).await.unwrap().remove(0);

    assert_eq!(release_worker_leases(&db.pool, "w1").await.unwrap(), 2);

    for job in &mine {
        assert_eq!(job_status(&db.pool, job.id).await, "queued");
    }
    assert_eq!(job_status(&db.pool, theirs.id).await, "leased");
    let reclaimed = claim(&db.pool, "w2", 10, LEASE).await.unwrap();
    assert_eq!(reclaimed.len(), 2);
    assert!(reclaimed.iter().all(|job| job.attempts == 1));
}

#[tokio::test]
async fn cancelling_a_benchmark_match_keeps_the_finalizers_benchmark() {
    let db = TestDb::create().await;
    let mut tx = db.pool.begin().await.unwrap();
    start_run(&mut tx, "r", Some("b"), &[answer("r", 0).benchmark("b")])
        .await
        .unwrap();
    tx.commit().await.unwrap();

    let mut conn = db.pool.acquire().await.unwrap();
    cancel_run(&mut conn, "r").await.unwrap();

    let benchmark: Option<String> = sqlx::query_scalar(
        "SELECT benchmark_id FROM jobs WHERE kind = 'finalize_run' AND run_id = 'r'",
    )
    .fetch_one(&db.pool)
    .await
    .unwrap();
    assert_eq!(benchmark.as_deref(), Some("b"));
}

#[tokio::test]
async fn delayed_jobs_wait_for_run_after() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0).delay(Duration::from_hours(1))]).await;

    assert_eq!(claim(&db.pool, "w1", 10, LEASE).await.unwrap(), Vec::new());
}

#[tokio::test]
async fn concurrent_claimers_never_double_claim() {
    let db = TestDb::create().await;
    let jobs: Vec<NewJob> = (0..200).map(|i| answer("r", i)).collect();
    add(&db.pool, &jobs).await;

    let mut workers = Vec::new();
    for worker in 0..8 {
        let pool = db.pool(2).await;
        workers.push(tokio::spawn(async move {
            let mut claimed = Vec::new();
            loop {
                let batch = claim(&pool, &format!("w{worker}"), 7, LEASE).await.unwrap();
                if batch.is_empty() {
                    return claimed;
                }
                claimed.extend(batch.into_iter().map(|job| job.id));
            }
        }));
    }
    let mut all = Vec::new();
    for worker in workers {
        all.extend(worker.await.unwrap());
    }

    let unique: HashSet<_> = all.iter().collect();
    assert_eq!(all.len(), 200);
    assert_eq!(unique.len(), 200);
}

#[tokio::test]
async fn backend_capacity_holds_across_claimers() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "limited", 2, false).await;
    let jobs: Vec<NewJob> = (0..30).map(|i| answer("r", i).backend("limited")).collect();
    add(&db.pool, &jobs).await;

    let in_flight = Arc::new(AtomicI32::new(0));
    let peak = Arc::new(AtomicI32::new(0));
    let mut workers = Vec::new();
    for worker in 0..3 {
        let pool = db.pool(2).await;
        let (in_flight, peak) = (Arc::clone(&in_flight), Arc::clone(&peak));
        workers.push(tokio::spawn(async move {
            let name = format!("w{worker}");
            let mut done = 0;
            loop {
                let batch = claim(&pool, &name, 5, LEASE).await.unwrap();
                if batch.is_empty() {
                    let left: i64 = sqlx::query_scalar(
                        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'leased')",
                    )
                    .fetch_one(&pool)
                    .await
                    .unwrap();
                    if left == 0 {
                        return done;
                    }
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    continue;
                }
                let now = in_flight
                    .fetch_add(i32::try_from(batch.len()).unwrap(), Ordering::SeqCst)
                    + i32::try_from(batch.len()).unwrap();
                peak.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(20)).await;
                for job in &batch {
                    in_flight.fetch_sub(1, Ordering::SeqCst);
                    assert!(complete(&pool, job).await.unwrap().recorded);
                    done += 1;
                }
            }
        }));
    }
    let mut done = 0;
    for worker in workers {
        done += worker.await.unwrap();
    }

    assert_eq!(done, 30);
    assert!(
        peak.load(Ordering::SeqCst) <= 2,
        "peak {}",
        peak.load(Ordering::SeqCst)
    );
}

#[tokio::test]
async fn concurrency_defaults_follow_python_backend_pool() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "local", 0, true).await;
    let local: Vec<NewJob> = (0..10).map(|i| answer("r", i).backend("local")).collect();
    let remote: Vec<NewJob> = (0..30)
        .map(|i| answer("r", i).backend("unregistered"))
        .collect();
    let unbounded: Vec<NewJob> = (0..30)
        .map(|i| {
            NewJob::new(Kind::FinalizeRun(FinalizeRun {
                run_id: format!("f{i}"),
                responder_id: String::new(),
            }))
        })
        .collect();
    add(&db.pool, &[local, remote, unbounded].concat()).await;

    let claimed = claim(&db.pool, "w1", 1000, LEASE).await.unwrap();
    let by_backend = |backend: Option<&str>| {
        claimed
            .iter()
            .filter(|job| job.backend_id.as_deref() == backend)
            .count()
    };

    assert_eq!(by_backend(Some("local")), 4);
    assert_eq!(by_backend(Some("unregistered")), 20);
    assert_eq!(by_backend(None), 30);
}

#[tokio::test]
async fn local_backends_finish_one_model_before_starting_the_next() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "local", 0, true).await;
    let jobs: Vec<NewJob> = (0..3)
        .map(|i| model_answer("r", i, "a", "local"))
        .chain((0..3).map(|i| model_answer("r", i, "b", "local")))
        .collect();
    add(&db.pool, &jobs).await;

    let first = claim(&db.pool, "w1", 100, LEASE).await.unwrap();
    assert_eq!(first.len(), 3);
    assert!(first.iter().all(|job| model_of(job) == "a"));
    assert_eq!(claim(&db.pool, "w2", 100, LEASE).await.unwrap().len(), 0);

    complete(&db.pool, &first[0]).await.unwrap();
    complete(&db.pool, &first[1]).await.unwrap();
    assert_eq!(claim(&db.pool, "w1", 100, LEASE).await.unwrap().len(), 0);

    complete(&db.pool, &first[2]).await.unwrap();
    let second = claim(&db.pool, "w1", 100, LEASE).await.unwrap();
    assert_eq!(second.len(), 3);
    assert!(second.iter().all(|job| model_of(job) == "b"));
}

#[tokio::test]
async fn local_barrier_keeps_queue_order_across_runs() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "local", 0, true).await;
    add(
        &db.pool,
        &[
            model_answer("r1", 0, "a", "local"),
            model_answer("r2", 0, "b", "local"),
            model_answer("r3", 0, "a", "local"),
        ],
    )
    .await;

    let mut order = Vec::new();
    for _ in 0..3 {
        let claimed = claim(&db.pool, "w1", 100, LEASE).await.unwrap();
        assert_eq!(claimed.len(), 1);
        order.push(model_of(&claimed[0]).to_owned());
        complete(&db.pool, &claimed[0]).await.unwrap();
    }
    assert_eq!(order, ["a", "b", "a"]);
}

#[tokio::test]
async fn local_barrier_respects_a_retry_waiting_behind_other_models() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "local", 0, true).await;
    add(
        &db.pool,
        &[
            model_answer("r", 0, "a", "local"),
            model_answer("r", 0, "b", "local"),
        ],
    )
    .await;

    let first = claim(&db.pool, "w1", 1, LEASE).await.unwrap();
    assert_eq!(model_of(&first[0]), "a");
    assert_eq!(
        fail(&db.pool, &first[0], "boom", true).await.unwrap(),
        FailOutcome::Retrying
    );

    let next = claim(&db.pool, "w1", 100, LEASE).await.unwrap();
    assert_eq!(next.len(), 1);
    assert_eq!(model_of(&next[0]), "b");
}

#[tokio::test]
async fn remote_backends_overlap_models() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "remote", 0, false).await;
    let jobs: Vec<NewJob> = (0..3)
        .map(|i| model_answer("r", i, "a", "remote"))
        .chain((0..3).map(|i| model_answer("r", i, "b", "remote")))
        .collect();
    add(&db.pool, &jobs).await;

    assert_eq!(claim(&db.pool, "w1", 100, LEASE).await.unwrap().len(), 6);
}

#[tokio::test]
async fn a_backlog_on_one_backend_cannot_starve_another() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "remote", 0, false).await;
    add_backend(&db.pool, "local", 0, true).await;
    let backlog: Vec<NewJob> = (0..10)
        .map(|i| model_answer("r1", i, "a", "remote"))
        .collect();
    add(&db.pool, &backlog).await;
    add(
        &db.pool,
        &[
            model_answer("r2", 0, "b", "local"),
            model_answer("r2", 1, "b", "local"),
        ],
    )
    .await;

    let batch = claim(&db.pool, "w1", 4, LEASE).await.unwrap();
    let on = |backend: &str| {
        batch
            .iter()
            .filter(|job| job.backend_id.as_deref() == Some(backend))
            .count()
    };
    assert_eq!((on("remote"), on("local")), (2, 2));
}

#[tokio::test]
async fn free_slots_go_to_the_backend_with_the_least_in_flight() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "remote", 0, false).await;
    add_backend(&db.pool, "local", 0, true).await;
    let backlog: Vec<NewJob> = (0..10)
        .map(|i| model_answer("r1", i, "a", "remote"))
        .collect();
    add(&db.pool, &backlog).await;
    claim(&db.pool, "w1", 3, LEASE).await.unwrap();
    add(&db.pool, &[model_answer("r2", 0, "b", "local")]).await;

    let next = claim(&db.pool, "w1", 1, LEASE).await.unwrap();

    assert_eq!(next[0].backend_id.as_deref(), Some("local"));
}

#[tokio::test]
async fn claims_stay_fast_with_a_large_backlog() {
    let db = TestDb::create().await;
    add_backend(&db.pool, "remote", 0, false).await;
    add_backend(&db.pool, "local", 0, true).await;
    sqlx::query(
        "INSERT INTO jobs (kind, run_id, backend_id, model_id, payload)
         SELECT 'answer_item', 'r', CASE WHEN n % 10 = 0 THEN 'local' ELSE 'remote' END,
                'm' || (n % 5)::text, '\\x'
         FROM generate_series(1, 100000) AS n",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    sqlx::query("ANALYZE jobs").execute(&db.pool).await.unwrap();

    let mut slowest = Duration::ZERO;
    for _ in 0..5 {
        let started = std::time::Instant::now();
        claim(&db.pool, "w1", 4, LEASE).await.unwrap();
        slowest = slowest.max(started.elapsed());
    }

    println!("slowest claim with a 100000 job backlog: {slowest:?}");
    assert!(slowest < Duration::from_secs(2), "{slowest:?}");
}

#[tokio::test]
async fn expired_leases_requeue_then_fail_and_still_finish_the_run() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0).max_attempts(2)]).await;
    let short = Duration::from_millis(300);

    assert_eq!(claim(&db.pool, "w1", 1, short).await.unwrap().len(), 1);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(
        reap_expired(&db.pool).await.unwrap(),
        ReapReport {
            requeued: 1,
            failed: 0
        }
    );

    let retry = claim(&db.pool, "w2", 1, short).await.unwrap();
    assert_eq!(retry[0].attempts, 2);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(
        reap_expired(&db.pool).await.unwrap(),
        ReapReport {
            requeued: 0,
            failed: 1
        }
    );

    assert_eq!(
        count(
            &db.pool,
            "SELECT COUNT(*) FROM jobs WHERE kind = 'answer_item' AND status = 'failed'"
        )
        .await,
        1
    );
    assert_eq!(finalize_jobs(&db.pool, "r").await, 1);
}

#[tokio::test]
async fn lease_extension_detects_cancellation() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0), answer("r", 1)]).await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);

    assert!(extend_lease(&db.pool, &job, LEASE).await.unwrap());
    let forged = ClaimedJob {
        lease: "w1/forged".into(),
        ..job.clone()
    };
    assert!(!extend_lease(&db.pool, &forged, LEASE).await.unwrap());

    let mut conn = db.pool.acquire().await.unwrap();
    assert_eq!(cancel_run(&mut conn, "r").await.unwrap(), 2);
    drop(conn);

    assert!(!extend_lease(&db.pool, &job, LEASE).await.unwrap());
    assert!(!complete(&db.pool, &job).await.unwrap().recorded);
    assert_eq!(finalize_jobs(&db.pool, "r").await, 1);
    assert!(
        claim(&db.pool, "w1", 10, LEASE)
            .await
            .unwrap()
            .iter()
            .all(|j| j.kind.name() == "finalize_run")
    );
}

#[tokio::test]
async fn failures_retry_with_backoff_then_fail_permanently() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0).max_attempts(2)]).await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);

    assert_eq!(
        fail(&db.pool, &job, "boom", true).await.unwrap(),
        FailOutcome::Retrying
    );
    assert_eq!(claim(&db.pool, "w1", 1, LEASE).await.unwrap(), Vec::new());
    sqlx::query("UPDATE jobs SET run_after = NOW()")
        .execute(&db.pool)
        .await
        .unwrap();
    let retry = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    assert_eq!(retry.attempts, 2);

    assert_eq!(
        fail(&db.pool, &retry, "boom again", true).await.unwrap(),
        FailOutcome::Failed {
            finalize_enqueued: true
        }
    );
    let error: String = sqlx::query_scalar("SELECT last_error FROM jobs WHERE id = $1")
        .bind(retry.id)
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(error, "boom again");
}

#[tokio::test]
async fn reschedule_returns_job_without_consuming_an_attempt() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0)]).await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);

    assert!(reschedule(&db.pool, &job, Duration::ZERO).await.unwrap());

    let again = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    assert_eq!(again.attempts, 1);
}

#[tokio::test]
async fn parallel_completions_enqueue_exactly_one_finalize() {
    let db = TestDb::create().await;
    let jobs: Vec<NewJob> = (0..100).map(|i| answer("r", i)).collect();
    begin_run(&db.pool, "r", &jobs).await;
    let claimed = claim(&db.pool, "w1", 100, LEASE).await.unwrap();
    assert_eq!(claimed.len(), 100);

    let completions = futures::future::join_all(claimed.into_iter().map(|job: ClaimedJob| {
        let pool = db.pool.clone();
        tokio::spawn(async move { complete(&pool, &job).await.unwrap() })
    }))
    .await;

    let finalized = completions
        .into_iter()
        .map(Result::unwrap)
        .filter(|completion| completion.finalize_enqueued)
        .count();
    assert_eq!(finalized, 1);
    assert_eq!(finalize_jobs(&db.pool, "r").await, 1);
    assert_eq!(
        count(
            &db.pool,
            "SELECT remaining::bigint FROM run_counters WHERE run_id = 'r'"
        )
        .await,
        0
    );
}

#[tokio::test]
async fn empty_runs_finalize_immediately() {
    let db = TestDb::create().await;

    begin_run(&db.pool, "empty", &[]).await;

    assert_eq!(finalize_jobs(&db.pool, "empty").await, 1);
}

#[tokio::test]
async fn cancelling_a_finished_run_keeps_its_runnable_finalizer() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0)]).await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    assert!(complete(&db.pool, &job).await.unwrap().finalize_enqueued);

    for _ in 0..2 {
        let mut conn = db.pool.acquire().await.unwrap();
        cancel_run(&mut conn, "r").await.unwrap();
    }

    assert_eq!(live_finalizers(&db.pool, "r").await, (1, 0));
    let claimed = claim(&db.pool, "w1", 10, LEASE).await.unwrap();
    assert_eq!(claimed.len(), 1);
    assert_eq!(claimed[0].kind.name(), "finalize_run");
}

#[tokio::test]
async fn cancelling_mid_run_twice_leaves_exactly_one_runnable_finalizer() {
    let db = TestDb::create().await;
    begin_run(
        &db.pool,
        "r",
        &[answer("r", 0), answer("r", 1), answer("r", 2)],
    )
    .await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    complete(&db.pool, &job).await.unwrap();

    for _ in 0..2 {
        let mut conn = db.pool.acquire().await.unwrap();
        cancel_run(&mut conn, "r").await.unwrap();
    }

    assert_eq!(live_finalizers(&db.pool, "r").await, (1, 0));
}

#[tokio::test]
async fn claim_never_resurrects_a_concurrently_cancelled_job() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0)]).await;
    let id: uuid::Uuid = sqlx::query_scalar("SELECT id FROM jobs WHERE kind = 'answer_item'")
        .fetch_one(&db.pool)
        .await
        .unwrap();

    let mut cancelling = db.pool.begin().await.unwrap();
    sqlx::query("UPDATE jobs SET status = 'cancelled' WHERE id = $1")
        .bind(id)
        .execute(&mut *cancelling)
        .await
        .unwrap();
    let pool = db.pool(2).await;
    let claim_task = tokio::spawn(async move { claim(&pool, "w1", 10, LEASE).await.unwrap() });
    tokio::time::sleep(Duration::from_millis(300)).await;
    cancelling.commit().await.unwrap();
    let claimed = claim_task.await.unwrap();

    assert_eq!(claimed, Vec::new());
    assert_eq!(job_status(&db.pool, id).await, "cancelled");
}

#[tokio::test]
async fn stale_task_cannot_touch_a_newer_lease_from_the_same_worker() {
    let db = TestDb::create().await;
    begin_run(&db.pool, "r", &[answer("r", 0)]).await;
    let stale = claim(&db.pool, "w1", 1, Duration::from_millis(200))
        .await
        .unwrap()
        .remove(0);
    tokio::time::sleep(Duration::from_millis(400)).await;
    reap_expired(&db.pool).await.unwrap();
    let current = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    assert_eq!(current.id, stale.id);
    assert_ne!(current.lease, stale.lease);

    assert!(!extend_lease(&db.pool, &stale, LEASE).await.unwrap());
    assert!(!complete(&db.pool, &stale).await.unwrap().recorded);
    assert_eq!(
        fail(&db.pool, &stale, "stale", true).await.unwrap(),
        FailOutcome::LeaseLost
    );
    assert!(!reschedule(&db.pool, &stale, Duration::ZERO).await.unwrap());
    assert_eq!(job_status(&db.pool, current.id).await, "leased");

    assert!(complete(&db.pool, &current).await.unwrap().recorded);
}

#[tokio::test]
async fn signals_deliver_jobs_and_cancellations() {
    let db = TestDb::create().await;
    let mut signals = Signals::connect(&db.pool, Duration::from_secs(30))
        .await
        .unwrap();

    add(
        &db.pool,
        &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep::default()))],
    )
    .await;
    let first = tokio::time::timeout(Duration::from_secs(5), signals.next())
        .await
        .unwrap();
    let mut conn = db.pool.acquire().await.unwrap();
    cancel_run(&mut conn, "run-42").await.unwrap();
    drop(conn);
    let second = tokio::time::timeout(Duration::from_secs(5), signals.next())
        .await
        .unwrap();

    assert_eq!(first, Signal::Jobs);
    assert_eq!(second, Signal::Cancel("run-42".into()));
}

#[tokio::test]
async fn signals_fall_back_to_polling() {
    let db = TestDb::create().await;
    let mut signals = Signals::connect(&db.pool, Duration::from_millis(100))
        .await
        .unwrap();

    let signal = tokio::time::timeout(Duration::from_secs(5), signals.next())
        .await
        .unwrap();

    assert_eq!(signal, Signal::Tick);
}

#[tokio::test]
async fn prune_removes_only_old_finished_jobs() {
    let db = TestDb::create().await;
    add(&db.pool, &[answer("r", 0), answer("r", 1)]).await;
    let job = claim(&db.pool, "w1", 1, LEASE).await.unwrap().remove(0);
    complete(&db.pool, &job).await.unwrap();
    sqlx::query("UPDATE jobs SET updated_at = NOW() - interval '2 days' WHERE status = 'done'")
        .execute(&db.pool)
        .await
        .unwrap();

    assert_eq!(
        prune_finished(&db.pool, Duration::from_hours(24))
            .await
            .unwrap(),
        1
    );
    assert_eq!(count(&db.pool, "SELECT COUNT(*) FROM jobs").await, 1);
}
