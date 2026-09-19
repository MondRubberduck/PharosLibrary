"""pytest entry point for the fresh-install smoke contract.

`pyproject.toml` sets testpaths=["tests"], but the smoke suite itself has
no pytest-collectable functions (it is a scripted end-to-end run that
self-spawns an isolated child to escape module-level config binds). Left
unwired, plain `pytest` reported green while silently skipping the
largest suite in the repo. This wrapper makes pytest run the contract.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_fresh_install_smoke_contract():
    r = subprocess.run(
        [sys.executable, "-B", str(REPO / "tests" / "test_fresh_install.py")],
        cwd=str(REPO), timeout=900)
    assert r.returncode == 0, (
        "fresh-install smoke suite failed -- run "
        "`python -B tests/test_fresh_install.py` directly for full output")


if __name__ == "__main__":
    # a bare `python tests/test_smoke_entry.py` used to exit 0 having run
    # NOTHING (no __main__) -- the exact silent-no-op this repo despises
    test_fresh_install_smoke_contract()
    print("test_smoke_entry: contract PASS")
