import pytest

from server.mpac_proto.testdata import generate_golden


def test_golden_matches_service_proto():
    with open(generate_golden.GOLDEN_PATH, encoding="utf-8") as f:
        committed = f.read()
    assert committed == generate_golden.render(generate_golden.generate()), (
        "Regenerate with: bazel run //server/mpac_proto/testdata:generate_golden"
    )


def test_golden_covers_every_message():
    names = {
        entry["message"]
        for entry in generate_golden.generate()
        if not entry["variant"].startswith("seed:")
    }
    assert len(names) == len(
        generate_golden.service_pb2.DESCRIPTOR.message_types_by_name
    )


def test_golden_includes_test_db_seeds():
    variants = [entry["variant"] for entry in generate_golden.generate()]
    assert any(v.startswith("seed:items/") for v in variants)
    assert any(v.startswith("seed:") and "items/" not in v for v in variants)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
