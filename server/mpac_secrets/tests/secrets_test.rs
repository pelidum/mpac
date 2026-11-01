#![allow(clippy::unwrap_used, clippy::expect_used)]

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use mpac_secrets::{MasterKey, SecretError, SecretStore};
use mpac_testing::TestDb;
use sqlx::PgPool;

fn key_url(name: &str, byte: u8) -> String {
    let dir = std::env::temp_dir().join(format!("mpac-secrets-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    std::fs::write(&path, BASE64.encode([byte; 32])).unwrap();
    format!("file://{}", path.display())
}

async fn backend(pool: &PgPool, id: &str) {
    sqlx::query("INSERT INTO backends (id, proto_jsonb, proto_bytes) VALUES ($1, '{}', '')")
        .bind(id)
        .execute(pool)
        .await
        .unwrap();
}

async fn store(pool: &PgPool, secrets: &SecretStore, id: &str, value: &str) {
    let mut conn = pool.acquire().await.unwrap();
    secrets
        .store_in(&mut conn, id, value, "admin@example.com")
        .await
        .unwrap();
}

#[tokio::test]
async fn stored_keys_load_back_and_never_appear_in_plaintext() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("round-trip", 1)).unwrap();
    backend(&db.pool, "b").await;

    store(&db.pool, &secrets, "b", "sk-plaintext").await;

    assert_eq!(
        secrets.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-plaintext")
    );
    let (ciphertext, wrapped): (Vec<u8>, Vec<u8>) =
        sqlx::query_as("SELECT ciphertext, wrapped_key FROM backend_secrets")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    let needle = b"sk-plaintext";
    assert!(!ciphertext.windows(needle.len()).any(|w| w == needle));
    assert!(!wrapped.windows(needle.len()).any(|w| w == needle));
}

#[tokio::test]
async fn the_cache_avoids_repeat_decryption_and_a_deleted_key_is_gone() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("cache", 2)).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "b", "sk-cached").await;
    secrets.load(&db.pool, "b").await.unwrap();
    secrets.load(&db.pool, "b").await.unwrap();

    let decrypts: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM secret_audit WHERE action = 'decrypt'")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(decrypts, 1);

    sqlx::query("DELETE FROM backend_secrets")
        .execute(&db.pool)
        .await
        .unwrap();
    assert_eq!(secrets.load(&db.pool, "b").await.unwrap(), None);
}

#[tokio::test]
async fn replacing_a_key_clears_the_cache_and_bumps_the_version() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("replace", 3)).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "b", "sk-old").await;
    secrets.load(&db.pool, "b").await.unwrap();

    store(&db.pool, &secrets, "b", "sk-new").await;

    assert_eq!(
        secrets.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-new")
    );
    let version: i32 = sqlx::query_scalar("SELECT version FROM backend_secrets")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(version, 2);
}

#[tokio::test]
async fn swapped_ciphertext_fails_to_decrypt() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("swap", 4)).unwrap();
    backend(&db.pool, "a").await;
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "a", "sk-a").await;
    store(&db.pool, &secrets, "b", "sk-b").await;

    sqlx::query(
        "UPDATE backend_secrets SET
           ciphertext = (SELECT ciphertext FROM backend_secrets WHERE backend_id = 'a'),
           wrapped_key = (SELECT wrapped_key FROM backend_secrets WHERE backend_id = 'a')
         WHERE backend_id = 'b'",
    )
    .execute(&db.pool)
    .await
    .unwrap();

    assert!(matches!(
        secrets.load(&db.pool, "b").await,
        Err(SecretError::Crypto)
    ));
}

#[tokio::test]
async fn deleting_a_backend_deletes_its_secret() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("delete", 5)).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "b", "sk").await;

    sqlx::query("DELETE FROM backends WHERE id = 'b'")
        .execute(&db.pool)
        .await
        .unwrap();

    let remaining: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM backend_secrets")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert_eq!(remaining, 0);
}

#[tokio::test]
async fn rotating_the_master_key_rewraps_without_changing_the_secret() {
    let db = TestDb::create().await;
    let old_url = key_url("rotate-old", 6);
    let old = SecretStore::open(&old_url).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &old, "b", "sk-rotate").await;
    let new = SecretStore::open(&key_url("rotate-new", 7)).unwrap();

    assert!(matches!(
        new.load(&db.pool, "b").await,
        Err(SecretError::WrongMasterKey { .. })
    ));

    let previous = MasterKey::open(&old_url).unwrap();
    assert_eq!(new.rewrap_from(&db.pool, &previous).await.unwrap(), 1);

    assert_eq!(
        new.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-rotate")
    );
    old.forget("b");
    assert!(old.load(&db.pool, "b").await.is_err());
}

#[tokio::test]
async fn every_key_change_and_decryption_is_audited_without_key_material() {
    let db = TestDb::create().await;
    let old_url = key_url("audit-old", 8);
    let old = SecretStore::open(&old_url).unwrap();
    backend(&db.pool, "b").await;

    store(&db.pool, &old, "b", "sk-audit-one").await;
    store(&db.pool, &old, "b", "sk-audit-two").await;
    old.load(&db.pool, "b").await.unwrap();
    old.load(&db.pool, "b").await.unwrap();
    let new = SecretStore::open(&key_url("audit-new", 9)).unwrap();
    new.rewrap_from(&db.pool, &MasterKey::open(&old_url).unwrap())
        .await
        .unwrap();

    let events: Vec<(String, String, Option<i32>)> = sqlx::query_as(
        "SELECT action, actor, key_version FROM secret_audit WHERE backend_id = 'b' ORDER BY id",
    )
    .fetch_all(&db.pool)
    .await
    .unwrap();
    let actions: Vec<&str> = events.iter().map(|e| e.0.as_str()).collect();
    assert_eq!(actions, ["create", "update", "decrypt", "rewrap"]);
    assert_eq!(events[0].1, "admin@example.com");
    assert_eq!(events[1].2, Some(2));
    assert!(events[2].1.starts_with("mpac:"));
    let dumped: String = sqlx::query_scalar("SELECT string_agg(t::text, ' ') FROM secret_audit t")
        .fetch_one(&db.pool)
        .await
        .unwrap();
    assert!(!dumped.contains("sk-audit"));
}

#[tokio::test]
async fn a_failed_audit_write_blocks_decryption() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("audit-fail", 10)).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "b", "sk").await;
    secrets.forget("b");
    sqlx::query("DROP TABLE secret_audit")
        .execute(&db.pool)
        .await
        .unwrap();

    assert!(matches!(
        secrets.load(&db.pool, "b").await,
        Err(SecretError::Query(_))
    ));
}

#[tokio::test]
async fn a_key_changed_by_another_process_is_used_without_any_signal() {
    let db = TestDb::create().await;
    let url = key_url("replicas", 7);
    let first = SecretStore::open(&url).unwrap();
    let second = SecretStore::open(&url).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &first, "b", "sk-old").await;
    assert_eq!(
        second.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-old")
    );

    store(&db.pool, &first, "b", "sk-new").await;

    assert_eq!(
        second.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-new")
    );
}

#[tokio::test]
async fn a_slow_load_of_an_old_key_cannot_serve_the_new_key_row() {
    let db = TestDb::create().await;
    let url = key_url("in-flight", 7);
    let writer = SecretStore::open(&url).unwrap();
    let reader = SecretStore::open(&url).unwrap();
    backend(&db.pool, "b").await;
    store(&db.pool, &writer, "b", "sk-old").await;
    let old = mpac_secrets::backend_record(&db.pool, "b")
        .await
        .unwrap()
        .unwrap()
        .secret
        .unwrap();

    store(&db.pool, &writer, "b", "sk-new").await;
    assert_eq!(
        reader.key_for(&db.pool, "b", &old).await.unwrap().as_ref(),
        "sk-old"
    );

    assert_eq!(
        reader.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-new")
    );
}

#[tokio::test]
async fn a_backend_record_reads_the_config_and_key_together() {
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&key_url("record", 7)).unwrap();
    backend(&db.pool, "keyless").await;
    backend(&db.pool, "keyed").await;
    store(&db.pool, &secrets, "keyed", "sk").await;

    let keyless = mpac_secrets::backend_record(&db.pool, "keyless")
        .await
        .unwrap()
        .unwrap();
    assert!(keyless.secret.is_none());
    let keyed = mpac_secrets::backend_record(&db.pool, "keyed")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        secrets
            .key_for(&db.pool, &keyed.id, keyed.secret.as_ref().unwrap())
            .await
            .unwrap()
            .as_ref(),
        "sk"
    );
    assert!(
        mpac_secrets::backend_record(&db.pool, "missing")
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn concurrent_cache_misses_decrypt_once() {
    let db = TestDb::create().await;
    let secrets = std::sync::Arc::new(SecretStore::open(&key_url("once", 7)).unwrap());
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "b", "sk").await;
    secrets.forget("b");

    let loads: Vec<_> = (0..16)
        .map(|_| {
            let secrets = std::sync::Arc::clone(&secrets);
            let pool = db.pool.clone();
            tokio::spawn(async move { secrets.load(&pool, "b").await.unwrap() })
        })
        .collect();
    for load in loads {
        assert_eq!(load.await.unwrap().as_deref(), Some("sk"));
    }

    let decrypts: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM secret_audit WHERE action = 'decrypt'")
            .fetch_one(&db.pool)
            .await
            .unwrap();
    assert_eq!(decrypts, 1);
}

#[tokio::test]
async fn a_real_aws_kms_key_round_trips_and_binds_the_backend_when_configured() {
    let Ok(url) = std::env::var("MPAC_TEST_AWS_KMS_URL") else {
        return;
    };
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&url).unwrap();
    backend(&db.pool, "a").await;
    backend(&db.pool, "b").await;
    store(&db.pool, &secrets, "a", "sk-kms-a").await;
    store(&db.pool, &secrets, "b", "sk-kms-b").await;
    secrets.forget("a");
    secrets.forget("b");

    assert_eq!(
        secrets.load(&db.pool, "a").await.unwrap().as_deref(),
        Some("sk-kms-a")
    );

    sqlx::query(
        "UPDATE backend_secrets SET wrapped_key =
           (SELECT wrapped_key FROM backend_secrets WHERE backend_id = 'a')
         WHERE backend_id = 'b'",
    )
    .execute(&db.pool)
    .await
    .unwrap();
    assert!(matches!(
        secrets.load(&db.pool, "b").await,
        Err(SecretError::Kms(_))
    ));
}

#[tokio::test]
async fn a_real_vault_transit_key_round_trips_when_configured() {
    let Ok(url) = std::env::var("MPAC_TEST_VAULT_URL") else {
        return;
    };
    let db = TestDb::create().await;
    let secrets = SecretStore::open(&url).unwrap();
    backend(&db.pool, "b").await;

    store(&db.pool, &secrets, "b", "sk-vault").await;

    assert_eq!(
        secrets.load(&db.pool, "b").await.unwrap().as_deref(),
        Some("sk-vault")
    );
}
