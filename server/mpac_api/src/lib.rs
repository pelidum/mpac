mod answers;
pub mod attachments;
pub mod auth;
mod backend_urls;
mod backends;
mod benchmarks;
mod create;
mod model_catalog;
mod models;
mod runs;
pub mod server;
mod service;
mod status;
mod test_defs;
mod test_items;
mod users;

use std::sync::Arc;
use std::time::Duration;

use mpac_blob::BlobStore;
use mpac_proto::Backend;
use mpac_secrets::{BackendRecord, SecretError, SecretStore};
use prost::Message as _;
use sqlx::PgPool;

use mpac_db::rate_limits::RateLimitCache;

use crate::models::HuggingFaceCache;

const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug)]
pub struct Api {
    pool: PgPool,
    http: reqwest::Client,
    rate_limits: RateLimitCache,
    hugging_face: HuggingFaceCache,
    blobs: Option<BlobStore>,
    max_upload_bytes: u64,
    secrets: Option<Arc<SecretStore>>,
}

impl Api {
    pub fn new(pool: PgPool) -> reqwest::Result<Self> {
        Ok(Self {
            pool,
            http: mpac_http::client(HTTP_TIMEOUT)?,
            rate_limits: RateLimitCache::default(),
            hugging_face: HuggingFaceCache::default(),
            blobs: None,
            max_upload_bytes: attachments::DEFAULT_MAX_UPLOAD_BYTES,
            secrets: None,
        })
    }

    #[must_use]
    pub fn with_blobs(mut self, blobs: Option<BlobStore>, max_upload_bytes: u64) -> Self {
        self.blobs = blobs;
        self.max_upload_bytes = max_upload_bytes;
        self
    }

    #[must_use]
    pub fn with_secrets(mut self, secrets: Option<Arc<SecretStore>>) -> Self {
        self.secrets = secrets;
        self
    }

    pub(crate) async fn backend_with_key(
        &self,
        record: BackendRecord,
    ) -> Result<Backend, tonic::Status> {
        let mut backend =
            Backend::decode(record.proto_bytes.as_slice()).map_err(status::internal)?;
        let Some(stored) = &record.secret else {
            return Ok(backend);
        };
        let Some(secrets) = &self.secrets else {
            tracing::error!(backend = %record.id, "backend has a provider key but no master key is set");
            return Err(tonic::Status::failed_precondition(format!(
                "Backend {} has a provider key, but this server has no master key.",
                record.id
            )));
        };
        let key = secrets
            .key_for(&self.pool, &record.id, stored)
            .await
            .map_err(|error| {
                tracing::error!(backend = %record.id, %error, "unable to load provider key");
                let message = format!("The provider key for backend {} is unavailable.", record.id);
                match error {
                    SecretError::Query(_) => tonic::Status::internal(message),
                    SecretError::Kms(_) | SecretError::Vault(_) => {
                        tonic::Status::unavailable(message)
                    }
                    _ => tonic::Status::failed_precondition(message),
                }
            })?;
        backend.api_key = key.to_string();
        Ok(backend)
    }
}
