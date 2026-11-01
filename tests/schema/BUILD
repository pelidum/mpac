load("@rules_python//python:defs.bzl", "py_test")

_DATA = ["//server/mpac"]

_ENV = {
    "MPAC_CLI_BIN": "$(rlocationpath //server/mpac)",
    "MPAC_CONTRACT_SERVER_ARGS": "serve",
    "MPAC_CONTRACT_SERVER_BIN": "$(rlocationpath //server/mpac)",
}

_TAGS = [
    "no-sandbox",
    "requires-docker",
    "requires-network",
]

py_test(
    name = "onboard_test",
    timeout = "moderate",
    srcs = ["onboard_test.py"],
    data = _DATA,
    env = _ENV,
    tags = _TAGS,
    deps = [
        "//proto:service_py_proto",
        "//tests/contract:harness",
        "@pypi//grpcio",
        "@pypi//protobuf",
        "@pypi//pytest",
    ],
)

py_test(
    name = "schema_test",
    timeout = "moderate",
    srcs = ["schema_test.py"],
    data = _DATA,
    env = _ENV,
    tags = _TAGS,
    deps = [
        "//tests/contract:harness",
        "@pypi//asyncpg",
        "@pypi//pytest",
    ],
)

py_test(
    name = "blob_config_test",
    timeout = "moderate",
    srcs = ["blob_config_test.py"],
    data = _DATA,
    env = _ENV,
    tags = _TAGS,
    deps = [
        "//proto:service_py_proto",
        "//tests/contract:harness",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//protobuf",
        "@pypi//pytest",
    ],
)

py_test(
    name = "secret_storage_test",
    timeout = "moderate",
    srcs = ["secret_storage_test.py"],
    data = _DATA,
    env = _ENV,
    tags = _TAGS,
    deps = [
        "//proto:service_py_proto",
        "//tests/contract:harness",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//pytest",
    ],
)
