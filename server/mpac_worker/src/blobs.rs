use std::time::Duration;

use mpac_blob::{BlobStore, upload_key};
use mpac_db::attachments as lifecycle;
use sqlx::PgPool;

use crate::HandlerError;

pub async fn prune_stale_uploads(
    pool: &PgPool,
    blobs: &BlobStore,
    older_than: Duration,
) -> Result<u64, HandlerError> {
    let mut tx = lifecycle::lock(pool).await?;
    let stale: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM attachments
         WHERE proto_jsonb->>'upload_state' = 'PENDING'
           AND created_at < NOW() - make_interval(secs => $1)",
    )
    .bind(older_than.as_secs_f64())
    .fetch_all(&mut *tx)
    .await?;
    let mut pruned = 0;
    for id in stale {
        lifecycle::queue(&mut tx, &upload_key(&id)).await?;
        pruned += sqlx::query(
            "DELETE FROM attachments WHERE id = $1 AND proto_jsonb->>'upload_state' = 'PENDING'",
        )
        .bind(&id)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    }
    tx.commit().await?;
    cleanup(pool, blobs).await?;
    let mut tx = lifecycle::lock(pool).await?;
    for key in blobs
        .older_objects(older_than.max(Duration::from_hours(24)))
        .await?
    {
        if lifecycle::unreferenced(&mut tx, &key).await? {
            blobs.delete(&key).await?;
        }
    }
    tx.commit().await?;
    Ok(pruned)
}

pub async fn cleanup(pool: &PgPool, blobs: &BlobStore) -> Result<(), HandlerError> {
    let mut tx = lifecycle::lock(pool).await?;
    for key in lifecycle::queued(&mut tx).await? {
        if lifecycle::unreferenced(&mut tx, &key).await?
            && let Err(error) = blobs.delete(&key).await
        {
            tracing::warn!(%key, %error, "blob cleanup will retry");
            continue;
        }
        lifecycle::completed(&mut tx, &key).await?;
    }
    tx.commit().await?;
    Ok(())
}
