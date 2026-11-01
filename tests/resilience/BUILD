load("@rules_python//python:defs.bzl", "py_library", "py_test")

py_library(
    name = "fake_provider",
    srcs = ["fake_provider.py"],
    visibility = ["//terraform/on_prem:__pkg__"],
)

py_test(
    name = "resilience_test",
    timeout = "long",
    srcs = ["resilience_test.py"],
    data = ["//server/mpac"],
    env = {
        "MPAC_CONTRACT_SERVER_ARGS": "serve",
        "MPAC_CONTRACT_SERVER_BIN": "$(rlocationpath //server/mpac)",
    },
    main = "resilience_test.py",
    tags = [
        "no-sandbox",
        "requires-docker",
        "requires-network",
    ],
    deps = [
        ":fake_provider",
        "//proto:service_py_proto",
        "//tests/contract:harness",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//pytest",
    ],
)
