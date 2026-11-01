load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")
load("//tests:defs.bzl", "rust_db_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_worker",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_blob",
        "//server/mpac_db",
        "//server/mpac_inference",
        "//server/mpac_proto",
        "//server/mpac_queue",
        "//server/mpac_secrets",
        "@crates//:bytes",
        "@crates//:prost",
        "@crates//:prost-types",
        "@crates//:rand",
        "@crates//:reqwest",
        "@crates//:sqlx",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tracing",
        "@crates//:uuid",
    ],
)

rust_test(
    name = "mpac_worker_test",
    size = "small",
    compile_data = ["//server/mpac_inference/testdata:inference_fixtures.json"],
    crate = ":mpac_worker",
    edition = "2024",
    lint_config = "//server:lints",
    deps = ["@crates//:serde_json"],
)

rust_test(
    name = "worker_db_bin",
    testonly = True,
    srcs = ["tests/worker_test.rs"],
    edition = "2024",
    lint_config = "//server:lints",
    tags = ["manual"],
    deps = [
        ":mpac_worker",
        "//server/mpac_blob",
        "//server/mpac_db",
        "//server/mpac_proto",
        "//server/mpac_queue",
        "//server/mpac_secrets",
        "//server/mpac_testing",
        "@crates//:bytes",
        "@crates//:hex",
        "@crates//:sha2",
        "@crates//:prost",
        "@crates//:sqlx",
        "@crates//:tokio",
        "@crates//:wiremock",
    ],
)

rust_db_test(
    name = "worker_db_test",
    binary = ":worker_db_bin",
)

rust_clippy_test(
    name = "mpac_worker_clippy_test",
    targets = [
        ":mpac_worker",
        ":mpac_worker_test",
        ":worker_db_bin",
    ],
)

rustfmt_test(
    name = "mpac_worker_rustfmt_test",
    targets = [
        ":mpac_worker",
        ":mpac_worker_test",
        ":worker_db_bin",
    ],
)
