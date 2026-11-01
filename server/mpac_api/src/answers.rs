use mpac_db::crud::{self, Table};
use mpac_proto::{ListRequest, TestRun, TestRunAnswer};
use tonic::{Request, Status};

use crate::Api;
use crate::auth::caller;
use crate::status::internal;

const DEFAULT_LIMIT: i64 = 50;

pub async fn list_test_run_answers(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<TestRunAnswer>, Status> {
    let caller = caller(&request)?;
    let ListRequest {
        parent_id, limit, ..
    } = request.into_inner();
    if !caller.admin {
        let run: Option<TestRun> =
            crud::get(&api.pool, Table::TestRuns, &parent_id, &caller.requester())
                .await
                .map_err(internal)?;
        let Some(run) = run else {
            return Ok(Vec::new());
        };
        if !run.test_id.is_empty() && run.owner != caller.id {
            let parent_visible =
                crud::get_bytes(&api.pool, Table::Tests, &run.test_id, &caller.requester())
                    .await
                    .map_err(internal)?
                    .is_some();
            if !parent_visible {
                return Ok(Vec::new());
            }
        }
    }
    let limit = if limit == 0 { DEFAULT_LIMIT } else { limit };
    let rows: Vec<Vec<u8>> = sqlx::query_scalar(
        "WITH item_ids AS (
            SELECT proto_jsonb ->> 'item_id' AS item_id
            FROM test_run_answers
            WHERE proto_jsonb ->> 'run_id' = $1
            GROUP BY 1
            ORDER BY 1
            LIMIT $2
        )
        SELECT proto_bytes FROM test_run_answers
        WHERE proto_jsonb ->> 'item_id' IN (SELECT item_id FROM item_ids)
          AND proto_jsonb ->> 'run_id' = $1",
    )
    .bind(&parent_id)
    .bind(limit)
    .fetch_all(&api.pool)
    .await
    .map_err(internal)?;
    Ok(crud::decode_all(rows))
}
