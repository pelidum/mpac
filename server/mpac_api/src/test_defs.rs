use std::time::SystemTime;

use mpac_db::crud::{self, Table, visibility_clause};
use mpac_proto::{DeleteRequest, GetRequest, ListRequest, ResponseCode, StatusReply, Test};
use prost::Message;
use sqlx::Row;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;

const DEFAULT_LIMIT: i64 = 50;

pub fn new_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

pub fn now() -> prost_types::Timestamp {
    prost_types::Timestamp::from(SystemTime::now())
}

fn decode_with_count(row: &sqlx::postgres::PgRow) -> Option<Test> {
    let bytes: Vec<u8> = row.try_get("proto_bytes").ok()?;
    let mut test = Test::decode(bytes.as_slice()).ok()?;
    test.item_count = row.try_get::<i32, _>("item_count").ok()?.into();
    Some(test)
}

pub async fn visible_test(api: &Api, caller: &Caller, id: &str) -> Result<Test, Status> {
    let row = if caller.admin {
        sqlx::query("SELECT proto_bytes, item_count FROM tests WHERE id = $1")
            .bind(id)
            .fetch_optional(&api.pool)
            .await
    } else {
        sqlx::query(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes, item_count FROM tests WHERE id = $1 AND {}",
            visibility_clause(2, None)
        )))
        .bind(id)
        .bind(&caller.id)
        .fetch_optional(&api.pool)
        .await
    }
    .map_err(internal)?;
    row.as_ref()
        .and_then(decode_with_count)
        .ok_or_else(|| internal(format!("no test found for {id}")))
}

pub async fn create_test(api: &Api, request: Request<Test>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let mut test = request.into_inner();
    test.id = new_id();
    test.owner = caller.id;
    test.created_at_utc = Some(now());
    test.modified_at_utc = Some(now());
    crud::create(&api.pool, Table::Tests, &test.id.clone(), &test, false)
        .await
        .map_err(internal)
}

pub async fn get_test(api: &Api, request: Request<GetRequest>) -> Result<Test, Status> {
    let caller = caller(&request)?;
    visible_test(api, &caller, &request.get_ref().id).await
}

pub async fn update_test(api: &Api, request: Request<Test>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let mut test = request.into_inner();
    let original = visible_test(api, &caller, &test.id)
        .await
        .map_err(|_| Status::not_found(format!("Test {} not found", test.id)))?;
    if caller.id != original.owner && !caller.admin {
        return Err(Status::permission_denied(
            "Only the test owner or admin can update this test.",
        ));
    }
    test.modified_at_utc = Some(now());
    test.created_at_utc = Some(original.created_at_utc.unwrap_or_default());
    test.owner = original.owner;
    crud::update(&api.pool, Table::Tests, &test.id.clone(), &test)
        .await
        .map_err(internal)
}

pub async fn delete_test(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let original = visible_test(api, &caller, &id)
        .await
        .map_err(|_| Status::not_found(format!("Test {id} not found")))?;
    if caller.id != original.owner && !caller.admin {
        return Err(Status::permission_denied(
            "Only the test owner or admin can delete this test.",
        ));
    }
    let mut tx = mpac_db::attachments::lock(&api.pool)
        .await
        .map_err(internal)?;
    let exclusive_attachments: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT proto_jsonb ->> 'attachment_id' AS att_id
         FROM test_items
         WHERE proto_jsonb ->> 'test_id' = $1
           AND proto_jsonb ->> 'attachment_id' != ''
           AND NOT EXISTS (
             SELECT 1 FROM test_items t2
             WHERE t2.proto_jsonb ->> 'attachment_id' = test_items.proto_jsonb ->> 'attachment_id'
               AND t2.proto_jsonb ->> 'test_id' != $1
           )",
    )
    .bind(&id)
    .fetch_all(&mut *tx)
    .await
    .map_err(internal)?;
    sqlx::query("DELETE FROM test_items WHERE proto_jsonb ->> 'test_id' = $1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    if !exclusive_attachments.is_empty() {
        sqlx::query("INSERT INTO blob_cleanup (key) SELECT DISTINCT proto_jsonb->>'uri' FROM attachments WHERE id = ANY($1) AND COALESCE(proto_jsonb->>'uri', '') <> '' ON CONFLICT DO NOTHING")
            .bind(&exclusive_attachments).execute(&mut *tx).await.map_err(internal)?;
        sqlx::query("DELETE FROM attachments WHERE id = ANY($1)")
            .bind(&exclusive_attachments)
            .execute(&mut *tx)
            .await
            .map_err(internal)?;
    }
    sqlx::query("DELETE FROM tests WHERE id = $1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    tx.commit().await.map_err(internal)?;
    crate::attachments::cleanup(api).await;
    Ok(StatusReply {
        code: ResponseCode::Success.into(),
        id,
        ..StatusReply::default()
    })
}

pub async fn list_tests(api: &Api, request: Request<ListRequest>) -> Result<Vec<Test>, Status> {
    let caller = caller(&request)?;
    let limit = if request.get_ref().limit > 0 {
        request.get_ref().limit
    } else {
        DEFAULT_LIMIT
    };
    let rows = if caller.admin {
        sqlx::query("SELECT proto_bytes, item_count FROM tests ORDER BY created_at DESC LIMIT $1")
            .bind(limit)
            .fetch_all(&api.pool)
            .await
    } else {
        sqlx::query(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes, item_count FROM tests WHERE {} ORDER BY created_at DESC LIMIT $2",
            visibility_clause(1, None)
        )))
        .bind(&caller.id)
        .bind(limit)
        .fetch_all(&api.pool)
        .await
    }
    .map_err(internal)?;
    Ok(rows.iter().filter_map(decode_with_count).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_id_is_uuid4_hex_like_python() {
        let id = new_id();

        assert_eq!(id.len(), 32);
        assert!(
            id.chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );
        assert_ne!(id, new_id());
    }
}
