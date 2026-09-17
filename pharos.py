"""pharos.py — repo-root CLI launcher (no install, no PYTHONPATH needed).

    python pharos.py init [ROOT]   detect asset folders, write pharos_config.json
    python pharos.py serve         start the dashboard + API on 127.0.0.1:8765
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "service"))

from asset_service.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
