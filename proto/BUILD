load("@protobuf//bazel:proto_library.bzl", "proto_library")
load("@rules_proto_grpc_doc//:defs.bzl", "doc_json_compile", "doc_markdown_compile")
load("@rules_proto_grpc_python//:defs.bzl", "python_grpc_library")
load("@rules_python//python:defs.bzl", "py_test")

exports_files([
    "jobs.proto",
    "service.proto",
])

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

proto_library(
    name = "jobs_proto",
    srcs = ["jobs.proto"],
    visibility = ["//visibility:public"],
    deps = [":service_proto"],
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
