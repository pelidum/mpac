load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_api",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    proc_macro_deps = ["@crates//:async-trait"],
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_auth",
        "//server/mpac_blob",
        "//server/mpac_db",
        "//server/mpac_http",
        "//server/mpac_proto",
        "//server/mpac_queue",
        "//server/mpac_secrets",
        "//server/mpac_worker",
        "@crates//:bytes",
        "@crates//:futures",
        "@crates//:hex",
        "@crates//:http",
        "@crates//:prost",
        "@crates//:prost-types",
        "@crates//:regex",
        "@crates//:reqwest",
        "@crates//:serde_json",
        "@crates//:sha2",
        "@crates//:sqlx",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tokio-stream",
        "@crates//:tonic",
        "@crates//:tonic-health",
        "@crates//:tonic-reflection",
        "@crates//:tower",
        "@crates//:tracing",
        "@crates//:url",
        "@crates//:uuid",
    ],
)

rust_test(
    name = "mpac_api_test",
    size = "small",
    crate = ":mpac_api",
    edition = "2024",
    lint_config = "//server:lints",
    rustc_flags = ["-Copt-level=3"],
    deps = ["@crates//:wiremock"],
)

rust_clippy_test(
    name = "mpac_api_clippy_test",
    targets = [
        ":mpac_api",
        ":mpac_api_test",
    ],
)

rustfmt_test(
    name = "mpac_api_rustfmt_test",
    targets = [
        ":mpac_api",
        ":mpac_api_test",
    ],
)
