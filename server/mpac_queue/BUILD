load("@rules_rust//rust:defs.bzl", "rust_clippy_test", "rust_library", "rust_test", "rustfmt_test")
load("//tests:defs.bzl", "rust_db_test")

exports_files(["Cargo.toml"])

rust_library(
    name = "mpac_queue",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//server:__subpackages__"],
    deps = [
        "//server/mpac_proto",
        "@crates//:prost",
        "@crates//:sqlx",
        "@crates//:thiserror",
        "@crates//:tokio",
        "@crates//:tracing",
        "@crates//:uuid",
    ],
)

rust_test(
    name = "mpac_queue_test",
    size = "small",
    crate = ":mpac_queue",
    edition = "2024",
    lint_config = "//server:lints",
)

rust_test(
    name = "queue_db_bin",
    testonly = True,
    srcs = ["tests/queue_test.rs"],
    edition = "2024",
    lint_config = "//server:lints",
    tags = ["manual"],
    deps = [
        ":mpac_queue",
        "//server/mpac_db",
        "//server/mpac_proto",
        "//server/mpac_testing",
        "@crates//:futures",
        "@crates//:prost",
        "@crates//:sqlx",
        "@crates//:tokio",
        "@crates//:uuid",
    ],
)

rust_db_test(
    name = "queue_db_test",
    binary = ":queue_db_bin",
)

rust_clippy_test(
    name = "mpac_queue_clippy_test",
    targets = [
        ":mpac_queue",
        ":mpac_queue_test",
        ":queue_db_bin",
    ],
)

rustfmt_test(
    name = "mpac_queue_rustfmt_test",
    targets = [
        ":mpac_queue",
        ":mpac_queue_test",
        ":queue_db_bin",
    ],
)
