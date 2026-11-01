load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")
load("//tests:defs.bzl", "rust_db_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_secrets",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_http",
        "@crates//:base64",
        "@crates//:hex",
        "@crates//:reqwest",
        "@crates//:ring",
        "@crates//:serde_json",
        "@crates//:sha2",
        "@crates//:sqlx",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tracing",
        "@crates//:url",
    ],
)

rust_test(
    name = "mpac_secrets_test",
    size = "small",
    crate = ":mpac_secrets",
    edition = "2024",
    lint_config = "//server:lints",
    deps = [
        "@crates//:wiremock",
    ],
)

rust_test(
    name = "secrets_db_bin",
    srcs = ["tests/secrets_test.rs"],
    edition = "2024",
    lint_config = "//server:lints",
    tags = ["manual"],
    deps = [
        ":mpac_secrets",
        "//server/mpac_testing",
        "@crates//:base64",
        "@crates//:sqlx",
        "@crates//:tokio",
    ],
)

rust_db_test(
    name = "secrets_db_test",
    binary = ":secrets_db_bin",
)

rust_clippy_test(
    name = "mpac_secrets_clippy_test",
    targets = [
        ":mpac_secrets",
        ":mpac_secrets_test",
        ":secrets_db_bin",
    ],
)

rustfmt_test(
    name = "mpac_secrets_rustfmt_test",
    targets = [
        ":mpac_secrets",
        ":mpac_secrets_test",
        ":secrets_db_bin",
    ],
)
