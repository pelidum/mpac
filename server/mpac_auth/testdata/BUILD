load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")

exports_files(["auth_fixtures.json"])

py_library(
    name = "generate_fixtures_lib",
    srcs = ["generate_fixtures.py"],
    deps = [
        "@pypi//pyjwt",
        "@pypi//werkzeug",
    ],
)

py_binary(
    name = "generate_fixtures",
    srcs = ["generate_fixtures.py"],
    deps = [":generate_fixtures_lib"],
)

py_test(
    name = "fixtures_valid_test",
    size = "small",
    srcs = ["fixtures_valid_test.py"],
    data = ["auth_fixtures.json"],
    deps = [
        ":generate_fixtures_lib",
        "@pypi//pytest",
    ],
)
