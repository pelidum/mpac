load("@rules_python//python:defs.bzl", "py_binary", "py_test")

filegroup(
    name = "test_db",
    srcs = glob([
        "test_db/*.json",
        "test_db/items/*.json",
    ]),
    visibility = ["//server:__subpackages__"],
)

filegroup(
    name = "test_db_attachments",
    srcs = glob(["test_db/attachments/**/*"]),
    visibility = [
        "//server:__subpackages__",
        "//tests:__subpackages__",
    ],
)

py_binary(
    name = "cli",
    srcs = ["mpac_cli.py"],
    data = glob(["test_db/**/*"]),
    main = "mpac_cli.py",
    deps = [
        "//proto:service_py_proto",
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

py_test(
    name = "cli_test",
    timeout = "short",
    srcs = [
        "mpac_cli.py",
        "mpac_cli_test.py",
    ],
    main = "mpac_cli_test.py",
    deps = [
        "//proto:service_py_proto",
        "@pypi//absl_py",
        "@pypi//cryptography",
        "@pypi//filetype",
        "@pypi//google_auth",
        "@pypi//grpcio",
        "@pypi//orjson",
        "@pypi//pandas",
        "@pypi//protobuf",
        "@pypi//pytest",
        "@pypi//requests",
        "@pypi//tqdm",
    ],
)
