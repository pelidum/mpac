use std::collections::HashMap;
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use mpac_blob::{BlobError, BlobStore, blob_key, sniff, sniff_head, upload_key};
use mpac_db::attachments as lifecycle;
use mpac_db::crud::{self, Table, visibility_clause};
use mpac_proto::file_attachment::UploadState;
use mpac_proto::visibility::VisibilityOptions;
use mpac_proto::{
    BatchGetAttachmentsRequest, CreateAttachmentUploadReply, CreateAttachmentUploadRequest,
    DeleteRequest, FileAttachment, GetRequest, ListRequest, ResponseCode, StatusReply, Visibility,
};
use prost::Message;
use sha2::{Digest, Sha256};
use tonic::{Request, Status};

use crate::Api;
use crate::auth::caller;
use crate::status::internal;
use crate::test_defs::new_id;

pub const DEFAULT_MAX_UPLOAD_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const MAX_INLINE_BYTES: usize = 100 * 1024 * 1024;
const MAX_INLINE_BYTES_WITH_BLOBS: usize = 8 * 1024 * 1024;
const UPLOAD_URL_TTL: Duration = Duration::from_mins(15);
const DOWNLOAD_URL_TTL: Duration = Duration::from_hours(1);
const UNSUPPORTED: &str =
    "Unsupported file type. Only audio, image, video, and text files are supported.";

fn reply(code: ResponseCode, id: String, reason: String) -> StatusReply {
    StatusReply {
        code: code.into(),
        reason,
        id,
    }
}

fn error(reason: impl Into<String>) -> StatusReply {
    reply(ResponseCode::Error, String::new(), reason.into())
}

fn too_large(size: impl std::fmt::Display, limit: impl std::fmt::Display) -> String {
    format!("File size {size} bytes exceeds maximum of {limit} bytes")
}

fn org_only() -> Visibility {
    Visibility {
        r#type: VisibilityOptions::VisibilityOrgOnly.into(),
        allowed_users: Vec::new(),
    }
}

fn kilobytes(size: u64) -> i64 {
    i64::try_from(size.div_ceil(1024)).unwrap_or(i64::MAX)
}

fn blob_store(api: &Api) -> Result<&BlobStore, Status> {
    api.blobs
        .as_ref()
        .ok_or_else(|| Status::failed_precondition("Blob storage is not configured"))
}

fn is_ready_blob(attachment: &FileAttachment) -> bool {
    attachment.upload_state == i32::from(UploadState::Ready) && !attachment.uri.is_empty()
}

async fn present(
    api: &Api,
    mut attachment: FileAttachment,
    sign: bool,
) -> Result<FileAttachment, Status> {
    let ready = is_ready_blob(&attachment);
    let key = std::mem::take(&mut attachment.uri);
    if sign && ready {
        attachment.uri = blob_store(api)?
            .sign_get(&key, DOWNLOAD_URL_TTL)
            .await
            .map_err(|error| {
                Status::unavailable(format!("Unable to sign attachment download: {error}"))
            })?;
    }
    Ok(attachment)
}

pub(crate) async fn cleanup(api: &Api) {
    let Some(blobs) = &api.blobs else {
        return;
    };
    if let Err(error) = cleanup_inner(api, blobs).await {
        tracing::warn!(%error, "blob cleanup deferred for retry");
    }
}

async fn cleanup_inner(api: &Api, blobs: &BlobStore) -> Result<(), Status> {
    let mut tx = lifecycle::lock(&api.pool).await.map_err(internal)?;
    for key in lifecycle::queued(&mut tx).await.map_err(internal)? {
        if lifecycle::unreferenced(&mut tx, &key)
            .await
            .map_err(internal)?
            && let Err(error) = blobs.delete(&key).await
        {
            tracing::warn!(%key, %error, "blob cleanup will retry");
            continue;
        }
        lifecycle::completed(&mut tx, &key)
            .await
            .map_err(internal)?;
    }
    tx.commit().await.map_err(internal)
}

#[allow(deprecated)]
pub async fn create_attachment(
    api: &Api,
    request: Request<FileAttachment>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let request = request.into_inner();
    if request.name.is_empty() {
        return Ok(error("Attachment name is required"));
    }
    if request.file.is_empty() {
        return Ok(error("File data is required"));
    }
    let limit = if api.blobs.is_some() {
        MAX_INLINE_BYTES_WITH_BLOBS
    } else {
        MAX_INLINE_BYTES
    };
    if request.file.len() > limit {
        return Ok(error(too_large(request.file.len(), limit)));
    }
    let Some(sniffed) = sniff(&request.name, &request.file) else {
        return Ok(error(UNSUPPORTED));
    };
    let sha256 = hex::encode(Sha256::digest(&request.file));
    let mut attachment = FileAttachment {
        id: sha256.clone(),
        name: request.name.clone(),
        extension: sniffed.extension,
        modality: sniffed.modality.into(),
        file_size: kilobytes(request.file.len() as u64),
        visibility: Some(org_only()),
        owner: caller.id,
        mime: sniffed.mime,
        ..FileAttachment::default()
    };
    let mut tx = lifecycle::lock(&api.pool).await.map_err(internal)?;
    match &api.blobs {
        Some(blobs) => {
            blobs
                .put(&blob_key(&sha256), Bytes::from(request.file))
                .await
                .map_err(internal)?;
            attachment.uri = blob_key(&sha256);
            attachment.sha256 = sha256;
            attachment.upload_state = UploadState::Ready.into();
        }
        None => attachment.file = request.file,
    }
    let inserted = sqlx::query("INSERT INTO attachments (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3) ON CONFLICT DO NOTHING")
        .bind(&attachment.id).bind(mpac_proto::json::to_jsonb(&attachment).map_err(internal)?)
        .bind(attachment.encode_to_vec()).execute(&mut *tx).await.map_err(internal)?.rows_affected();
    tx.commit().await.map_err(internal)?;
    let created = if inserted == 0 {
        reply(
            ResponseCode::Error,
            attachment.id.clone(),
            "Object already exists".into(),
        )
    } else {
        reply(ResponseCode::Success, attachment.id.clone(), String::new())
    };
    if created.code != i32::from(ResponseCode::Success) {
        return Ok(created);
    }
    Ok(reply(
        ResponseCode::Success,
        attachment.id,
        format!("Attachment '{}' created successfully", attachment.name),
    ))
}

pub async fn create_attachment_upload(
    api: &Api,
    request: Request<CreateAttachmentUploadRequest>,
) -> Result<CreateAttachmentUploadReply, Status> {
    let caller = caller(&request)?;
    let blobs = blob_store(api)?;
    let request = request.into_inner();
    if request.name.is_empty() {
        return Err(Status::invalid_argument("Attachment name is required"));
    }
    let size = u64::try_from(request.size_bytes)
        .ok()
        .filter(|size| *size > 0)
        .ok_or_else(|| Status::invalid_argument("size_bytes must be positive"))?;
    if size > api.max_upload_bytes {
        return Err(Status::invalid_argument(too_large(
            size,
            api.max_upload_bytes,
        )));
    }
    let id = new_id();
    let pending = FileAttachment {
        id: id.clone(),
        name: request.name,
        file_size: kilobytes(size),
        visibility: Some(request.visibility.unwrap_or_else(org_only)),
        owner: caller.id,
        mime: request.mime,
        uri: upload_key(&id),
        upload_state: UploadState::Pending.into(),
        ..FileAttachment::default()
    };
    let created = crud::create(&api.pool, Table::Attachments, &id, &pending, false)
        .await
        .map_err(internal)?;
    if created.code != i32::from(ResponseCode::Success) {
        return Err(Status::internal("Unable to record the upload"));
    }
    let upload_uri = blobs
        .sign_put(&upload_key(&id), UPLOAD_URL_TTL)
        .await
        .map_err(internal)?;
    Ok(CreateAttachmentUploadReply {
        attachment_id: id,
        upload_uri,
        headers: HashMap::new(),
        expires_at: Some(prost_types::Timestamp::from(
            SystemTime::now() + UPLOAD_URL_TTL,
        )),
    })
}

async fn discard_upload(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    id: &str,
) -> Result<(), Status> {
    lifecycle::queue(tx, &upload_key(id))
        .await
        .map_err(internal)?;
    sqlx::query("DELETE FROM attachments WHERE id = $1")
        .bind(id)
        .execute(&mut **tx)
        .await
        .map_err(internal)?;
    Ok(())
}

async fn reject_upload(api: &Api, id: &str) -> Result<(), Status> {
    let mut tx = lifecycle::lock(&api.pool).await.map_err(internal)?;
    if let Some(row) = crud::get_for_update::<FileAttachment>(&mut tx, Table::Attachments, id)
        .await
        .map_err(internal)?
        && row.upload_state == i32::from(UploadState::Pending)
    {
        discard_upload(&mut tx, id).await?;
    }
    tx.commit().await.map_err(internal)
}

pub async fn finalize_attachment(
    api: &Api,
    request: Request<GetRequest>,
) -> Result<FileAttachment, Status> {
    let caller = caller(&request)?;
    let blobs = blob_store(api)?;
    let id = request.into_inner().id;
    let staging = format!("staging/{}", new_id());
    let result = finalize_inner(api, blobs, &caller, &id, &staging).await;
    if let Err(error) = blobs.delete(&staging).await {
        tracing::warn!(%error, "staging cleanup deferred to orphan reconciliation");
    }
    cleanup(api).await;
    result
}

async fn finalize_inner(
    api: &Api,
    blobs: &BlobStore,
    caller: &crate::auth::Caller,
    id: &str,
    staging: &str,
) -> Result<FileAttachment, Status> {
    let pending = crud::get_unchecked::<FileAttachment>(&api.pool, Table::Attachments, id)
        .await
        .map_err(internal)?
        .filter(|row| row.upload_state == i32::from(UploadState::Pending))
        .ok_or_else(|| Status::not_found(format!("Upload {id} not found")))?;
    if pending.owner != caller.id && !caller.admin {
        return Err(Status::permission_denied(
            "Only the uploader or an admin can finalize this upload.",
        ));
    }
    blobs
        .copy(&upload_key(id), staging)
        .await
        .map_err(|error| match error {
            BlobError::NotFound(_) => {
                Status::failed_precondition(format!("Upload {id} has no uploaded content"))
            }
            other => internal(other),
        })?;
    let inspected = match blobs.inspect(staging, api.max_upload_bytes).await {
        Ok(inspected) => inspected,
        Err(BlobError::NotFound(_)) => {
            return Err(Status::failed_precondition(format!(
                "Upload {id} has no uploaded content"
            )));
        }
        Err(BlobError::TooLarge { size, limit, .. }) => {
            reject_upload(api, id).await?;
            return Err(Status::invalid_argument(too_large(size, limit)));
        }
        Err(error) => return Err(internal(error)),
    };
    if inspected.size == 0 {
        reject_upload(api, id).await?;
        return Err(Status::invalid_argument("File data is required"));
    }
    let Some(sniffed) = sniff_head(&pending.name, &inspected.head, inspected.utf8) else {
        reject_upload(api, id).await?;
        return Err(Status::invalid_argument(UNSUPPORTED));
    };
    let mut tx = lifecycle::lock(&api.pool).await.map_err(internal)?;
    let current = crud::get_for_update::<FileAttachment>(&mut tx, Table::Attachments, id)
        .await
        .map_err(internal)?;
    if current.as_ref() != Some(&pending) {
        return Err(Status::not_found(
            "Upload was removed or changed during inspection",
        ));
    }
    blobs
        .copy(staging, &blob_key(&inspected.sha256))
        .await
        .map_err(internal)?;
    let ready = FileAttachment {
        id: inspected.sha256.clone(),
        extension: sniffed.extension,
        modality: sniffed.modality.into(),
        file_size: kilobytes(inspected.size),
        mime: sniffed.mime,
        uri: blob_key(&inspected.sha256),
        sha256: inspected.sha256.clone(),
        upload_state: UploadState::Ready.into(),
        ..pending
    };
    let stored = store_ready(&mut tx, caller, id, &ready).await?;
    tx.commit().await.map_err(internal)?;
    present(api, stored, true).await
}

async fn store_ready(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    caller: &crate::auth::Caller,
    upload_id: &str,
    ready: &FileAttachment,
) -> Result<FileAttachment, Status> {
    if let Some(existing) =
        crud::get_for_update::<FileAttachment>(tx, Table::Attachments, &ready.id)
            .await
            .map_err(internal)?
    {
        let visible: bool = sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT EXISTS (SELECT 1 FROM attachments WHERE id = $1 AND {})",
            visibility_clause(2, None)
        )))
        .bind(&ready.id)
        .bind(&caller.id)
        .fetch_one(&mut **tx)
        .await
        .map_err(internal)?;
        if !caller.admin && !visible {
            return Err(Status::permission_denied(
                "Duplicate content belongs to an inaccessible attachment",
            ));
        }
        if existing.visibility != ready.visibility {
            return Err(Status::already_exists(
                "Duplicate attachment has different visibility",
            ));
        }
        discard_upload(tx, upload_id).await?;
        return Ok(existing);
    }
    sqlx::query(
        "INSERT INTO attachments (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)",
    )
    .bind(&ready.id)
    .bind(mpac_proto::json::to_jsonb(ready).map_err(internal)?)
    .bind(ready.encode_to_vec())
    .execute(&mut **tx)
    .await
    .map_err(internal)?;
    discard_upload(tx, upload_id).await?;
    Ok(ready.clone())
}

pub async fn get_attachment(
    api: &Api,
    request: Request<GetRequest>,
) -> Result<FileAttachment, Status> {
    let caller = caller(&request)?;
    let attachment = crud::get(
        &api.pool,
        Table::Attachments,
        &request.get_ref().id,
        &caller.requester(),
    )
    .await
    .map_err(internal)?
    .unwrap_or_default();
    present(api, attachment, true).await
}

#[allow(deprecated)]
pub async fn batch_get_attachments(
    api: &Api,
    request: Request<BatchGetAttachmentsRequest>,
) -> Result<Vec<FileAttachment>, Status> {
    let caller = caller(&request)?;
    let BatchGetAttachmentsRequest { ids, metadata_only } = request.into_inner();
    let rows: Vec<Vec<u8>> = if caller.admin {
        sqlx::query_scalar("SELECT proto_bytes FROM attachments WHERE id = ANY($1::text[])")
            .bind(&ids)
            .fetch_all(&api.pool)
            .await
    } else {
        sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
            "SELECT proto_bytes FROM attachments WHERE id = ANY($1::text[]) AND {}",
            visibility_clause(2, None)
        )))
        .bind(&ids)
        .bind(&caller.id)
        .fetch_all(&api.pool)
        .await
    }
    .map_err(internal)?;
    let mut attachments = Vec::with_capacity(rows.len());
    for mut attachment in crud::decode_all::<FileAttachment>(rows) {
        if metadata_only {
            attachment.file.clear();
        }
        attachments.push(present(api, attachment, !metadata_only).await?);
    }
    Ok(attachments)
}

pub async fn delete_attachment(
    api: &Api,
    request: Request<DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    let mut tx = lifecycle::lock(&api.pool).await.map_err(internal)?;
    let original = crud::get_for_update::<FileAttachment>(&mut tx, Table::Attachments, &id)
        .await
        .map_err(internal)?
        .unwrap_or_default();
    if original.owner != caller.id && !caller.admin {
        return Err(Status::permission_denied(
            "Only the attachment owner or admin can delete this attachment.",
        ));
    }
    if original.id.is_empty() {
        return Ok(reply(
            ResponseCode::Error,
            id,
            "Attachment not found".into(),
        ));
    }
    lifecycle::queue(&mut tx, &original.uri)
        .await
        .map_err(internal)?;
    sqlx::query("DELETE FROM attachments WHERE id = $1")
        .bind(&id)
        .execute(&mut *tx)
        .await
        .map_err(internal)?;
    tx.commit().await.map_err(internal)?;
    cleanup(api).await;
    Ok(reply(ResponseCode::Success, id, String::new()))
}

#[allow(deprecated)]
pub async fn list_attachments(
    api: &Api,
    request: Request<ListRequest>,
) -> Result<Vec<FileAttachment>, Status> {
    let caller = caller(&request)?;
    let rows: Vec<Vec<u8>> = sqlx::query_scalar(sqlx::AssertSqlSafe(format!(
        "SELECT proto_bytes FROM attachments WHERE COALESCE(proto_jsonb->>'upload_state', '') <> 'PENDING' AND ($1 OR {}) ORDER BY created_at DESC LIMIT $3",
        visibility_clause(2, None))))
        .bind(caller.admin).bind(&caller.id).bind(crud::effective_limit(request.get_ref().limit))
        .fetch_all(&api.pool).await.map_err(internal)?;
    Ok(crud::decode_all::<FileAttachment>(rows)
        .into_iter()
        .filter(|attachment| attachment.upload_state != i32::from(UploadState::Pending))
        .map(|mut attachment| {
            attachment.file.clear();
            attachment.uri.clear();
            attachment
        })
        .collect())
}
