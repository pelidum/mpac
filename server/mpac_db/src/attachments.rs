use sqlx::{PgConnection, PgPool, Postgres, Transaction};

pub async fn lock(pool: &PgPool) -> Result<Transaction<'_, Postgres>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(674288901)")
        .execute(&mut *tx)
        .await?;
    Ok(tx)
}

pub async fn queue(connection: &mut PgConnection, key: &str) -> Result<(), sqlx::Error> {
    if !key.is_empty() {
        sqlx::query("INSERT INTO blob_cleanup (key) VALUES ($1) ON CONFLICT DO NOTHING")
            .bind(key)
            .execute(connection)
            .await?;
    }
    Ok(())
}

pub async fn unreferenced(connection: &mut PgConnection, key: &str) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT NOT EXISTS (SELECT 1 FROM attachments WHERE proto_jsonb->>'uri' = $1)",
    )
    .bind(key)
    .fetch_one(connection)
    .await
}

pub async fn queued(connection: &mut PgConnection) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar("SELECT key FROM blob_cleanup ORDER BY created_at LIMIT 100")
        .fetch_all(connection)
        .await
}

pub async fn completed(connection: &mut PgConnection, key: &str) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM blob_cleanup WHERE key = $1")
        .bind(key)
        .execute(connection)
        .await?;
    Ok(())
}
