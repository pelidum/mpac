load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_testing",
    testonly = True,
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_db",
        "@crates//:sqlx",
        "@crates//:uuid",
    ],
)

rust_clippy_test(
    name = "mpac_testing_clippy_test",
    targets = [":mpac_testing"],
)

rustfmt_test(
    name = "mpac_testing_rustfmt_test",
    targets = [":mpac_testing"],
)
