use mpac_db::crud::{self, Table, visibility_clause};
use mpac_proto::{Benchmark, DeleteRequest, GetRequest, ListRequest, ResponseCode, StatusReply};
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;
use crate::test_defs::now;

const DEFAULT_LIMIT: i64 = 50;

pub async fn visible_benchmark(api: &Api, caller: &Caller, id: &str) -> Result<Benchmark, Status> {
    let benchmark: Option<Benchmark> =
        crud::get(&api.pool, Table::Benchmarks, id, &caller.requester())
            .await
            .map_err(internal)?;
    let Some(benchmark) = benchmark else {
        return Ok(Benchmark::default());
    };
    if !benchmark.test_id.is_empty()
        && crud::get_bytes(
            &api.pool,
            Table::Tests,
            &benchmark.test_id,
            &caller.requester(),
        )
        .await
        .map_err(internal)?
        .is_none()
    {
        return Err(Status::not_found("Benchmark not found."));
    }
    Ok(benchmark)
}

pub async fn get_benchmark(api: &Api, request: Request<GetRequest>) -> Result<Benchmark, Status> {
    let caller = caller(&request)?;
    visible_benchmark(api, &caller, &request.get_ref().id).await
}

pub async fn update_benchmark(
    api: &Api,
    request: Request<Benchmark>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let requested = request.into_inner();
    let original = visible_benchmark(api, &caller, &requested.id)
        .await
        .map_err(|_| Status::not_found(format!("Benchmark {} not found", requested.id)))?;
    if caller.id != original.owner && !caller.admin {
        return Err(Status::permission_denied(
            "Only the benchmark owner or admin can update this benchmark.",
        ));
    }
    let mut tx = api.pool.begin().await.map_err(internal)?;
    let Some(mut stored) =
        crud::get_for_update::<Benchmark>(&mut tx, Table::Benchmarks, &requested.id)
            .await
            .map_err(internal)?
    else {
        return Err(Status::not_found(format!(
            "Benchmark {} not found",
            requested.id
        )));
    };
    if requested.status == i32::from(ResponseCode::Cancelled)
        && stored.status == i32::from(ResponseCode::InProgress)
    {
        stored.status = ResponseCode::Cancelled.into();
        stored.completed_at_utc = Some(now());
        crud::update_in(&mut tx, Table::Benchmarks, &requested.id, &stored)
            .await
            .map_err(internal)?;
        mpac_worker::runs::cancel_benchmark_runs(&mut tx, &requested.id)
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

pub async fn delete_benchmark(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    crud::delete(
        &api.pool,
        Table::Benchmarks,
        &request.get_ref().id,
        &caller.requester(),
    )
    .await
    .map_err(internal)
}

pub async fn list_benchmarks(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<Benchmark>, Status> {
    let caller = caller(&request)?;
    let limit = if request.get_ref().limit > 0 {
        request.get_ref().limit
    } else {
        DEFAULT_LIMIT
    };
    let base =
        "SELECT b.proto_bytes FROM benchmarks b JOIN tests t ON t.id = b.proto_jsonb->>'test_id'";
    let rows: Vec<Vec<u8>> = if caller.admin {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "{base} ORDER BY b.created_at DESC LIMIT $1"
        )))
        .bind(limit)
        .fetch_all(&api.pool)
        .await
    } else {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "{base} WHERE {} ORDER BY b.created_at DESC LIMIT $2",
            visibility_clause(1, Some("t"))
        )))
        .bind(&caller.id)
        .bind(limit)
        .fetch_all(&api.pool)
        .await
    }
    .map_err(internal)?;
    Ok(crud::decode_all(rows))
}
