use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use mpac_blob::BlobStore;
use mpac_proto::FILE_DESCRIPTOR_SET;
use mpac_proto::mpac_server::MpacServer;
use mpac_secrets::SecretStore;
use sqlx::PgPool;
use tokio::sync::oneshot;
use tonic::transport::Server;
use tonic_health::ServingStatus;

use crate::Api;
use crate::auth::{AuthLayer, Authenticator};

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub address: SocketAddr,
    pub api: bool,
    pub max_message_bytes: usize,
    pub reflection: bool,
    pub jwt_secret: Option<Vec<u8>>,
    pub shutdown_grace: Duration,
    pub blobs: Option<BlobStore>,
    pub max_upload_bytes: u64,
    pub secrets: Option<Arc<SecretStore>>,
}

#[derive(Debug, thiserror::Error)]
pub enum ServeError {
    #[error("gRPC transport failed: {0}")]
    Transport(#[from] tonic::transport::Error),
    #[error("reflection setup failed: {0}")]
    Reflection(#[from] tonic_reflection::server::Error),
    #[error("HTTP client setup failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("server task failed: {0}")]
    Join(#[from] tokio::task::JoinError),
}

pub async fn serve(
    pool: PgPool,
    config: ServerConfig,
    shutdown: impl Future<Output = ()> + Send,
) -> Result<(), ServeError> {
    let (reporter, health) = tonic_health::server::health_reporter();
    if config.api {
        reporter.set_serving::<MpacServer<Api>>().await;
    }
    reporter
        .set_service_status("", ServingStatus::Serving)
        .await;

    let mpac = if config.api {
        Some(
            MpacServer::new(
                Api::new(pool.clone())?
                    .with_blobs(config.blobs.clone(), config.max_upload_bytes)
                    .with_secrets(config.secrets.clone()),
            )
            .max_decoding_message_size(config.max_message_bytes)
            .max_encoding_message_size(config.max_message_bytes),
        )
    } else {
        None
    };
    let (reflection_v1, reflection_v1alpha) = if config.reflection {
        let builder = || {
            tonic_reflection::server::Builder::configure()
                .register_encoded_file_descriptor_set(FILE_DESCRIPTOR_SET)
        };
        (
            Some(builder().build_v1()?),
            Some(builder().build_v1alpha()?),
        )
    } else {
        (None, None)
    };

    let authenticator = Arc::new(Authenticator::new(pool, config.jwt_secret.clone()));
    let (stop, stopped) = oneshot::channel::<()>();
    let router = Server::builder()
        .layer(AuthLayer::new(authenticator))
        .add_service(health)
        .add_optional_service(mpac)
        .add_optional_service(reflection_v1)
        .add_optional_service(reflection_v1alpha);
    let mut server = tokio::spawn(router.serve_with_shutdown(config.address, async {
        stopped.await.ok();
    }));
    tracing::info!(address = %config.address, api = config.api, reflection = config.reflection, "starting MPAC gRPC server");

    tokio::select! {
        result = &mut server => return Ok(result??),
        () = shutdown => {}
    }

    tracing::info!("received shutdown signal, draining");
    reporter.set_not_serving::<MpacServer<Api>>().await;
    reporter
        .set_service_status("", ServingStatus::NotServing)
        .await;
    stop.send(()).ok();
    if let Ok(result) = tokio::time::timeout(config.shutdown_grace, &mut server).await {
        return Ok(result??);
    }
    tracing::warn!("shutdown grace period elapsed; aborting in-flight requests");
    server.abort();
    Ok(())
}
