use prost::{Message, Name};
use prost_reflect::{DynamicMessage, MessageDescriptor, SerializeOptions};

use crate::DESCRIPTOR_POOL;

#[derive(Debug, thiserror::Error)]
pub enum JsonError {
    #[error("message type {0} is not in the descriptor pool")]
    UnknownMessage(String),
    #[error("failed to decode {message}: {source}")]
    Decode {
        message: String,
        source: prost::DecodeError,
    },
    #[error("failed to serialize {message}: {source}")]
    Serialize {
        message: String,
        source: serde_json::Error,
    },
    #[error("serialized {message} is not UTF-8: {source}")]
    Utf8 {
        message: String,
        source: std::string::FromUtf8Error,
    },
}

fn storage_options() -> SerializeOptions {
    SerializeOptions::new()
        .use_proto_field_name(true)
        .skip_default_fields(false)
}

fn descriptor(full_name: &str) -> Result<MessageDescriptor, JsonError> {
    DESCRIPTOR_POOL
        .get_message_by_name(full_name)
        .ok_or_else(|| JsonError::UnknownMessage(full_name.to_owned()))
}

pub fn encoded_to_jsonb(full_name: &str, bytes: &[u8]) -> Result<String, JsonError> {
    let dynamic = DynamicMessage::decode(descriptor(full_name)?, bytes).map_err(|source| {
        JsonError::Decode {
            message: full_name.to_owned(),
            source,
        }
    })?;
    let mut serializer = serde_json::Serializer::new(Vec::new());
    dynamic
        .serialize_with_options(&mut serializer, &storage_options())
        .map_err(|source| JsonError::Serialize {
            message: full_name.to_owned(),
            source,
        })?;
    String::from_utf8(serializer.into_inner()).map_err(|source| JsonError::Utf8 {
        message: full_name.to_owned(),
        source,
    })
}

pub fn to_jsonb<M: Message + Name>(message: &M) -> Result<String, JsonError> {
    encoded_to_jsonb(&M::full_name(), &message.encode_to_vec())
}

#[cfg(test)]
mod tests {
    use base64::Engine;
    use base64::engine::general_purpose::STANDARD;
    use serde_json::Value;

    use super::*;
    use crate::{TestRun, Visibility, visibility};

    const GOLDEN: &str = include_str!("../testdata/proto_jsonb_golden.json");

    fn golden_entries() -> Vec<Value> {
        serde_json::from_str::<Value>(GOLDEN)
            .unwrap()
            .as_array()
            .unwrap()
            .clone()
    }

    #[test]
    fn matches_python_message_to_json_for_every_golden_entry() {
        let entries = golden_entries();
        assert!(entries.len() > 60);
        for entry in entries {
            let message = entry["message"].as_str().unwrap();
            let variant = entry["variant"].as_str().unwrap();
            let bytes = STANDARD
                .decode(entry["proto_bytes"].as_str().unwrap())
                .unwrap();

            let actual: Value =
                serde_json::from_str(&encoded_to_jsonb(message, &bytes).unwrap()).unwrap();

            assert_eq!(actual, entry["proto_json"], "{message} ({variant})");
        }
    }

    #[test]
    fn to_jsonb_uses_proto_field_names_and_enum_names() {
        let visibility = Visibility {
            r#type: visibility::VisibilityOptions::VisibilityAdmin.into(),
            allowed_users: vec![],
        };

        let json: Value = serde_json::from_str(&to_jsonb(&visibility).unwrap()).unwrap();

        assert_eq!(
            json,
            serde_json::json!({"type": "VISIBILITY_ADMIN", "allowed_users": []})
        );
    }

    #[test]
    fn to_jsonb_writes_shortest_float32() {
        let run = TestRun {
            total_cost_usd: 1.6,
            ..TestRun::default()
        };

        let json = to_jsonb(&run).unwrap();

        assert!(json.contains(r#""total_cost_usd":1.6"#), "{json}");
    }

    #[test]
    fn to_jsonb_stringifies_int64() {
        let run = TestRun {
            total_tokens: 42,
            ..TestRun::default()
        };

        let json: Value = serde_json::from_str(&to_jsonb(&run).unwrap()).unwrap();

        assert_eq!(json["total_tokens"], "42");
        assert_eq!(json["sample_size"], "0");
    }
}
