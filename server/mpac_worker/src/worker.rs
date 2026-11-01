use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use mpac_proto::mpac::jobs::job::Kind;
use mpac_queue::signals::{Signal, Signals};
use mpac_queue::{ClaimedJob, FailOutcome};
use tokio::task::{AbortHandle, JoinSet};
use tokio::time::{Instant, Interval, MissedTickBehavior};

use crate::{Context, HandlerResult, Outcome, answer_item, benchmark, blobs, finalize};

const RELEASE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone)]
pub struct WorkerConfig {
    pub id: String,
    pub concurrency: usize,
    pub lease: Duration,
    pub poll: Duration,
    pub reap_every: Duration,
    pub prune_every: Duration,
    pub retain_finished: Duration,
    pub retain_uploads: Duration,
    pub shutdown_grace: Duration,
}

impl WorkerConfig {
    pub fn new(concurrency: usize) -> Self {
        Self {
            id: format!(
                "{}-{}-{}",
                std::env::var("HOSTNAME").unwrap_or_else(|_| "mpac".into()),
                std::process::id(),
                &uuid::Uuid::new_v4().simple().to_string()[..8]
            ),
            concurrency: concurrency.max(1),
            lease: Duration::from_secs(60),
            poll: Duration::from_secs(5),
            reap_every: Duration::from_secs(15),
            prune_every: Duration::from_hours(1),
            retain_finished: Duration::from_hours(24 * 7),
            retain_uploads: Duration::from_hours(24),
            shutdown_grace: Duration::from_secs(25),
        }
    }
}

async fn dispatch(ctx: &Context, job: &ClaimedJob) -> HandlerResult {
    match &job.kind {
        Kind::AnswerItem(work) => answer_item::handle(ctx, job, work).await,
        Kind::FinalizeRun(work) => finalize::handle(ctx, job, &work.run_id).await,
        Kind::BenchmarkStep(work) => benchmark::handle(ctx, job, &work.benchmark_id).await,
    }
}

async fn run_job(ctx: Arc<Context>, config: Arc<WorkerConfig>, job: ClaimedJob) {
    let mut lease = tokio::time::interval(config.lease / 3);
    lease.set_missed_tick_behavior(MissedTickBehavior::Delay);
    lease.reset();
    let mut held_until = Instant::now() + config.lease;
    let work = dispatch(&ctx, &job);
    tokio::pin!(work);
    let result = loop {
        tokio::select! {
            result = &mut work => break Some(result),
            _ = lease.tick() => {
                let renewed_at = Instant::now();
                match mpac_queue::extend_lease(&ctx.pool, &job, config.lease).await {
                    Ok(true) => held_until = renewed_at + config.lease,
                    Ok(false) => break None,
                    Err(error) if Instant::now() >= held_until => {
                        tracing::warn!(job = %job.id, %error, "lease extension failed past lease expiry");
                        break None;
                    }
                    Err(error) => tracing::warn!(job = %job.id, %error, "lease extension failed"),
                }
            }
        }
    };
    let Some(result) = result else {
        tracing::info!(job = %job.id, kind = job.kind.name(), "lease lost; abandoning job");
        return;
    };
    let finished = match result {
        Ok(Outcome::Done) => mpac_queue::complete(&ctx.pool, &job).await.map(|_| ()),
        Ok(Outcome::Committed) => Ok(()),
        Ok(Outcome::LeaseLost) => {
            tracing::info!(job = %job.id, kind = job.kind.name(), "lease lost before commit; result discarded");
            Ok(())
        }
        Ok(Outcome::Reschedule(delay)) => mpac_queue::reschedule(&ctx.pool, &job, delay)
            .await
            .map(|_| ()),
        Err(error) => {
            tracing::error!(job = %job.id, kind = job.kind.name(), %error, "job failed");
            match mpac_queue::fail(&ctx.pool, &job, &error.to_string(), error.retryable()).await {
                Ok(FailOutcome::LeaseLost) => {
                    tracing::warn!(job = %job.id, "lease lost before failure was recorded");
                    Ok(())
                }
                other => other.map(|_| ()),
            }
        }
    };
    if let Err(error) = finished {
        tracing::error!(job = %job.id, %error, "failed to record job outcome; the lease will expire and the job will be retried");
    }
}

fn ticker(period: Duration) -> Interval {
    let mut interval = tokio::time::interval(period);
    interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
    interval
}

struct InFlight {
    tasks: JoinSet<()>,
    by_scope: HashMap<String, Vec<AbortHandle>>,
}

impl InFlight {
    fn spawn(&mut self, ctx: &Arc<Context>, config: &Arc<WorkerConfig>, job: ClaimedJob) {
        let scopes: Vec<String> = [job.run_id.clone(), job.benchmark_id.clone()]
            .into_iter()
            .flatten()
            .collect();
        let handle = self
            .tasks
            .spawn(run_job(Arc::clone(ctx), Arc::clone(config), job));
        for scope in scopes {
            self.by_scope.entry(scope).or_default().push(handle.clone());
        }
    }

    fn cancel(&mut self, scope: &str) {
        if let Some(handles) = self.by_scope.remove(scope) {
            tracing::info!(scope, tasks = handles.len(), "aborting in-flight jobs");
            for handle in handles {
                handle.abort();
            }
        }
    }

    fn reap_finished(&mut self) {
        while self.tasks.try_join_next().is_some() {}
        self.by_scope.retain(|_, handles| {
            handles.retain(|handle| !handle.is_finished());
            !handles.is_empty()
        });
    }
}

async fn claim_into(ctx: &Arc<Context>, config: &Arc<WorkerConfig>, in_flight: &mut InFlight) {
    in_flight.reap_finished();
    let free = config.concurrency.saturating_sub(in_flight.tasks.len());
    if free == 0 {
        return;
    }
    match mpac_queue::claim(
        &ctx.pool,
        &config.id,
        i64::try_from(free).unwrap_or(i64::MAX),
        config.lease,
    )
    .await
    {
        Ok(jobs) => {
            for job in jobs {
                in_flight.spawn(ctx, config, job);
            }
        }
        Err(error) => tracing::error!(%error, "claim failed"),
    }
}

enum Event {
    Signal(Signal),
    TaskDone,
    Reap,
    Prune,
}

async fn handle_event(
    ctx: &Arc<Context>,
    config: &Arc<WorkerConfig>,
    in_flight: &mut InFlight,
    event: Event,
) {
    match event {
        Event::Signal(Signal::Jobs | Signal::Tick) | Event::TaskDone => {
            claim_into(ctx, config, in_flight).await;
        }
        Event::Signal(Signal::Cancel(scope)) => in_flight.cancel(&scope),
        Event::Signal(Signal::BackendChanged(backend)) => {
            tracing::debug!(%backend, "backend changed; clearing its inference state");
            ctx.engine.forget_backend(&backend);
            if let Some(secrets) = &ctx.secrets {
                secrets.forget(&backend);
            }
        }
        Event::Reap => match mpac_queue::reap_expired(&ctx.pool).await {
            Ok(report) if report.requeued + report.failed > 0 => {
                tracing::warn!(?report, "reaped expired leases");
            }
            Ok(_) => {}
            Err(error) => tracing::error!(%error, "reaping failed"),
        },
        Event::Prune => {
            if let Err(error) = mpac_queue::prune_finished(&ctx.pool, config.retain_finished).await
            {
                tracing::error!(%error, "pruning finished jobs failed");
            }
            if let Some(store) = &ctx.blobs {
                match blobs::prune_stale_uploads(&ctx.pool, store, config.retain_uploads).await {
                    Ok(0) => {}
                    Ok(pruned) => tracing::info!(pruned, "pruned abandoned attachment uploads"),
                    Err(error) => tracing::error!(%error, "pruning abandoned uploads failed"),
                }
            }
        }
    }
}

pub async fn run(
    ctx: Arc<Context>,
    config: WorkerConfig,
    shutdown: impl Future<Output = ()> + Send,
) -> Result<(), mpac_queue::QueueError> {
    let config = Arc::new(config);
    let mut signals = Signals::connect(&ctx.pool, config.poll).await?;
    let mut reap = ticker(config.reap_every);
    let mut prune = ticker(config.prune_every);
    let mut in_flight = InFlight {
        tasks: JoinSet::new(),
        by_scope: HashMap::new(),
    };
    tracing::info!(worker = %config.id, concurrency = config.concurrency, "worker started");
    claim_into(&ctx, &config, &mut in_flight).await;
    tokio::pin!(shutdown);
    loop {
        let event = tokio::select! {
            () = &mut shutdown => break,
            signal = signals.next() => Event::Signal(signal),
            Some(_) = in_flight.tasks.join_next(), if !in_flight.tasks.is_empty() => Event::TaskDone,
            _ = reap.tick() => Event::Reap,
            _ = prune.tick() => Event::Prune,
        };
        let handled = handle_event(&ctx, &config, &mut in_flight, event);
        tokio::select! {
            () = &mut shutdown => break,
            () = handled => {}
        }
    }
    tracing::info!(in_flight = in_flight.tasks.len(), "worker draining");
    let drain = async { while in_flight.tasks.join_next().await.is_some() {} };
    if tokio::time::timeout(config.shutdown_grace, drain)
        .await
        .is_err()
    {
        tracing::warn!(
            in_flight = in_flight.tasks.len(),
            "drain grace elapsed; releasing in-flight jobs"
        );
        in_flight.tasks.abort_all();
        while in_flight.tasks.join_next().await.is_some() {}
    }
    match tokio::time::timeout(
        RELEASE_TIMEOUT,
        mpac_queue::release_worker_leases(&ctx.pool, &config.id),
    )
    .await
    {
        Ok(Ok(0)) => {}
        Ok(Ok(released)) => tracing::info!(released, "released leases for other workers"),
        Ok(Err(error)) => tracing::error!(%error, "releasing leases failed; they will expire"),
        Err(_) => tracing::error!("releasing leases timed out; they will expire"),
    }
    Ok(())
}
