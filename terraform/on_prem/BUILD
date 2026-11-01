load("@rules_python//python:defs.bzl", "py_binary")

py_binary(
    name = "smoke",
    srcs = ["smoke.py"],
    deps = [
        "//proto:service_py_proto",
        "@pypi//grpcio",
        "@pypi//requests",
    ],
)

py_binary(
    name = "soak",
    srcs = ["soak.py"],
    deps = [
        "//proto:service_py_proto",
        "//tests/resilience:fake_provider",
        "@pypi//grpcio",
        "@pypi//requests",
    ],
)
