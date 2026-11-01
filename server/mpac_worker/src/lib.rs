pub mod answer_item;
pub mod benchmark;
pub mod blobs;
pub mod finalize;
pub mod metrics;
pub mod runs;
pub mod worker;

use std::sync::Arc;
use std::time::Duration;

use mpac_blob::{BlobCache, BlobStore};
use mpac_db::rate_limits::RateLimitCache;
use mpac_inference::Engine;
use mpac_queue::ClaimedJob;
use mpac_secrets::SecretStore;
use sqlx::{PgPool, Postgres, Transaction};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

const BLOB_CACHE_BYTES: usize = 256 * 1024 * 1024;
const ATTACHMENT_BUDGET_MIB: u32 = 1024;

#[derive(Debug)]
pub struct Context {
    pub pool: PgPool,
    pub engine: Engine,
    pub limits: RateLimitCache,
    pub blobs: Option<BlobStore>,
    pub blob_cache: BlobCache,
    pub secrets: Option<Arc<SecretStore>>,
    attachment_budget: Arc<Semaphore>,
}

impl Context {
    pub fn new(pool: PgPool) -> reqwest::Result<Self> {
        Ok(Self {
            pool,
            engine: Engine::new()?,
            limits: RateLimitCache::default(),
            blobs: None,
            blob_cache: BlobCache::new(BLOB_CACHE_BYTES),
            secrets: None,
            attachment_budget: Arc::new(Semaphore::new(ATTACHMENT_BUDGET_MIB as usize)),
        })
    }

    #[must_use]
    pub fn with_blobs(mut self, blobs: Option<BlobStore>) -> Self {
        self.blobs = blobs;
        self
    }

    #[must_use]
    pub fn with_secrets(mut self, secrets: Option<Arc<SecretStore>>) -> Self {
        self.secrets = secrets;
        self
    }
}

impl Context {
    pub(crate) async fn reserve_attachment_memory(
        &self,
        file_size_kib: i64,
    ) -> Result<OwnedSemaphorePermit, tokio::sync::AcquireError> {
        let mib = u32::try_from(file_size_kib / 1024).unwrap_or(u32::MAX);
        let working_copies = mib.saturating_mul(3).saturating_add(1);
        Arc::clone(&self.attachment_budget)
            .acquire_many_owned(working_copies.min(ATTACHMENT_BUDGET_MIB))
            .await
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Done,
    Committed,
    LeaseLost,
    Reschedule(Duration),
}

#[derive(Debug, thiserror::Error)]
pub enum HandlerError {
    #[error("database error: {0}")]
    Database(#[from] sqlx::Error),
    #[error(transparent)]
    Crud(#[from] mpac_db::crud::CrudError),
    #[error(transparent)]
    Secret(#[from] mpac_secrets::SecretError),
    #[error(transparent)]
    Queue(#[from] mpac_queue::QueueError),
    #[error(transparent)]
    Runs(#[from] runs::RunError),
    #[error(transparent)]
    Limits(#[from] mpac_db::rate_limits::LimitError),
    #[error(transparent)]
    Json(#[from] mpac_proto::json::JsonError),
    #[error(transparent)]
    Blob(#[from] mpac_blob::BlobError),
    #[error("{0}")]
    Invalid(String),
}

impl HandlerError {
    pub fn retryable(&self) -> bool {
        !matches!(self, HandlerError::Invalid(_))
    }
}

pub type HandlerResult = Result<Outcome, HandlerError>;

pub async fn fenced(
    ctx: &Context,
    job: &ClaimedJob,
) -> Result<Option<Transaction<'static, Postgres>>, HandlerError> {
    let mut tx = ctx.pool.begin().await?;
    if mpac_queue::complete_in(&mut tx, job).await?.recorded {
        Ok(Some(tx))
    } else {
        Ok(None)
    }
}
