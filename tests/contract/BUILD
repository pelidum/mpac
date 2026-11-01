load("@rules_python//python:defs.bzl", "py_library", "py_test")

py_library(
    name = "harness",
    srcs = ["harness.py"],
    imports = ["."],
    visibility = [
        "//server:__subpackages__",
        "//tests:__subpackages__",
    ],
    deps = [
        "//proto:service_py_proto",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//grpcio_health_checking",
        "@pypi//pyjwt",
        "@rules_python//python/runfiles",
    ],
)

py_library(
    name = "contract_lib",
    srcs = ["conftest.py"] + glob(["test_*.py"]),
    data = ["//cli:test_db_attachments"],
    deps = [
        "@pypi//filetype",
        ":harness",
        "//proto:service_py_proto",
        "@pypi//asyncpg",
        "@pypi//grpcio",
        "@pypi//grpcio_health_checking",
        "@pypi//protobuf",
        "@pypi//pyjwt",
        "@pypi//pytest",
        "@rules_python//python/runfiles",
    ],
)

py_test(
    name = "contract_rust_test",
    timeout = "moderate",
    srcs = ["contract_test.py"],
    data = ["//server/mpac"],
    env = {
        "MPAC_CONTRACT_BLOBS": "1",
        "MPAC_CONTRACT_SERVER_ARGS": "serve",
        "MPAC_CONTRACT_SERVER_BIN": "$(rlocationpath //server/mpac)",
    },
    main = "contract_test.py",
    tags = [
        "no-sandbox",
        "requires-docker",
        "requires-network",
    ],
    deps = [":contract_lib"],
)

py_test(
    name = "contract_rust_inline_test",
    timeout = "moderate",
    srcs = ["contract_test.py"],
    data = ["//server/mpac"],
    env = {
        "MPAC_CONTRACT_SERVER_ARGS": "serve",
        "MPAC_CONTRACT_SERVER_BIN": "$(rlocationpath //server/mpac)",
    },
    main = "contract_test.py",
    tags = [
        "no-sandbox",
        "requires-docker",
        "requires-network",
    ],
    deps = [":contract_lib"],
)

py_test(
    name = "contract_rust_split_test",
    timeout = "moderate",
    srcs = ["contract_test.py"],
    data = ["//server/mpac"],
    env = {
        "MPAC_CONTRACT_BLOBS": "1",
        "MPAC_CONTRACT_EXTRA_WORKERS": "3",
        "MPAC_CONTRACT_SERVER_ARGS": "serve --roles=api",
        "MPAC_CONTRACT_SERVER_BIN": "$(rlocationpath //server/mpac)",
    },
    main = "contract_test.py",
    tags = [
        "no-sandbox",
        "requires-docker",
        "requires-network",
    ],
    deps = [":contract_lib"],
)
