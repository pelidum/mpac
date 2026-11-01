use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;

use futures::future::join_all;
use mpac_db::crud::{self, Table};
use mpac_proto::{Backend, BackendType, GetCreditsReply, GetRequest, ListRequest, Model};
use serde_json::{Value, json};
use tonic::{Request, Status};

use crate::Api;
use crate::auth::caller;
use crate::backend_urls;
use crate::model_catalog::{
    DEBUG_RANDOM, LLAMA_CPP, apply_hugging_face, debug_models, enrich_llama_cpp, llama_cpp_repo_id,
    model_from_dict, ollama_model,
};
use crate::status::internal;

const BACKEND_TIMEOUT: Duration = Duration::from_secs(5);
const CREDITS_TIMEOUT: Duration = Duration::from_secs(3);
const HUGGING_FACE_TIMEOUT: Duration = Duration::from_secs(8);
const OPENROUTER_CREDITS_URL: &str = "https://openrouter.ai/api/v1/credits";
const HUGGING_FACE_URL: &str = "https://huggingface.co";

#[derive(Debug, Default)]
pub struct HuggingFaceCache {
    metadata: Mutex<HashMap<String, Value>>,
    configs: Mutex<HashMap<String, Value>>,
}

impl HuggingFaceCache {
    fn get(map: &Mutex<HashMap<String, Value>>, key: &str) -> Option<Value> {
        map.lock().ok()?.get(key).cloned()
    }

    fn put(map: &Mutex<HashMap<String, Value>>, key: &str, value: Value) {
        if let Ok(mut map) = map.lock() {
            map.insert(key.to_owned(), value);
        }
    }
}

pub fn backend_type_name(backend: &Backend) -> Option<String> {
    BackendType::try_from(backend.backend_type)
        .ok()
        .map(|kind| kind.as_str_name().to_lowercase())
}

async fn fetch_json(
    api: &Api,
    url: &str,
    api_key: &str,
    timeout: Duration,
) -> Result<Value, String> {
    let mut request = api.http.get(url).timeout(timeout);
    if !api_key.is_empty() {
        request = request.bearer_auth(api_key);
    }
    let response = request.send().await.map_err(|e| e.to_string())?;
    let response = response.error_for_status().map_err(|e| e.to_string())?;
    response.json().await.map_err(|e| e.to_string())
}

async fn hugging_face_get(api: &Api, url: &str) -> Value {
    match api.http.get(url).timeout(HUGGING_FACE_TIMEOUT).send().await {
        Ok(response) if response.status() == reqwest::StatusCode::OK => {
            response.json().await.unwrap_or_else(|_| json!({}))
        }
        Ok(response) => {
            tracing::debug!(url, status = %response.status(), "hugging face fetch returned non-200");
            json!({})
        }
        Err(error) => {
            tracing::warn!(url, %error, "hugging face fetch failed");
            json!({})
        }
    }
}

async fn hugging_face_config(api: &Api, repo: &str) -> Value {
    if let Some(config) = HuggingFaceCache::get(&api.hugging_face.configs, repo) {
        return config;
    }
    let config = hugging_face_get(
        api,
        &format!("{HUGGING_FACE_URL}/{repo}/resolve/main/config.json"),
    )
    .await;
    HuggingFaceCache::put(&api.hugging_face.configs, repo, config.clone());
    config
}

async fn hugging_face_info(api: &Api, repo_id: &str) -> (Value, Value) {
    let meta = if let Some(meta) = HuggingFaceCache::get(&api.hugging_face.metadata, repo_id) {
        meta
    } else {
        let meta = hugging_face_get(api, &format!("{HUGGING_FACE_URL}/api/models/{repo_id}")).await;
        HuggingFaceCache::put(&api.hugging_face.metadata, repo_id, meta.clone());
        meta
    };
    let base_model = match meta.get("cardData").and_then(|card| card.get("base_model")) {
        Some(Value::String(base)) => Some(base.clone()),
        Some(Value::Array(bases)) => bases.first().and_then(Value::as_str).map(str::to_owned),
        _ => None,
    };
    let config_repo = base_model
        .filter(|b| !b.is_empty())
        .unwrap_or_else(|| repo_id.to_owned());
    if HuggingFaceCache::get(&api.hugging_face.configs, &config_repo).is_none() {
        let config = hugging_face_config(api, &config_repo).await;
        let empty = config.as_object().is_none_or(serde_json::Map::is_empty);
        if empty && config_repo != repo_id {
            let fallback = hugging_face_config(api, repo_id).await;
            HuggingFaceCache::put(&api.hugging_face.configs, &config_repo, fallback);
        }
    }
    let config =
        HuggingFaceCache::get(&api.hugging_face.configs, &config_repo).unwrap_or_else(|| json!({}));
    (meta, config)
}

fn data_array(body: &Value, key: &str) -> Vec<Value> {
    body.get(key)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

async fn llama_cpp_models(api: &Api, backend: &Backend) -> Result<Vec<Model>, String> {
    let body = fetch_json(
        api,
        &format!("{}/models", backend.base_url),
        &backend.api_key,
        BACKEND_TIMEOUT,
    )
    .await?;
    let raw_models = data_array(&body, "data");
    let lookups = raw_models.iter().map(|raw| async {
        match llama_cpp_repo_id(raw.get("id").and_then(Value::as_str).unwrap_or_default()) {
            Some(repo) => Some(hugging_face_info(api, &repo).await),
            None => None,
        }
    });
    let metadata = join_all(lookups).await;
    Ok(raw_models
        .iter()
        .zip(metadata)
        .map(|(raw, hf)| {
            let mut enriched = enrich_llama_cpp(raw);
            if let Some((meta, config)) = hf {
                apply_hugging_face(&mut enriched, &meta, &config);
            }
            Model {
                backend_id: backend.id.clone(),
                ..model_from_dict(&Value::Object(enriched), LLAMA_CPP)
            }
        })
        .collect())
}

async fn models_for_backend(api: &Api, backend: &Backend) -> Result<Vec<Model>, String> {
    let kind = backend_type_name(backend).ok_or("unknown backend type")?;
    if !backend.base_url.is_empty() && kind != DEBUG_RANDOM && !backend.is_local {
        backend_urls::validate(&backend.base_url).await?;
    }
    match kind.as_str() {
        DEBUG_RANDOM => Ok(debug_models(&backend.id)),
        "ollama" => {
            let url = backend.base_url.replace("/v1", "/api/tags");
            let body = fetch_json(api, &url, "", BACKEND_TIMEOUT).await?;
            Ok(data_array(&body, "models")
                .iter()
                .map(|raw| Model {
                    backend_id: backend.id.clone(),
                    ..ollama_model(raw)
                })
                .collect())
        }
        LLAMA_CPP => llama_cpp_models(api, backend).await,
        _ => {
            let url = format!("{}/models", backend.base_url);
            let body = fetch_json(api, &url, &backend.api_key, BACKEND_TIMEOUT).await?;
            Ok(data_array(&body, "data")
                .iter()
                .map(|raw| Model {
                    backend_id: backend.id.clone(),
                    ..model_from_dict(raw, &kind)
                })
                .collect())
        }
    }
}

struct ModelListing {
    models: Vec<Model>,
    key_failure: Option<Status>,
}

async fn all_models(api: &Api) -> Result<ModelListing, Status> {
    let records = mpac_secrets::enabled_backend_records(&api.pool, 1000)
        .await
        .map_err(internal)?;
    let mut backends = Vec::with_capacity(records.len());
    let mut key_failure = None;
    for record in records {
        match api.backend_with_key(record).await {
            Ok(backend) => backends.push(backend),
            Err(status) => key_failure = Some(status),
        }
    }
    let listings = join_all(
        backends
            .iter()
            .map(|backend| models_for_backend(api, backend)),
    )
    .await;
    let models = backends
        .iter()
        .zip(listings)
        .flat_map(|(backend, listing)| {
            listing.unwrap_or_else(|error| {
                tracing::error!(backend = %backend.id, %error, "failed to list models for backend");
                Vec::new()
            })
        })
        .collect();
    Ok(ModelListing {
        models,
        key_failure,
    })
}

pub async fn list_models(api: &Api, request: Request<ListRequest>) -> Result<Vec<Model>, Status> {
    caller(&request)?;
    let listing = all_models(api).await?;
    match listing.key_failure {
        Some(status) if listing.models.is_empty() => Err(status),
        _ => Ok(listing.models),
    }
}

pub async fn get_model(api: &Api, request: Request<GetRequest>) -> Result<Model, Status> {
    caller(&request)?;
    let id = request.into_inner().id;
    let listing = all_models(api).await?;
    listing
        .models
        .into_iter()
        .find(|model| model.id == id)
        .ok_or_else(|| {
            listing
                .key_failure
                .unwrap_or_else(|| Status::not_found(format!("Model '{id}' not found")))
        })
}

#[allow(clippy::cast_possible_truncation)]
fn as_f32(value: Option<&Value>) -> f32 {
    value.and_then(Value::as_f64).unwrap_or_default() as f32
}

pub async fn get_credits(
    api: &Api,
    request: Request<GetRequest>,
) -> Result<GetCreditsReply, Status> {
    let caller = caller(&request)?;
    let id = request.into_inner().id;
    crud::get_bytes(&api.pool, Table::Backends, &id, &caller.requester())
        .await
        .map_err(internal)?
        .ok_or_else(|| Status::not_found(format!("Backend {id} not found")))?;
    let record = mpac_secrets::backend_record(&api.pool, &id)
        .await
        .map_err(internal)?
        .ok_or_else(|| Status::not_found(format!("Backend {id} not found")))?;
    let backend = api.backend_with_key(record).await?;
    let backend_type =
        backend_type_name(&backend).ok_or_else(|| internal("unknown backend type"))?;
    let mut reply = GetCreditsReply {
        backend_type: backend_type.clone(),
        ..GetCreditsReply::default()
    };
    if backend_type == "openrouter" {
        let body = fetch_json(
            api,
            OPENROUTER_CREDITS_URL,
            &backend.api_key,
            CREDITS_TIMEOUT,
        )
        .await
        .map_err(internal)?;
        let data = body.get("data").cloned().unwrap_or_else(|| json!({}));
        reply.total_credits = as_f32(data.get("total_credits"));
        reply.credits_used = as_f32(data.get("total_usage"));
        reply.credits_remaining = reply.total_credits - reply.credits_used;
    }
    Ok(reply)
}

#[cfg(test)]
mod tests {
    use sqlx::postgres::PgPoolOptions;
    use wiremock::matchers::{header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;

    fn api() -> Api {
        let pool = PgPoolOptions::new()
            .connect_lazy("postgres://unused@localhost/unused")
            .unwrap();
        Api::new(pool).unwrap()
    }

    fn backend(kind: BackendType, base_url: String, is_local: bool) -> Backend {
        Backend {
            id: "b1".into(),
            backend_type: kind.into(),
            base_url,
            api_key: "sk-test".into(),
            is_local,
            ..Backend::default()
        }
    }

    #[tokio::test]
    async fn openai_compatible_listing_sends_key_and_maps_models() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/models"))
            .and(header("authorization", "Bearer sk-test"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "data": [{"id": "meta/llama-3", "context_length": 4096}]
            })))
            .mount(&server)
            .await;

        let models = models_for_backend(
            &api(),
            &backend(BackendType::Vllm, format!("{}/v1", server.uri()), true),
        )
        .await
        .unwrap();

        assert_eq!(models.len(), 1);
        assert_eq!(models[0].id, "meta/llama-3");
        assert_eq!(models[0].provider, "meta");
        assert_eq!(models[0].backend_id, "b1");
        assert_eq!(
            models[0]
                .capabilities
                .as_ref()
                .unwrap()
                .context_window_length,
            4096
        );
    }

    #[tokio::test]
    async fn ollama_listing_uses_tags_endpoint() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/tags"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "models": [{
                    "model": "qwen2:7b",
                    "name": "qwen2:7b",
                    "details": {"family": "qwen2", "format": "gguf", "parameter_size": "7.6B", "quantization_level": "Q4_0"}
                }]
            })))
            .mount(&server)
            .await;

        let models = models_for_backend(
            &api(),
            &backend(BackendType::Ollama, format!("{}/v1", server.uri()), true),
        )
        .await
        .unwrap();

        assert_eq!(models[0].id, "qwen2:7b");
        assert_eq!(models[0].family, "qwen2");
        assert_eq!(models[0].parameters_label, "7.6B");
        assert_eq!(models[0].quantization, "Q4_0");
        assert!(models[0].pricing.is_none());
    }

    #[tokio::test]
    async fn llama_cpp_listing_without_repo_skips_hugging_face() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/models"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "data": [{"id": "local-llava-13b"}]
            })))
            .mount(&server)
            .await;

        let models = models_for_backend(
            &api(),
            &backend(BackendType::LlamaCpp, format!("{}/v1", server.uri()), true),
        )
        .await
        .unwrap();

        assert_eq!(models[0].name, "local-llava-13b");
        let capabilities = models[0].capabilities.as_ref().unwrap();
        assert_eq!(capabilities.modality, "text+image-to-text");
        assert_eq!(capabilities.context_window_length, 0);
    }

    #[tokio::test]
    async fn private_backend_url_is_blocked_unless_local() {
        let server = MockServer::start().await;

        let result = models_for_backend(
            &api(),
            &backend(BackendType::Vllm, format!("{}/v1", server.uri()), false),
        )
        .await;

        assert!(result.unwrap_err().contains("private address"));
    }

    #[test]
    fn backend_type_names_match_python_lowercase_enum_names() {
        let debug = Backend {
            backend_type: BackendType::DebugRandom.into(),
            ..Backend::default()
        };

        assert_eq!(backend_type_name(&debug).as_deref(), Some("debug_random"));
        assert_eq!(
            backend_type_name(&Backend {
                backend_type: 99,
                ..Backend::default()
            }),
            None
        );
    }
}
