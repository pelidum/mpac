use mpac_db::crud::{self, Table};
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::mpac::jobs::{BenchmarkStep, Job};
use mpac_proto::{
    Backend, BackendType, ResponseCode, Test, TestItem, TestRun, TestRunAnswer, TestType,
};
use mpac_queue::{ClaimedJob, NewJob};
use prost::Message;
use sqlx::Row;

use crate::metrics::{ResponderInput, TimedAnswer, local_inference_cost, responder_metrics};
use crate::runs::{now, responder_key};
use crate::{Context, HandlerError, HandlerResult, Outcome};

const DEBUG_RUN_COST: f64 = 0.001;

async fn sample_items(ctx: &Context, run_id: &str) -> Result<Vec<TestItem>, sqlx::Error> {
    let payloads: Vec<Vec<u8>> = sqlx::query_scalar(
        "SELECT payload FROM jobs WHERE run_id = $1 AND kind = 'answer_item' ORDER BY seq",
    )
    .bind(run_id)
    .fetch_all(&ctx.pool)
    .await?;
    let mut ids: Vec<String> = Vec::new();
    for payload in payloads {
        if let Ok(Job {
            kind: Some(Kind::AnswerItem(item)),
        }) = Job::decode(payload.as_slice())
            && !ids.contains(&item.item_id)
        {
            ids.push(item.item_id);
        }
    }
    let rows: Vec<Vec<u8>> =
        sqlx::query_scalar("SELECT proto_bytes FROM test_items WHERE id = ANY($1)")
            .bind(&ids)
            .fetch_all(&ctx.pool)
            .await?;
    let items: Vec<TestItem> = crud::decode_all(rows);
    Ok(ids
        .iter()
        .filter_map(|id| items.iter().find(|item| &item.id == id).cloned())
        .collect())
}

async fn answers(ctx: &Context, run_id: &str) -> Result<Vec<TimedAnswer>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT proto_bytes, EXTRACT(EPOCH FROM created_at)::float8 AS stored_at
         FROM test_run_answers WHERE proto_jsonb->>'run_id' = $1 ORDER BY created_at",
    )
    .bind(run_id)
    .fetch_all(&ctx.pool)
    .await?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            let bytes: Vec<u8> = row.try_get("proto_bytes").ok()?;
            Some(TimedAnswer {
                answer: TestRunAnswer::decode(bytes.as_slice()).ok()?,
                stored_at: row.try_get("stored_at").ok()?,
            })
        })
        .collect())
}

#[allow(clippy::cast_precision_loss)]
fn seconds(timestamp: Option<prost_types::Timestamp>) -> f64 {
    timestamp.map_or(0.0, |t| t.seconds as f64 + f64::from(t.nanos) / 1e9)
}

async fn run_cost(
    ctx: &Context,
    run: &TestRun,
    answers: &[TimedAnswer],
    completed: prost_types::Timestamp,
) -> Result<f32, HandlerError> {
    let total_cost: f64 = answers
        .iter()
        .map(|a| f64::from(a.answer.input_cost) + f64::from(a.answer.output_cost))
        .sum();
    let last_backend_id = run.models.last().map_or(run.backend_id.clone(), |model| {
        if model.backend_id.is_empty() {
            run.backend_id.clone()
        } else {
            model.backend_id.clone()
        }
    });
    let backend = crud::get_unchecked::<Backend>(&ctx.pool, Table::Backends, &last_backend_id)
        .await?
        .unwrap_or_default();
    let kind = BackendType::try_from(backend.backend_type).unwrap_or(BackendType::Unspecified);
    let mut cost = total_cost;
    if kind == BackendType::DebugRandom {
        cost = DEBUG_RUN_COST;
    }
    if cost == 0.0
        && matches!(
            kind,
            BackendType::Ollama | BackendType::LlamaCpp | BackendType::Vllm
        )
    {
        let created = run.created_at_utc.map_or(0, |t| t.seconds);
        #[allow(clippy::cast_precision_loss)]
        let duration = (completed.seconds - created) as f64;
        cost = local_inference_cost(duration);
    }
    #[allow(clippy::cast_possible_truncation)]
    Ok(cost as f32)
}

async fn finish_totals(
    ctx: &Context,
    run: &mut TestRun,
    answers: &[TimedAnswer],
) -> Result<(), HandlerError> {
    let completed = now();
    run.completed_at_utc = Some(completed);
    run.total_tokens = answers
        .iter()
        .map(|a| a.answer.input_tokens + a.answer.output_tokens)
        .sum();
    run.total_cost_usd = run_cost(ctx, run, answers, completed).await?;
    Ok(())
}

async fn enqueue_benchmark_step(
    conn: &mut sqlx::PgConnection,
    job: &ClaimedJob,
) -> Result<(), HandlerError> {
    if let Some(benchmark_id) = &job.benchmark_id {
        mpac_queue::enqueue(
            conn,
            &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep {
                benchmark_id: benchmark_id.clone(),
                round: 0,
            }))
            .benchmark(benchmark_id)],
        )
        .await?;
    }
    Ok(())
}

async fn advance_after_deleted_run(ctx: &Context, job: &ClaimedJob) -> HandlerResult {
    if job.benchmark_id.is_none() {
        return Ok(Outcome::Done);
    }
    let Some(mut tx) = crate::fenced(ctx, job).await? else {
        return Ok(Outcome::LeaseLost);
    };
    enqueue_benchmark_step(&mut tx, job).await?;
    tx.commit().await?;
    Ok(Outcome::Committed)
}

pub async fn handle(ctx: &Context, job: &ClaimedJob, run_id: &str) -> HandlerResult {
    let responder = match &job.kind {
        Kind::FinalizeRun(work) => work.responder_id.as_str(),
        _ => "",
    };
    let Some(mut run) = crud::get_unchecked::<TestRun>(&ctx.pool, Table::TestRuns, run_id).await?
    else {
        return advance_after_deleted_run(ctx, job).await;
    };
    let test = crud::get_unchecked::<Test>(&ctx.pool, Table::Tests, &run.test_id)
        .await?
        .unwrap_or_default();
    let evaluation = test.r#type == i32::from(TestType::Evaluation);
    let sample = sample_items(ctx, run_id).await?;
    let answers = answers(ctx, run_id).await?;
    let started = seconds(run.created_at_utc);

    let mut responders: Vec<(String, String)> = Vec::new();
    for model in &run.models {
        let key = responder_key(model);
        if !responders.iter().any(|(k, _)| *k == key) {
            responders.push((key, model.id.clone()));
        }
    }
    run.metrics = responders
        .iter()
        .filter(|(key, _)| responder.is_empty() || key == responder)
        .map(|(responder, model_id)| {
            let mine: Vec<&TimedAnswer> = answers
                .iter()
                .filter(|a| {
                    let key = if a.answer.responder_id.is_empty() {
                        &a.answer.model_id
                    } else {
                        &a.answer.responder_id
                    };
                    key == responder
                })
                .collect();
            responder_metrics(&ResponderInput {
                responder_id: responder,
                model_id,
                evaluation,
                sample: &sample,
                answers: &mine,
                started_at: started,
            })
        })
        .collect();

    if responder.is_empty() {
        finish_totals(ctx, &mut run, &answers).await?;
    }

    let Some(mut tx) = crate::fenced(ctx, job).await? else {
        return Ok(Outcome::LeaseLost);
    };
    let Some(mut stored) =
        crud::get_for_update::<TestRun>(&mut tx, Table::TestRuns, run_id).await?
    else {
        enqueue_benchmark_step(&mut tx, job).await?;
        tx.commit().await?;
        return Ok(Outcome::Committed);
    };
    if !responder.is_empty() {
        if stored.status == i32::from(ResponseCode::InProgress) {
            for metrics in run.metrics {
                stored
                    .metrics
                    .retain(|existing| existing.responder_id != metrics.responder_id);
                stored.metrics.push(metrics);
            }
            crud::update_in(&mut tx, Table::TestRuns, run_id, &stored).await?;
        }
        tx.commit().await?;
        return Ok(Outcome::Committed);
    }
    stored.metrics = run.metrics;
    stored.completed_at_utc = run.completed_at_utc;
    stored.total_tokens = run.total_tokens;
    stored.total_cost_usd = run.total_cost_usd;
    if stored.status != i32::from(ResponseCode::Cancelled) {
        stored.status = ResponseCode::Success.into();
    }
    crud::update_in(&mut tx, Table::TestRuns, run_id, &stored).await?;

    enqueue_benchmark_step(&mut tx, job).await?;
    tx.commit().await?;
    Ok(Outcome::Committed)
}
