load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")

exports_files(["proto_jsonb_golden.json"])

py_library(
    name = "generate_golden_lib",
    srcs = ["generate_golden.py"],
    deps = [
        "//proto:service_py_proto",
        "@pypi//protobuf",
    ],
)

py_binary(
    name = "generate_golden",
    srcs = ["generate_golden.py"],
    data = ["//cli:test_db"],
    deps = [":generate_golden_lib"],
)

py_test(
    name = "golden_up_to_date_test",
    size = "small",
    srcs = ["golden_up_to_date_test.py"],
    data = [
        "proto_jsonb_golden.json",
        "//cli:test_db",
    ],
    deps = [
        ":generate_golden_lib",
        "@pypi//pytest",
    ],
)
