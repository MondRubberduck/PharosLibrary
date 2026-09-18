# Contributing

Pharos is stdlib-only Python by deliberate choice - the core server must
run with zero pip installs. Keep it that way (Engines/Blender/UE stay
optional, pipeline-side).

Before every commit: `python -B tests/test_db.py && python -B
tests/test_uasset_parser.py && python -B tests/test_pack_verify.py &&
python -B tests/test_fresh_install.py` must pass (or just run `pytest` —
the smoke suite is wired in via `tests/test_smoke_entry.py`). The
fresh-install suite is the product contract: a stranger's machine, from
`pharos.py init` to queryable data.

- The library on disk is READ-ONLY. Tests must use fixtures under tmp.
- Counts in docs are generated from the live registry, never typed.
- Personal data (names, machine paths, pack names) must never enter
  tracked files - `tools/leak_scan.py` checks.
- Bug fixes ship with a regression check in the suite, not prose.

UI is plain-HTML/no-build templates; backend is stdlib `http.server`.
