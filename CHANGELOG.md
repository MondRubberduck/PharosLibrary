# Changelog

## 0.9.0 (2026-09-23)
Release candidate for the public launch. Two scene-building stress tests and a real "stranger machine" run (fresh clone, other Windows laptop, other Python and Blender, messy test library) drove every change. The main setup flow now runs end to end without a single manual workaround.

**Setup works on other people's machines**
- **Old or foreign registry.** A registry left behind by another library, or by an older Pharos, now makes `ingest`, `serve`, `scanner.py` and `indexer.py` stop with one clear message that names the file to delete. Before, it crashed or silently mixed two libraries. Older registries are migrated before use. Re-running `init` one folder higher is accepted.
- **Converted packs.** Unreal packs and KitBash kits are imported by `ingest` itself, exactly once and with their material recipes.
  - `ingest` builds the model and kit indexes and adds newly converted pack folders to the config (additive only).
  - It notices packs that were copied in, removed, or sit at any depth.
  - The scanner no longer indexes converted packs a second time, and it removes scan rows left over from before a pack was converted.
- **Small libraries.** They no longer hit "fewer than 10 records = FATAL". The index guard now only refuses a near-empty result replacing a real index.
- **Any animation folder name.** doctor, the agent docs, MCP, previews and chain 3 all use the configured section instead of assuming `Animation`. `/batchrender` reports failures honestly.
- **Any Blender, any Unreal.** Blender is found by `BLENDER_EXE`, then PATH, then the newest installed version, instead of a hard-coded Blender 5.1 path. Unreal 5.5+ conversions no longer fail on missing vertex counts; the verifier reads them from the exported FBX.
- **Non-Latin folder names.** Japanese, Cyrillic and emoji folder names no longer crash init, ingest or the scanner when an agent reads the output through a pipe.
- **Input files as people write them.** A `pharos_config.json` saved with a BOM (Windows PowerShell) is read. A purchase CSV with headers like `name,url,price` imports correctly.
- **Configured sections.** `ingest` scans the configured audio folder, and `init` prints the sections it actually writes.

**The library tells the truth**
- **Material recipes flag, never drop.** Placeholder and master-default textures are flagged, masks are no longer used as ORM maps, and the served `primary` pick is the one the Blender builder wires. Exact role aliases (e.g. `Base Map`, `Emmisive`, `NRM`) are recognised.
- **Real counts and heights.** Triangle and vertex counts come from the exported FBX, and unknown counts are stored as NULL instead of 0. `height_m` is the Z (up) extent.
- **Complete crawls.** Texture sets without a preview image and HDR/EXR panoramas are indexed, and macOS metadata files are skipped.
- **Search.** Plural words match (`crates` finds `crate`), audio search no longer floods, and every search endpoint reports `mode`/`exact`. Audio ids stay stable across restarts.
- **Chain 3 stays in scope.** It no longer re-indexes folders `ingest` already scans or kit exports that the kit index covers.

**Tests and CI**
- One CI step per test suite, the MCP check really runs in CI, and a new Linux job builds and reopens scenes in headless Blender.
- Tests never touch a user's config or pipeline outputs.

**Docs**
- The README has an artist-friendly "Get started" and measured token numbers.
- Setup, playbook, capability, pipeline and MCP docs were corrected against the code.

## 0.3.0 (2026-09-21)
Generalization: the second external-review batch. Pharos now works on
libraries that do not look like the author's own — verified with a
stranger-layout end-to-end simulation, MCP-over-stdio, and a real
Blender crowd regression.

- `.fbx` counts as a MESH extension now; the animation-vs-mesh tie is
  broken by binary evidence (AnimStack/AnimationCurve markers via the
  existing probe). A folder of static FBX models is a MODEL folder and
  gets scanned (it used to become the "animation" section and index
  nothing). Pure-BVH or marker-positive folders stay animation.
- Files DIRECTLY in the library root are counted and reported loudly
  (they are invisible to every section; a flat library used to index
  NOTHING with every signal green).
- Section picks are weighted by per-kind asset count, and the
  catalog-CSV folder never wins a content section (alphabetical order
  once let a 2-screenshot CSV folder hijack "textures").
- init fails loudly (exit 2) when an existing config points at a
  DIFFERENT library and --force is absent; the old silent success
  indexed the wrong library.
- ingest: scan folders come from the config (`scan_folders`, written by
  init) with drift reporting; purchases whose Local Folder exists on
  disk are marked 'local' (the availability ladder ran only via a
  script nothing invoked); exit codes are honest (4 = a step failed,
  3 = nothing was indexed at all).
- Dimension/height filters exclude unmeasured meshes and disclose how
  many were hidden (`unmeasured_excluded`); generated docs carry the
  configured port/interpreter and the "zero means EITHER no content OR
  not indexed — run doctor" honesty.
- Scan rows are keyed by file path (meshes) and section-relative path
  (audio): same-named assets in sibling folders both survive one scan,
  and separately-scanned audio folders no longer overwrite each other.
- Texture sets group by (folder, stem): same-named sets from different
  packs stay separate and resolution subfolders stop becoming phantom
  map channels. macOS AppleDouble (._*) and .DS_Store files are
  skipped.
- scene_builder: manifest positions convert Y-up -> Blender Z-up (the
  same Rx90 mapping rotations get — verbatim positions once floated
  assets at manifest-depth as height); the crowd ring lies on the
  GROUND plane (it was vertical: 4 of 6 instances ±1.7 m in the air);
  every crowd instance duplicates the WHOLE hierarchy (linked armature
  duplicates used to leave 5 of 6 instances as invisible skeletons);
  build() validates the manifest first and refuses invalid ones.
  All proven by reopened-.blend measurement.
- scene_manifest: path checks always run (one cosmetic unknown key used
  to suppress every missing-file error); unknown top-level keys are
  informational notes, not errors.
- MCP: `total` is the true match count (it was the truncated window);
  texture resolution filters match case-insensitively; the
  library-first doctrine ladder is in the pharos_stats docstring.
- HTTP: search tokenizers accept non-ASCII word characters AND fold
  accents both sides ("vase" finds "Vâse"); OR-fallback mesh items
  carry the same fields as primary items (fbx_path/view_url/height_m);
  pack-id regex accepts &, ', ,, !, # (legal vendor names);
  open_explorer dispatches per-OS (os.startfile was Windows-only ->
  500 elsewhere); socket timeout + connection-drop guard; audiofile
  suffix ranges (bytes=-N) and out-of-range clamps fixed; LIKE
  wildcards escaped in pack-file queries; non-loopback bind warning
  now says what actually happens (LAN browsers get 403 from the Host
  allow-list).
- collection_import sniffs the CSV delimiter (semicolon Excel exports
  used to import N rows of empty strings as success).
- Pipeline: verify_pack_export accepts SkeletalMesh with null triangles
  by deriving the count from the exported FBX; macos-latest added to
  the CI matrix; convert_packs.sh replaced mapfile (bash 3.2 on stock
  macOS); the dead `vision` extra is gone; THIRD_PARTY_NOTICES carries
  the full MIT text; README's "delete the registry" claim scoped
  honestly (crawler tables rebuild automatically; scan rows return on
  the next ingest).


## 0.2.3 (2026-09-19)
External-review Batch 2: the pipeline layer's silent-success failure
modes. Fixes proven where engines allow (two real-Blender end-to-end
runs in verification), code-verified where they need UE (noted below).

- `verify_pack_export`: SkeletalMesh packs verify again — editor
  metrics expose no triangle count, so the exporter legitimately writes
  null and every skeletal pack FAILED verification forever; the count
  is now derived at verify time from the exported FBX (engine-free,
  regression-tested with real FBX bytes).
- `relink_pack.sh`: the verdict honors the relink report's `ok` flag —
  a FATAL/ignored relink left the v1 manifest, the verifier PASSed the
  valid v1 file, and the run exited 0 with the tool's purpose silently
  no-opped. Failing verdicts now exit 7 (code-verified; runtime proof
  needs UE).
- KitBash export failures are no longer permanent: an all-failed export
  writes NO manifest (drivers skip on its existence) and exits 1 with a
  `KB3D_EXPORT_FAILED` marker; the batch driver requires the completion
  marker AND a clean Blender exit, re-exports zero-group or corrupt
  manifest residue instead of skipping it, and exits nonzero when kits
  were lost. Proven end-to-end in headless Blender (good kit exports;
  empty kit leaves no manifest; residue gets re-exported).
- The audio index cache is root-stamped: switching `AGENT_AUDIO_ROOT`
  rescans instead of serving another root's data; `--rescan` forces and
  `AGENT_AUDIO_META` overrides the cache path (the texture chain got
  all three in 0.2.1; audio now matches).
- The native indexing worklist is written LF-only (CRLF rode through
  Git Bash `read -r` as `path\r` and Blender silently failed on every
  entry), and `index_native_all.sh` checks Blender's exit codes — a
  crashed run now exits 9 instead of always 0 (stub-tested), honors
  `BLENDER_EXE`, and fails loudly when no output was written at all.
- scene_builder's KitBash texture remap cache can hit again: the cache
  keys stripped `kb3d_` while the lookup kept it (the O(N)→cached
  optimization was dead code since its birth); both sides now share
  pure helpers in `asset_service.kb3d_paths`, symmetry pinned by unit
  test, and the full build+reopen regression still passes.
- indexer pruning is scoped to the indexed root's `canonical_root`: an
  offline second library root is no longer wiped from the registry when
  another root gets re-indexed (regression-tested with two roots).
- `convert_packs.sh --no-replace` checks the REAL target layout when
  `--out-root` redirects output (the gate was vacuous in exactly that
  case).

## 0.2.2 (2026-09-19)
External-review Batch 1: fresh-install crash, two XSS sinks, dead UI
features, re-scan duplication, and importer/pipeline robustness — every
fix regression-tested, live-verified against the real registry, and the
whole contract re-run green.

- `pharos.py ingest` no longer crashes on a library without a purchase
  CSV (FileNotFoundError killed the chain before textures/audio/meshes
  ran; each importer is now individually guarded, matching serve).
- Stored-XSS closed on the registry grid and the pack page: every
  interpolated value escaped, ids/exts travel in data-attributes read
  by delegated listeners instead of inline onclick string splices
  (pack ids are folder paths; `'` is a legal Windows filename char).
- Template extraction artifacts fixed: the pack page's per-file "3D"
  link never rendered and the type column always showed `.` (regexes
  carried literal `\\.` from their Python-string origins); the assets
  page stripped DIGITS from prices (`[^\\d.]`). Served-page scripts now
  syntax-checked during verification.
- Texture sets no longer duplicate on every re-scan: a partial unique
  index (name, folder WHERE source='scan') makes the scanner's
  INSERT OR REPLACE actually replace; legacy duplicates are collapsed
  by a self-migration (verified on a copy of the live registry).
- The animation section got the token-bomb guard every other section
  had (`q=???` returned the whole library).
- One corrupt FBX can no longer abort a whole scan: fbx_dims raises a
  parse error instead of IndexError when a property length walks past
  the buffer, and the scanner wraps per-file work with section-level
  commits and a guaranteed close (a poison file now yields a zero-dims
  row — recorded, never guessed).
- meshes_import: cp1252/latin-1 fallback for crawler jsonls (one bad
  filename byte aborted the geometry rebuild with 0 rows; audio had
  the chain since 0.2.1).
- Animation page in-page previews render again (`holderHelper` was
  never declared — strict-mode ReferenceError on every render) and BVH
  viewer playback works (the loader got an ArrayBuffer where it
  string-splits; now TextDecoder-decoded).
- MCP server: refuses to CREATE a registry (preflight + mode=rw — an
  unconfigured start used to drop a stray empty assets.sqlite in the
  client's CWD), the texture resolution filter runs BEFORE the limit,
  and animation_clips counts only Animation packs (was every FBX in
  the DB; live: 1313, not 10281).
- promote_native can no longer feed the stage-2 blends file to the
  wholesale overwrite (it sorts after every timestamped run; the
  newest-wins glob introduced in 0.2.1 would have erased all
  FBX/OBJ container records once run_blends had run).
- `/api/open_explorer` answers HEAD with 405 (probes — link checkers,
  curl -I — must never launch Explorer), and the server now prints the
  loud non-loopback-bind warning SECURITY.md always promised.
- Housekeeping: `__version__` re-synced (drift test added),
  test_smoke_entry has a `__main__` (was a silent exit-0 no-op), CI
  matrix sets `fail-fast: false` (one leg's failure no longer hides
  the others — the run-1 lesson), leak_scan covers one more
  library-content marker and its three occurrences are scrubbed, pack
  page reads `validation_status` (the "human verified" chip can now
  appear).

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
- CI actually green for the first time: a multi-line f-string expression
  in `init.py` was a SyntaxError on Python 3.10 (legal on 3.12, so it
  never failed locally) and had failed the first CI job -- and thereby,
  via fail-fast, every single CI run since the workflow was added. Found
  by auditing the CI history during the 0.2.1 release push; fixed and
  covered by an AST scan across the codebase (this was the only
  occurrence).
- Release hygiene: README rewritten around a clear user/agent split;
  removed superseded and orphaned one-off scripts (convert_all.sh,
  tree_fingerprint.py, index_blends.sh, verify_kb3d_fbx.py,
  verify_fbx_blender.py, hash_files.py, scan_corpus.py) and the
  internal tagging plan.
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
