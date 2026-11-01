load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_auth",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "@crates//:hex",
        "@crates//:jsonwebtoken",
        "@crates//:pbkdf2",
        "@crates//:rand",
        "@crates//:scrypt",
        "@crates//:serde_json",
        "@crates//:sha2",
        "@crates//:subtle",
        "@crates//:thiserror",
    ],
)

rust_test(
    name = "mpac_auth_test",
    size = "small",
    compile_data = ["//server/mpac_auth/testdata:auth_fixtures.json"],
    crate = ":mpac_auth",
    edition = "2024",
    lint_config = "//server:lints",
    rustc_flags = ["-Copt-level=3"],
)

rust_clippy_test(
    name = "mpac_auth_clippy_test",
    targets = [
        ":mpac_auth",
        ":mpac_auth_test",
    ],
)

rustfmt_test(
    name = "mpac_auth_rustfmt_test",
    targets = [
        ":mpac_auth",
        ":mpac_auth_test",
    ],
)
