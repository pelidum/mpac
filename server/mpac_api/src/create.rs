use mpac_db::crud::{self, Table};
use mpac_db::rate_limits::{LimitError, projected_benchmark_cost, projected_run_cost};
use mpac_proto::json::to_jsonb;
use mpac_proto::mpac::jobs::BenchmarkStep;
use mpac_proto::mpac::jobs::job::Kind;
use mpac_proto::{
    Backend, Benchmark, BenchmarkRequest, BenchmarkResponse, ResponseCode, TestRunReply,
    TestRunRequest, TestType,
};
use mpac_queue::NewJob;
use mpac_worker::benchmark::total_rounds;
use mpac_worker::runs::{NewRun, create_run, now};
use prost::Message;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;
use crate::test_defs::{new_id, visible_test};

const DEFAULT_BENCHMARK_SAMPLE: i64 = 25;

fn limit_status(error: LimitError) -> Status {
    match error {
        LimitError::Exhausted(message) => Status::resource_exhausted(message),
        LimitError::Lookup(message) => internal(message),
    }
}

async fn check_budget(
    api: &Api,
    caller: &Caller,
    projected: Option<f64>,
    label: &str,
) -> Result<(), Status> {
    api.rate_limits
        .check_spend_limits(&api.pool, &caller.id)
        .await
        .map_err(limit_status)?;
    api.rate_limits
        .check_projected_cost(&api.pool, &caller.id, projected, label)
        .await
        .map_err(limit_status)
}

pub async fn create_test_run(
    api: &Api,
    request: Request<TestRunRequest>,
) -> Result<Vec<TestRunReply>, Status> {
    let caller = caller(&request)?;
    let request = request.into_inner();
    let backend = if request.backend_id.is_empty() {
        Backend::default()
    } else {
        crud::get(
            &api.pool,
            Table::Backends,
            &request.backend_id,
            &caller.requester(),
        )
        .await
        .map_err(internal)?
        .unwrap_or_default()
    };
    check_budget(api, &caller, projected_run_cost(&request, &backend), "run").await?;
    let test = visible_test(api, &caller, &request.test_id).await?;
    let run = create_run(
        &api.pool,
        NewRun {
            test_id: request.test_id,
            owner: caller.id,
            models: request.models,
            include_reasoning: request.include_reasoning,
            sample_size: request.sample_size,
            backend_id: request.backend_id,
            benchmark_id: None,
        },
    )
    .await
    .map_err(internal)?;
    Ok(vec![TestRunReply {
        run_id: run.id,
        test_id: test.id,
        code: ResponseCode::InProgress.into(),
        ..TestRunReply::default()
    }])
}

pub async fn create_benchmark(
    api: &Api,
    request: Request<BenchmarkRequest>,
) -> Result<Vec<BenchmarkResponse>, Status> {
    let caller = caller(&request)?;
    let request = request.into_inner();
    let sample_size = if request.sample_size > 0 {
        request.sample_size
    } else {
        DEFAULT_BENCHMARK_SAMPLE
    };
    let projected =
        projected_benchmark_cost(&request.models, sample_size, request.include_reasoning);
    check_budget(api, &caller, projected, "benchmark").await?;
    let test = visible_test(api, &caller, &request.test_id).await?;
    if test.r#type != i32::from(TestType::Evaluation) {
        return Err(Status::invalid_argument(
            "Benchmarks require tests of type: EVALUATION",
        ));
    }
    let benchmark = Benchmark {
        id: new_id(),
        test_id: request.test_id,
        owner: caller.id,
        created_at_utc: Some(now()),
        status: ResponseCode::InProgress.into(),
        model_count: i64::try_from(request.models.len()).unwrap_or(i64::MAX),
        models: request.models,
        sample_size,
        include_reasoning: request.include_reasoning,
        ..Benchmark::default()
    };
    let mut tx = api.pool.begin().await.map_err(internal)?;
    sqlx::query("INSERT INTO benchmarks (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)")
        .bind(&benchmark.id)
        .bind(to_jsonb(&benchmark).map_err(internal)?)
        .bind(benchmark.encode_to_vec())
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    mpac_queue::enqueue(
        &mut tx,
        &[NewJob::new(Kind::BenchmarkStep(BenchmarkStep {
            benchmark_id: benchmark.id.clone(),
            round: 0,
        }))
        .benchmark(&benchmark.id)],
    )
    .await
    .map_err(internal)?;
    tx.commit().await.map_err(internal)?;
    Ok(vec![BenchmarkResponse {
        benchmark_id: benchmark.id,
        code: ResponseCode::InProgress.into(),
        total_rounds: total_rounds(benchmark.models.len()),
        ..BenchmarkResponse::default()
    }])
}
