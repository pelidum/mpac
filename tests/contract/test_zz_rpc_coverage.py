import harness


def test_every_mpac_rpc_is_exercised():
    missing = harness.all_rpc_paths() - harness.RECORDER.methods
    assert not missing, f"RPCs without contract coverage: {sorted(missing)}"
