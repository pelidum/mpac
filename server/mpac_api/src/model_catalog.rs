use std::sync::LazyLock;

use mpac_proto::Model;
use mpac_proto::model::{ModelCapabilities, ModelPricing};
use regex::Regex;
use serde_json::{Map, Value, json};

pub const DEBUG_RANDOM: &str = "debug_random";
pub const LLAMA_CPP: &str = "llama_cpp";
const DEFAULT_CONTEXT_WINDOW: i64 = 8192;
const DEBUG_RANDOM_MODELS: usize = 100;
const VISION_HINTS: [&str; 8] = [
    "vision",
    "-vl",
    "vl-",
    "llava",
    "minicpm-v",
    "pixtral",
    "qvq",
    "internvl",
];

static PARAMETERS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(\d+(?:\.\d+)?[bmk](?:[a-z]\d+[bmk])?)").unwrap_or_else(|_| unreachable!())
});
static GGUF_SUFFIX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)[-.]?gguf$").unwrap_or_else(|_| unreachable!()));
static TRAILING_LETTER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"-[A-Za-z]$").unwrap_or_else(|_| unreachable!()));

fn text(dict: &Value, key: &str) -> Option<String> {
    dict.get(key).and_then(Value::as_str).map(str::to_owned)
}

fn price(value: Option<&Value>) -> Option<f32> {
    match value {
        None => Some(0.0),
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(raw)) => raw.trim().parse::<f64>().ok(),
        Some(Value::Bool(flag)) => Some(f64::from(u8::from(*flag))),
        Some(_) => None,
    }
    .map(|v| {
        #[allow(clippy::cast_possible_truncation)]
        let v = v as f32;
        v
    })
}

fn pricing(dict: &Value) -> ModelPricing {
    let source = dict.get("pricing").cloned().unwrap_or_else(|| json!({}));
    let mut values = [0.0_f32; 4];
    for (slot, key) in values
        .iter_mut()
        .zip(["prompt", "completion", "image", "audio"])
    {
        let Some(value) = price(source.get(key)) else {
            tracing::error!(key, "unparseable model pricing");
            break;
        };
        *slot = value;
    }
    ModelPricing {
        input_token_cost: values[0],
        output_token_cost: values[1],
        image_token_cost: values[2],
        audio_token_cost: values[3],
    }
}

fn modality(dict: &Value, backend_type: &str) -> String {
    if let Some(modality) = dict
        .get("architecture")
        .filter(|a| a.is_object())
        .and_then(|a| text(a, "modality"))
    {
        return modality;
    }
    if backend_type == LLAMA_CPP {
        let tokens = format!(
            "{} {}",
            text(dict, "name").unwrap_or_default(),
            text(dict, "id").unwrap_or_default()
        )
        .to_lowercase();
        if VISION_HINTS.iter().any(|hint| tokens.contains(hint)) {
            return "text+image-to-text".into();
        }
    }
    "text-to-text".into()
}

pub fn model_from_dict(dict: &Value, backend_type: &str) -> Model {
    let id = text(dict, "id");
    let provider = id
        .as_deref()
        .and_then(|id| id.split_once('/'))
        .map(|(provider, _)| provider.to_owned())
        .unwrap_or_default();
    let architecture = dict.get("architecture").filter(|a| a.is_object());
    let context_window_length = match dict.get("context_length").and_then(Value::as_i64) {
        Some(length) => length,
        None if backend_type != LLAMA_CPP => DEFAULT_CONTEXT_WINDOW,
        None => 0,
    };
    Model {
        provider,
        id: id.unwrap_or_default(),
        name: text(dict, "name").unwrap_or_default(),
        description: text(dict, "description").unwrap_or_default(),
        pricing: Some(pricing(dict)),
        capabilities: Some(ModelCapabilities {
            modality: modality(dict, backend_type),
            context_window_length,
            tokenizer: architecture
                .and_then(|a| text(a, "tokenizer"))
                .unwrap_or_default(),
            instruct: architecture
                .and_then(|a| text(a, "instruct_type"))
                .unwrap_or_default(),
        }),
        license: text(dict, "license").unwrap_or_default(),
        ..Model::default()
    }
}

pub fn ollama_model(raw: &Value) -> Model {
    let details = raw.get("details").cloned().unwrap_or_else(|| json!({}));
    let id = text(raw, "model");
    Model {
        provider: id
            .as_deref()
            .unwrap_or_default()
            .split('/')
            .next()
            .unwrap_or_default()
            .to_owned(),
        id: id.unwrap_or_default(),
        name: text(raw, "name").unwrap_or_default(),
        family: text(&details, "family").unwrap_or_default(),
        format: text(&details, "format").unwrap_or_default(),
        parameters_label: text(&details, "parameter_size").unwrap_or_default(),
        quantization: text(&details, "quantization_level").unwrap_or_default(),
        ..Model::default()
    }
}

pub fn debug_models(backend_id: &str) -> Vec<Model> {
    let know_it_all = json!({
        "id": "debug/know_it_all",
        "name": "Debug Random Model: Know It All 1 Million Billion Parameters",
        "description": "Your source of PERFECT ALWAYS CORRECT answers that SHOULD NEVER BE USED FOR ANYTHING IMPORTANT.",
    });
    std::iter::once(know_it_all)
        .chain((1..=DEBUG_RANDOM_MODELS).map(|i| {
            json!({
                "id": format!("debug/debug_random_model_{i}"),
                "name": format!("Debug Random Model #{i} 0B"),
                "description": format!("Your #{i} source of random answers that SHOULD NEVER BE USED FOR ANYTHING IMPORTANT."),
            })
        }))
        .map(|dict| Model {
            backend_id: backend_id.to_owned(),
            ..model_from_dict(&dict, DEBUG_RANDOM)
        })
        .collect()
}

pub fn llama_cpp_repo_id(model_id: &str) -> Option<String> {
    let repo = model_id.split(':').next().unwrap_or(model_id);
    repo.contains('/').then(|| repo.to_owned())
}

pub fn enrich_llama_cpp(raw: &Value) -> Map<String, Value> {
    let model_id = text(raw, "id").unwrap_or_default();
    let (repo_part, quantization) = match model_id.rsplit_once(':') {
        Some((repo, quant)) => (repo.to_owned(), quant.to_owned()),
        None => (model_id.clone(), String::new()),
    };
    let provider = repo_part
        .split_once('/')
        .map(|(provider, _)| provider.to_owned())
        .unwrap_or_default();
    let repo_name = repo_part.rsplit('/').next().unwrap_or_default();
    let clean_name = GGUF_SUFFIX.replace(repo_name, "").into_owned();
    let found = PARAMETERS.find(&clean_name);
    let parameters_label = found.map(|m| m.as_str().to_uppercase()).unwrap_or_default();
    let family = match found {
        Some(m) => {
            let prefix = clean_name[..m.start()].trim_matches('-').trim();
            TRAILING_LETTER.replace(prefix, "").into_owned()
        }
        None => clean_name.clone(),
    };
    let mut enriched = Map::new();
    enriched.insert("id".into(), Value::String(model_id));
    enriched.insert("name".into(), Value::String(clean_name));
    enriched.insert("provider".into(), Value::String(provider));
    enriched.insert("family".into(), Value::String(family));
    enriched.insert("format".into(), Value::String("gguf".into()));
    enriched.insert("quantization".into(), Value::String(quantization));
    enriched.insert("parameters_label".into(), Value::String(parameters_label));
    enriched
}

pub fn apply_hugging_face(enriched: &mut Map<String, Value>, meta: &Value, config: &Value) {
    match meta
        .get("pipeline_tag")
        .and_then(Value::as_str)
        .unwrap_or_default()
    {
        "image-text-to-text" | "visual-question-answering" | "image-to-text" => {
            enriched.insert(
                "architecture".into(),
                json!({"modality": "text+image-to-text"}),
            );
        }
        "text-to-speech" => {
            enriched.insert("architecture".into(), json!({"modality": "text-to-audio"}));
        }
        _ => {}
    }
    let context = config
        .get("max_position_embeddings")
        .filter(|v| !v.is_null())
        .or_else(|| {
            config
                .get("text_config")
                .and_then(|t| t.get("max_position_embeddings"))
        })
        .and_then(Value::as_i64)
        .filter(|ctx| *ctx != 0);
    if let Some(context) = context {
        enriched.insert("context_length".into(), json!(context));
    }
    if let Some(license) = meta
        .get("cardData")
        .and_then(|card| card.get("license"))
        .and_then(Value::as_str)
        .filter(|license| !license.is_empty())
    {
        enriched.insert("license".into(), Value::String(license.to_owned()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openrouter_style_dict_maps_like_python() {
        let model = model_from_dict(
            &json!({
                "id": "anthropic/claude-x",
                "name": "Claude X",
                "description": "d",
                "pricing": {"prompt": "0.000003", "completion": "0.000015", "image": 0, "audio": "0"},
                "context_length": 200_000,
                "architecture": {"modality": "text+image->text", "tokenizer": "Claude", "instruct_type": null},
                "license": "proprietary"
            }),
            "openrouter",
        );

        assert_eq!(model.provider, "anthropic");
        let pricing = model.pricing.unwrap();
        assert!((pricing.input_token_cost - 0.000_003).abs() < f32::EPSILON);
        assert!((pricing.output_token_cost - 0.000_015).abs() < f32::EPSILON);
        let capabilities = model.capabilities.unwrap();
        assert_eq!(capabilities.context_window_length, 200_000);
        assert_eq!(capabilities.modality, "text+image->text");
        assert_eq!(capabilities.tokenizer, "Claude");
        assert_eq!(capabilities.instruct, "");
        assert_eq!(model.license, "proprietary");
    }

    #[test]
    fn defaults_and_bad_pricing_stop_like_python() {
        let model = model_from_dict(
            &json!({"id": "plain", "pricing": {"prompt": "1", "completion": "abc", "image": "2"}}),
            "vllm",
        );

        assert_eq!(model.provider, "");
        let pricing = model.pricing.unwrap();
        assert!((pricing.input_token_cost - 1.0).abs() < f32::EPSILON);
        assert!(pricing.image_token_cost.abs() < f32::EPSILON);
        let capabilities = model.capabilities.unwrap();
        assert_eq!(capabilities.context_window_length, 8192);
        assert_eq!(capabilities.modality, "text-to-text");
    }

    #[test]
    fn llama_cpp_has_no_default_context_and_detects_vision() {
        let model = model_from_dict(
            &json!({"id": "org/Qwen2-VL-7B-GGUF", "name": "Qwen2-VL-7B"}),
            LLAMA_CPP,
        );

        let capabilities = model.capabilities.unwrap();
        assert_eq!(capabilities.context_window_length, 0);
        assert_eq!(capabilities.modality, "text+image-to-text");
    }

    #[test]
    fn enrich_llama_cpp_ids() {
        let flash = enrich_llama_cpp(&json!({"id": "unsloth/GLM-4.7-Flash-GGUF:Q4_K_M"}));
        assert_eq!(flash["name"], "GLM-4.7-Flash");
        assert_eq!(flash["provider"], "unsloth");
        assert_eq!(flash["quantization"], "Q4_K_M");
        assert_eq!(flash["parameters_label"], "");
        assert_eq!(flash["family"], "GLM-4.7-Flash");

        let moe = enrich_llama_cpp(&json!({"id": "unsloth/Qwen3.5-122B-A10B-GGUF"}));
        assert_eq!(moe["name"], "Qwen3.5-122B-A10B");
        assert_eq!(moe["parameters_label"], "122B");
        assert_eq!(moe["family"], "Qwen3.5");

        let gemma = enrich_llama_cpp(&json!({"id": "unsloth/gemma-4-26B-A4B-it-GGUF"}));
        assert_eq!(gemma["parameters_label"], "26B");
        assert_eq!(gemma["family"], "gemma-4");

        for (id, parameters, family) in [
            ("bartowski/Llama-3.1-8B-Instruct-GGUF", "8B", "Llama-3.1"),
            ("TheBloke/Mixtral-8x7B-v0.1-GGUF", "7B", "Mixtral-8x"),
        ] {
            let enriched = enrich_llama_cpp(&json!({"id": id}));
            assert_eq!(enriched["parameters_label"], parameters, "{id}");
            assert_eq!(enriched["family"], family, "{id}");
        }
    }

    #[test]
    fn hugging_face_metadata_applies_modality_context_and_license() {
        let mut enriched = enrich_llama_cpp(&json!({"id": "org/model-7B-GGUF"}));
        apply_hugging_face(
            &mut enriched,
            &json!({"pipeline_tag": "image-text-to-text", "cardData": {"license": "apache-2.0"}}),
            &json!({"text_config": {"max_position_embeddings": 32_768}}),
        );

        let model = model_from_dict(&Value::Object(enriched), LLAMA_CPP);
        assert_eq!(model.license, "apache-2.0");
        let capabilities = model.capabilities.unwrap();
        assert_eq!(capabilities.context_window_length, 32768);
        assert_eq!(capabilities.modality, "text+image-to-text");
    }

    #[test]
    fn debug_backend_exposes_know_it_all_and_hundred_random_models() {
        let models = debug_models("b1");

        assert_eq!(models.len(), 101);
        assert_eq!(models[0].id, "debug/know_it_all");
        assert_eq!(models[100].id, "debug/debug_random_model_100");
        assert!(
            models
                .iter()
                .all(|m| m.backend_id == "b1" && m.provider == "debug")
        );
    }

    #[test]
    fn repo_ids_require_an_owner() {
        assert_eq!(
            llama_cpp_repo_id("unsloth/x-GGUF:Q4").as_deref(),
            Some("unsloth/x-GGUF")
        );
        assert_eq!(llama_cpp_repo_id("local-model"), None);
    }
}
