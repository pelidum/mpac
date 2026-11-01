load("@rules_rust//cargo:defs.bzl", "cargo_build_script")
load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

cargo_build_script(
    name = "build_script",
    srcs = ["build.rs"],
    build_script_env = {
        "MPAC_PROTO_ROOT": "$${pwd}",
        "PROTOBUF_WELL_KNOWN_PROTOS": "$(execpaths @protobuf//:well_known_type_protos)",
        "PROTOC": "$(execpath @protobuf//:protoc)",
    },
    data = [
        "//proto:jobs.proto",
        "//proto:service.proto",
        "@protobuf//:well_known_type_protos",
    ],
    edition = "2024",
    lint_config = "//server:lints",
    tools = ["@protobuf//:protoc"],
    deps = [
        "@crates//:prost-build",
        "@crates//:tonic-prost-build",
    ],
)

rust_library(
    name = "mpac_proto",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        ":build_script",
        "@crates//:prost",
        "@crates//:prost-reflect",
        "@crates//:prost-types",
        "@crates//:serde_json",
        "@crates//:thiserror",
        "@crates//:tonic",
        "@crates//:tonic-prost",
    ],
)

rust_test(
    name = "mpac_proto_test",
    size = "small",
    compile_data = ["//server/mpac_proto/testdata:proto_jsonb_golden.json"],
    crate = ":mpac_proto",
    edition = "2024",
    lint_config = "//server:lints",
    deps = ["@crates//:base64"],
)

rust_clippy_test(
    name = "mpac_proto_clippy_test",
    targets = [
        ":mpac_proto",
        ":mpac_proto_test",
    ],
)

rustfmt_test(
    name = "mpac_proto_rustfmt_test",
    targets = [
        ":mpac_proto",
        ":mpac_proto_test",
    ],
)
