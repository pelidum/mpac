use mpac_proto::json::{JsonError, to_jsonb};
use mpac_proto::{ResponseCode, StatusReply};
use prost::{Message, Name};
use sqlx::postgres::PgArguments;
use sqlx::query::QueryScalar;
use sqlx::{PgConnection, PgPool};

const DEFAULT_LIST_LIMIT: i64 = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Table {
    Attachments,
    Backends,
    Benchmarks,
    TestItems,
    TestRunAnswers,
    TestRuns,
    Tests,
    Users,
}

impl Table {
    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Table::Attachments => "attachments",
            Table::Backends => "backends",
            Table::Benchmarks => "benchmarks",
            Table::TestItems => "test_items",
            Table::TestRunAnswers => "test_run_answers",
            Table::TestRuns => "test_runs",
            Table::Tests => "tests",
            Table::Users => "users",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Requester {
    pub id: String,
    pub admin: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum CrudError {
    #[error("database query failed: {0}")]
    Query(#[from] sqlx::Error),
    #[error(transparent)]
    Json(#[from] JsonError),
}

#[must_use]
pub fn visibility_clause(param: usize, alias: Option<&str>) -> String {
    let col = alias.map_or_else(|| "proto_jsonb".to_owned(), |a| format!("{a}.proto_jsonb"));
    format!(
        "(
        {col}->'visibility' IS NULL
        OR {col}->'visibility'->>'type' IS NULL
        OR {col}->'visibility'->>'type' = 'VISIBILITY_UNSET'
        OR {col}->'visibility'->>'type' = 'VISIBILITY_PUBLIC'
        OR {col}->'visibility'->>'type' = 'VISIBILITY_ORG_ONLY'
        OR (
            {col}->'visibility'->>'type' = 'VISIBILITY_SPECIFIED_USERS'
            AND ({col}->>'owner' = ${param} OR {col}->'visibility'->'allowed_users' ? ${param})
        )
        OR (
            {col}->'visibility'->>'type' = 'VISIBILITY_OWNER_ONLY'
            AND {col}->>'owner' = ${param}
        )
    )"
    )
}

#[must_use]
pub fn effective_limit(limit: i64) -> i64 {
    if limit == 0 {
        DEFAULT_LIST_LIMIT
    } else {
        limit
    }
}

fn reply(code: ResponseCode, id: &str) -> StatusReply {
    StatusReply {
        code: code.into(),
        id: id.to_owned(),
        ..StatusReply::default()
    }
}

pub async fn is_admin(pool: &PgPool, user_id: &str) -> Result<bool, CrudError> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM users \
         WHERE proto_jsonb ->> 'id' = $1 AND proto_jsonb @> '{\"role\": \"ADMIN\"}')",
    )
    .bind(user_id)
    .fetch_one(pool)
    .await?)
}

pub async fn get_bytes(
    pool: &PgPool,
    table: Table,
    id: &str,
    requester: &Requester,
) -> Result<Option<Vec<u8>>, CrudError> {
    let query: QueryScalar<'_, _, Vec<u8>, PgArguments> = if requester.admin {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes FROM {} WHERE id = $1 LIMIT 1",
            table.name()
        )))
        .bind(id.to_owned())
    } else {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes FROM {} WHERE id = $1 AND {} LIMIT 1",
            table.name(),
            visibility_clause(2, None)
        )))
        .bind(id.to_owned())
        .bind(requester.id.clone())
    };
    Ok(query.fetch_optional(pool).await?)
}

pub async fn get<M: Message + Default>(
    pool: &PgPool,
    table: Table,
    id: &str,
    requester: &Requester,
) -> Result<Option<M>, CrudError> {
    Ok(get_bytes(pool, table, id, requester)
        .await?
        .and_then(|bytes| M::decode(bytes.as_slice()).ok()))
}

pub async fn get_unchecked<M: Message + Default>(
    pool: &PgPool,
    table: Table,
    id: &str,
) -> Result<Option<M>, CrudError> {
    let bytes: Option<Vec<u8>> = sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
        "SELECT proto_bytes FROM {} WHERE id = $1 LIMIT 1",
        table.name()
    )))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(bytes.and_then(|bytes| M::decode(bytes.as_slice()).ok()))
}

pub async fn create<M: Message + Name>(
    pool: &PgPool,
    table: Table,
    id: &str,
    message: &M,
    overwrite: bool,
) -> Result<StatusReply, CrudError> {
    let json = to_jsonb(message)?;
    let result = sqlx::query(sqlx::AssertSqlSafe(format!(
        "INSERT INTO {} (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)",
        table.name()
    )))
    .bind(id)
    .bind(json)
    .bind(message.encode_to_vec())
    .execute(pool)
    .await;
    match result {
        Ok(_) => Ok(reply(ResponseCode::Success, id)),
        Err(sqlx::Error::Database(error)) if error.is_unique_violation() => {
            if overwrite {
                update(pool, table, id, message).await
            } else {
                Ok(StatusReply {
                    reason: "Object already exists".into(),
                    ..reply(ResponseCode::Error, id)
                })
            }
        }
        Err(error) => {
            tracing::error!(table = table.name(), %error, "unable to create object");
            Ok(reply(ResponseCode::Error, id))
        }
    }
}

pub async fn update<M: Message + Name>(
    pool: &PgPool,
    table: Table,
    id: &str,
    message: &M,
) -> Result<StatusReply, CrudError> {
    let json = to_jsonb(message)?;
    let result = sqlx::query(sqlx::AssertSqlSafe(format!(
        "UPDATE {} SET proto_jsonb = $1::jsonb, proto_bytes = $2 WHERE id = $3",
        table.name()
    )))
    .bind(json)
    .bind(message.encode_to_vec())
    .bind(id)
    .execute(pool)
    .await;
    Ok(match result {
        Ok(_) => reply(ResponseCode::Success, id),
        Err(error) => {
            tracing::error!(table = table.name(), %error, "unable to update object");
            reply(ResponseCode::Error, id)
        }
    })
}

pub async fn upsert_in<M: Message + Name>(
    conn: &mut PgConnection,
    table: Table,
    id: &str,
    message: &M,
) -> Result<(), CrudError> {
    sqlx::query(sqlx::AssertSqlSafe(format!(
        "INSERT INTO {} (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)
         ON CONFLICT (id) DO UPDATE SET proto_jsonb = EXCLUDED.proto_jsonb,
                                        proto_bytes = EXCLUDED.proto_bytes",
        table.name()
    )))
    .bind(id)
    .bind(to_jsonb(message)?)
    .bind(message.encode_to_vec())
    .execute(&mut *conn)
    .await?;
    Ok(())
}

pub async fn update_in<M: Message + Name>(
    conn: &mut PgConnection,
    table: Table,
    id: &str,
    message: &M,
) -> Result<bool, CrudError> {
    let updated = sqlx::query(sqlx::AssertSqlSafe(format!(
        "UPDATE {} SET proto_jsonb = $1::jsonb, proto_bytes = $2 WHERE id = $3",
        table.name()
    )))
    .bind(to_jsonb(message)?)
    .bind(message.encode_to_vec())
    .bind(id)
    .execute(&mut *conn)
    .await?;
    Ok(updated.rows_affected() == 1)
}

pub async fn get_for_update<M: Message + Default>(
    conn: &mut PgConnection,
    table: Table,
    id: &str,
) -> Result<Option<M>, CrudError> {
    let bytes: Option<Vec<u8>> = sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
        "SELECT proto_bytes FROM {} WHERE id = $1 FOR UPDATE",
        table.name()
    )))
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(bytes.and_then(|bytes| M::decode(bytes.as_slice()).ok()))
}

pub async fn delete(
    pool: &PgPool,
    table: Table,
    id: &str,
    requester: &Requester,
) -> Result<StatusReply, CrudError> {
    let mut conn = pool.acquire().await?;
    delete_in(&mut conn, table, id, requester).await
}

pub async fn delete_in(
    conn: &mut PgConnection,
    table: Table,
    id: &str,
    requester: &Requester,
) -> Result<StatusReply, CrudError> {
    let row: Option<(Option<String>, Option<String>)> =
        sqlx::query_as(sqlx::AssertSqlSafe(format!(
            "SELECT proto_jsonb ->> 'id', proto_jsonb ->> 'owner' FROM {} WHERE id = $1",
            table.name()
        )))
        .bind(id)
        .fetch_optional(&mut *conn)
        .await?;
    let Some((json_id, owner)) = row else {
        return Ok(reply(ResponseCode::Error, id));
    };
    let allowed = requester.admin
        || owner.as_deref() == Some(requester.id.as_str())
        || json_id.as_deref() == Some(requester.id.as_str());
    if !allowed {
        return Ok(reply(ResponseCode::Error, id));
    }
    sqlx::query(sqlx::AssertSqlSafe(format!(
        "DELETE FROM {} WHERE id = $1",
        table.name()
    )))
    .bind(id)
    .execute(&mut *conn)
    .await?;
    Ok(reply(ResponseCode::Success, id))
}

pub async fn list_bytes(
    pool: &PgPool,
    table: Table,
    requester: &Requester,
    limit: i64,
) -> Result<Vec<Vec<u8>>, CrudError> {
    let limit = effective_limit(limit);
    let rows = if requester.admin {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes FROM {} ORDER BY created_at DESC LIMIT $1",
            table.name()
        )))
        .bind(limit)
        .fetch_all(pool)
        .await?
    } else {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes FROM {} WHERE {} ORDER BY created_at DESC LIMIT $2",
            table.name(),
            visibility_clause(1, None)
        )))
        .bind(requester.id.clone())
        .bind(limit)
        .fetch_all(pool)
        .await?
    };
    Ok(rows)
}

pub fn decode_all<M: Message + Default>(rows: Vec<Vec<u8>>) -> Vec<M> {
    rows.into_iter()
        .filter_map(|bytes| M::decode(bytes.as_slice()).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn visibility_clause_uses_requested_parameter_and_alias() {
        let clause = visibility_clause(3, Some("t"));

        assert!(clause.contains("t.proto_jsonb->>'owner' = $3"));
        assert!(clause.contains("t.proto_jsonb->'visibility'->'allowed_users' ? $3"));
        assert!(!clause.contains("$1"));
    }

    #[test]
    fn effective_limit_defaults_zero_to_fifty() {
        assert_eq!(effective_limit(0), 50);
        assert_eq!(effective_limit(7), 7);
    }

    #[test]
    fn table_names_match_python_mpac_objects() {
        let names: Vec<&str> = [
            Table::Attachments,
            Table::Backends,
            Table::Benchmarks,
            Table::TestItems,
            Table::TestRunAnswers,
            Table::TestRuns,
            Table::Tests,
            Table::Users,
        ]
        .map(Table::name)
        .to_vec();

        assert_eq!(
            names,
            [
                "attachments",
                "backends",
                "benchmarks",
                "test_items",
                "test_run_answers",
                "test_runs",
                "tests",
                "users"
            ]
        );
    }
}
