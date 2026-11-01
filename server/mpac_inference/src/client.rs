use std::time::{Duration, Instant};

use futures::StreamExt;
use mpac_proto::TokenLogprob;
use mpac_proto::token_logprob::TopLogprob;
use reqwest::StatusCode;
use serde_json::{Map, Value};

use crate::pystr::{str_of, strip};

const MAX_RETRIES: u32 = 2;
const INITIAL_RETRY_DELAY: f64 = 0.5;
const MAX_RETRY_DELAY: f64 = 8.0;
const MAX_RETRY_AFTER: f64 = 60.0;
const STREAM_SERVER_ERROR_RETRIES: u32 = 2;
const STREAM_SERVER_ERROR_BACKOFF: f64 = 0.5;
const REASONING_FIELDS: [&str; 3] = ["reasoning_content", "thinking", "reasoning"];
const CHARS_PER_TOKEN: f64 = 4.0;
const LIVE_REASONING_MIN_FRACTION: f64 = 0.5;
const PLACEHOLDER_API_KEY: &str = "NOT_A_REAL_API_KEY";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ChatError {
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    Status(String),
    #[error("{0}")]
    Connection(String),
    #[error("{message}")]
    Stream { message: String, server_error: bool },
    #[error("{0}")]
    Decode(String),
}

impl ChatError {
    pub fn is_bad_request(&self) -> bool {
        matches!(self, ChatError::BadRequest(_))
    }

    fn connection() -> Self {
        ChatError::Connection("Connection error.".into())
    }

    fn timed_out() -> Self {
        ChatError::Connection("Request timed out.".into())
    }
}

#[derive(Clone)]
pub struct Endpoint {
    pub base_url: String,
    pub api_key: String,
    pub request_timeout: Duration,
}

impl std::fmt::Debug for Endpoint {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Endpoint")
            .field("base_url", &self.base_url)
            .field("request_timeout", &self.request_timeout)
            .finish_non_exhaustive()
    }
}

impl Endpoint {
    fn url(&self) -> Option<String> {
        let base = self.base_url.trim_end_matches('/');
        (!base.is_empty()).then(|| format!("{base}/chat/completions"))
    }

    fn api_key(&self) -> &str {
        if self.api_key.is_empty() {
            PLACEHOLDER_API_KEY
        } else {
            &self.api_key
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct StreamResult {
    pub raw_response: String,
    pub reasoning: String,
    pub ttft: f64,
    pub output_tps: f64,
    pub task_duration: f64,
    pub usage: Option<Value>,
    pub logprobs: Option<Vec<TokenLogprob>>,
    pub deltas: i64,
}

#[derive(Debug, Clone)]
pub struct ChatClient {
    http: reqwest::Client,
}

pub fn int_field(object: Option<&Value>, name: &str) -> i64 {
    match object.and_then(|o| o.get(name)) {
        Some(Value::Number(number)) if number.is_i64() || number.is_u64() => {
            number.as_i64().unwrap_or(i64::MAX)
        }
        _ => 0,
    }
}

fn status_error(status: StatusCode, body: &str) -> ChatError {
    let text = strip(body);
    let rendered = serde_json::from_str::<Value>(text)
        .map_or_else(|_| text.to_owned(), |value| str_of(&value));
    let message = format!("Error code: {} - {rendered}", status.as_u16());
    if status == StatusCode::BAD_REQUEST {
        ChatError::BadRequest(message)
    } else {
        ChatError::Status(message)
    }
}

fn should_retry(response: &reqwest::Response) -> bool {
    match response
        .headers()
        .get("x-should-retry")
        .and_then(|v| v.to_str().ok())
    {
        Some("true") => return true,
        Some("false") => return false,
        _ => {}
    }
    let status = response.status();
    matches!(status.as_u16(), 408 | 409 | 429) || status.is_server_error()
}

fn retry_after(response: Option<&reqwest::Response>) -> Option<f64> {
    let headers = response?.headers();
    let millis = headers
        .get("retry-after-ms")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<f64>().ok())
        .map(|ms| ms / 1000.0);
    let seconds = || {
        headers
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<f64>().ok())
    };
    millis
        .or_else(seconds)
        .filter(|delay| *delay > 0.0 && *delay <= MAX_RETRY_AFTER)
}

fn retry_delay(attempt: u32, response: Option<&reqwest::Response>) -> Duration {
    if let Some(delay) = retry_after(response) {
        return Duration::from_secs_f64(delay);
    }
    let base = (INITIAL_RETRY_DELAY * 2_f64.powi(i32::try_from(attempt).unwrap_or(30)))
        .min(MAX_RETRY_DELAY);
    let jitter = 1.0 - 0.25 * rand::random::<f64>();
    Duration::from_secs_f64(base * jitter)
}

fn reasoning_text(delta: &Value) -> Option<&str> {
    REASONING_FIELDS.iter().find_map(|field| {
        delta
            .get(*field)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
    })
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::Bool(flag)) => Some(f64::from(u8::from(*flag))),
        _ => None,
    }
}

fn token_logprobs(choice: &Value) -> Option<Vec<TokenLogprob>> {
    let content = choice.get("logprobs")?.get("content")?.as_array()?;
    Some(
        content
            .iter()
            .filter_map(|entry| {
                let token = entry.get("token")?.as_str()?;
                let logprob = number(entry.get("logprob"))?;
                let top_logprobs = entry
                    .get("top_logprobs")
                    .and_then(Value::as_array)
                    .map(|alts| {
                        alts.iter()
                            .filter_map(|alt| {
                                Some(TopLogprob {
                                    token: alt.get("token")?.as_str()?.to_owned(),
                                    logprob: number(alt.get("logprob"))?,
                                })
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                Some(TokenLogprob {
                    token: token.to_owned(),
                    logprob,
                    top_logprobs,
                })
            })
            .collect(),
    )
}

#[derive(Debug, Default)]
struct StreamState {
    content: String,
    reasoning: String,
    usage: Option<Value>,
    first: Option<Instant>,
    last: Option<Instant>,
    first_content: Option<Instant>,
    last_content: Option<Instant>,
    deltas: i64,
    logprobs: Vec<TokenLogprob>,
    saw_logprobs: bool,
}

impl StreamState {
    fn absorb(&mut self, chunk: &Value) {
        if let Some(usage) = chunk.get("usage").filter(|u| !u.is_null()) {
            self.usage = Some(usage.clone());
        }
        let Some(choice) = chunk
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
        else {
            return;
        };
        let Some(delta) = choice.get("delta").filter(|d| !d.is_null()) else {
            return;
        };
        let text = delta
            .get("content")
            .and_then(Value::as_str)
            .filter(|t| !t.is_empty());
        let reasoning = reasoning_text(delta);
        if text.is_none() && reasoning.is_none() {
            return;
        }
        let now = Instant::now();
        self.first.get_or_insert(now);
        self.last = Some(now);
        self.deltas += 1;
        if let Some(text) = text {
            self.first_content.get_or_insert(now);
            self.last_content = Some(now);
            self.content.push_str(text);
            if let Some(tokens) = token_logprobs(choice) {
                self.saw_logprobs = true;
                self.logprobs.extend(tokens);
            }
        }
        if let Some(reasoning) = reasoning {
            self.reasoning.push_str(reasoning);
        }
    }

    fn finish(self, started: Instant) -> StreamResult {
        let ended = Instant::now();
        let mut window = (self.first, self.last);
        #[allow(clippy::cast_precision_loss)]
        let n_out = match &self.usage {
            Some(usage) => {
                let mut n_out = int_field(Some(usage), "completion_tokens");
                let reasoning_tokens =
                    int_field(usage.get("completion_tokens_details"), "reasoning_tokens");
                let streamed = self.reasoning.chars().count() as f64 / CHARS_PER_TOKEN;
                let live = streamed >= LIVE_REASONING_MIN_FRACTION * reasoning_tokens as f64;
                if reasoning_tokens != 0 && !live {
                    n_out = (n_out - reasoning_tokens).max(0);
                    window = (self.first_content, self.last_content);
                }
                n_out
            }
            None => self.deltas,
        };
        #[allow(clippy::cast_precision_loss)]
        let output_tps = match window {
            (Some(start), Some(end)) if end > start && n_out >= 2 => {
                (n_out - 1) as f64 / (end - start).as_secs_f64()
            }
            _ => 0.0,
        };
        StreamResult {
            raw_response: self.content,
            reasoning: strip(&self.reasoning).to_owned(),
            ttft: self
                .first
                .map_or(0.0, |first| (first - started).as_secs_f64()),
            output_tps,
            task_duration: (ended - started).as_secs_f64(),
            usage: self.usage,
            logprobs: self.saw_logprobs.then_some(self.logprobs),
            deltas: self.deltas,
        }
    }
}

fn stream_error(chunk: &Value) -> Option<ChatError> {
    let error = chunk
        .get("error")
        .filter(|e| !e.is_null() && *e != &Value::Bool(false))?;
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .filter(|m| !m.is_empty())
        .unwrap_or("An error occurred during streaming")
        .to_owned();
    let server_error = error.get("type").and_then(Value::as_str) == Some("server_error")
        || error
            .get("code")
            .and_then(Value::as_i64)
            .is_some_and(|code| code >= 500);
    Some(ChatError::Stream {
        message,
        server_error,
    })
}

impl ChatClient {
    pub fn new() -> reqwest::Result<Self> {
        mpac_http::install_crypto_provider();
        Ok(Self {
            http: reqwest::Client::builder()
                .user_agent(mpac_http::USER_AGENT)
                .build()?,
        })
    }

    async fn send(
        &self,
        endpoint: &Endpoint,
        body: &Value,
    ) -> Result<reqwest::Response, ChatError> {
        let url = endpoint.url().ok_or_else(ChatError::connection)?;
        let mut attempt = 0;
        loop {
            let request = self
                .http
                .post(&url)
                .bearer_auth(endpoint.api_key())
                .json(body)
                .send();
            let outcome = match tokio::time::timeout(endpoint.request_timeout, request).await {
                Err(_) => Err(ChatError::timed_out()),
                Ok(Err(error)) if error.is_timeout() => Err(ChatError::timed_out()),
                Ok(Err(_)) => Err(ChatError::connection()),
                Ok(Ok(response)) => Ok(response),
            };
            match outcome {
                Ok(response) if response.status().is_success() => return Ok(response),
                Ok(response) => {
                    if attempt < MAX_RETRIES && should_retry(&response) {
                        tokio::time::sleep(retry_delay(attempt, Some(&response))).await;
                        attempt += 1;
                        continue;
                    }
                    let status = response.status();
                    let text = response.text().await.unwrap_or_default();
                    return Err(status_error(status, &text));
                }
                Err(error) => {
                    if attempt < MAX_RETRIES {
                        tokio::time::sleep(retry_delay(attempt, None)).await;
                        attempt += 1;
                        continue;
                    }
                    return Err(error);
                }
            }
        }
    }

    pub async fn stream(
        &self,
        endpoint: &Endpoint,
        model: &str,
        messages: &[Value],
        params: &Map<String, Value>,
    ) -> Result<StreamResult, ChatError> {
        let mut body = Map::new();
        body.insert("messages".into(), Value::Array(messages.to_vec()));
        body.insert("model".into(), Value::String(model.to_owned()));
        body.insert("stream".into(), Value::Bool(true));
        body.insert(
            "stream_options".into(),
            serde_json::json!({"include_usage": true}),
        );
        for (key, value) in params {
            body.insert(key.clone(), value.clone());
        }
        let started = Instant::now();
        let response = self.send(endpoint, &Value::Object(body)).await?;
        let mut bytes = response.bytes_stream();
        let mut buffer: Vec<u8> = Vec::new();
        let mut data = String::new();
        let mut state = StreamState::default();
        loop {
            let next = tokio::time::timeout(endpoint.request_timeout, bytes.next())
                .await
                .map_err(|_| ChatError::timed_out())?;
            let Some(chunk) = next else {
                break;
            };
            let chunk = chunk.map_err(|error| {
                if error.is_timeout() {
                    ChatError::timed_out()
                } else {
                    ChatError::connection()
                }
            })?;
            buffer.extend_from_slice(&chunk);
            while let Some(newline) = buffer.iter().position(|b| *b == b'\n') {
                let line: Vec<u8> = buffer.drain(..=newline).collect();
                let line = String::from_utf8_lossy(&line);
                let line = line.trim_end_matches(['\n', '\r']);
                if line.is_empty() {
                    if data.is_empty() {
                        continue;
                    }
                    let event = std::mem::take(&mut data);
                    if event == "[DONE]" {
                        return Ok(state.finish(started));
                    }
                    let value: Value = serde_json::from_str(&event)
                        .map_err(|error| ChatError::Decode(error.to_string()))?;
                    if let Some(error) = stream_error(&value) {
                        return Err(error);
                    }
                    state.absorb(&value);
                } else if let Some(field) = line.strip_prefix("data:") {
                    if !data.is_empty() {
                        data.push('\n');
                    }
                    data.push_str(field.strip_prefix(' ').unwrap_or(field));
                }
            }
        }
        if !data.is_empty() && data != "[DONE]" {
            let value: Value = serde_json::from_str(&data)
                .map_err(|error| ChatError::Decode(error.to_string()))?;
            if let Some(error) = stream_error(&value) {
                return Err(error);
            }
            state.absorb(&value);
        }
        Ok(state.finish(started))
    }

    pub async fn stream_with_retry(
        &self,
        endpoint: &Endpoint,
        model: &str,
        messages: &[Value],
        params: &Map<String, Value>,
    ) -> Result<StreamResult, ChatError> {
        let mut attempt = 0;
        loop {
            match self.stream(endpoint, model, messages, params).await {
                Err(ChatError::Stream {
                    server_error: true,
                    message,
                }) if attempt < STREAM_SERVER_ERROR_RETRIES => {
                    tracing::warn!(model, %message, attempt = attempt + 1, "in-stream server error, retrying");
                    tokio::time::sleep(Duration::from_secs_f64(
                        STREAM_SERVER_ERROR_BACKOFF
                            * 2_f64.powi(i32::try_from(attempt).unwrap_or(0)),
                    ))
                    .await;
                    attempt += 1;
                }
                result => return result,
            }
        }
    }

    pub async fn complete(
        &self,
        endpoint: &Endpoint,
        model: &str,
        messages: &[Value],
    ) -> Result<Value, ChatError> {
        let body = serde_json::json!({"messages": messages, "model": model});
        let response = self.send(endpoint, &body).await?;
        let text = tokio::time::timeout(endpoint.request_timeout, response.text())
            .await
            .map_err(|_| ChatError::timed_out())?
            .map_err(|_| ChatError::connection())?;
        serde_json::from_str(&text).map_err(|error| ChatError::Decode(error.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn status_errors_render_like_openai_python() {
        let error = status_error(
            StatusCode::BAD_REQUEST,
            r#"{"error": {"message": "logprobs not supported", "code": 400}}"#,
        );
        assert_eq!(
            error,
            ChatError::BadRequest(
                "Error code: 400 - {'error': {'message': 'logprobs not supported', 'code': 400}}"
                    .into()
            )
        );
        assert_eq!(
            status_error(StatusCode::NOT_FOUND, ""),
            ChatError::Status("Error code: 404 - ".into())
        );
        assert_eq!(
            status_error(StatusCode::BAD_GATEWAY, "upstream down"),
            ChatError::Status("Error code: 502 - upstream down".into())
        );
    }

    #[test]
    fn stream_errors_detect_server_errors() {
        assert_eq!(
            stream_error(&json!({"error": {"message": "overloaded", "type": "server_error"}})),
            Some(ChatError::Stream {
                message: "overloaded".into(),
                server_error: true
            })
        );
        assert_eq!(
            stream_error(&json!({"error": {"code": 400}})),
            Some(ChatError::Stream {
                message: "An error occurred during streaming".into(),
                server_error: false
            })
        );
        assert_eq!(stream_error(&json!({"choices": []})), None);
    }

    #[test]
    fn int_field_ignores_floats_like_python_int_attr() {
        let usage = json!({"completion_tokens": 12, "prompt_tokens": 3.0});
        assert_eq!(int_field(Some(&usage), "completion_tokens"), 12);
        assert_eq!(int_field(Some(&usage), "prompt_tokens"), 0);
        assert_eq!(int_field(None, "x"), 0);
    }

    #[test]
    fn hidden_reasoning_measures_content_window_only() {
        let started = Instant::now();
        let mut state = StreamState::default();
        state.absorb(&json!({"choices": [{"delta": {"content": "Yes"}}]}));
        std::thread::sleep(Duration::from_millis(5));
        state.absorb(&json!({"choices": [{"delta": {"content": " indeed"}}]}));
        state.absorb(&json!({"choices": [], "usage": {"completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 40}}}));

        let result = state.finish(started);

        assert_eq!(result.raw_response, "Yes indeed");
        assert_eq!(result.deltas, 2);
        assert!(result.output_tps > 0.0);
        assert!(result.usage.is_some());
        assert_eq!(result.logprobs, None);
    }

    #[test]
    fn logprobs_and_reasoning_fields_are_collected() {
        let mut state = StreamState::default();
        state.absorb(&json!({"choices": [{"delta": {"reasoning_content": "think "}}]}));
        state.absorb(&json!({"choices": [{"delta": {"content": "No"}, "logprobs": {"content": [
            {"token": "No", "logprob": -0.1, "top_logprobs": [{"token": "No", "logprob": -0.1}, {"token": "Yes", "logprob": -2.5}]}
        ]}}]}));

        let result = state.finish(Instant::now());

        assert_eq!(result.reasoning, "think");
        let logprobs = result.logprobs.unwrap();
        assert_eq!(logprobs[0].top_logprobs.len(), 2);
        assert!(result.usage.is_none());
    }
}
