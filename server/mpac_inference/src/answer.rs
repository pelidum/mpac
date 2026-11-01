use std::collections::HashSet;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use mpac_proto::model::ModelPricing;
use mpac_proto::test_run_answer::Status;
use mpac_proto::{
    Backend, BackendType, ChoiceProbability, FileModality, Model, TestItem, TestRunAnswer,
};
use rand::seq::IndexedRandom;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::client::{ChatClient, ChatError, Endpoint, StreamResult, int_field};
use crate::confidence::set_answer_confidence;
use crate::prompt::{
    AttachmentData, JUSTIFICATION_PROMPT, logprob_params, messages, reasoning_params,
};
use crate::validate::validate_answer;

pub const LOCAL_REQUEST_TIMEOUT: Duration = Duration::from_secs(600);
pub const REMOTE_REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const WAIT_SLACK: Duration = Duration::from_secs(10);
const DEBUG_INPUT_TOKENS: i64 = 500;
const DEBUG_OUTPUT_TOKENS: i64 = 100;

static THINK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<think>(.*?)</think>").unwrap_or_else(|_| unreachable!()));

#[derive(Clone)]
pub struct AnswerInput<'a> {
    pub answer_id: String,
    pub run_id: &'a str,
    pub item: &'a TestItem,
    pub model: &'a Model,
    pub backend: &'a Backend,
    pub attachment: Option<AttachmentData<'a>>,
    pub include_reasoning: bool,
    pub instructions: &'a str,
}

impl std::fmt::Debug for AnswerInput<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AnswerInput")
            .field("answer_id", &self.answer_id)
            .field("run_id", &self.run_id)
            .field("backend_id", &self.backend.id)
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
pub struct Engine {
    client: ChatClient,
    logprobs_unsupported: Mutex<HashSet<(String, String)>>,
}

#[derive(Debug, Default, Clone, Copy)]
struct ModalityTokens {
    image: i64,
    input_audio: i64,
    output_audio: i64,
    video: i64,
    reasoning: i64,
}

fn modality_tokens(usage: Option<&Value>) -> ModalityTokens {
    let prompt = usage.and_then(|u| u.get("prompt_tokens_details"));
    let completion = usage.and_then(|u| u.get("completion_tokens_details"));
    let or = |primary: i64, fallback: i64| if primary != 0 { primary } else { fallback };
    ModalityTokens {
        image: int_field(usage, "image_tokens"),
        input_audio: or(
            int_field(usage, "input_audio_tokens"),
            int_field(prompt, "audio_tokens"),
        ),
        output_audio: or(
            int_field(usage, "output_audio_tokens"),
            int_field(completion, "audio_tokens"),
        ),
        video: int_field(usage, "video_tokens"),
        reasoning: or(
            int_field(usage, "reasoning_tokens"),
            int_field(completion, "reasoning_tokens"),
        ),
    }
}

#[allow(clippy::cast_precision_loss)]
fn costs(
    input_tokens: i64,
    output_tokens: i64,
    modality: ModalityTokens,
    pricing: ModelPricing,
) -> (f64, f64) {
    let base_prompt =
        (input_tokens - (modality.image + modality.input_audio + modality.video)).max(0);
    let input = base_prompt as f64 * f64::from(pricing.input_token_cost)
        + modality.image as f64 * f64::from(pricing.image_token_cost)
        + modality.input_audio as f64 * f64::from(pricing.audio_token_cost);
    let output = output_tokens as f64 * f64::from(pricing.output_token_cost)
        + modality.output_audio as f64 * f64::from(pricing.audio_token_cost);
    (input, output)
}

fn openrouter_cost(usage: Option<&Value>) -> Option<f64> {
    let cost = usage?.get("cost")?;
    let value = match cost {
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.parse().ok()?,
        _ => return None,
    };
    (value != 0.0).then_some(value)
}

fn backend_type(backend: &Backend) -> String {
    BackendType::try_from(backend.backend_type)
        .map(|kind| kind.as_str_name().to_lowercase())
        .unwrap_or_default()
}

#[allow(clippy::cast_possible_truncation)]
fn f32_of(value: f64) -> f32 {
    value as f32
}

fn debug_confidence(answer: &mut TestRunAnswer, choices: &[String]) {
    let mut unique: Vec<&String> = Vec::new();
    for choice in choices {
        if !unique.contains(&choice) {
            unique.push(choice);
        }
    }
    let mut weights: Vec<(String, f64)> = unique
        .into_iter()
        .map(|c| (c.clone(), rand::random::<f64>()))
        .collect();
    let top = weights.iter().map(|(_, w)| *w).fold(0.0, f64::max) + rand::random::<f64>();
    match weights.iter_mut().find(|(c, _)| *c == answer.answer) {
        Some((_, weight)) => *weight = top,
        None => weights.push((answer.answer.clone(), top)),
    }
    let coverage = 0.9 + 0.1 * rand::random::<f64>();
    let total: f64 = weights.iter().map(|(_, w)| w).sum();
    weights.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    answer.choice_probabilities = weights
        .iter()
        .map(|(choice, weight)| ChoiceProbability {
            choice: choice.clone(),
            probability: f32_of(coverage * weight / total),
        })
        .collect();
    answer.confidence = answer.choice_probabilities.first().map(|p| p.probability);
}

impl Engine {
    pub fn new() -> reqwest::Result<Self> {
        Ok(Self {
            client: ChatClient::new()?,
            logprobs_unsupported: Mutex::new(HashSet::new()),
        })
    }

    pub fn forget_backend(&self, backend_id: &str) {
        if let Ok(mut set) = self.logprobs_unsupported.lock() {
            set.retain(|(backend, _)| backend != backend_id);
        }
    }

    async fn answer_debug(input: &AnswerInput<'_>, answer: &mut TestRunAnswer) {
        answer.reasoning = "DEBUG ANSWER: Provided by the MPAC debug_random backend.".into();
        answer.answer = if input.model.id.ends_with("know_it_all") {
            input.item.answer.clone()
        } else {
            input
                .item
                .choices
                .choose(&mut rand::rng())
                .cloned()
                .unwrap_or_default()
        };
        answer.is_correct = answer.answer == input.item.answer;
        answer.attachment_type = input
            .attachment
            .map_or(FileModality::Text.into(), |a| a.meta.modality);
        let started = Instant::now();
        tokio::time::sleep(Duration::from_secs_f64(rand::random::<f64>())).await;
        let duration = started.elapsed().as_secs_f64();
        answer.task_duration = f32_of(duration);
        answer.ttft = f32_of(duration * 0.25);
        if duration > 0.0 {
            answer.output_tps = f32_of(99.0 / (duration * 0.75));
        }
        answer.input_tokens = DEBUG_INPUT_TOKENS;
        answer.output_tokens = DEBUG_OUTPUT_TOKENS;
        answer.input_cost = 0.0;
        answer.output_cost = 0.0;
        answer.raw_response.clone_from(&answer.answer);
        answer.status = Status::Ok.into();
        debug_confidence(answer, &input.item.choices);
    }

    async fn attempt(
        &self,
        endpoint: &Endpoint,
        model: &str,
        messages: &[Value],
        params: &Map<String, Value>,
    ) -> Result<StreamResult, AttemptError> {
        match tokio::time::timeout(
            endpoint.request_timeout + WAIT_SLACK,
            self.client
                .stream_with_retry(endpoint, model, messages, params),
        )
        .await
        {
            Err(_) => Err(AttemptError::Timeout),
            Ok(Err(error)) => Err(AttemptError::Chat(error)),
            Ok(Ok(result)) => Ok(result),
        }
    }

    async fn stream_with_fallbacks(
        &self,
        input: &AnswerInput<'_>,
        endpoint: &Endpoint,
        messages: &[Value],
        kind: &str,
    ) -> Result<StreamResult, AttemptError> {
        let key = (input.backend.id.clone(), input.model.id.clone());
        let mut reasoning = reasoning_params(kind, input.include_reasoning);
        let unsupported = self
            .logprobs_unsupported
            .lock()
            .is_ok_and(|set| set.contains(&key));
        let mut logprobs = if unsupported {
            Map::new()
        } else {
            logprob_params()
        };
        let merged = |a: &Map<String, Value>, b: &Map<String, Value>| {
            let mut merged = a.clone();
            merged.extend(b.clone());
            merged
        };
        let model = input.model.id.as_str();
        let first = self
            .attempt(endpoint, model, messages, &merged(&reasoning, &logprobs))
            .await;
        let Err(AttemptError::Chat(first_error)) = &first else {
            return first;
        };
        if !first_error.is_bad_request() || (reasoning.is_empty() && logprobs.is_empty()) {
            return first;
        }
        let mut drop_logprobs = !logprobs.is_empty()
            && (first_error.to_string().to_lowercase().contains("logprob") || reasoning.is_empty());
        if drop_logprobs {
            logprobs.clear();
        } else {
            reasoning.clear();
        }
        tracing::info!(model, dropped = if drop_logprobs { "logprobs" } else { "reasoning params" }, error = %first_error, "retrying without rejected params");
        let second = self
            .attempt(endpoint, model, messages, &merged(&reasoning, &logprobs))
            .await;
        let result = match second {
            Err(AttemptError::Chat(error)) if error.is_bad_request() => {
                if reasoning.is_empty() && logprobs.is_empty() {
                    return Err(AttemptError::Chat(error));
                }
                if !logprobs.is_empty() && error.to_string().to_lowercase().contains("logprob") {
                    drop_logprobs = true;
                }
                self.attempt(endpoint, model, messages, &Map::new()).await?
            }
            other => other?,
        };
        if drop_logprobs && let Ok(mut set) = self.logprobs_unsupported.lock() {
            set.insert(key);
        }
        Ok(result)
    }

    #[allow(clippy::too_many_lines)]
    pub async fn answer(&self, input: &AnswerInput<'_>) -> TestRunAnswer {
        let kind = backend_type(input.backend);
        let mut answer = TestRunAnswer {
            id: input.answer_id.clone(),
            run_id: input.run_id.to_owned(),
            item_id: input.item.id.clone(),
            model_id: input.model.id.clone(),
            responder_id: if input.model.responder_id.is_empty() {
                input.model.id.clone()
            } else {
                input.model.responder_id.clone()
            },
            ..TestRunAnswer::default()
        };
        if kind == "debug_random" {
            Self::answer_debug(input, &mut answer).await;
            return answer;
        }

        let chat = messages(input.item, input.instructions, input.attachment);
        match input.attachment {
            Some(attachment) => {
                answer.has_attachment = true;
                answer.attachment_type = attachment.meta.modality;
            }
            None => {
                answer.has_attachment = false;
                answer.attachment_type = FileModality::Text.into();
            }
        }
        let endpoint = Endpoint {
            base_url: input.backend.base_url.clone(),
            api_key: input.backend.api_key.clone(),
            request_timeout: if input.backend.is_local {
                LOCAL_REQUEST_TIMEOUT
            } else {
                REMOTE_REQUEST_TIMEOUT
            },
        };
        let pricing = input.model.pricing.unwrap_or_default();
        let started = Instant::now();
        let mut streamed = false;
        match self
            .stream_with_fallbacks(input, &endpoint, &chat, &kind)
            .await
        {
            Ok(result) => {
                streamed = true;
                answer.raw_response.clone_from(&result.raw_response);
                answer.reasoning.clone_from(&result.reasoning);
                answer.ttft = f32_of(result.ttft);
                answer.output_tps = f32_of(result.output_tps);
                answer.task_duration = f32_of(result.task_duration);
                answer.usage_estimated = result.usage.is_none();
                if result.usage.is_none() {
                    answer.output_tokens = result.deltas;
                }
                answer.status = Status::Ok.into();
                if answer.reasoning.is_empty()
                    && let Some(thought) =
                        THINK.captures(&answer.raw_response).and_then(|c| c.get(1))
                {
                    answer.reasoning = crate::pystr::strip(thought.as_str()).to_owned();
                }
                answer.answer = validate_answer(&answer.raw_response, &input.item.choices);
                answer.is_correct = input.item.answer == answer.answer;
                set_answer_confidence(&mut answer, result.logprobs.as_deref(), &input.item.choices);

                let usage = result.usage.as_ref();
                let (input_tokens, output_tokens) = if usage.is_some() {
                    (
                        int_field(usage, "prompt_tokens"),
                        int_field(usage, "completion_tokens"),
                    )
                } else {
                    (0, answer.output_tokens)
                };
                let modality = modality_tokens(usage);
                answer.reasoning_tokens = modality.reasoning;
                let (input_cost, output_cost) =
                    costs(input_tokens, output_tokens, modality, pricing);
                answer.input_tokens = input_tokens;
                answer.output_tokens = output_tokens;
                match (kind.as_str(), openrouter_cost(usage)) {
                    ("openrouter", Some(cost)) => {
                        answer.input_cost = f32_of(cost);
                        answer.output_cost = 0.0;
                    }
                    _ => {
                        answer.input_cost = f32_of(input_cost);
                        answer.output_cost = f32_of(output_cost);
                    }
                }
            }
            Err(AttemptError::Timeout) => {
                tracing::warn!(model = %input.model.id, item = %input.item.id, "inference timeout");
                answer.status = Status::Timeout.into();
                answer.error = "Inference request timed out".into();
            }
            Err(AttemptError::Chat(ChatError::BadRequest(message))) => {
                answer.status = Status::BadRequest.into();
                answer.error = match input.attachment {
                    Some(attachment) => format!(
                        "Unable to handle attachment type {}: {message}",
                        attachment.meta.mime
                    ),
                    None => message,
                };
            }
            Err(AttemptError::Chat(ChatError::Connection(message))) => {
                tracing::warn!(model = %input.model.id, item = %input.item.id, %message, "connection error");
                answer.status = Status::ConnectionError.into();
                answer.error = format!("Connection error: {message}");
            }
            Err(AttemptError::Chat(error)) => {
                tracing::error!(model = %input.model.id, %error, "inference failed");
                answer.status = Status::Error.into();
                answer.error = format!("Unexpected error: {error}");
            }
        }
        if !streamed {
            answer.task_duration = f32_of(started.elapsed().as_secs_f64());
        }
        if input.include_reasoning
            && answer.status == i32::from(Status::Ok)
            && answer.reasoning.is_empty()
        {
            self.justify(input, &endpoint, &chat, &kind, pricing, &mut answer)
                .await;
        }
        answer
    }

    async fn justify(
        &self,
        input: &AnswerInput<'_>,
        endpoint: &Endpoint,
        chat: &[Value],
        kind: &str,
        pricing: ModelPricing,
        answer: &mut TestRunAnswer,
    ) {
        let mut messages: Vec<Value> = chat
            .iter()
            .filter(|message| !message.get("content").is_some_and(Value::is_array))
            .cloned()
            .collect();
        messages.push(json!({"role": "assistant", "content": answer.answer}));
        messages.push(json!({"role": "user", "content": JUSTIFICATION_PROMPT}));
        let call = self.client.complete(endpoint, &input.model.id, &messages);
        let result = match tokio::time::timeout(endpoint.request_timeout + WAIT_SLACK, call).await {
            Err(_) => {
                tracing::warn!(model = %input.model.id, item = %input.item.id, "justification timeout");
                answer.error = "Justification request timed out".into();
                return;
            }
            Ok(Err(ChatError::Connection(message))) => {
                answer.error = format!("Justification connection error: {message}");
                return;
            }
            Ok(Err(error)) => {
                answer.error = format!("Justification failed: {error}");
                return;
            }
            Ok(Ok(result)) => result,
        };
        let Some(choice) = result
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
        else {
            return;
        };
        choice
            .get("message")
            .and_then(|m| m.get("content"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .clone_into(&mut answer.justification);
        let Some(usage) = result.get("usage").filter(|u| u.is_object()) else {
            answer.error =
                "Justification failed: 'NoneType' object has no attribute 'prompt_tokens'".into();
            return;
        };
        let input_tokens = int_field(Some(usage), "prompt_tokens");
        let output_tokens = int_field(Some(usage), "completion_tokens");
        let (input_cost, output_cost) = costs(
            input_tokens,
            output_tokens,
            modality_tokens(Some(usage)),
            pricing,
        );
        answer.input_tokens += input_tokens;
        answer.output_tokens += output_tokens;
        match (kind, openrouter_cost(Some(usage))) {
            ("openrouter", Some(cost)) => {
                answer.input_cost = f32_of(f64::from(answer.input_cost) + cost);
            }
            _ => {
                answer.input_cost = f32_of(f64::from(answer.input_cost) + input_cost);
                answer.output_cost = f32_of(f64::from(answer.output_cost) + output_cost);
            }
        }
    }
}

#[derive(Debug)]
enum AttemptError {
    Timeout,
    Chat(ChatError),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modality_tokens_prefer_top_level_then_details() {
        let usage = json!({
            "image_tokens": 7,
            "prompt_tokens_details": {"audio_tokens": 3, "cached_tokens": 1},
            "completion_tokens_details": {"reasoning_tokens": 11, "audio_tokens": 2},
            "output_audio_tokens": 0
        });

        let tokens = modality_tokens(Some(&usage));

        assert_eq!(
            (
                tokens.image,
                tokens.input_audio,
                tokens.output_audio,
                tokens.reasoning
            ),
            (7, 3, 2, 11)
        );
    }

    #[test]
    fn costs_split_modalities_like_python() {
        let pricing = ModelPricing {
            input_token_cost: 0.5,
            output_token_cost: 2.0,
            image_token_cost: 1.0,
            audio_token_cost: 3.0,
        };
        let modality = ModalityTokens {
            image: 10,
            input_audio: 5,
            output_audio: 1,
            ..ModalityTokens::default()
        };

        let (input, output) = costs(100, 20, modality, pricing);

        assert!((input - (85.0 * 0.5 + 10.0 + 15.0)).abs() < 1e-9);
        assert!((output - (40.0 + 3.0)).abs() < 1e-9);
    }

    #[test]
    fn openrouter_cost_requires_truthy_value() {
        assert_eq!(
            openrouter_cost(Some(&json!({"cost": 0.0012}))),
            Some(0.0012)
        );
        assert_eq!(openrouter_cost(Some(&json!({"cost": 0}))), None);
        assert_eq!(openrouter_cost(None), None);
    }

    #[test]
    fn debug_confidence_puts_answer_first() {
        let mut answer = TestRunAnswer {
            answer: "no".into(),
            ..TestRunAnswer::default()
        };

        debug_confidence(&mut answer, &["yes".into(), "no".into(), "yes".into()]);

        assert_eq!(answer.choice_probabilities.len(), 2);
        assert_eq!(answer.choice_probabilities[0].choice, "no");
        assert_eq!(
            answer.confidence,
            Some(answer.choice_probabilities[0].probability)
        );
        let total: f32 = answer
            .choice_probabilities
            .iter()
            .map(|p| p.probability)
            .sum();
        assert!(total <= 1.0 + 1e-6);
    }
}

#[cfg(test)]
mod engine_tests {
    use std::fmt::Write;

    use mpac_proto::model::ModelPricing;
    use wiremock::matchers::{body_partial_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    fn sse(chunks: &[Value]) -> ResponseTemplate {
        let mut body = String::new();
        for chunk in chunks {
            let _ = write!(body, "data: {chunk}\n\n");
        }
        body.push_str("data: [DONE]\n\n");
        ResponseTemplate::new(200).set_body_raw(body, "text/event-stream")
    }

    fn content(text: &str) -> Value {
        json!({"choices": [{"delta": {"content": text}}]})
    }

    fn item() -> TestItem {
        TestItem {
            id: "item-1".into(),
            question: "Is the sky blue?".into(),
            choices: vec!["Yes".into(), "No".into()],
            answer: "Yes".into(),
            ..TestItem::default()
        }
    }

    fn model(id: &str) -> Model {
        Model {
            id: id.into(),
            pricing: Some(ModelPricing {
                input_token_cost: 0.001,
                output_token_cost: 0.002,
                ..ModelPricing::default()
            }),
            ..Model::default()
        }
    }

    fn backend(server: &MockServer, kind: BackendType) -> Backend {
        Backend {
            id: "b1".into(),
            backend_type: kind.into(),
            base_url: format!("{}/v1", server.uri()),
            api_key: "sk-test".into(),
            is_local: true,
            ..Backend::default()
        }
    }

    async fn run(
        engine: &Engine,
        backend: &Backend,
        model: &Model,
        include_reasoning: bool,
    ) -> TestRunAnswer {
        let item = item();
        engine
            .answer(&AnswerInput {
                answer_id: "answer-1".into(),
                run_id: "run-1",
                item: &item,
                model,
                backend,
                attachment: None,
                include_reasoning,
                instructions: "",
            })
            .await
    }

    async fn bodies(server: &MockServer) -> Vec<Value> {
        server
            .received_requests()
            .await
            .unwrap()
            .iter()
            .map(|request| serde_json::from_slice(&request.body).unwrap())
            .collect()
    }

    #[test]
    fn forgetting_a_backend_clears_only_its_logprobs_cache() {
        let engine = Engine::new().unwrap();
        for key in [("b1", "m1"), ("b1", "m2"), ("b2", "m1")] {
            engine
                .logprobs_unsupported
                .lock()
                .unwrap()
                .insert((key.0.into(), key.1.into()));
        }

        engine.forget_backend("b1");

        let remaining = engine.logprobs_unsupported.lock().unwrap().clone();
        assert_eq!(remaining, HashSet::from([("b2".into(), "m1".into())]));
    }

    #[tokio::test]
    async fn streamed_answer_is_validated_scored_and_priced() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(sse(&[
                json!({"choices": [{"delta": {"reasoning_content": "It is daytime."}}]}),
                json!({"choices": [{"delta": {"content": "Yes"}, "logprobs": {"content": [
                    {"token": "Yes", "logprob": -0.05, "top_logprobs": [{"token": "Yes", "logprob": -0.05}, {"token": "No", "logprob": -3.0}]}
                ]}}]}),
                content("."),
                json!({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30}}),
            ]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            false,
        )
        .await;

        assert_eq!(answer.status, i32::from(Status::Ok));
        assert_eq!(answer.id, "answer-1");
        assert_eq!(answer.answer, "Yes");
        assert!(answer.is_correct);
        assert_eq!(answer.raw_response, "Yes.");
        assert_eq!(answer.reasoning, "It is daytime.");
        assert_eq!((answer.input_tokens, answer.output_tokens), (120, 30));
        assert!((answer.input_cost - 0.12).abs() < 1e-6);
        assert!((answer.output_cost - 0.06).abs() < 1e-6);
        assert!(answer.confidence.unwrap() > 0.9);
        assert!(!answer.usage_estimated);
        let sent = &bodies(&server).await[0];
        assert_eq!(sent["model"], "m1");
        assert_eq!(sent["stream"], true);
        assert_eq!(sent["stream_options"], json!({"include_usage": true}));
        assert_eq!(sent["logprobs"], true);
        assert_eq!(sent["top_logprobs"], 20);
        assert_eq!(sent["messages"][0]["role"], "system");
        assert!(
            sent["messages"][1]["content"]
                .as_str()
                .unwrap()
                .contains("['Yes', 'No']")
        );
    }

    #[tokio::test]
    async fn rejected_logprobs_fall_back_and_are_remembered() {
        let server = MockServer::start().await;
        Mock::given(body_partial_json(json!({"logprobs": true})))
            .respond_with(
                ResponseTemplate::new(400)
                    .set_body_json(json!({"error": {"message": "logprobs are not supported"}})),
            )
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(sse(&[content("No")]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();
        let backend = backend(&server, BackendType::Vllm);

        let first = run(&engine, &backend, &model("m1"), false).await;
        let second = run(&engine, &backend, &model("m1"), false).await;

        assert_eq!(first.answer, "No");
        assert_eq!(second.answer, "No");
        assert!(first.usage_estimated);
        assert_eq!(first.output_tokens, 1);
        let sent = bodies(&server).await;
        assert_eq!(sent.len(), 3);
        assert!(sent[1].get("logprobs").is_none());
        assert!(sent[2].get("logprobs").is_none());
    }

    #[tokio::test]
    async fn rejected_reasoning_params_are_dropped_first() {
        let server = MockServer::start().await;
        Mock::given(body_partial_json(json!({"reasoning_effort": "medium"})))
            .respond_with(
                ResponseTemplate::new(400)
                    .set_body_json(json!({"error": {"message": "unknown parameter"}})),
            )
            .mount(&server)
            .await;
        Mock::given(body_partial_json(json!({"stream": true})))
            .respond_with(sse(&[
                json!({"choices": [{"delta": {"content": "Yes", "reasoning": "because"}}]}),
            ]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Openai),
            &model("m1"),
            true,
        )
        .await;

        assert_eq!(answer.status, i32::from(Status::Ok));
        assert_eq!(answer.reasoning, "because");
        let sent = bodies(&server).await;
        assert_eq!(sent.len(), 2);
        assert!(sent[1].get("reasoning_effort").is_none());
        assert_eq!(sent[1]["logprobs"], true);
    }

    #[tokio::test]
    async fn server_errors_are_retried_like_the_openai_sdk() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(503).set_body_string("busy"))
            .up_to_n_times(2)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(sse(&[content("Yes")]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            false,
        )
        .await;

        assert_eq!(answer.answer, "Yes");
        assert_eq!(bodies(&server).await.len(), 3);
    }

    #[tokio::test]
    async fn exhausted_retries_record_python_error_text() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(503).set_body_json(json!({"error": "busy"})))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            false,
        )
        .await;

        assert_eq!(answer.status, i32::from(Status::Error));
        assert_eq!(
            answer.error,
            "Unexpected error: Error code: 503 - {'error': 'busy'}"
        );
        assert!(answer.task_duration > 0.0);
    }

    #[tokio::test]
    async fn bad_requests_without_fallbacks_are_recorded() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(
                ResponseTemplate::new(400)
                    .set_body_json(json!({"error": {"message": "context too long"}})),
            )
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            false,
        )
        .await;

        assert_eq!(answer.status, i32::from(Status::BadRequest));
        assert_eq!(
            answer.error,
            "Error code: 400 - {'error': {'message': 'context too long'}}"
        );
        assert_eq!(bodies(&server).await.len(), 2);
    }

    #[tokio::test]
    async fn unreachable_backends_are_connection_errors() {
        let engine = Engine::new().unwrap();
        let backend = Backend {
            id: "b1".into(),
            backend_type: BackendType::Vllm.into(),
            base_url: "http://127.0.0.1:9/v1".into(),
            is_local: true,
            ..Backend::default()
        };

        let answer = run(&engine, &backend, &model("m1"), false).await;

        assert_eq!(answer.status, i32::from(Status::ConnectionError));
        assert_eq!(answer.error, "Connection error: Connection error.");
    }

    #[tokio::test]
    async fn in_stream_server_errors_are_retried() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(sse(&[
                json!({"error": {"message": "overloaded", "type": "server_error"}}),
            ]))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(sse(&[content("No")]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            false,
        )
        .await;

        assert_eq!(answer.answer, "No");
        assert!(!answer.is_correct);
    }

    #[tokio::test]
    async fn missing_reasoning_triggers_justification_call() {
        let server = MockServer::start().await;
        Mock::given(body_partial_json(json!({"stream": true})))
            .respond_with(sse(&[
                content("Yes"),
                json!({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}),
            ]))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "choices": [{"message": {"content": "Because it scatters blue light."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8}
            })))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Vllm),
            &model("m1"),
            true,
        )
        .await;

        assert_eq!(answer.justification, "Because it scatters blue light.");
        assert_eq!((answer.input_tokens, answer.output_tokens), (30, 9));
        let sent = bodies(&server).await;
        assert_eq!(sent.len(), 2);
        assert!(sent[1].get("stream").is_none());
        let roles: Vec<&str> = sent[1]["messages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["role"].as_str().unwrap())
            .collect();
        assert_eq!(roles, ["system", "user", "assistant", "user"]);
        assert_eq!(sent[1]["messages"][2]["content"], "Yes");
    }

    #[tokio::test]
    async fn openrouter_reported_cost_wins() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(sse(&[content("Yes"), json!({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "cost": 0.0042}})]))
            .mount(&server)
            .await;
        let engine = Engine::new().unwrap();

        let answer = run(
            &engine,
            &backend(&server, BackendType::Openrouter),
            &model("m1"),
            false,
        )
        .await;

        assert!((answer.input_cost - 0.0042).abs() < 1e-7);
        assert!(answer.output_cost.abs() < f32::EPSILON);
    }

    #[test]
    fn debug_output_never_contains_the_api_key() {
        let endpoint = Endpoint {
            base_url: "https://example.test/v1".into(),
            api_key: "sk-secret".into(),
            request_timeout: Duration::from_secs(1),
        };
        let backend = Backend {
            api_key: "sk-secret".into(),
            ..Backend::default()
        };
        let item = TestItem::default();
        let model = Model::default();
        let input = AnswerInput {
            answer_id: "a".into(),
            run_id: "r",
            item: &item,
            model: &model,
            backend: &backend,
            attachment: None,
            include_reasoning: false,
            instructions: "",
        };

        assert!(!format!("{endpoint:?}").contains("sk-secret"));
        assert!(!format!("{input:?}").contains("sk-secret"));
    }

    #[tokio::test]
    async fn debug_backend_answers_without_http() {
        let engine = Engine::new().unwrap();
        let debug = Backend {
            backend_type: BackendType::DebugRandom.into(),
            ..Backend::default()
        };

        let answer = run(&engine, &debug, &model("debug/know_it_all"), false).await;

        assert_eq!(answer.answer, "Yes");
        assert!(answer.is_correct);
        assert_eq!((answer.input_tokens, answer.output_tokens), (500, 100));
        assert_eq!(answer.attachment_type, i32::from(FileModality::Text));
    }
}
