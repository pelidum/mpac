import os

import pytest

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    selected = os.environ.get("MPAC_CONTRACT_TESTS", "").split()
    targets = [os.path.join(here, name) for name in selected] or [here]
    raise SystemExit(pytest.main([*targets, "-v", "-p", "no:cacheprovider"]))
