load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_http",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "@crates//:reqwest",
        "@crates//:rustls",
    ],
)

rust_test(
    name = "mpac_http_test",
    size = "small",
    crate = ":mpac_http",
    edition = "2024",
    lint_config = "//server:lints",
)

rust_clippy_test(
    name = "mpac_http_clippy_test",
    targets = [
        ":mpac_http",
        ":mpac_http_test",
    ],
)

rustfmt_test(
    name = "mpac_http_rustfmt_test",
    targets = [
        ":mpac_http",
        ":mpac_http_test",
    ],
)
