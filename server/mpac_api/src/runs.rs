use mpac_db::crud::{self, Table, visibility_clause};
use mpac_proto::test_run_confusion_matrix::ModelCounts;
use mpac_proto::{
    DeleteRequest, GetRequest, ListRequest, ResponseCode, RunProgress, StatusReply, TestRun,
    TestRunConfusionMatrix,
};
use sqlx::Row;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;
use crate::test_defs::now;

pub const RESPONDER_KEY_SQL: &str =
    "COALESCE(NULLIF(proto_jsonb->>'responder_id', ''), proto_jsonb->>'model_id')";
const DEFAULT_LIMIT: i64 = 50;

async fn stored_run(api: &Api, caller: &Caller, id: &str) -> Result<Option<TestRun>, Status> {
    crud::get(&api.pool, Table::TestRuns, id, &caller.requester())
        .await
        .map_err(internal)
}

pub async fn visible_run(api: &Api, caller: &Caller, id: &str) -> Result<TestRun, Status> {
    let Some(run) = stored_run(api, caller, id).await? else {
        return Ok(TestRun::default());
    };
    if !run.test_id.is_empty()
        && crud::get_bytes(&api.pool, Table::Tests, &run.test_id, &caller.requester())
            .await
            .map_err(internal)?
            .is_none()
    {
        return Err(Status::not_found("Test run not found."));
    }
    Ok(run)
}

pub async fn get_test_run(api: &Api, request: Request<GetRequest>) -> Result<TestRun, Status> {
    let caller = caller(&request)?;
    visible_run(api, &caller, &request.get_ref().id).await
}

pub async fn get_run_progress(
    api: &Api,
    request: Request<GetRequest>,
) -> Result<RunProgress, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let mut progress = RunProgress::default();
    if stored_run(api, &caller, &id).await?.is_none() {
        return Ok(progress);
    }
    let rows = sqlx::query(sqlx::AssertSqlSafe(format!(
        "SELECT {RESPONDER_KEY_SQL} AS responder_id, COUNT(*)::int AS cnt
         FROM test_run_answers WHERE proto_jsonb->>'run_id' = $1 GROUP BY 1"
    )))
    .bind(&id)
    .fetch_all(&api.pool)
    .await;
    match rows {
        Ok(rows) => {
            for row in rows {
                if let (Ok(Some(responder)), Ok(count)) = (
                    row.try_get::<Option<String>, _>("responder_id"),
                    row.try_get::<i32, _>("cnt"),
                ) {
                    progress.answer_counts.insert(responder, count);
                }
            }
        }
        Err(error) => tracing::error!(run = %id, %error, "GetRunProgress failed"),
    }
    Ok(progress)
}

pub async fn get_test_run_confusion_matrix(
    api: &Api,
    request: Request<GetRequest>,
) -> Result<TestRunConfusionMatrix, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let mut matrix = TestRunConfusionMatrix::default();
    if stored_run(api, &caller, &id).await?.is_none() {
        return Ok(matrix);
    }
    let responder = RESPONDER_KEY_SQL.replace("proto_jsonb", "a.proto_jsonb");
    let rows = sqlx::query(sqlx::AssertSqlSafe(format!(
        "SELECT {responder} AS responder_id,
            COUNT(*) FILTER (WHERE (i.proto_jsonb->>'is_relevant')::bool
                               AND (a.proto_jsonb->>'is_correct')::bool)::int AS tp,
            COUNT(*) FILTER (WHERE NOT (i.proto_jsonb->>'is_relevant')::bool
                               AND (a.proto_jsonb->>'is_correct')::bool)::int AS tn,
            COUNT(*) FILTER (WHERE NOT (i.proto_jsonb->>'is_relevant')::bool
                               AND NOT (a.proto_jsonb->>'is_correct')::bool)::int AS fp,
            COUNT(*) FILTER (WHERE (i.proto_jsonb->>'is_relevant')::bool
                               AND NOT (a.proto_jsonb->>'is_correct')::bool)::int AS fn
         FROM test_run_answers a
         JOIN test_items i ON i.id = a.proto_jsonb->>'item_id'
         WHERE a.proto_jsonb->>'run_id' = $1
         GROUP BY 1"
    )))
    .bind(&id)
    .fetch_all(&api.pool)
    .await;
    match rows {
        Ok(rows) => {
            for row in rows {
                let Ok(Some(responder)) = row.try_get::<Option<String>, _>("responder_id") else {
                    continue;
                };
                let count = |name: &str| row.try_get::<i32, _>(name).unwrap_or_default();
                matrix.per_model.insert(
                    responder,
                    ModelCounts {
                        true_positives: count("tp"),
                        true_negatives: count("tn"),
                        false_positives: count("fp"),
                        false_negatives: count("fn"),
                    },
                );
            }
        }
        Err(error) => tracing::error!(run = %id, %error, "GetTestRunConfusionMatrix failed"),
    }
    Ok(matrix)
}

pub async fn update_test_run(api: &Api, request: Request<TestRun>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let requested = request.into_inner();
    let original = visible_run(api, &caller, &requested.id)
        .await
        .map_err(|_| Status::not_found(format!("Test run {} not found", requested.id)))?;
    if caller.id != original.owner && !caller.admin {
        return Err(Status::permission_denied(
            "Only the run owner or admin can update this test run.",
        ));
    }
    let mut tx = api.pool.begin().await.map_err(internal)?;
    let Some(mut stored) = crud::get_for_update::<TestRun>(&mut tx, Table::TestRuns, &requested.id)
        .await
        .map_err(internal)?
    else {
        return Err(Status::not_found(format!(
            "Test run {} not found",
            requested.id
        )));
    };
    if requested.status == i32::from(ResponseCode::Cancelled)
        && stored.status == i32::from(ResponseCode::InProgress)
    {
        stored.status = ResponseCode::Cancelled.into();
        stored.completed_at_utc = Some(now());
        crud::update_in(&mut tx, Table::TestRuns, &requested.id, &stored)
            .await
            .map_err(internal)?;
        mpac_queue::cancel_run(&mut tx, &requested.id)
            .await
            .map_err(internal)?;
    }
    tx.commit().await.map_err(internal)?;
    Ok(StatusReply {
        code: ResponseCode::Success.into(),
        reason: String::new(),
        id: requested.id,
    })
}

async fn cancel_queued_jobs(api: &Api, run_id: &str) -> Result<(), Status> {
    let mut tx = api.pool.begin().await.map_err(internal)?;
    mpac_queue::cancel_run(&mut tx, run_id)
        .await
        .map_err(internal)?;
    tx.commit().await.map_err(internal)
}

pub async fn delete_test_run(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let original = visible_run(api, &caller, &id)
        .await
        .map_err(|_| Status::not_found(format!("TestRun {id} not found")))?;
    if caller.id != original.owner && !caller.admin {
        return Err(Status::permission_denied(
            "Only the run owner or admin can delete this test run.",
        ));
    }
    cancel_queued_jobs(api, &id).await?;
    let mut tx = api.pool.begin().await.map_err(internal)?;
    for sql in [
        "DELETE FROM test_run_answers WHERE proto_jsonb ->> 'run_id' = $1",
        "DELETE FROM run_counters WHERE run_id = $1",
        "DELETE FROM test_runs WHERE id = $1",
    ] {
        sqlx::query(sql)
            .bind(&id)
            .execute(&mut *tx)
            .await
            .map_err(internal)?;
    }
    tx.commit().await.map_err(internal)?;
    Ok(StatusReply {
        code: ResponseCode::Success.into(),
        id,
        ..StatusReply::default()
    })
}

fn epoch_seconds(timestamp: &prost_types::Timestamp) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let seconds = timestamp.seconds as f64;
    seconds + f64::from(timestamp.nanos) / 1e9
}

pub async fn list_test_runs(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<TestRun>, Status> {
    let caller = caller(&request)?;
    let request = request.into_inner();
    let limit = if request.limit > 0 {
        request.limit
    } else {
        DEFAULT_LIMIT
    };
    let mut clauses = Vec::new();
    let mut param = 0;
    let mut next = || {
        param += 1;
        param
    };
    if !request.parent_id.is_empty() {
        clauses.push(format!("tr.proto_jsonb->>'test_id' = ${}", next()));
    }
    if request.created_after.is_some() {
        clauses.push(format!("tr.created_at >= to_timestamp(${})", next()));
    }
    if request.created_before.is_some() {
        clauses.push(format!("tr.created_at < to_timestamp(${})", next()));
    }
    if !caller.admin {
        let visibility = visibility_clause(next(), Some("t"));
        let owner = next();
        clauses.push(format!(
            "(({visibility}) OR (t.id IS NULL AND tr.proto_jsonb->>'owner' = ${owner}))"
        ));
    }
    let limit_param = next();
    let mut sql = "SELECT tr.proto_bytes FROM test_runs tr \
                   LEFT JOIN tests t ON t.id = tr.proto_jsonb->>'test_id'"
        .to_owned();
    if !clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&clauses.join(" AND "));
    }
    sql.push_str(" ORDER BY tr.created_at DESC LIMIT $");
    sql.push_str(&limit_param.to_string());

    let mut query = sqlx::query_scalar::<_, Vec<u8>>(sqlx::AssertSqlSafe(sql));
    if !request.parent_id.is_empty() {
        query = query.bind(request.parent_id.clone());
    }
    if let Some(after) = &request.created_after {
        query = query.bind(epoch_seconds(after));
    }
    if let Some(before) = &request.created_before {
        query = query.bind(epoch_seconds(before));
    }
    if !caller.admin {
        query = query.bind(caller.id.clone()).bind(caller.id.clone());
    }
    let rows = query
        .bind(limit)
        .fetch_all(&api.pool)
        .await
        .map_err(internal)?;
    Ok(crud::decode_all(rows))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_seconds_includes_nanos() {
        let timestamp = prost_types::Timestamp {
            seconds: 10,
            nanos: 500_000_000,
        };
        assert!((epoch_seconds(&timestamp) - 10.5).abs() < 1e-9);
    }
}
