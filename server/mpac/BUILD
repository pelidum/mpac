load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_image_index", "oci_load", "oci_push")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_clippy_test", "rust_test", "rustfmt_test")

exports_files(["Cargo.toml"])

rust_binary(
    name = "mpac",
    srcs = glob(["src/**/*.rs"]),
    edition = "2024",
    lint_config = "//server:lints",
    version = "1.0.0",
    visibility = ["//visibility:public"],
    deps = [
        "//server/mpac_api",
        "//server/mpac_blob",
        "//server/mpac_db",
        "//server/mpac_secrets",
        "//server/mpac_worker",
        "@crates//:anyhow",
        "@crates//:clap",
        "@crates//:tokio",
        "@crates//:tracing",
        "@crates//:tracing-subscriber",
    ],
)

rust_test(
    name = "mpac_test",
    size = "small",
    crate = ":mpac",
    edition = "2024",
    lint_config = "//server:lints",
)

rust_clippy_test(
    name = "mpac_clippy_test",
    targets = [
        ":mpac",
        ":mpac_test",
    ],
)

rustfmt_test(
    name = "mpac_rustfmt_test",
    targets = [
        ":mpac",
        ":mpac_test",
    ],
)

pkg_tar(
    name = "mpac_layer",
    srcs = [":mpac"],
    package_dir = "/usr/local/bin",
    tags = ["manual"],
)

oci_image(
    name = "mpac_image",
    base = "@distroless_base_nossl",
    entrypoint = [
        "/usr/local/bin/mpac",
        "serve",
    ],
    tags = ["manual"],
    tars = [":mpac_layer"],
    user = "nonroot",
)

oci_image_index(
    name = "mpac_image_index",
    images = [":mpac_image"],
    platforms = [
        "//server:linux_amd64_gnu",
        "//server:linux_arm64_gnu",
    ],
    tags = ["manual"],
)

[
    platform_transition_filegroup(
        name = "mpac_image_" + arch,
        srcs = [":mpac_image"],
        tags = ["manual"],
        target_platform = "//server:linux_{}_gnu".format(arch),
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

[
    oci_load(
        name = "mpac_image_load_" + arch,
        image = ":mpac_image_" + arch,
        repo_tags = ["ghcr.io/pelidum/mpac_server:latest-" + arch],
        tags = ["manual"],
    )
    for arch in [
        "amd64",
        "arm64",
    ]
]

oci_push(
    name = "mpac_image_push",
    image = ":mpac_image_index",
    remote_tags = ["latest"],
    repository = "ghcr.io/pelidum/mpac_server",
    tags = ["manual"],
)
