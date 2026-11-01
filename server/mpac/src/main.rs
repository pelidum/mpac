mod args;

use std::future::Future;
use std::io::IsTerminal;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use clap::Parser;
use mpac_api::server::{ServerConfig, serve};
use mpac_worker::worker::WorkerConfig;
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tracing_subscriber::EnvFilter;

use crate::args::{Cli, Command, Role, ServeArgs};

const SHUTDOWN_GRACE: Duration = Duration::from_secs(25);

fn init_logging() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into());
    let json = match std::env::var("MPAC_LOG_FORMAT").as_deref() {
        Ok("json") => true,
        Ok("text") => false,
        _ => !std::io::stdout().is_terminal(),
    };
    let builder = tracing_subscriber::fmt().with_env_filter(filter);
    if json {
        builder.json().flatten_event(true).init();
    } else {
        builder.with_ansi(std::io::stdout().is_terminal()).init();
    }
}

async fn shutdown_signal() {
    let interrupt = tokio::signal::ctrl_c();
    #[cfg(unix)]
    match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
        Ok(mut terminate) => {
            tokio::select! {
                _ = interrupt => {}
                _ = terminate.recv() => {}
            }
        }
        Err(error) => {
            tracing::warn!(%error, "SIGTERM handler unavailable; only Ctrl-C stops the server");
            interrupt.await.ok();
        }
    }
    #[cfg(not(unix))]
    interrupt.await.ok();
}

async fn stopped(mut receiver: watch::Receiver<bool>) {
    while !*receiver.borrow() {
        if receiver.changed().await.is_err() {
            return;
        }
    }
}

async fn run_serve(args: ServeArgs) -> anyhow::Result<()> {
    let host: IpAddr = args.host.parse().context("--host must be an IP address")?;
    let pool = mpac_db::connect(&args.db.config()).await?;
    if !args.skip_migrate {
        mpac_db::migrate(&pool).await?;
    }
    if let Some(onboarding) = args.onboarding.onboarding() {
        mpac_db::onboarding::onboard_admin(&pool, &onboarding).await?;
    }
    let blobs = args.blobs.open()?;
    let secrets = args.secrets()?.map(Arc::new);
    let (stop, stopping) = watch::channel(false);
    let signal_stop = stop.clone();
    tokio::spawn(async move {
        shutdown_signal().await;
        signal_stop.send(true).ok();
    });

    let worker = if args.roles.contains(&Role::Worker) {
        let context = Arc::new(
            mpac_worker::Context::new(pool.clone())?
                .with_blobs(blobs.clone())
                .with_secrets(secrets.clone()),
        );
        let config = WorkerConfig::new(args.worker_concurrency);
        Some(tokio::spawn(mpac_worker::worker::run(
            context,
            config,
            stopped(stopping.clone()),
        )))
    } else {
        None
    };
    let config = ServerConfig {
        address: SocketAddr::new(host, args.port),
        api: args.roles.contains(&Role::Api),
        max_message_bytes: args.max_message_bytes(blobs.is_some()),
        reflection: args.reflection,
        jwt_secret: args.jwt_secret.map(String::into_bytes),
        shutdown_grace: SHUTDOWN_GRACE,
        blobs,
        max_upload_bytes: args.max_attachment_bytes,
        secrets,
    };
    let server = serve(pool.clone(), config, stopped(stopping.clone()));
    let result = match worker {
        Some(worker) => supervise(server, worker, &stop).await,
        None => server.await.map_err(Into::into),
    };
    pool.close().await;
    result
}

async fn supervise<S, SE, WE>(
    server: S,
    mut worker: JoinHandle<Result<(), WE>>,
    stop: &watch::Sender<bool>,
) -> anyhow::Result<()>
where
    S: Future<Output = Result<(), SE>>,
    SE: Into<anyhow::Error>,
    WE: Into<anyhow::Error>,
{
    tokio::pin!(server);
    tokio::select! {
        result = &mut server => {
            stop.send(true).ok();
            let worker = worker.await;
            result.map_err(Into::into)?;
            worker?.map_err(Into::into)
        }
        exit = &mut worker => {
            let stopping = *stop.borrow();
            if !stopping {
                tracing::error!("inference worker stopped unexpectedly; shutting down");
                stop.send(true).ok();
            }
            server.await.map_err(Into::into)?;
            exit?.map_err(Into::into)?;
            if stopping {
                Ok(())
            } else {
                anyhow::bail!("inference worker stopped unexpectedly")
            }
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_logging();
    match Cli::parse().command {
        Command::Serve(args) => run_serve(args).await?,
        Command::Worker(mut args) => {
            args.roles = vec![Role::Worker];
            run_serve(args).await?;
        }
        Command::Migrate(args) => {
            let pool = mpac_db::connect(&args.db.config()).await?;
            mpac_db::migrate(&pool).await?;
        }
        Command::RotateSecretKey(args) => {
            let pool = mpac_db::connect(&args.db.config()).await?;
            let current = mpac_secrets::SecretStore::open(&args.secret_key_url)?;
            let previous = mpac_secrets::MasterKey::open(&args.previous_secret_key_url)?;
            let rewrapped = current.rewrap_from(&pool, &previous).await?;
            tracing::info!(rewrapped, "rewrapped provider keys");
        }
        Command::Onboard(args) => {
            let onboarding = args
                .onboarding
                .onboarding()
                .context("--admin_onboarding_id (or MPAC_ADMIN_ONBOARDING_ID) is required")?;
            let pool = mpac_db::connect(&args.db.config()).await?;
            mpac_db::onboarding::onboard_admin(&pool, &onboarding).await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn server(stop: &watch::Sender<bool>) -> impl Future<Output = anyhow::Result<()>> + use<> {
        let stopping = stop.subscribe();
        async move {
            stopped(stopping).await;
            Ok(())
        }
    }

    #[tokio::test]
    async fn server_failure_stops_the_worker_and_fails_the_process() {
        let (stop, _) = watch::channel(false);
        let stopping = stop.subscribe();
        let worker = tokio::spawn(async move {
            stopped(stopping).await;
            Ok::<(), anyhow::Error>(())
        });

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            supervise(
                async { Err::<(), _>(anyhow::anyhow!("address in use")) },
                worker,
                &stop,
            ),
        )
        .await
        .unwrap();

        assert!(*stop.borrow());
        assert_eq!(result.unwrap_err().to_string(), "address in use");
    }

    #[tokio::test]
    async fn worker_failure_stops_the_server_and_fails_the_process() {
        let (stop, _) = watch::channel(false);
        let worker = tokio::spawn(async { Err::<(), _>(anyhow::anyhow!("listen failed")) });

        let result = supervise(server(&stop), worker, &stop).await;

        assert!(*stop.borrow());
        assert_eq!(result.unwrap_err().to_string(), "listen failed");
    }

    #[tokio::test]
    async fn worker_returning_early_without_error_still_fails_the_process() {
        let (stop, _) = watch::channel(false);
        let worker = tokio::spawn(async { Ok::<(), anyhow::Error>(()) });

        let result = supervise(server(&stop), worker, &stop).await;

        assert!(*stop.borrow());
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn requested_shutdown_is_clean() {
        let (stop, _) = watch::channel(false);
        let worker_stop = stop.subscribe();
        let worker = tokio::spawn(async move {
            stopped(worker_stop).await;
            Ok::<(), anyhow::Error>(())
        });
        stop.send(true).unwrap();

        supervise(server(&stop), worker, &stop).await.unwrap();
    }
}
