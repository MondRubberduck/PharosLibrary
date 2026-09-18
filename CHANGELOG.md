# Changelog

## 0.2.1 (2026-09-18)
Honesty + pipeline-correctness pass (audit follow-up; counts below are
printed by the suites themselves, never hand-typed).

- Data retention now truly covers all three tables: the audio table
  gained a `source` column and `audio_import` no longer wipes
  scanner-indexed rows when a crawl jsonl exists (UNIQUE(rel) contract:
  the crawl row replaces the scan row for files it knows, scan-only
  files survive). Regression-tested.
- CSRF hardening on the localhost server: mutating POST endpoints reject
  foreign `Origin` headers (403) and opaque content types (415);
  `/api/open_explorer` rejects cross-site `Sec-Fetch-Site` requests.
  Non-browser clients (curl, agents) are unaffected. Regression-tested.
- `verify_pack_export`: the "v2 manifest without a wiring block is a
  hard fail" guard actually fires now (it compared a string against a
  tuple and never triggered). Regression-tested via mutation.
- `build_kb3d_index`: relative kit FBX paths (as written by
  `export_kb3d`) are absolutized instead of landing in the index dead.
  Regression-tested.
- `promote_native`: promotes the NEWEST index run; a hard-coded 2026-09-15
  snapshot name used to win first and serve stale data forever.
- `convert_packs.sh`: `--no-replace` is implemented (was parsed and
  silently ignored) and game-root splitting no longer breaks on /Game
  folders containing spaces.
- The UE conversion sandbox is no longer a hidden prerequisite: it was
  never shipped (its EngineAssociation must match the local UE), but the
  drivers dead-ended without saying so. New `pipeline/conversion/
  make_sandbox.py` generates it locally (version derived from `UE_EXE`),
  and both drivers now print that exact command when it is missing.
- Fresh-install smoke suite prints its own check count; a pytest wrapper
  (`tests/test_smoke_entry.py`) makes plain `pytest` run the contract —
  previously pytest reported green while skipping it entirely.
- New `tests/test_pack_verify.py` regression suite for the pipeline
  layer, wired into CI.
- README: "no pip installs" claims scoped honestly (optional MCP adds
  one); macOS marked as should-work/CI-unverified instead of implied
  verified. CHANGELOG 0.2.0 check counts corrected (40 source-level /
  42 executed, not "44").
- Public-repo hygiene: library-specific pack names and provenance notes
  removed from tracked pipeline sources (moved to gitignored local JSON
  files); `leak_scan.py` covers those markers and no longer skips
  filenames containing spaces. Pipeline scratch outputs gitignored.

## 0.2.0 (2026-09-18)
- Two-phase agent workflow (playbook), ingestion summaries + interview
- Per-slot material wiring, emissive/opacity, ground/asset texture sets,
  Y-up rotation conjugation, crowd stagger, orphan purge
- stdlib FBX dimension parser (fbx_dims), hardened against truncation
- Pipeline chains shipped in-repo (conversion / kitbash / native /
  agent_index) with config-driven paths
- Security: Host allow-list, real path jails, no shell=True
- Data retention: scanner rows survive every rebuild (meshes, textures —
  audio followed in 0.2.1); indexer refuses to prune on unreachable roots
- Fresh-install smoke suite (42 executed checks) + CI (win/linux, py3.10/3.12)

## 0.1.0 (2026-09-16)
- Initial public shape: HTTP API + dashboard, MCP server (6 tools),
  SQLite registry rebuilt from crawler outputs, scene manifest +
  headless Blender builder
