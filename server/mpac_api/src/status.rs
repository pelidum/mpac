use std::fmt::Display;
use std::pin::Pin;

use tokio_stream::Stream;
use tonic::Status;

pub type ResponseStream<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send + 'static>>;

pub fn stream<T: Send + 'static>(items: Vec<T>) -> ResponseStream<T> {
    Box::pin(tokio_stream::iter(items.into_iter().map(Ok)))
}

pub fn internal(error: impl Display) -> Status {
    tracing::error!(%error, "request failed");
    Status::aborted("An internal error occurred")
}
