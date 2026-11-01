use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use ring::aead::{AES_256_GCM, Aad, LessSafeKey, NONCE_LEN, Nonce, UnboundKey};
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{PgConnection, PgPool, Row};
use url::Url;

mod kms;

const KEY_BYTES: usize = 32;
const CACHE_TTL: Duration = Duration::from_mins(5);
const VAULT_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, thiserror::Error)]
pub enum SecretError {
    #[error("secret query failed: {0}")]
    Query(#[from] sqlx::Error),
    #[error("secret encryption failed")]
    Crypto,
    #[error("invalid secret key url: {0}")]
    InvalidUrl(String),
    #[error("unable to read the master key: {0}")]
    KeyFile(String),
    #[error("vault request failed: {0}")]
    Vault(String),
    #[error("kms request failed: {0}")]
    Kms(String),
    #[error("secret for backend {backend} uses master key {stored}, but this server has {current}")]
    WrongMasterKey {
        backend: String,
        stored: String,
        current: String,
    },
}

fn random_bytes<const N: usize>() -> Result<[u8; N], SecretError> {
    let mut bytes = [0_u8; N];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| SecretError::Crypto)?;
    Ok(bytes)
}

fn aead(key: &[u8]) -> Result<LessSafeKey, SecretError> {
    let unbound = UnboundKey::new(&AES_256_GCM, key).map_err(|_| SecretError::Crypto)?;
    Ok(LessSafeKey::new(unbound))
}

fn seal(key: &[u8], associated: &[u8], plaintext: &[u8]) -> Result<Vec<u8>, SecretError> {
    let nonce_bytes: [u8; NONCE_LEN] = random_bytes()?;
    let mut in_out = plaintext.to_vec();
    aead(key)?
        .seal_in_place_append_tag(
            Nonce::assume_unique_for_key(nonce_bytes),
            Aad::from(associated),
            &mut in_out,
        )
        .map_err(|_| SecretError::Crypto)?;
    let mut sealed = nonce_bytes.to_vec();
    sealed.append(&mut in_out);
    Ok(sealed)
}

fn open(key: &[u8], associated: &[u8], sealed: &[u8]) -> Result<Vec<u8>, SecretError> {
    if sealed.len() < NONCE_LEN {
        return Err(SecretError::Crypto);
    }
    let (nonce_bytes, ciphertext) = sealed.split_at(NONCE_LEN);
    let nonce = Nonce::try_assume_unique_for_key(nonce_bytes).map_err(|_| SecretError::Crypto)?;
    let mut in_out = ciphertext.to_vec();
    let plaintext = aead(key)?
        .open_in_place(nonce, Aad::from(associated), &mut in_out)
        .map_err(|_| SecretError::Crypto)?;
    Ok(plaintext.to_vec())
}

enum Provider {
    File {
        key: [u8; KEY_BYTES],
    },
    Vault {
        http: reqwest::Client,
        address: String,
        mount: String,
        key_name: String,
        token: String,
    },
    AwsKms(kms::AwsKms),
    GcpKms(kms::GcpKms),
}

pub struct MasterKey {
    id: String,
    provider: Provider,
}

impl std::fmt::Debug for MasterKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MasterKey")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

impl MasterKey {
    pub fn open(url: &str) -> Result<Self, SecretError> {
        let parsed = Url::parse(url).map_err(|error| SecretError::InvalidUrl(error.to_string()))?;
        match parsed.scheme() {
            "file" => Self::from_file(parsed.path()),
            "aws-kms" => Ok(Self {
                id: format!("aws-kms:{}", parsed.path()),
                provider: Provider::AwsKms(kms::AwsKms::open(parsed.path())?),
            }),
            "gcp-kms" => Ok(Self {
                id: format!("gcp-kms:{}", parsed.path()),
                provider: Provider::GcpKms(kms::GcpKms::open(parsed.path())?),
            }),
            "vault+http" | "vault+https" => Self::vault(&parsed),
            other => Err(SecretError::InvalidUrl(format!(
                "unsupported scheme {other:?}; use file://, vault+https://, aws-kms: or gcp-kms:"
            ))),
        }
    }

    fn from_file(path: &str) -> Result<Self, SecretError> {
        let contents = std::fs::read_to_string(path)
            .map_err(|error| SecretError::KeyFile(format!("{path}: {error}")))?;
        let decoded = BASE64
            .decode(contents.trim())
            .map_err(|_| SecretError::KeyFile(format!("{path}: not valid base64")))?;
        let key: [u8; KEY_BYTES] = decoded.try_into().map_err(|_| {
            SecretError::KeyFile(format!("{path}: must decode to {KEY_BYTES} bytes"))
        })?;
        let fingerprint = hex::encode(Sha256::digest(key));
        Ok(Self {
            id: format!("file:{}", &fingerprint[..16]),
            provider: Provider::File { key },
        })
    }

    fn vault(url: &Url) -> Result<Self, SecretError> {
        let host = url
            .host_str()
            .ok_or_else(|| SecretError::InvalidUrl("vault url needs a host".into()))?;
        let scheme = url.scheme().trim_start_matches("vault+");
        let address = url.port().map_or_else(
            || format!("{scheme}://{host}"),
            |port| format!("{scheme}://{host}:{port}"),
        );
        let path = url.path().trim_matches('/');
        let (mount, key_name) = path.rsplit_once('/').ok_or_else(|| {
            SecretError::InvalidUrl("vault url path must be /<transit mount>/<key name>".into())
        })?;
        let token = std::env::var("VAULT_TOKEN")
            .ok()
            .filter(|token| !token.is_empty())
            .ok_or_else(|| SecretError::Vault("VAULT_TOKEN is not set".into()))?;
        Ok(Self {
            id: format!("vault:{host}/{path}"),
            provider: Provider::Vault {
                http: mpac_http::client(VAULT_TIMEOUT)
                    .map_err(|error| SecretError::Vault(error.to_string()))?,
                address,
                mount: mount.to_owned(),
                key_name: key_name.to_owned(),
                token,
            },
        })
    }

    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    async fn wrap(&self, associated: &str, data_key: &[u8]) -> Result<Vec<u8>, SecretError> {
        match &self.provider {
            Provider::File { key } => seal(key, associated.as_bytes(), data_key),
            Provider::Vault { .. } => {
                let body = json!({
                    "plaintext": BASE64.encode(data_key),
                    "associated_data": BASE64.encode(associated),
                });
                let reply = self.transit("encrypt", &body).await?;
                let ciphertext = reply
                    .pointer("/data/ciphertext")
                    .and_then(Value::as_str)
                    .ok_or_else(|| SecretError::Vault("reply has no ciphertext".into()))?;
                Ok(ciphertext.as_bytes().to_vec())
            }
            Provider::AwsKms(kms) => kms.encrypt(associated, data_key).await,
            Provider::GcpKms(kms) => kms.encrypt(associated, data_key).await,
        }
    }

    async fn unwrap(&self, associated: &str, wrapped: &[u8]) -> Result<Vec<u8>, SecretError> {
        match &self.provider {
            Provider::File { key } => open(key, associated.as_bytes(), wrapped),
            Provider::Vault { .. } => {
                let ciphertext = std::str::from_utf8(wrapped).map_err(|_| SecretError::Crypto)?;
                let body = json!({
                    "ciphertext": ciphertext,
                    "associated_data": BASE64.encode(associated),
                });
                let reply = self.transit("decrypt", &body).await?;
                let plaintext = reply
                    .pointer("/data/plaintext")
                    .and_then(Value::as_str)
                    .ok_or_else(|| SecretError::Vault("reply has no plaintext".into()))?;
                BASE64
                    .decode(plaintext)
                    .map_err(|_| SecretError::Vault("reply is not base64".into()))
            }
            Provider::AwsKms(kms) => kms.decrypt(associated, wrapped).await,
            Provider::GcpKms(kms) => kms.decrypt(associated, wrapped).await,
        }
    }

    async fn transit(&self, operation: &str, body: &Value) -> Result<Value, SecretError> {
        let Provider::Vault {
            http,
            address,
            mount,
            key_name,
            token,
        } = &self.provider
        else {
            return Err(SecretError::Crypto);
        };
        let response = http
            .post(format!("{address}/v1/{mount}/{operation}/{key_name}"))
            .header("X-Vault-Token", token)
            .json(body)
            .send()
            .await
            .map_err(|error| SecretError::Vault(error.without_url().to_string()))?;
        let status = response.status();
        if !status.is_success() {
            return Err(SecretError::Vault(format!(
                "{operation} returned HTTP {status}"
            )));
        }
        response
            .json()
            .await
            .map_err(|error| SecretError::Vault(error.without_url().to_string()))
    }
}

struct CachedKey {
    ciphertext: Vec<u8>,
    value: Arc<str>,
    stored_at: Instant,
}

pub struct StoredSecret {
    ciphertext: Vec<u8>,
    wrapped_key: Vec<u8>,
    master_key_id: String,
    version: i32,
}

pub struct BackendRecord {
    pub id: String,
    pub proto_bytes: Vec<u8>,
    pub secret: Option<StoredSecret>,
}

const BACKEND_RECORD_QUERY: &str =
    "SELECT b.id, b.proto_bytes, s.ciphertext, s.wrapped_key, s.master_key_id, s.version
     FROM backends b LEFT JOIN backend_secrets s ON s.backend_id = b.id";

fn backend_record_from(row: &sqlx::postgres::PgRow) -> Result<BackendRecord, SecretError> {
    let ciphertext: Option<Vec<u8>> = row.try_get("ciphertext")?;
    let secret = match ciphertext {
        Some(ciphertext) => Some(StoredSecret {
            ciphertext,
            wrapped_key: row.try_get("wrapped_key")?,
            master_key_id: row.try_get("master_key_id")?,
            version: row.try_get("version")?,
        }),
        None => None,
    };
    Ok(BackendRecord {
        id: row.try_get("id")?,
        proto_bytes: row.try_get("proto_bytes")?,
        secret,
    })
}

pub async fn backend_record(
    pool: &PgPool,
    backend_id: &str,
) -> Result<Option<BackendRecord>, SecretError> {
    let row = sqlx::query(sqlx::AssertSqlSafe(format!(
        "{BACKEND_RECORD_QUERY} WHERE b.id = $1"
    )))
    .bind(backend_id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(backend_record_from).transpose()
}

pub async fn enabled_backend_records(
    pool: &PgPool,
    limit: i64,
) -> Result<Vec<BackendRecord>, SecretError> {
    let rows = sqlx::query(sqlx::AssertSqlSafe(format!(
        "{BACKEND_RECORD_QUERY} WHERE b.proto_jsonb ->> 'enabled' = 'true'
         ORDER BY b.created_at DESC LIMIT $1"
    )))
    .bind(limit)
    .fetch_all(pool)
    .await?;
    rows.iter().map(backend_record_from).collect()
}

pub struct SecretStore {
    master: MasterKey,
    process: String,
    cache: Mutex<HashMap<String, CachedKey>>,
    decrypting: tokio::sync::Mutex<()>,
}

impl std::fmt::Debug for SecretStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecretStore")
            .field("master", &self.master)
            .finish_non_exhaustive()
    }
}

async fn record(
    conn: &mut PgConnection,
    backend_id: &str,
    action: &str,
    actor: &str,
    version: Option<i32>,
) -> Result<(), SecretError> {
    sqlx::query(
        "INSERT INTO secret_audit (backend_id, action, actor, key_version)
         VALUES ($1, $2, $3, $4)",
    )
    .bind(backend_id)
    .bind(action)
    .bind(actor)
    .bind(version)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn record_delete(
    conn: &mut PgConnection,
    backend_id: &str,
    actor: &str,
) -> Result<(), SecretError> {
    record(conn, backend_id, "delete", actor, None).await
}

pub async fn has_secret<'e>(
    executor: impl sqlx::PgExecutor<'e>,
    backend_id: &str,
) -> Result<bool, SecretError> {
    Ok(
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM backend_secrets WHERE backend_id = $1)")
            .bind(backend_id)
            .fetch_one(executor)
            .await?,
    )
}

impl SecretStore {
    #[must_use]
    pub fn new(master: MasterKey) -> Self {
        let host = std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into());
        Self {
            master,
            process: format!("mpac:{host}:{}", std::process::id()),
            cache: Mutex::new(HashMap::new()),
            decrypting: tokio::sync::Mutex::new(()),
        }
    }

    pub fn open(url: &str) -> Result<Self, SecretError> {
        MasterKey::open(url).map(Self::new)
    }

    #[must_use]
    pub fn master_key_id(&self) -> &str {
        self.master.id()
    }

    pub async fn store_in(
        &self,
        conn: &mut PgConnection,
        backend_id: &str,
        plaintext: &str,
        actor: &str,
    ) -> Result<(), SecretError> {
        let data_key: [u8; KEY_BYTES] = random_bytes()?;
        let ciphertext = seal(&data_key, backend_id.as_bytes(), plaintext.as_bytes())?;
        let wrapped = self.master.wrap(backend_id, &data_key).await?;
        let version: i32 = sqlx::query_scalar(
            "INSERT INTO backend_secrets (backend_id, ciphertext, wrapped_key, master_key_id)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (backend_id) DO UPDATE SET
               ciphertext = EXCLUDED.ciphertext,
               wrapped_key = EXCLUDED.wrapped_key,
               master_key_id = EXCLUDED.master_key_id,
               version = backend_secrets.version + 1,
               updated_at = NOW()
             RETURNING version",
        )
        .bind(backend_id)
        .bind(ciphertext)
        .bind(wrapped)
        .bind(self.master.id())
        .fetch_one(&mut *conn)
        .await?;
        let action = if version == 1 { "create" } else { "update" };
        record(conn, backend_id, action, actor, Some(version)).await?;
        self.forget(backend_id);
        Ok(())
    }

    fn cached(&self, backend_id: &str, ciphertext: &[u8]) -> Option<Arc<str>> {
        let cache = self.cache.lock().ok()?;
        cache
            .get(backend_id)
            .filter(|entry| entry.ciphertext == ciphertext && entry.stored_at.elapsed() < CACHE_TTL)
            .map(|entry| Arc::clone(&entry.value))
    }

    pub async fn load(
        &self,
        pool: &PgPool,
        backend_id: &str,
    ) -> Result<Option<Arc<str>>, SecretError> {
        let row = sqlx::query(
            "SELECT ciphertext, wrapped_key, master_key_id, version FROM backend_secrets
             WHERE backend_id = $1",
        )
        .bind(backend_id)
        .fetch_optional(pool)
        .await?;
        let Some(row) = row else {
            self.forget(backend_id);
            return Ok(None);
        };
        let stored = StoredSecret {
            ciphertext: row.try_get("ciphertext")?,
            wrapped_key: row.try_get("wrapped_key")?,
            master_key_id: row.try_get("master_key_id")?,
            version: row.try_get("version")?,
        };
        self.key_for(pool, backend_id, &stored).await.map(Some)
    }

    pub async fn key_for(
        &self,
        pool: &PgPool,
        backend_id: &str,
        stored: &StoredSecret,
    ) -> Result<Arc<str>, SecretError> {
        if let Some(value) = self.cached(backend_id, &stored.ciphertext) {
            return Ok(value);
        }
        let _decrypting = self.decrypting.lock().await;
        if let Some(value) = self.cached(backend_id, &stored.ciphertext) {
            return Ok(value);
        }
        if stored.master_key_id != self.master.id() {
            return Err(SecretError::WrongMasterKey {
                backend: backend_id.to_owned(),
                stored: stored.master_key_id.clone(),
                current: self.master.id().to_owned(),
            });
        }
        let data_key = self.master.unwrap(backend_id, &stored.wrapped_key).await?;
        let plaintext = open(&data_key, backend_id.as_bytes(), &stored.ciphertext)?;
        let value: Arc<str> = String::from_utf8(plaintext)
            .map_err(|_| SecretError::Crypto)?
            .into();
        let mut conn = pool.acquire().await?;
        record(
            &mut conn,
            backend_id,
            "decrypt",
            &self.process,
            Some(stored.version),
        )
        .await?;
        if let Ok(mut cache) = self.cache.lock() {
            cache.insert(
                backend_id.to_owned(),
                CachedKey {
                    ciphertext: stored.ciphertext.clone(),
                    value: Arc::clone(&value),
                    stored_at: Instant::now(),
                },
            );
        }
        Ok(value)
    }

    pub fn forget(&self, backend_id: &str) {
        if let Ok(mut cache) = self.cache.lock() {
            cache.remove(backend_id);
        }
    }

    pub async fn rewrap_from(
        &self,
        pool: &PgPool,
        previous: &MasterKey,
    ) -> Result<u64, SecretError> {
        let mut tx = pool.begin().await?;
        let rows = sqlx::query(
            "SELECT backend_id, wrapped_key, version FROM backend_secrets
             WHERE master_key_id = $1 FOR UPDATE",
        )
        .bind(previous.id())
        .fetch_all(&mut *tx)
        .await?;
        let mut rewrapped = 0;
        for row in rows {
            let backend_id: String = row.try_get("backend_id")?;
            let wrapped: Vec<u8> = row.try_get("wrapped_key")?;
            let data_key = previous.unwrap(&backend_id, &wrapped).await?;
            let replacement = self.master.wrap(&backend_id, &data_key).await?;
            sqlx::query(
                "UPDATE backend_secrets
                 SET wrapped_key = $2, master_key_id = $3, updated_at = NOW()
                 WHERE backend_id = $1",
            )
            .bind(&backend_id)
            .bind(replacement)
            .bind(self.master.id())
            .execute(&mut *tx)
            .await?;
            record(
                &mut tx,
                &backend_id,
                "rewrap",
                &self.process,
                row.try_get("version")?,
            )
            .await?;
            rewrapped += 1;
        }
        tx.commit().await?;
        Ok(rewrapped)
    }
}

#[cfg(test)]
mod tests {
    use wiremock::matchers::{header, method, path};
    use wiremock::{Mock, MockServer, Request, ResponseTemplate};

    use super::*;

    #[test]
    fn sealed_values_are_bound_to_their_associated_data() {
        let key = [7_u8; KEY_BYTES];
        let sealed = seal(&key, b"backend-a", b"secret").unwrap();

        assert_eq!(open(&key, b"backend-a", &sealed).unwrap(), b"secret");
        assert!(open(&key, b"backend-b", &sealed).is_err());
        assert!(open(&[8_u8; KEY_BYTES], b"backend-a", &sealed).is_err());
    }

    #[test]
    fn each_seal_uses_a_new_nonce() {
        let key = [7_u8; KEY_BYTES];

        assert_ne!(
            seal(&key, b"a", b"secret").unwrap(),
            seal(&key, b"a", b"secret").unwrap()
        );
    }

    #[test]
    fn unsupported_and_incomplete_urls_are_rejected() {
        assert!(matches!(
            MasterKey::open("s3://bucket"),
            Err(SecretError::InvalidUrl(_))
        ));
        assert!(matches!(
            MasterKey::open("file:///does/not/exist"),
            Err(SecretError::KeyFile(_))
        ));
    }

    #[test]
    fn kms_urls_keep_the_key_name_as_the_master_key_id() {
        let key =
            MasterKey::open("gcp-kms:projects/P/locations/l/keyRings/Ring/cryptoKeys/Key").unwrap();

        assert_eq!(
            key.id(),
            "gcp-kms:projects/P/locations/l/keyRings/Ring/cryptoKeys/Key"
        );
        assert!(matches!(
            MasterKey::open("aws-kms:not-an-arn"),
            Err(SecretError::InvalidUrl(_))
        ));
    }

    #[tokio::test]
    async fn vault_transit_round_trips_the_data_key_with_its_associated_data() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/transit/encrypt/mpac"))
            .and(header("X-Vault-Token", "token"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(body["associated_data"], BASE64.encode("backend"));
                let plaintext = body["plaintext"].as_str().unwrap();
                ResponseTemplate::new(200)
                    .set_body_json(json!({"data": {"ciphertext": format!("vault:v1:{plaintext}")}}))
            })
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/v1/transit/decrypt/mpac"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                let plaintext = body["ciphertext"]
                    .as_str()
                    .unwrap()
                    .trim_start_matches("vault:v1:");
                ResponseTemplate::new(200).set_body_json(json!({"data": {"plaintext": plaintext}}))
            })
            .mount(&server)
            .await;
        let master = MasterKey {
            id: "vault:test".into(),
            provider: Provider::Vault {
                http: mpac_http::client(VAULT_TIMEOUT).unwrap(),
                address: server.uri(),
                mount: "transit".into(),
                key_name: "mpac".into(),
                token: "token".into(),
            },
        };

        let wrapped = master.wrap("backend", b"data-key").await.unwrap();

        assert!(wrapped.starts_with(b"vault:v1:"));
        assert_eq!(
            master.unwrap("backend", &wrapped).await.unwrap(),
            b"data-key"
        );
    }

    #[tokio::test]
    async fn vault_errors_do_not_reveal_the_token() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(403))
            .mount(&server)
            .await;
        let master = MasterKey {
            id: "vault:test".into(),
            provider: Provider::Vault {
                http: mpac_http::client(VAULT_TIMEOUT).unwrap(),
                address: server.uri(),
                mount: "transit".into(),
                key_name: "mpac".into(),
                token: "super-secret-token".into(),
            },
        };

        let error = master.wrap("backend", b"key").await.unwrap_err();

        assert!(error.to_string().contains("403"));
        assert!(!format!("{error:?}{master:?}").contains("super-secret-token"));
    }
}
