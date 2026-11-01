use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use mpac_db::crud::{self, Table};
use mpac_inference::{AnswerInput, AttachmentData};
use mpac_proto::file_attachment::UploadState;
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::mpac::jobs::{AnswerItem, FinalizeRun};
use mpac_proto::test_run_answer::Status;
use mpac_proto::{
    Backend, FileAttachment, Model, ResponseCode, Test, TestItem, TestRun, TestRunAnswer,
};
use mpac_queue::{ClaimedJob, NewJob};
use prost::Message as _;

use crate::runs::{now, responder_key};
use crate::{Context, HandlerError, HandlerResult, Outcome};

const BREAKER_THRESHOLD: i64 = 5;
const SKIPPED_ERROR: &str =
    "Skipped - backend unreachable (circuit breaker open after repeated connection errors)";

async fn breaker_open(
    ctx: &Context,
    run_id: &str,
    responder_id: &str,
) -> Result<bool, sqlx::Error> {
    let recent: Vec<Option<String>> = sqlx::query_scalar(
        "SELECT proto_jsonb->>'status' FROM test_run_answers
         WHERE proto_jsonb->>'run_id' = $1
           AND COALESCE(NULLIF(proto_jsonb->>'responder_id', ''), proto_jsonb->>'model_id') = $2
         ORDER BY created_at DESC LIMIT $3",
    )
    .bind(run_id)
    .bind(responder_id)
    .bind(BREAKER_THRESHOLD)
    .fetch_all(&ctx.pool)
    .await?;
    Ok(
        i64::try_from(recent.len()).unwrap_or(0) == BREAKER_THRESHOLD
            && recent
                .iter()
                .all(|status| status.as_deref() == Some("CONNECTION_ERROR")),
    )
}

fn until_next_minute() -> Duration {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        % 60;
    Duration::from_secs(60 - elapsed + 1)
}

async fn run_cost(ctx: &Context, run_id: &str) -> Result<f64, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT COALESCE(SUM(COALESCE((proto_jsonb->>'input_cost')::float8, 0)
                            + COALESCE((proto_jsonb->>'output_cost')::float8, 0)), 0)
         FROM test_run_answers WHERE proto_jsonb->>'run_id' = $1",
    )
    .bind(run_id)
    .fetch_one(&ctx.pool)
    .await
}

#[allow(deprecated)]
async fn load_attachment(
    ctx: &Context,
    mut attachment: FileAttachment,
) -> Result<(FileAttachment, Bytes), HandlerError> {
    if attachment.upload_state != i32::from(UploadState::Ready) || attachment.uri.is_empty() {
        let inline = Bytes::from(std::mem::take(&mut attachment.file));
        return Ok((attachment, inline));
    }
    let blobs = ctx.blobs.as_ref().ok_or_else(|| {
        HandlerError::Invalid(format!(
            "attachment {} is in blob storage, which this worker has not configured",
            attachment.id
        ))
    })?;
    let bytes = ctx
        .blob_cache
        .get_or_load(blobs, &attachment.uri)
        .await
        .map_err(|error| match error {
            mpac_blob::BlobError::TooLarge { .. } => HandlerError::Invalid(format!(
                "Attachment exceeds the 64 MiB inference limit: {error}"
            )),
            other => HandlerError::Blob(other),
        })?;
    Ok((attachment, bytes))
}

fn exceeds_inference_limit(attachment: &FileAttachment) -> bool {
    u64::try_from(attachment.file_size).unwrap_or(0) * 1024 > mpac_blob::cache::MAX_LOAD_BYTES
        && attachment.upload_state == i32::from(UploadState::Ready)
        && !attachment.uri.is_empty()
}

pub async fn cancel_for_spend(ctx: &Context, run_id: &str) -> Result<(), HandlerError> {
    let mut tx = ctx.pool.begin().await?;
    let Some(mut run) = crud::get_for_update::<TestRun>(&mut tx, Table::TestRuns, run_id).await?
    else {
        return Ok(());
    };
    if run.status != i32::from(ResponseCode::InProgress) {
        return Ok(());
    }
    tracing::warn!(run = %run.id, owner = %run.owner, "spend limit reached; cancelling run");
    run.status = ResponseCode::Cancelled.into();
    run.completed_at_utc = Some(now());
    crud::update_in(&mut tx, Table::TestRuns, run_id, &run).await?;
    mpac_queue::cancel_run(&mut tx, run_id).await?;
    tx.commit().await?;
    Ok(())
}

async fn enqueue_completed_responder(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    run: &TestRun,
    answer: &TestRunAnswer,
) -> Result<(), HandlerError> {
    let completed: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM test_run_answers
         WHERE proto_jsonb->>'run_id' = $1
           AND COALESCE(NULLIF(proto_jsonb->>'responder_id', ''), proto_jsonb->>'model_id') = $2",
    )
    .bind(&run.id)
    .bind(&answer.responder_id)
    .fetch_one(&mut **tx)
    .await?;
    // The requested sample can exceed the available items (or use the default).
    // Each model receives the same actual sample when the run is created.
    let total: i32 = sqlx::query_scalar("SELECT total FROM run_counters WHERE run_id = $1")
        .bind(&run.id)
        .fetch_one(&mut **tx)
        .await?;
    let instances = run
        .models
        .iter()
        .filter(|m| responder_key(m) == answer.responder_id)
        .count();
    let expected = i64::from(total) * i64::try_from(instances).unwrap_or(0)
        / i64::try_from(run.models.len()).unwrap_or(1).max(1);
    if expected > 0 && completed == expected {
        mpac_queue::enqueue(
            tx,
            &[NewJob::new(Kind::FinalizeRun(FinalizeRun {
                run_id: run.id.clone(),
                responder_id: answer.responder_id.clone(),
            }))
            .run(&run.id)],
        )
        .await?;
    }
    Ok(())
}

async fn answer_from_backend(
    ctx: &Context,
    run: &TestRun,
    item: &TestItem,
    work: &AnswerItem,
    model: &Model,
    mut answer: TestRunAnswer,
) -> Result<TestRunAnswer, HandlerError> {
    let test = crud::get_unchecked::<Test>(&ctx.pool, Table::Tests, &run.test_id)
        .await?
        .unwrap_or_default();
    let backend_id = if model.backend_id.is_empty() {
        run.backend_id.clone()
    } else {
        model.backend_id.clone()
    };
    let record = mpac_secrets::backend_record(&ctx.pool, &backend_id).await?;
    let mut backend = record
        .as_ref()
        .and_then(|record| Backend::decode(record.proto_bytes.as_slice()).ok())
        .unwrap_or_default();
    if let Some(stored) = record.as_ref().and_then(|record| record.secret.as_ref()) {
        let Some(secrets) = &ctx.secrets else {
            return Err(HandlerError::Invalid(format!(
                "backend {backend_id} has a provider key, but this worker has no master key"
            )));
        };
        backend.api_key = secrets
            .key_for(&ctx.pool, &backend_id, stored)
            .await?
            .to_string();
    }
    let attachment = if item.attachment_id.is_empty() {
        None
    } else {
        crud::get_unchecked::<FileAttachment>(&ctx.pool, Table::Attachments, &item.attachment_id)
            .await?
    };
    if let Some(oversized) = attachment.as_ref().filter(|a| exceeds_inference_limit(a)) {
        answer.status = Status::BadRequest.into();
        answer.has_attachment = true;
        answer.attachment_type = oversized.modality;
        answer.error = format!(
            "Attachment {} exceeds the 64 MiB inference limit",
            oversized.name
        );
    } else {
        let _memory = match &attachment {
            Some(attachment) => Some(
                ctx.reserve_attachment_memory(attachment.file_size)
                    .await
                    .map_err(|error| HandlerError::Invalid(error.to_string()))?,
            ),
            None => None,
        };
        let attachment = match attachment {
            Some(attachment) => Some(load_attachment(ctx, attachment).await?),
            None => None,
        };
        answer = ctx
            .engine
            .answer(&AnswerInput {
                answer_id: answer.id.clone(),
                run_id: &run.id,
                item,
                model,
                backend: &backend,
                attachment: attachment.as_ref().map(|(meta, bytes)| AttachmentData {
                    meta,
                    bytes: bytes.as_ref(),
                }),
                include_reasoning: work.include_reasoning,
                instructions: &test.instructions,
            })
            .await;
        if !work.responder_id.is_empty() {
            answer.responder_id.clone_from(&work.responder_id);
        }
    }
    Ok(answer)
}

pub async fn handle(ctx: &Context, job: &ClaimedJob, work: &AnswerItem) -> HandlerResult {
    let Some(run) =
        crud::get_unchecked::<TestRun>(&ctx.pool, Table::TestRuns, &work.run_id).await?
    else {
        return Ok(Outcome::Done);
    };
    if run.status != i32::from(ResponseCode::InProgress) {
        return Ok(Outcome::Done);
    }
    let Some(item) =
        crud::get_unchecked::<TestItem>(&ctx.pool, Table::TestItems, &work.item_id).await?
    else {
        return Ok(Outcome::Done);
    };
    let model = work.model.clone().unwrap_or_default();
    let mut answer = TestRunAnswer {
        id: job.id.simple().to_string(),
        run_id: run.id.clone(),
        item_id: item.id.clone(),
        model_id: model.id.clone(),
        responder_id: work.responder_id.clone(),
        ..TestRunAnswer::default()
    };

    if breaker_open(ctx, &run.id, &work.responder_id).await? {
        answer.status = Status::Skipped.into();
        answer.error = SKIPPED_ERROR.into();
    } else {
        if ctx.limits.tpm_exhausted(&ctx.pool, &run.owner).await? {
            return Ok(Outcome::Reschedule(until_next_minute()));
        }
        answer = answer_from_backend(ctx, &run, &item, work, &model, answer).await?;
    }

    let Some(mut tx) = crate::fenced(ctx, job).await? else {
        return Ok(Outcome::LeaseLost);
    };
    crud::upsert_in(&mut tx, Table::TestRunAnswers, &answer.id, &answer).await?;
    let tokens = answer.input_tokens + answer.output_tokens;
    if tokens > 0 {
        ctx.limits
            .record_tpm_in(&ctx.pool, &mut tx, &run.owner, tokens)
            .await?;
    }
    enqueue_completed_responder(&mut tx, &run, &answer).await?;
    tx.commit().await?;

    let in_flight = run_cost(ctx, &run.id).await?;
    if ctx
        .limits
        .spend_exceeded(&ctx.pool, &run.owner, in_flight)
        .await?
    {
        cancel_for_spend(ctx, &run.id).await?;
    }
    Ok(Outcome::Committed)
}
