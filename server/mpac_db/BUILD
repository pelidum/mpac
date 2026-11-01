load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_db",
    srcs = glob(["src/**/*.rs"]),
    compile_data = glob(["migrations/*.sql"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_auth",
        "//server/mpac_proto",
        "@crates//:prost",
        "@crates//:prost-types",
        "@crates//:sqlx",
        "@crates//:thiserror",
        "@crates//:tracing",
    ],
)

rust_test(
    name = "mpac_db_test",
    size = "small",
    crate = ":mpac_db",
    edition = "2024",
    lint_config = "//server:lints",
    rustc_flags = ["-Copt-level=3"],    deps = ["@crates//:tokio"],
)

rust_clippy_test(
    name = "mpac_db_clippy_test",
    targets = [
        ":mpac_db",
        ":mpac_db_test",
    ],
)

rustfmt_test(
    name = "mpac_db_rustfmt_test",
    targets = [
        ":mpac_db",
        ":mpac_db_test",
    ],
)
