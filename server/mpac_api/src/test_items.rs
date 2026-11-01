use std::collections::BTreeMap;

use mpac_db::crud::{self, Table};
use mpac_proto::json::to_jsonb;
use mpac_proto::{
    BatchCreateTestItemsReply, BatchCreateTestItemsRequest, DeleteRequest, GetRequest, ListRequest,
    ResponseCode, StatusReply, TestItem,
};
use prost::Message;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;
use crate::test_defs::{new_id, visible_test};

const DEFAULT_LIMIT: i64 = 50;

fn reply(code: ResponseCode, id: String, reason: String) -> StatusReply {
    StatusReply {
        code: code.into(),
        reason,
        id,
    }
}

async fn may_edit(api: &Api, caller: &Caller, test_id: &str) -> bool {
    match visible_test(api, caller, test_id).await {
        Ok(test) => caller.admin || test.owner == caller.id,
        Err(_) => false,
    }
}

async fn adjust_item_count(api: &Api, test_id: &str, delta: i32) -> Result<(), Status> {
    sqlx::query("UPDATE tests SET item_count = GREATEST(0, item_count + $1) WHERE id = $2")
        .bind(delta)
        .bind(test_id)
        .execute(&api.pool)
        .await
        .map(|_| ())
        .map_err(internal)
}

pub async fn create_test_item(
    api: &Api,
    request: Request<TestItem>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let mut item = request.into_inner();
    let test = visible_test(api, &caller, &item.test_id).await?;
    if test.owner != caller.id && !caller.admin {
        return Ok(reply(
            ResponseCode::Error,
            item.test_id,
            "User does not have permission to add items to this test.".into(),
        ));
    }
    item.id = new_id();
    let created = crud::create(&api.pool, Table::TestItems, &item.id.clone(), &item, false)
        .await
        .map_err(internal)?;
    if created.code == i32::from(ResponseCode::Success) {
        adjust_item_count(api, &item.test_id, 1).await?;
    }
    Ok(created)
}

pub async fn batch_create_test_items(
    api: &Api,
    request: Request<BatchCreateTestItemsRequest>,
) -> Result<BatchCreateTestItemsReply, Status> {
    let caller = caller(&request)?;
    let items = request.into_inner().items;
    let mut permitted_tests = BTreeMap::new();
    for item in &items {
        if !permitted_tests.contains_key(&item.test_id) {
            let allowed = may_edit(api, &caller, &item.test_id).await;
            permitted_tests.insert(item.test_id.clone(), allowed);
        }
    }

    let mut results = Vec::with_capacity(items.len());
    let mut permitted = Vec::new();
    for mut item in items {
        if permitted_tests[&item.test_id] {
            item.id = new_id();
            permitted.push(item);
        } else {
            let reason = format!(
                "User does not have permission to add items to test {}.",
                item.test_id
            );
            results.push(reply(ResponseCode::Error, item.test_id, reason));
        }
    }

    let inserted: Result<(), sqlx::Error> = async {
        let mut tx = api.pool.begin().await?;
        let mut counts: BTreeMap<&str, i32> = BTreeMap::new();
        for item in &permitted {
            let json = to_jsonb(item).map_err(|error| sqlx::Error::Encode(Box::new(error)))?;
            sqlx::query(
                "INSERT INTO test_items (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)",
            )
            .bind(&item.id)
            .bind(json)
            .bind(item.encode_to_vec())
            .execute(&mut *tx)
            .await?;
            *counts.entry(item.test_id.as_str()).or_default() += 1;
        }
        for (test_id, delta) in counts {
            sqlx::query("UPDATE tests SET item_count = item_count + $1 WHERE id = $2")
                .bind(delta)
                .bind(test_id)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await
    }
    .await;

    let succeeded = inserted.is_ok();
    if let Err(error) = &inserted {
        tracing::error!(%error, "bulk insert failed");
    }
    for item in permitted {
        results.push(if succeeded {
            reply(ResponseCode::Success, item.id, String::new())
        } else {
            reply(
                ResponseCode::Error,
                item.id,
                "An internal error occurred".into(),
            )
        });
    }
    let successful = results
        .iter()
        .filter(|r| r.code == i32::from(ResponseCode::Success))
        .count();
    let failed = results.len() - successful;
    Ok(BatchCreateTestItemsReply {
        results,
        successful_count: i32::try_from(successful).unwrap_or(i32::MAX),
        failed_count: i32::try_from(failed).unwrap_or(i32::MAX),
    })
}

pub async fn get_test_item(api: &Api, request: Request<GetRequest>) -> Result<TestItem, Status> {
    let caller = caller(&request)?;
    let item: Option<TestItem> = crud::get(
        &api.pool,
        Table::TestItems,
        &request.get_ref().id,
        &caller.requester(),
    )
    .await
    .map_err(internal)?;
    let Some(item) = item else {
        return Ok(TestItem::default());
    };
    if !caller.admin
        && crud::get_bytes(&api.pool, Table::Tests, &item.test_id, &caller.requester())
            .await
            .map_err(internal)?
            .is_none()
    {
        return Ok(TestItem::default());
    }
    Ok(item)
}

pub async fn update_test_item(
    api: &Api,
    request: Request<TestItem>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let item = request.into_inner();
    let stored: Option<TestItem> =
        crud::get(&api.pool, Table::TestItems, &item.id, &caller.requester())
            .await
            .map_err(internal)?;
    let moved_from = stored
        .map(|s| s.test_id)
        .filter(|test_id| *test_id != item.test_id);
    let parents: Vec<&str> = moved_from
        .as_deref()
        .into_iter()
        .chain(std::iter::once(item.test_id.as_str()))
        .collect();
    for parent in parents {
        let test = visible_test(api, &caller, parent)
            .await
            .map_err(|_| Status::not_found(format!("Test {parent} not found")))?;
        if test.owner != caller.id && !caller.admin {
            return Err(Status::permission_denied(
                "Only the test owner or admin can update items in this test.",
            ));
        }
    }
    let reply = crud::update(&api.pool, Table::TestItems, &item.id.clone(), &item)
        .await
        .map_err(internal)?;
    if let Some(previous) = moved_from
        && reply.code == i32::from(ResponseCode::Success)
    {
        adjust_item_count(api, &previous, -1).await?;
        adjust_item_count(api, &item.test_id, 1).await?;
    }
    Ok(reply)
}

pub async fn delete_test_item(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let item: TestItem = crud::get(&api.pool, Table::TestItems, &id, &caller.requester())
        .await
        .map_err(|_| Status::not_found(format!("TestItem {id} not found")))?
        .unwrap_or_default();
    let test = visible_test(api, &caller, &item.test_id)
        .await
        .map_err(|_| Status::not_found(format!("Test {} not found", item.test_id)))?;
    if test.owner != caller.id && !caller.admin {
        return Err(Status::permission_denied(
            "Only the test owner or admin can delete items from this test.",
        ));
    }
    let mut tx = api.pool.begin().await.map_err(internal)?;
    let deleted = sqlx::query("DELETE FROM test_items WHERE id = $1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    if deleted.rows_affected() == 0 {
        return Ok(reply(ResponseCode::Error, id, String::new()));
    }
    sqlx::query("UPDATE tests SET item_count = GREATEST(0, item_count - 1) WHERE id = $1")
        .bind(&item.test_id)
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    tx.commit().await.map_err(internal)?;
    Ok(reply(ResponseCode::Success, id, String::new()))
}

pub async fn list_test_items(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<TestItem>, Status> {
    let caller = caller(&request)?;
    let ListRequest {
        parent_id, limit, ..
    } = request.into_inner();
    if !caller.admin
        && crud::get_bytes(&api.pool, Table::Tests, &parent_id, &caller.requester())
            .await
            .map_err(internal)?
            .is_none()
    {
        return Ok(Vec::new());
    }
    let limit = if limit == 0 { DEFAULT_LIMIT } else { limit };
    let rows: Vec<Vec<u8>> = sqlx::query_scalar(
        "SELECT proto_bytes FROM test_items WHERE proto_jsonb ->> 'test_id' = $1 ORDER BY created_at LIMIT $2",
    )
    .bind(&parent_id)
    .bind(limit)
    .fetch_all(&api.pool)
    .await
    .map_err(internal)?;
    Ok(crud::decode_all(rows))
}
