load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_image_layer")
load("@protobuf//bazel:proto_library.bzl", "proto_library")
load("@protobuf//bazel:py_proto_library.bzl", "py_proto_library")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load", "oci_push")
load("@rules_pkg//:pkg.bzl", "pkg_tar")
load("@rules_proto_grpc_doc//:defs.bzl", "doc_json_compile", "doc_markdown_compile")
load("@rules_proto_grpc_python//:defs.bzl", "python_grpc_library")
load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")
load("//:py_layer.bzl", "py_oci_image")

# gRPC Service
proto_library(
    name = "service_proto",
    srcs = ["service.proto"],
    visibility = ["//visibility:public"],
    deps = [
        "@protobuf//:any_proto",
        "@protobuf//:empty_proto",
        "@protobuf//:timestamp_proto",
    ],
)

doc_json_compile(
    name = "mpac_proto_json",
    protos = [
        ":service_proto",
    ],
    visibility = ["//visibility:public"],
)

doc_markdown_compile(
    name = "mpac_proto_doc",
    protos = [
        ":service_proto",
    ],
    visibility = ["//visibility:public"],
)

python_grpc_library(
    name = "service_py_proto",
    protos = [":service_proto"],
    visibility = ["//visibility:public"],
)

# Client
py_binary(
    name = "cli",
    srcs = ["cli.py"],
    data = glob(["test_db/**/*"]),
    deps = [
        ":server",
        ":service_py_proto",
        "@pypi//absl_py",
        "@pypi//filetype",
        "@pypi//grpcio",
        "@pypi//google_auth",
        "@pypi//orjson",
        "@pypi//pandas",
        "@pypi//protobuf",
        "@pypi//requests",
        "@pypi//tqdm",
    ],
)

# Server
py_binary(
    name = "server",
    srcs = ["server.py"],
    visibility = ["//visibility:public"],
    deps = [
        ":service_py_proto",
        "//server/objects:auth",
        "//server/objects:db",
        "//server/objects:handlers",
        "//server/objects:inference",
        "//server/objects:rate_limits",
        "@pypi//absl_py",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//grpcio_health_checking",
        "@pypi//grpcio_reflection",
        "@pypi//huggingface_hub",
        "@pypi//openai",
        "@pypi//requests",
    ],
    # Serialization trick for ~50x performance payoff / GIL workaround
    env = {
        "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "upb",
    },
)

py_test(
    name = "server_test",
    timeout = "short",
    srcs = ["server_test.py"],
    deps = [
        ":server",
        ":service_py_proto",
        "@pypi//absl_py",
        "@pypi//grpcio",
        "@pypi//protobuf",
        "@pypi//pytest",
        "@pypi//pytest_grpc",
    ],
)

py_test(
    name = "proto_compatibility_test",
    timeout = "short",
    srcs = ["proto_compatibility_test.py"],
    deps = [
        ":service_py_proto",
        "@pypi//protobuf",
        "@pypi//pytest",
    ],
)

py_image_layer(
    name = "server_layers",
    binary = ":server",
)

oci_image(
    name = "server_image",
    base = "@py3_11",
    entrypoint = ["/server/server"],
    tars = [":server_layers"],
)

platform_transition_filegroup(
    name = "server_platform_image",
    srcs = [":server_image"],
    target_platform = select({
        "@platforms//cpu:x86_64": "//:x86_64_linux",
    }),
)

oci_load(
    name = "server_image_load",
    image = ":server_platform_image",
    repo_tags = ["ghcr.io/pelidum/mpac_server:latest"],
)

oci_push(
    name = "server_image_push",
    image = ":server_platform_image",
    remote_tags = ["latest"],
    repository = "ghcr.io/pelidum/mpac_server",
)
