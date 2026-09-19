load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_image_layer")
load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load", "oci_push")
load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")
load("//:py_layer.bzl", "py_oci_image")

filegroup(
    name = "templates",
    srcs = glob(["templates/*.html"]),
)

filegroup(
    name = "static",
    srcs = glob(["static/**"]),
)

py_library(
    name = "grpc_client",
    srcs = ["grpc_client.py"],
    deps = [
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//grpcio",
        "@pypi//pyjwt",
    ],
    visibility = ["//visibility:public"],
)

py_library(
    name = "auth_lib",
    srcs = ["auth.py"],
    deps = [
        ":grpc_client",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//authlib",
        "@pypi//fastapi",
        "@pypi//grpcio",
        "@pypi//pyjwt",
        "@pypi//requests",
        "@pypi//starlette",
    ],
)

py_library(
    name = "dependencies",
    srcs = ["dependencies.py"],
    deps = [
        ":auth_lib",
        "@pypi//fastapi",
        "@pypi//pyjwt",
    ],
)

py_library(
    name = "rate_limit",
    srcs = ["rate_limit.py"],
)

py_library(
    name = "csrf",
    srcs = ["csrf.py"],
    deps = [
        "@pypi//starlette",
    ],
)

py_library(
    name = "state",
    srcs = ["state.py"],
    deps = [
        ":grpc_client",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//pyjwt",
    ],
)

py_library(
    name = "routers",
    srcs = glob(["routers/*.py"]),
    deps = [
        ":auth_lib",
        ":dependencies",
        ":grpc_client",
        ":state",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//duckdb",
        "@pypi//fastapi",
        "@pypi//markdown",
        "@pypi//pandas",
        "@pypi//protobuf",
        "@pypi//weasyprint",
        "@pypi//werkzeug",
    ],
)

py_library(
    name = "server_lib",
    srcs = ["server.py"],
    deps = [
        ":auth_lib",
        ":csrf",
        ":dependencies",
        ":grpc_client",
        ":rate_limit",
        ":routers",
        ":state",
        "//server:service_py_proto",
        "@pypi//absl_py",
        "@pypi//authlib",
        "@pypi//duckdb",
        "@pypi//fastapi",
        "@pypi//grpcio",
        "@pypi//httpx",
        "@pypi//itsdangerous",
        "@pypi//jinja2",
        "@pypi//markdown",
        "@pypi//pandas",
        "@pypi//protobuf",
        "@pypi//pyjwt",
        "@pypi//python_multipart",
        "@pypi//requests",
        "@pypi//starlette",
        "@pypi//uvicorn",
        "@pypi//werkzeug",
    ],
)

py_binary(
    name = "mpac_ui",
    srcs = ["server.py"],
    main = "server.py",
    data = [
        ":static",
        ":templates",
        "//server:mpac_proto_json",
    ],
    deps = [":server_lib"],
)

_UI_TEST_DEPS = [
    ":auth_lib",
    ":csrf",
    ":dependencies",
    ":grpc_client",
    ":rate_limit",
    ":routers",
    ":server_lib",
    ":state",
    "//server:service_py_proto",
    "@pypi//authlib",
    "@pypi//duckdb",
    "@pypi//fastapi",
    "@pypi//httpx",
    "@pypi//jinja2",
    "@pypi//markdown",
    "@pypi//pandas",
    "@pypi//pyjwt",
    "@pypi//pytest",
    "@pypi//pytest_asyncio",
    "@pypi//starlette",
    "@pypi//uvicorn",
    "@pypi//weasyprint",
    "@pypi//werkzeug",
]

_UI_TEST_DATA = [
    ":static",
    ":templates",
    "//server:mpac_proto_json",
]

py_test(
    name = "test_auth",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_auth.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_test(
    name = "test_admin",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_admin.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_test(
    name = "test_runs",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_runs.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_test(
    name = "test_settings",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_settings.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_test(
    name = "test_rate_limit",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_rate_limit.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_test(
    name = "test_csrf",
    size = "small",
    srcs = [
        "tests/conftest.py",
        "tests/test_csrf.py",
    ],
    deps = _UI_TEST_DEPS,
    data = _UI_TEST_DATA,
)

py_image_layer(
    name = "mpac_ui_layers",
    binary = ":mpac_ui",
)

oci_image(
    name = "mpac_ui_image",
    base = "@py3_11",
    entrypoint = ["/ui/mpac_ui"],
    tars = [
        "@weasyprint_syslibs//:flat",
        ":mpac_ui_layers",
    ],
)

platform_transition_filegroup(
    name = "mpac_ui_platform_image",
    srcs = [":mpac_ui_image"],
    target_platform = select({
        "@platforms//cpu:x86_64": "//:x86_64_linux",
    }),
)

oci_load(
    name = "mpac_ui_image_load",
    image = ":mpac_ui_platform_image",
    repo_tags = ["ghcr.io/pelidum/mpac_ui:latest"],
)

oci_push(
    name = "mpac_ui_image_push",
    image = ":mpac_ui_platform_image",
    remote_tags = ["latest"],
    repository = "ghcr.io/pelidum/mpac_ui",
)
