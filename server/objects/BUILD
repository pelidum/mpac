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


py_library(
    name = "auth",
    srcs = ["auth.py"],
    deps = [
        "@pypi//asyncpg",
        "@pypi//cachetools",
        "@pypi//grpcio",
        "@pypi//pyjwt",
    ],
    visibility = ["//visibility:public"],
)

py_library(
    name = "db",
    srcs = ["db.py"],
    deps = [
        ":auth",
        "//server:service_py_proto",
        "@pypi//asyncpg",
        "@pypi//absl_py",
        "@pypi//filetype",
        "@pypi//protobuf",
        "@pypi//scikit_learn",
        "@pypi//werkzeug",
    ],
    visibility = ["//visibility:public"],
)

py_library(
    name = "backend_pool",
    srcs = ["backend_pool.py"],
    deps = [
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//httpx",
        "@pypi//openai",
    ],
    visibility = ["//visibility:public"],
)

py_library(
    name = "inference",
    srcs = ["inference.py"],
    deps = [
        ":backend_pool",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//grpcio",
        "@pypi//openai",
        "@pypi//scikit_learn",
        "@pypi//tenacity",
    ],
    visibility = ["//visibility:public"],
)

py_library(
    name = "answer_status",
    srcs = ["answer_status.py"],
    deps = ["//server:service_py_proto"],
    visibility = ["//visibility:public"],
)

py_test(
    name = "answer_status_test",
    size = "small",
    srcs = ["answer_status_test.py"],
    deps = [
        ":answer_status",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "backend_pool_test",
    size = "small",
    srcs = ["backend_pool_test.py"],
    deps = [
        ":backend_pool",
        "//server:service_py_proto",
        "@pypi//httpx",
        "@pypi//openai",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_test(
    name = "backends_test",
    size = "small",
    srcs = ["backends_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//grpcio",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_test(
    name = "auth_test",
    size = "small",
    srcs = ["auth_test.py"],
    deps = [
        ":auth",
        "@pypi//absl_py",
        "@pypi//grpcio",
        "@pypi//pyjwt",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_test(
    name = "db_test",
    size = "small",
    srcs = ["db_test.py"],
    deps = [
        ":db",
        "//server:service_py_proto",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_test(
    name = "runs_test",
    size = "small",
    srcs = ["runs_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_test(
    name = "models_test",
    size = "small",
    srcs = ["models_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "inference_test",
    size = "small",
    srcs = ["inference_test.py"],
    deps = [
        ":inference",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "attachments_test",
    size = "small",
    srcs = ["attachments_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "test_items_test",
    size = "small",
    srcs = ["test_items_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "answers_test",
    size = "small",
    srcs = ["answers_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//pytest",
    ],
)

py_test(
    name = "benchmarks_test",
    size = "small",
    srcs = ["benchmarks_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//grpcio",
        "@pypi//pytest",
    ],
)

py_test(
    name = "tests_test",
    size = "small",
    srcs = ["tests_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//grpcio",
        "@pypi//pytest",
    ],
)

py_test(
    name = "users_test",
    size = "small",
    srcs = ["users_test.py"],
    deps = [
        ":handlers",
        "//server:service_py_proto",
        "@pypi//grpcio",
        "@pypi//pytest",
    ],
)

py_library(
    name = "rate_limits",
    srcs = ["rate_limits.py"],
    deps = [
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//cachetools",
        "@pypi//grpcio",
    ],
    visibility = ["//visibility:public"],
)

py_test(
    name = "rate_limits_test",
    size = "small",
    srcs = ["rate_limits_test.py"],
    deps = [
        ":rate_limits",
        "//server:service_py_proto",
        "@pypi//grpcio",
        "@pypi//pytest",
        "@pypi//pytest_asyncio",
    ],
)

py_library(
    name = "handlers",
    srcs = [
        "answers.py",
        "attachments.py",
        "backends.py",
        "benchmarks.py",
        "models.py",
        "runs.py",
        "tests.py",
        "test_items.py",
        "users.py",
    ],
    deps = [
        ":answer_status",
        ":backend_pool",
        ":db",
        ":inference",
        ":rate_limits",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//grpcio",
        "@pypi//openai",
        "@pypi//pandas",
        "@pypi//protobuf",
        "@pypi//requests",
        "@pypi//scikit_learn",
        "@pypi//tenacity",
        "@pypi//werkzeug",
    ],
    visibility = ["//visibility:public"],
)