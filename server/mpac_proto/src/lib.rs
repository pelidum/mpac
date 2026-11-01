pub mod job;
pub mod json;

use std::sync::LazyLock;

use prost_reflect::DescriptorPool;

#[allow(clippy::all, clippy::pedantic, unused_qualifications)]
pub mod mpac {
    tonic::include_proto!("pelidum.services.grpc.mpac");

    pub mod jobs {
        tonic::include_proto!("pelidum.services.grpc.mpac.jobs");
    }
}

pub use mpac::*;

pub const PACKAGE: &str = "pelidum.services.grpc.mpac";

pub const FILE_DESCRIPTOR_SET: &[u8] =
    include_bytes!(concat!(env!("OUT_DIR"), "/mpac_descriptor.bin"));

#[allow(clippy::expect_used)]
pub static DESCRIPTOR_POOL: LazyLock<DescriptorPool> = LazyLock::new(|| {
    DescriptorPool::decode(FILE_DESCRIPTOR_SET).expect("embedded descriptor set is valid")
});

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use std::collections::HashSet;

    use base64::Engine;
    use base64::engine::general_purpose::STANDARD;
    use prost::{Message, Name};
    use prost_reflect::{DynamicMessage, Kind, MessageDescriptor};
    use serde_json::Value;

    use super::*;
    use crate::json::encoded_to_jsonb;

    const GOLDEN: &str = include_str!("../testdata/proto_jsonb_golden.json");

    macro_rules! typed_round_trip {
        ($full_name:expr, $bytes:expr, $($message:ty),+ $(,)?) => {
            $(
                if $full_name == <$message>::full_name() {
                    return Some(<$message>::decode($bytes).unwrap().encode_to_vec());
                }
            )+
        };
    }

    fn stored_row_round_trip(full_name: &str, bytes: &[u8]) -> Option<Vec<u8>> {
        typed_round_trip!(
            full_name,
            bytes,
            Backend,
            Benchmark,
            FileAttachment,
            Test,
            TestItem,
            TestRun,
            TestRunAnswer,
            User,
        );
        None
    }

    fn has_map_field(message: &MessageDescriptor, seen: &mut HashSet<String>) -> bool {
        if !seen.insert(message.full_name().to_owned()) {
            return false;
        }
        message.fields().any(|field| {
            field.is_map()
                || matches!(field.kind(), Kind::Message(child) if has_map_field(&child, seen))
        })
    }

    fn populated_test_run() -> TestRun {
        TestRun {
            id: "run-1".into(),
            test_id: "test-1".into(),
            owner: "owner@example.com".into(),
            status: ResponseCode::InProgress.into(),
            models: vec![Model {
                id: "model-1".into(),
                ..Model::default()
            }],
            include_reasoning: true,
            sample_size: 25,
            total_cost_usd: 1.5,
            total_tokens: i64::MAX,
            metrics: vec![test_run::TestRunMetrics {
                responder_id: "model-1#0".into(),
                precision: 0.75,
                modality_scores: HashMap::from([("IMAGE".into(), 0.5), ("AUDIO".into(), 0.25)]),
                created_at_utc: Some(prost_types::Timestamp {
                    seconds: 1_761_900_000,
                    nanos: 123_000_000,
                }),
                ..test_run::TestRunMetrics::default()
            }],
            created_at_utc: Some(prost_types::Timestamp {
                seconds: 1_761_900_000,
                nanos: 0,
            }),
            backend_id: "backend-1".into(),
            ..TestRun::default()
        }
    }

    #[test]
    fn descriptor_file_matches_bazel_proto_library_path() {
        assert!(
            DESCRIPTOR_POOL
                .get_file_by_name("proto/service.proto")
                .is_some()
        );
    }

    #[test]
    fn service_methods_resolve_to_package_messages() {
        let service = DESCRIPTOR_POOL
            .get_service_by_name(&format!("{PACKAGE}.MPAC"))
            .unwrap();
        assert!(service.methods().len() > 40);
        for method in service.methods() {
            for message in [method.input(), method.output()] {
                assert_eq!(message.package_name(), PACKAGE, "{}", method.name());
            }
        }
    }

    #[test]
    fn generated_type_names_exist_in_descriptor_pool() {
        let names = [
            Backend::full_name(),
            Benchmark::full_name(),
            FileAttachment::full_name(),
            Model::full_name(),
            Test::full_name(),
            TestItem::full_name(),
            TestRun::full_name(),
            test_run::TestRunMetrics::full_name(),
            TestRunAnswer::full_name(),
            User::full_name(),
            Visibility::full_name(),
        ];
        for name in names {
            assert!(
                DESCRIPTOR_POOL.get_message_by_name(&name).is_some(),
                "{name}"
            );
        }
    }

    #[test]
    fn well_known_types_map_to_prost_types() {
        let created_at = DESCRIPTOR_POOL
            .get_message_by_name(&TestRun::full_name())
            .unwrap()
            .get_field_by_name("created_at_utc")
            .unwrap();
        let Kind::Message(timestamp) = created_at.kind() else {
            panic!("created_at_utc is not a message");
        };
        assert_eq!(timestamp.full_name(), prost_types::Timestamp::full_name());
    }

    #[test]
    fn test_run_round_trips_through_dynamic_message() {
        let run = populated_test_run();
        let descriptor = DESCRIPTOR_POOL
            .get_message_by_name(&TestRun::full_name())
            .unwrap();

        let dynamic = DynamicMessage::decode(descriptor, run.encode_to_vec().as_slice()).unwrap();
        let decoded = TestRun::decode(dynamic.encode_to_vec().as_slice()).unwrap();

        assert_eq!(decoded, run);
    }

    #[test]
    #[allow(deprecated)]
    fn file_attachment_bytes_round_trip_through_dynamic_message() {
        let attachment = FileAttachment {
            id: "sha".into(),
            name: "cat.jpg".into(),
            extension: "jpg".into(),
            modality: FileModality::Image.into(),
            file: (0..=255).collect(),
            file_size: 1,
            visibility: Some(Visibility {
                r#type: visibility::VisibilityOptions::VisibilityOrgOnly.into(),
                allowed_users: vec!["a@example.com".into()],
            }),
            owner: "owner@example.com".into(),
            mime: "image/jpeg".into(),
            uri: "blobs/sha".into(),
            sha256: "sha".into(),
            upload_state: file_attachment::UploadState::Ready.into(),
        };
        let descriptor = DESCRIPTOR_POOL
            .get_message_by_name(&FileAttachment::full_name())
            .unwrap();

        let bytes = attachment.encode_to_vec();
        let dynamic = DynamicMessage::decode(descriptor, bytes.as_slice()).unwrap();

        assert_eq!(dynamic.encode_to_vec(), bytes);
    }

    #[test]
    fn python_stored_rows_round_trip_through_typed_messages() {
        let entries: Vec<Value> = serde_json::from_str(GOLDEN).unwrap();
        let mut checked = 0;
        for entry in &entries {
            let message = entry["message"].as_str().unwrap();
            let variant = entry["variant"].as_str().unwrap();
            let python_bytes = STANDARD
                .decode(entry["proto_bytes"].as_str().unwrap())
                .unwrap();
            let Some(rust_bytes) = stored_row_round_trip(message, &python_bytes) else {
                continue;
            };

            let rust_json: Value =
                serde_json::from_str(&encoded_to_jsonb(message, &rust_bytes).unwrap()).unwrap();
            assert_eq!(rust_json, entry["proto_json"], "{message} ({variant})");

            let descriptor = DESCRIPTOR_POOL.get_message_by_name(message).unwrap();
            if !has_map_field(&descriptor, &mut HashSet::new()) {
                assert_eq!(rust_bytes, python_bytes, "{message} ({variant})");
            }
            checked += 1;
        }
        assert!(checked > 130, "only {checked} stored rows checked");
    }
}
