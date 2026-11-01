use std::time::Duration;

use sqlx::PgPool;
use sqlx::postgres::PgListener;
use tokio::time::{Interval, MissedTickBehavior};

use crate::{BACKEND_CHANGED_CHANNEL, CANCEL_CHANNEL, JOBS_CHANNEL, QueueError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    Jobs,
    Cancel(String),
    BackendChanged(String),
    Tick,
}

#[derive(Debug)]
pub struct Signals {
    listener: PgListener,
    ticker: Interval,
}

impl Signals {
    pub async fn connect(pool: &PgPool, interval: Duration) -> Result<Self, QueueError> {
        let mut listener = PgListener::connect_with(pool).await?;
        listener
            .listen_all([JOBS_CHANNEL, CANCEL_CHANNEL, BACKEND_CHANGED_CHANNEL])
            .await?;
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        ticker.reset();
        Ok(Self { listener, ticker })
    }

    pub async fn next(&mut self) -> Signal {
        tokio::select! {
            notification = self.listener.recv() => match notification {
                Ok(notification) => {
                    let payload = notification.payload().to_owned();
                    match notification.channel() {
                        CANCEL_CHANNEL => Signal::Cancel(payload),
                        BACKEND_CHANGED_CHANNEL => Signal::BackendChanged(payload),
                        _ => Signal::Jobs,
                    }
                }
                Err(error) => {
                    tracing::warn!(%error, "job listener connection lost; polling until it reconnects");
                    Signal::Tick
                }
            },
            _ = self.ticker.tick() => Signal::Tick,
        }
    }
}
