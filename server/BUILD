load("@rules_rust//rust:defs.bzl", "rust_lint_config")

exports_files([
    "Cargo.lock",
    "Cargo.toml",
    "clippy.toml",
    "rustfmt.toml",
])

rust_lint_config(
    name = "lints",
    clippy = {
        "pedantic": "warn",
        "expect_used": "warn",
        "unwrap_used": "warn",
        "dbg_macro": "warn",
        "todo": "warn",
        "missing_errors_doc": "allow",
        "missing_panics_doc": "allow",
        "module_name_repetitions": "allow",
        "must_use_candidate": "allow",
        "single_match_else": "allow",
    },
    rustc = {
        "unsafe_code": "forbid",
        "unused_qualifications": "warn",
    },
    visibility = ["//server:__subpackages__"],
)

platform(
    name = "linux_amd64_gnu",
    constraint_values = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
        "@zig_sdk//libc:gnu.2.28",
    ],
    visibility = ["//server:__subpackages__"],
)

platform(
    name = "linux_arm64_gnu",
    constraint_values = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
        "@zig_sdk//libc:gnu.2.28",
    ],
    visibility = ["//server:__subpackages__"],
)
