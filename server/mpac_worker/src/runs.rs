use std::time::SystemTime;

use mpac_db::crud;
use mpac_proto::json::to_jsonb;
use mpac_proto::mpac::jobs::AnswerItem;
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::{Model, ResponseCode, TestItem, TestRun};
use mpac_queue::NewJob;
use prost::Message;
use sqlx::{PgConnection, PgPool};

pub const DEFAULT_SAMPLE_SIZE: i64 = 10;

#[derive(Debug, Clone, Default)]
pub struct NewRun {
    pub test_id: String,
    pub owner: String,
    pub models: Vec<Model>,
    pub include_reasoning: bool,
    pub sample_size: i64,
    pub backend_id: String,
    pub benchmark_id: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum RunError {
    #[error("run query failed: {0}")]
    Query(#[from] sqlx::Error),
    #[error(transparent)]
    Queue(#[from] mpac_queue::QueueError),
    #[error(transparent)]
    Json(#[from] mpac_proto::json::JsonError),
    #[error(transparent)]
    Crud(#[from] crud::CrudError),
}

pub fn responder_key(model: &Model) -> String {
    if model.responder_id.is_empty() {
        model.id.clone()
    } else {
        model.responder_id.clone()
    }
}

pub fn now() -> prost_types::Timestamp {
    prost_types::Timestamp::from(SystemTime::now())
}

pub async fn sample_items(
    conn: &mut PgConnection,
    test_id: &str,
    sample_size: i64,
) -> Result<Vec<TestItem>, RunError> {
    let limit = if sample_size > 0 {
        sample_size
    } else {
        DEFAULT_SAMPLE_SIZE
    };
    let rows: Vec<Vec<u8>> = sqlx::query_scalar(
        "SELECT proto_bytes FROM test_items WHERE proto_jsonb ->> 'test_id' = $1 ORDER BY RANDOM() LIMIT $2",
    )
    .bind(test_id)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    Ok(crud::decode_all(rows))
}

pub fn answer_jobs(run: &TestRun, items: &[TestItem], benchmark_id: Option<&str>) -> Vec<NewJob> {
    let mut jobs = Vec::with_capacity(run.models.len() * items.len());
    for model in &run.models {
        let backend = if model.backend_id.is_empty() {
            run.backend_id.clone()
        } else {
            model.backend_id.clone()
        };
        for item in items {
            let mut job = NewJob::new(Kind::AnswerItem(AnswerItem {
                run_id: run.id.clone(),
                item_id: item.id.clone(),
                responder_id: responder_key(model),
                model: Some(model.clone()),
                include_reasoning: run.include_reasoning,
            }))
            .run(&run.id)
            .backend(backend.clone())
            .owner(&run.owner);
            if let Some(benchmark_id) = benchmark_id {
                job = job.benchmark(benchmark_id);
            }
            jobs.push(job);
        }
    }
    jobs
}

pub async fn create_run(pool: &PgPool, request: NewRun) -> Result<TestRun, RunError> {
    let mut tx = pool.begin().await?;
    let run = create_run_in(&mut tx, request).await?;
    tx.commit().await?;
    Ok(run)
}

pub async fn create_run_in(conn: &mut PgConnection, request: NewRun) -> Result<TestRun, RunError> {
    let run = TestRun {
        id: uuid::Uuid::new_v4().simple().to_string(),
        test_id: request.test_id,
        owner: request.owner,
        status: ResponseCode::InProgress.into(),
        models: request.models,
        include_reasoning: request.include_reasoning,
        sample_size: request.sample_size,
        backend_id: request.backend_id,
        created_at_utc: Some(now()),
        ..TestRun::default()
    };
    let items = sample_items(conn, &run.test_id, run.sample_size).await?;
    let jobs = answer_jobs(&run, &items, request.benchmark_id.as_deref());
    sqlx::query("INSERT INTO test_runs (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)")
        .bind(&run.id)
        .bind(to_jsonb(&run)?)
        .bind(run.encode_to_vec())
        .execute(&mut *conn)
        .await?;
    mpac_queue::start_run(conn, &run.id, request.benchmark_id.as_deref(), &jobs).await?;
    Ok(run)
}

pub async fn cancel_benchmark_runs(
    conn: &mut PgConnection,
    benchmark_id: &str,
) -> Result<u64, RunError> {
    let run_ids: Vec<String> = sqlx::query_scalar(
        "SELECT run_id FROM benchmark_runs
         WHERE benchmark_id = $1 AND run_id IS NOT NULL ORDER BY round, match_index",
    )
    .bind(benchmark_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut cancelled = 0;
    for run_id in run_ids {
        let Some(mut run) =
            crud::get_for_update::<TestRun>(conn, crud::Table::TestRuns, &run_id).await?
        else {
            continue;
        };
        if run.status == i32::from(ResponseCode::InProgress) {
            run.status = ResponseCode::Cancelled.into();
            run.completed_at_utc = Some(now());
            crud::update_in(conn, crud::Table::TestRuns, &run_id, &run).await?;
            cancelled += 1;
        }
        mpac_queue::cancel_run(conn, &run_id).await?;
    }
    mpac_queue::cancel_benchmark(conn, benchmark_id).await?;
    Ok(cancelled)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answer_jobs_are_model_major_with_backend_fallback() {
        let run = TestRun {
            id: "r".into(),
            owner: "o@example.com".into(),
            backend_id: "run-backend".into(),
            models: vec![
                Model {
                    id: "a".into(),
                    ..Model::default()
                },
                Model {
                    id: "b".into(),
                    backend_id: "own".into(),
                    responder_id: "b#2".into(),
                    ..Model::default()
                },
            ],
            ..TestRun::default()
        };
        let items: Vec<TestItem> = (0..2)
            .map(|i| TestItem {
                id: format!("i{i}"),
                ..TestItem::default()
            })
            .collect();

        let jobs = answer_jobs(&run, &items, Some("bench"));

        let summary: Vec<(String, Option<String>, String)> = jobs
            .iter()
            .map(|job| {
                let Kind::AnswerItem(item) = &job.kind else {
                    panic!()
                };
                (
                    item.responder_id.clone(),
                    job.backend_id.clone(),
                    item.item_id.clone(),
                )
            })
            .collect();
        assert_eq!(
            summary,
            vec![
                ("a".into(), Some("run-backend".into()), "i0".into()),
                ("a".into(), Some("run-backend".into()), "i1".into()),
                ("b#2".into(), Some("own".into()), "i0".into()),
                ("b#2".into(), Some("own".into()), "i1".into()),
            ]
        );
        assert!(
            jobs.iter()
                .all(|j| j.benchmark_id.as_deref() == Some("bench")
                    && j.owner.as_deref() == Some("o@example.com"))
        );
    }
}
