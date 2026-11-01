pub mod cache;
pub mod sniff;

use std::sync::Arc;
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use futures::StreamExt;
use http::Method;
use object_store::aws::{AmazonS3Builder, AmazonS3ConfigKey};
use object_store::gcp::{GoogleCloudStorageBuilder, GoogleConfigKey};
use object_store::memory::InMemory;
use object_store::path::Path;
use object_store::signer::Signer;
use object_store::{ClientConfigKey, ObjectStore, ObjectStoreExt, PutPayload};
use sha2::{Digest, Sha256};
use url::Url;

pub use cache::BlobCache;
pub use sniff::{SIGNATURE_BYTES, Sniffed, Utf8Validator, sniff, sniff_head};

const TRANSFER_TIMEOUT: &str = "15m";
pub const BLOB_URL_ENV: &str = "MPAC_BLOB_URL";
pub const PUBLIC_ENDPOINT_ENV: &str = "MPAC_BLOB_PUBLIC_ENDPOINT";

#[derive(Debug, thiserror::Error)]
pub enum BlobError {
    #[error("blob store request failed: {0}")]
    Store(#[from] object_store::Error),
    #[error("blob {0} does not exist")]
    NotFound(String),
    #[error("blob {key} is {size} bytes, over the {limit} byte limit")]
    TooLarge { key: String, size: u64, limit: u64 },
    #[error("invalid blob url: {0}")]
    Url(#[from] url::ParseError),
    #[error("unsupported blob url scheme {0:?}; use s3:// or gs://")]
    UnsupportedScheme(String),
}

fn not_found(key: &str, error: object_store::Error) -> BlobError {
    match error {
        object_store::Error::NotFound { .. } => BlobError::NotFound(key.to_owned()),
        other => BlobError::Store(other),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inspected {
    pub sha256: String,
    pub size: u64,
    pub head: Vec<u8>,
    pub utf8: bool,
}

#[derive(Debug, Clone)]
pub struct BlobStore {
    store: Arc<dyn ObjectStore>,
    signer: Arc<dyn Signer>,
}

pub fn blob_key(sha256: &str) -> String {
    format!("blobs/{sha256}")
}

pub fn upload_key(id: &str) -> String {
    format!("tmp/{id}")
}

impl BlobStore {
    pub fn from_env() -> Result<Option<Self>, BlobError> {
        let var = |name: &str| std::env::var(name).ok().filter(|value| !value.is_empty());
        var(BLOB_URL_ENV)
            .map(|url| Self::open(&url, var(PUBLIC_ENDPOINT_ENV).as_deref()))
            .transpose()
    }

    pub fn open(url: &str, public_endpoint: Option<&str>) -> Result<Self, BlobError> {
        mpac_http::install_crypto_provider();
        match Url::parse(url)?.scheme() {
            "s3" => {
                let store = Arc::new(
                    AmazonS3Builder::from_env()
                        .with_url(url)
                        .with_config(
                            AmazonS3ConfigKey::Client(ClientConfigKey::Timeout),
                            TRANSFER_TIMEOUT,
                        )
                        .build()?,
                );
                let signer: Arc<dyn Signer> = match public_endpoint {
                    Some(endpoint) => Arc::new(
                        AmazonS3Builder::from_env()
                            .with_url(url)
                            .with_endpoint(endpoint)
                            .build()?,
                    ),
                    None => store.clone(),
                };
                Ok(Self { store, signer })
            }
            "gs" => {
                let store = Arc::new(
                    GoogleCloudStorageBuilder::from_env()
                        .with_url(url)
                        .with_config(
                            GoogleConfigKey::Client(ClientConfigKey::Timeout),
                            TRANSFER_TIMEOUT,
                        )
                        .build()?,
                );
                Ok(Self {
                    signer: store.clone(),
                    store,
                })
            }
            other => Err(BlobError::UnsupportedScheme(other.to_owned())),
        }
    }

    pub fn in_memory() -> Self {
        mpac_http::install_crypto_provider();
        let signer = AmazonS3Builder::new()
            .with_bucket_name("in-memory")
            .with_region("us-east-1")
            .with_access_key_id("in-memory")
            .with_secret_access_key("in-memory")
            .build()
            .map_or_else(
                |error| panic!("static S3 signer config is invalid: {error}"),
                Arc::new,
            );
        Self {
            store: Arc::new(InMemory::new()),
            signer,
        }
    }

    pub async fn put(&self, key: &str, blob: Bytes) -> Result<(), BlobError> {
        self.store
            .put(&Path::from(key), PutPayload::from_bytes(blob))
            .await?;
        Ok(())
    }

    pub async fn get(&self, key: &str) -> Result<Bytes, BlobError> {
        let result = self
            .store
            .get(&Path::from(key))
            .await
            .map_err(|error| not_found(key, error))?;
        Ok(result.bytes().await?)
    }

    pub async fn size(&self, key: &str) -> Result<u64, BlobError> {
        Ok(self
            .store
            .head(&Path::from(key))
            .await
            .map_err(|error| not_found(key, error))?
            .size)
    }

    pub async fn copy(&self, from: &str, to: &str) -> Result<(), BlobError> {
        self.store
            .copy(&Path::from(from), &Path::from(to))
            .await
            .map_err(|error| not_found(from, error))
    }

    pub async fn older_objects(&self, age: Duration) -> Result<Vec<String>, BlobError> {
        let cutoff = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .saturating_sub(age)
            .as_secs();
        let mut objects = self.store.list(None);
        let mut keys = Vec::new();
        while let Some(object) = objects.next().await {
            let object = object?;
            let key = object.location.to_string();
            if (key.starts_with("tmp/") || key.starts_with("staging/") || key.starts_with("blobs/"))
                && u64::try_from(object.last_modified.timestamp()).unwrap_or_default() < cutoff
            {
                keys.push(key);
            }
        }
        Ok(keys)
    }

    pub async fn get_limited(&self, key: &str, limit: u64) -> Result<Bytes, BlobError> {
        let result = self
            .store
            .get(&Path::from(key))
            .await
            .map_err(|error| not_found(key, error))?;
        if result.meta.size > limit {
            return Err(BlobError::TooLarge {
                key: key.to_owned(),
                size: result.meta.size,
                limit,
            });
        }
        let mut stream = result.into_stream();
        let mut content = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            let size = content.len() as u64 + chunk.len() as u64;
            if size > limit {
                return Err(BlobError::TooLarge {
                    key: key.to_owned(),
                    size,
                    limit,
                });
            }
            content.extend_from_slice(&chunk);
        }
        Ok(Bytes::from(content))
    }

    pub async fn delete(&self, key: &str) -> Result<(), BlobError> {
        match self.store.delete(&Path::from(key)).await {
            Ok(()) | Err(object_store::Error::NotFound { .. }) => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    pub async fn sign_get(&self, key: &str, ttl: Duration) -> Result<String, BlobError> {
        Ok(self
            .signer
            .signed_url(Method::GET, &Path::from(key), ttl)
            .await?
            .into())
    }

    pub async fn sign_put(&self, key: &str, ttl: Duration) -> Result<String, BlobError> {
        Ok(self
            .signer
            .signed_url(Method::PUT, &Path::from(key), ttl)
            .await?
            .into())
    }

    pub async fn inspect(&self, key: &str, limit: u64) -> Result<Inspected, BlobError> {
        let size = self.size(key).await?;
        if size > limit {
            return Err(BlobError::TooLarge {
                key: key.to_owned(),
                size,
                limit,
            });
        }
        let mut stream = self
            .store
            .get(&Path::from(key))
            .await
            .map_err(|error| not_found(key, error))?
            .into_stream();
        let mut hasher = Sha256::new();
        let mut head = Vec::with_capacity(SIGNATURE_BYTES);
        let mut utf8 = Utf8Validator::default();
        let mut read: u64 = 0;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            read += chunk.len() as u64;
            if read > limit {
                return Err(BlobError::TooLarge {
                    key: key.to_owned(),
                    size: read,
                    limit,
                });
            }
            hasher.update(&chunk);
            utf8.feed(&chunk);
            let wanted = SIGNATURE_BYTES.saturating_sub(head.len()).min(chunk.len());
            head.extend_from_slice(&chunk[..wanted]);
        }
        Ok(Inspected {
            sha256: hex::encode(hasher.finalize()),
            size: read,
            head,
            utf8: utf8.is_valid(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn inspect_hashes_streams_and_keeps_the_signature_head() {
        let store = BlobStore::in_memory();
        let content: Vec<u8> = (0..20_000u32).map(|i| b"abc"[i as usize % 3]).collect();
        store
            .put("tmp/x", Bytes::from(content.clone()))
            .await
            .unwrap();

        let inspected = store.inspect("tmp/x", 1 << 20).await.unwrap();

        assert_eq!(inspected.sha256, hex::encode(Sha256::digest(&content)));
        assert_eq!(inspected.size, 20_000);
        assert_eq!(inspected.head, content[..SIGNATURE_BYTES]);
        assert!(inspected.utf8);
    }

    #[tokio::test]
    async fn inspect_rejects_objects_over_the_limit() {
        let store = BlobStore::in_memory();
        store
            .put("tmp/big", Bytes::from_static(b"0123456789"))
            .await
            .unwrap();

        let error = store.inspect("tmp/big", 9).await.unwrap_err();

        assert!(matches!(error, BlobError::TooLarge { size: 10, .. }));
    }

    #[tokio::test]
    async fn missing_objects_are_not_found_and_deletes_are_idempotent() {
        let store = BlobStore::in_memory();

        assert!(matches!(
            store.get("blobs/none").await,
            Err(BlobError::NotFound(_))
        ));
        store.delete("blobs/none").await.unwrap();
    }

    #[tokio::test]
    async fn copy_then_delete_moves_an_upload() {
        let store = BlobStore::in_memory();
        store
            .put(&upload_key("u"), Bytes::from_static(b"data"))
            .await
            .unwrap();

        store
            .copy(&upload_key("u"), &blob_key("sha"))
            .await
            .unwrap();
        store.delete(&upload_key("u")).await.unwrap();

        assert_eq!(store.get(&blob_key("sha")).await.unwrap(), "data");
        assert!(store.size(&upload_key("u")).await.is_err());
    }

    #[tokio::test]
    async fn bounded_reads_reject_before_materializing_large_content() {
        let store = BlobStore::in_memory();
        store
            .put("blobs/large", Bytes::from_static(b"12345"))
            .await
            .unwrap();
        assert!(matches!(
            store.get_limited("blobs/large", 4).await,
            Err(BlobError::TooLarge {
                size: 5,
                limit: 4,
                ..
            })
        ));
        assert_eq!(store.get_limited("blobs/large", 5).await.unwrap(), "12345");
    }

    #[test]
    fn rejects_unknown_schemes() {
        assert!(matches!(
            BlobStore::open("ftp://bucket", None),
            Err(BlobError::UnsupportedScheme(scheme)) if scheme == "ftp"
        ));
    }

    #[tokio::test]
    async fn public_endpoint_is_used_only_for_signing() {
        let blobs = BlobStore::open("s3://mpac", Some("https://files.example.com")).unwrap();

        let url = blobs
            .sign_get(&blob_key("abc"), Duration::from_secs(60))
            .await
            .unwrap();

        assert!(
            url.starts_with("https://files.example.com/mpac/blobs/abc?"),
            "{url}"
        );
        assert!(url.contains("X-Amz-Signature="));
    }
}
