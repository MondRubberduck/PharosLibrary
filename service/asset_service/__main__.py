"""python -m asset_service — the Pharos CLI.

    init    detect asset folders and write pharos_config.json
    survey  classify a library's folders by content (read-only; --json)
    map     the library map: show | refresh | set <folder> k=v | apply | pending
    convert / extract / previews   the engine chains, gated by the map's decisions
    measure <file> | --clips <folder> | --index <folder>
            the standard-library measure: facts of one file, the clips
            table of an animation folder, the index of a model folder
    doctor  check setup end-to-end; print exact fixes for every gap
    ingest  run the safe import chains in order; print the decisions
            that need the user (UE crawl? blend handling? purchase CSV?)
    serve   start the HTTP dashboard + API (same as browse.py); without a
            config it starts in SETUP MODE and serves only /setup
    setup   start the server and open the setup page: pick the library
            folder, correct the map, index
    docs    regenerate the agent entry files (AGENT_START_HERE / AGENT_API)
"""

import sys


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        from .init import run_init
        return run_init(sys.argv[2:])
    if cmd == "survey":
        from .survey import run_survey
        return run_survey(sys.argv[2:])
    if cmd == "map":
        from .library_map import run_map
        return run_map(sys.argv[2:])
    if cmd in ("convert", "extract", "previews"):
        from .chains import run_chain
        return run_chain(cmd, sys.argv[2:])
    if cmd == "measure":
        from .measure import main as measure_main
        return measure_main(sys.argv[2:])
    if cmd == "doctor":
        from .doctor import run_doctor
        return run_doctor(sys.argv[2:])
    if cmd == "ingest":
        from .ingest import run_ingest
        return run_ingest(sys.argv[2:])
    if cmd == "serve":
        from .browse import main as serve_main
        return serve_main(["--no-open"] + sys.argv[2:])
    if cmd == "setup":
        from .browse import main as serve_main
        return serve_main(["--setup"] + sys.argv[2:])
    if cmd == "docs":
        from .agent_docs import run_docs
        return run_docs(sys.argv[2:])
    print(__doc__)
    return 0 if not cmd else 2


if __name__ == "__main__":
    raise SystemExit(main())
