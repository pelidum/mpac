"Runs a Rust test binary against a real PostgreSQL provided by the Python test harness."

load("@rules_python//python:defs.bzl", "py_test")

def rust_db_test(name, binary, timeout = "moderate", **kwargs):
    """Wraps `binary` (a rust_test tagged manual) so it runs with MPAC_TEST_PG_* set.

    Args:
        name: the py_test target name.
        binary: label of the rust_test to run.
        timeout: Bazel test timeout.
        **kwargs: forwarded to py_test.
    """
    py_test(
        name = name,
        timeout = timeout,
        srcs = ["//tests:rust_db_test.py"],
        main = "//tests:rust_db_test.py",
        data = [binary],
        env = {"MPAC_RUST_DB_TEST_BIN": "$(rlocationpath {})".format(binary)},
        tags = [
            "no-sandbox",
            "requires-docker",
            "requires-network",
        ],
        deps = ["//tests/contract:harness"],
        **kwargs
    )
