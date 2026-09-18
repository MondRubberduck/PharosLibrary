"""python -m asset_service — the Pharos CLI.

    init    detect asset folders and write pharos_config.json
    doctor  check setup end-to-end; print exact fixes for every gap
    ingest  run the safe import chains in order; print the decisions
            that need the user (UE crawl? blend handling? purchase CSV?)
    serve   start the HTTP dashboard + API (same as browse.py)
    docs    regenerate the agent entry files (AGENT_START_HERE / AGENT_API)
"""

import sys


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        from .init import run_init
        return run_init(sys.argv[2:])
    if cmd == "doctor":
        from .doctor import run_doctor
        return run_doctor(sys.argv[2:])
    if cmd == "ingest":
        from .ingest import run_ingest
        return run_ingest(sys.argv[2:])
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
