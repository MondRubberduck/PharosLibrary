"""python -m asset_service — the Pharos CLI.

    init    detect asset folders and write pharos_config.json
    serve   start the HTTP dashboard + API (same as browse.py)
    docs    regenerate the agent entry files (AGENT_START_HERE / AGENT_API)
"""

import sys


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        from .init import run_init
        return run_init(sys.argv[2:])
    if cmd == "serve":
        from .browse import main as serve_main
        return serve_main(["--no-open"] + sys.argv[2:])
    if cmd == "docs":
        from .agent_docs import run_docs
        return run_docs(sys.argv[2:])
    print(__doc__)
    return 0 if not cmd else 2


if __name__ == "__main__":
    raise SystemExit(main())
