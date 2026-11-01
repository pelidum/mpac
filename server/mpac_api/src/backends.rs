use mpac_db::crud::{self, Table};
use mpac_proto::{
    Backend, DeleteRequest, GetRequest, ListRequest, ResponseCode, SecretAuditEvent, StatusReply,
};
use prost::Message;
use sqlx::Row;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;
use crate::test_defs::{new_id, now};

const DEFAULT_LIMIT: i64 = 1000;

fn require_admin(caller: &Caller, action: &str) -> Result<(), Status> {
    if caller.admin {
        Ok(())
    } else {
        Err(Status::permission_denied(format!(
            "Only admins can {action} backends."
        )))
    }
}

async fn stored_backend(api: &Api, caller: &Caller, id: &str) -> Result<Backend, Status> {
    crud::get_bytes(&api.pool, Table::Backends, id, &caller.requester())
        .await
        .map_err(internal)?
        .and_then(|bytes| Backend::decode(bytes.as_slice()).ok())
        .ok_or_else(|| Status::not_found(format!("Backend {id} not found")))
}

fn same_origin(first: &str, second: &str) -> bool {
    match (url::Url::parse(first), url::Url::parse(second)) {
        (Ok(first), Ok(second)) => first.origin() == second.origin(),
        _ => first == second,
    }
}

async fn store_key(
    api: &Api,
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    backend_id: &str,
    key: &str,
    actor: &str,
) -> Result<(), Status> {
    let secrets = api.secrets.as_ref().ok_or_else(|| {
        Status::failed_precondition(
            "Provider keys need a master key. Set MPAC_SECRET_KEY_URL on the server.",
        )
    })?;
    secrets
        .store_in(tx, backend_id, key, actor)
        .await
        .map_err(internal)
}

async fn notify_changed(api: &Api, backend_id: &str) {
    if let Err(error) = mpac_queue::notify_backend_changed(&api.pool, backend_id).await {
        tracing::warn!(backend = %backend_id, %error, "unable to signal a backend change");
    }
}

pub async fn create_backend(api: &Api, request: Request<Backend>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    require_admin(&caller, "create")?;
    let mut backend = request.into_inner();
    backend.id = new_id();
    backend.owner.clone_from(&caller.id);
    backend.enabled = true;
    backend.created_at_utc = Some(now());
    backend.modified_at_utc = Some(now());
    let key = std::mem::take(&mut backend.api_key);
    let mut tx = api.pool.begin().await.map_err(internal)?;
    crud::upsert_in(&mut tx, Table::Backends, &backend.id, &backend)
        .await
        .map_err(internal)?;
    if !key.is_empty() {
        store_key(api, &mut tx, &backend.id, &key, &caller.id).await?;
    }
    tx.commit().await.map_err(internal)?;
    Ok(StatusReply {
        code: ResponseCode::Success.into(),
        id: backend.id,
        ..StatusReply::default()
    })
}

pub async fn get_backend(api: &Api, request: Request<GetRequest>) -> Result<Backend, Status> {
    let caller = caller(&request)?;
    let mut backend = stored_backend(api, &caller, &request.get_ref().id).await?;
    backend.api_key.clear();
    Ok(backend)
}

pub async fn update_backend(api: &Api, request: Request<Backend>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    require_admin(&caller, "update")?;
    let mut backend = request.into_inner();
    let mut tx = api.pool.begin().await.map_err(internal)?;
    let stored = crud::get_for_update::<Backend>(&mut tx, Table::Backends, &backend.id)
        .await
        .map_err(internal)?
        .ok_or_else(|| Status::not_found(format!("Backend {} not found", backend.id)))?;
    backend.modified_at_utc = Some(now());
    backend.created_at_utc = Some(stored.created_at_utc.unwrap_or_default());
    backend.owner = stored.owner;
    let key = std::mem::take(&mut backend.api_key);
    if key.is_empty()
        && !same_origin(&backend.base_url, &stored.base_url)
        && mpac_secrets::has_secret(&mut *tx, &backend.id)
            .await
            .map_err(internal)?
    {
        return Err(Status::invalid_argument(
            "Enter the API key again to use it with a new host.",
        ));
    }
    crud::update_in(&mut tx, Table::Backends, &backend.id, &backend)
        .await
        .map_err(internal)?;
    if !key.is_empty() {
        store_key(api, &mut tx, &backend.id, &key, &caller.id).await?;
    }
    tx.commit().await.map_err(internal)?;
    if let Some(secrets) = &api.secrets {
        secrets.forget(&backend.id);
    }
    notify_changed(api, &backend.id).await;
    Ok(StatusReply {
        code: ResponseCode::Success.into(),
        id: backend.id,
        ..StatusReply::default()
    })
}

pub async fn delete_backend(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    require_admin(&caller, "delete")?;
    let id = request.into_inner().id;
    let mut tx = api.pool.begin().await.map_err(internal)?;
    crud::get_for_update::<Backend>(&mut tx, Table::Backends, &id)
        .await
        .map_err(internal)?;
    let had_key = mpac_secrets::has_secret(&mut *tx, &id)
        .await
        .map_err(internal)?;
    let reply = crud::delete_in(&mut tx, Table::Backends, &id, &caller.requester())
        .await
        .map_err(internal)?;
    if had_key && reply.code == i32::from(ResponseCode::Success) {
        mpac_secrets::record_delete(&mut tx, &id, &caller.id)
            .await
            .map_err(internal)?;
    }
    tx.commit().await.map_err(internal)?;
    if let Some(secrets) = &api.secrets {
        secrets.forget(&id);
    }
    notify_changed(api, &id).await;
    Ok(reply)
}

pub async fn list_backends(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<Backend>, Status> {
    let caller = caller(&request)?;
    let limit = if request.get_ref().limit > 0 {
        request.get_ref().limit
    } else {
        DEFAULT_LIMIT
    };
    let rows = if caller.admin {
        crud::list_bytes(&api.pool, Table::Backends, &caller.requester(), limit)
            .await
            .map_err(internal)?
    } else {
        sqlx::query_scalar(
            "SELECT proto_bytes FROM backends WHERE proto_jsonb ->> 'enabled' = 'true' \
             ORDER BY created_at DESC LIMIT $1",
        )
        .bind(limit)
        .fetch_all(&api.pool)
        .await
        .map_err(internal)?
    };
    let mut backends: Vec<Backend> = crud::decode_all(rows);
    for backend in &mut backends {
        backend.api_key.clear();
    }
    Ok(backends)
}

pub async fn list_secret_audit(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<SecretAuditEvent>, Status> {
    let caller = caller(&request)?;
    if !caller.admin {
        return Err(Status::permission_denied(
            "Only admins can read the secret audit trail.",
        ));
    }
    let request = request.into_inner();
    let limit = if request.limit > 0 {
        request.limit
    } else {
        DEFAULT_LIMIT
    };
    let rows = sqlx::query(
        "SELECT backend_id, action, actor, COALESCE(key_version, 0) AS key_version,
                EXTRACT(EPOCH FROM created_at)::float8 AS created_at
         FROM secret_audit
         WHERE $1 = '' OR backend_id = $1
         ORDER BY id DESC LIMIT $2",
    )
    .bind(&request.parent_id)
    .bind(limit)
    .fetch_all(&api.pool)
    .await
    .map_err(internal)?;
    rows.iter()
        .map(|row| {
            let created: f64 = row.try_get("created_at").map_err(internal)?;
            Ok(SecretAuditEvent {
                backend_id: row.try_get("backend_id").map_err(internal)?,
                action: row.try_get("action").map_err(internal)?,
                actor: row.try_get("actor").map_err(internal)?,
                key_version: row.try_get("key_version").map_err(internal)?,
                created_at_utc: Some(prost_types::Timestamp::from(
                    std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(created),
                )),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_compares_scheme_host_and_port_only() {
        assert!(same_origin("https://a.test/v1", "https://a.test/v2"));
        assert!(!same_origin("https://a.test/v1", "https://b.test/v1"));
        assert!(!same_origin("https://a.test/v1", "http://a.test/v1"));
        assert!(!same_origin("https://a.test/v1", "https://a.test:8443/v1"));
        assert!(same_origin("", ""));
        assert!(!same_origin("not a url", "https://a.test"));
    }
}
