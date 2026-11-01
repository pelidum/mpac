load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_blob",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_http",
        "//server/mpac_proto",
        "@crates//:bytes",
        "@crates//:futures",
        "@crates//:hex",
        "@crates//:http",
        "@crates//:infer",
        "@crates//:object_store",
        "@crates//:sha2",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tracing",
        "@crates//:url",
    ],
)

rust_test(
    name = "mpac_blob_test",
    size = "small",
    crate = ":mpac_blob",
    env = {
        "AWS_ACCESS_KEY_ID": "test-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "AWS_REGION": "us-east-1",
    },
    edition = "2024",
    lint_config = "//server:lints",
)

rust_clippy_test(
    name = "mpac_blob_clippy_test",
    targets = [
        ":mpac_blob",
        ":mpac_blob_test",
    ],
)

rustfmt_test(
    name = "mpac_blob_rustfmt_test",
    targets = [
        ":mpac_blob",
        ":mpac_blob_test",
    ],
)
