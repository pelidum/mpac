load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_inference",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_http",
        "//server/mpac_proto",
        "@crates//:base64",
        "@crates//:futures",
        "@crates//:rand",
        "@crates//:regex",
        "@crates//:reqwest",
        "@crates//:serde_json",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tracing",
    ],
)

rust_test(
    name = "mpac_inference_test",
    size = "small",
    compile_data = ["//server/mpac_inference/testdata:inference_fixtures.json"],
    crate = ":mpac_inference",
    edition = "2024",
    lint_config = "//server:lints",
    deps = ["@crates//:wiremock"],
)

rust_clippy_test(
    name = "mpac_inference_clippy_test",
    targets = [
        ":mpac_inference",
        ":mpac_inference_test",
    ],
)

rustfmt_test(
    name = "mpac_inference_rustfmt_test",
    targets = [
        ":mpac_inference",
        ":mpac_inference_test",
    ],
)
