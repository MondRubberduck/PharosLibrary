# Changelog

## 0.2.1 (2026-09-18)
Honesty + pipeline-correctness pass, then the setup-experience phase
(Phase 0-2 of the audit roadmap; counts below are printed by the suites
themselves, never hand-typed).

- `pharos.py doctor` (+ `--json`): one command that checks python,
  config, registry schema, PER-SECTION coverage (a zero section is
  reported as "not indexed" with the filling command -- empty is a
  state, not an error), section folders on disk, generated-docs
  staleness, the configured port, and optional engines (Blender, UE,
  git-bash, MCP). Every gap carries a paste-ready fix.
- `pharos.py ingest`: runs every chain that is safe to run
  automatically in order (scanner for model folders and audio,
  animation indexer, the four importers, agent docs) and prints an
  ASK YOUR USER block with the decisions that must be relayed (UE pack
  crawl, KitBash3D/.blend handling, purchase CSV, crawl-quality
  indexes). `init` now surfaces .blend folders as such a decision.
- The texture index chain self-builds its `tex_meta.json` cache
  (previously a hand-made leftover no script produced -- the chain was
  author-machine-only). Dims come from a stdlib header sniffer
  (PNG/JPEG/GIF/BMP/TIFF/WEBP/EXR/HDR/PSD); unparseable files are
  "unknown", never guessed. `--rescan` rebuilds; `AGENT_TEX_META`
  overrides the cache path.
- docs/STARTING_PROMPT.md: the paste-ready setup prompt for users.
  docs/AGENT_SETUP_BRIEF.md: the agent's setup contract -- decision
  matrix, mandatory interview, definition of done. README, `init` and
  the generated AGENT_START_HERE point at them.
- scene_builder: a texture set whose display name matches no filename
  in its folder now falls back to ALL maps in the folder (the folder is
  the set the user pointed at). Found by live verification: a "1k"
  subfolder row used to silently lose the ground plane.
- Live verification (scratch harness, real registry): server boot +
  published counts, geometry search honouring metre filters with
  on-disk recipe spot checks, jailed image serving + traversal escapes,
  audio HTTP Range, MCP over stdio (6 tools, live numbers), and a
  headless Blender build from API-picked assets reopened and asserted
  (ground snapping, rotation conjugation, per-slot materials, live
  textures, textured ground plane).
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
