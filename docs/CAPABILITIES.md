# Pharos — Agent Capability Matrix

What this codebase lets a downstream coding agent do, exactly as the code
stands today. Every claim here is verifiable against the running system;
nothing on this list is aspirational. If a capability is missing or
limited, it says so under "Limits".

The one-line pitch: **an agent queries Pharos instead of walking the
filesystem, and answers asset questions in seconds at a fraction of the
token cost.** Operating manual (setup + build doctrine):
`AGENT_PLAYBOOK.md` in this folder.

---

## 1. Interfaces the agent can use

| Interface | How | Use for |
|---|---|---|
| HTTP JSON API | `GET http://127.0.0.1:8765/api/*` (localhost, no auth) | everything — search, details, file bytes, previews |
| MCP server | `python -m service.pharos_mcp_server` (stdio, 10 tools) | agents with MCP support (Claude Desktop, Cursor, …) |
| Web dashboard | `http://127.0.0.1:8765` in a browser | human verification of what the agent found |
| Setup page | `http://127.0.0.1:8765/setup` (`python pharos.py setup` opens it); API `/api/setup/state`, `/api/setup/survey`, `/api/setup/map`, `/api/setup/confirm`, `/api/setup/progress`, `/api/fs/drives`, `/api/fs/list` (folder browsing on loopback binds only) | picking the library, correcting the survey, deciding, indexing; a server without a config serves only this |
| CLI | `python pharos.py setup/init/survey/map/ingest/serve/doctor`, `scanner.py`, `indexer.py`, `scene_manifest.py` | onboarding, ingestion, validation, builds |
| Agent index files | `<library_root>/_Agent_Files/*.md`, `*.jsonl` | offline reading when the server is down |

## 2. What the agent can ask

- **Geometry**: `/api/meshes` — find meshes by free-text query (every
  word must match; plurals, aliases and `-word` exclusions understood;
  best match first), by metre dimensions (`min_dim/max_dim/min_h/max_h`),
  pack, source, kind, theme, triangle budget (`max_tri`), texture presence.
  Answers are SHORT items (about 90 tokens each): stable `key` (the
  library-relative file path), real bounding box, triangle count, themes,
  whether a material recipe exists (`wired`). The full record of a chosen mesh — material
  recipe (`recipe`, `hero_textures`), material names, placement corners,
  3D preview URL — comes from `/api/meshes/item?key=` (or `detail=1`).
  `/api/meshes/packs` lists the pack names, their wiring health and the
  linked purchases (whose store images show what the pack looks like).
  Full records are size-budgeted (about 20k tokens per answer; the rest
  is listed in `not_returned`), and a key goes into a URL fully
  percent-encoded (`#` → `%23`).
  `bbox_m` is [x, y, z] metres, Z-up, so the height is `bbox_m[2]` (scan
  rows keep their raw file axes). A model's file is the answer's `root`
  plus its `key` (for a `container` item, minus the key's last
  `#object` part); a model outside the library carries its absolute
  `fbx`, and full records always do. Persist
  `key`: `id`s are session handles and `(pack, name)` is not unique.
  The models in an answer were on disk when indexed (`missing_file`
  marks one that was not); a search with query words also names the
  owner's matching purchases that are NOT downloaded
  (`owned_not_downloaded`, following the search's style), so an agent
  can ask for them.
- **Honest answers**: every list answer says how it matched (`mode`,
  `exact`, per-item `tier`), which parameters it could not use
  (`warnings`) and which library words are close to an unknown query word
  (`did_you_mean`).
- **Recipe contract**: `recipe.slots[].maps[]` serves every texture the
  material references, flagged instead of dropped: `source`
  (`override | default | unnamed`, best first), `placeholder: true` for
  master-material fill textures, `role` incl. `packed` with `channels`
  `{r|g|b|a: ao|roughness|metallic|height}` (null = order unknown, never
  assume one) and `mask` (never an ORM). A slot with `resolved: false`
  (+ `unresolved_reason`) keeps the FBX's own material. `recipe.primary`
  and `hero_textures` are the builder's own pick: no placeholders, no
  `default`-source emissive/opacity/height, and no `default`-source map
  for a role the slot's own packed map supplies (roughness / metallic /
  ao then come from `packed_channels`).
- **Textures**: `/api/textures/items` — PBR sets by group/subcategory/query,
  with the MEASURED pixel size (`px`, `res`; filters `res=`, `min_px=`)
  and the map roles each set has (`has=height`); item detail lists every
  file and its role (`maps`).
- **Audio**: `/api/audio/items` — sounds by category/subcategory/duration
  (`min_dur`, `max_dur`);
  in-browser playback via `/audiofile` (HTTP Range).
- **Purchases**: `/api/collection/items` — owned products by store/type,
  with the three-state availability model (`local` = on disk,
  `owned-not-downloaded` = bought but not downloaded, never silently
  substituted).
- **Animations**: `/api/anim/clips` — clip search across packs, played as
  rendered webm previews or live three.js viewer.
- **Search semantics** (all five item endpoints): AND-first over stemmed
  tokens with word-boundary short-token matching, automatic ranked
  OR-fallback, per-item `tier` and response `mode`/`exact` so an agent can
  tell exact hits from adjacent ones. Queries whose tokens are all too
  short return zero results (never the whole library).
- **Files, jailed**: `/file` (FBX/BVH), `/timg`, `/cimg`, `/audiofile`,
  `/res`, `/preview/<key>.webm` — all path-jailed to configured roots.
- **OS integration**: `/api/open_explorer` (`dry=1` resolves a path without
  opening) — show-in-Explorer / copy-absolute-path for the human.

## 3. What content the system can absorb

| Content | Ingestion path | Notes |
|---|---|---|
| Any mesh folder (FBX/OBJ/GLB/STL/BLEND) | `python service/asset_service/scanner.py <folder>` | FBX: real bbox in metres + exact triangle/vertex counts via the stdlib binary walker (`fbx_dims.py`, ground-truth verified vs Blender measurements, 197/200 within 5%); OBJ parses vertices; GLB/STL via trimesh if installed; .blend indexed as name+path placeholder |
| Texture folders (PBR sets, loose map families) | `textures_import` at server start, or scanner | sets grouped per leaf folder / stripped map-channel suffixes |
| Audio folders | `scanner.py` (WAV duration from header) or crawler `library_files.jsonl` | jsonl path additionally brings categories, keywords, sample rates |
| Animation clip packs (FBX/BVH) | `indexer.py --root <library_root>` | builds the pack grid; previews are rendered once by the browser |
| Purchase catalog | any CSV with Name/URL/Price-ish headers (`collection_csv` config key names it) | three-state availability via optional `availability.jsonl` |
| Unreal `.uasset` packs | `pipeline/conversion/` (optional; needs UE 5.x + Git Bash) | exports FBX+textures+manifest per pack, metres @ scale 1.0, engine-exact material wiring; source libraries stay read-only (sandbox copy + tree fingerprints) |
| KitBash-style `.blend` kits | `pipeline/kitbash/` (optional; needs Blender) | one FBX per `_grp` assembly + `kit_manifest.json` |
| Raw FBX/OBJ/BLEND containers | `pipeline/native/` (optional; needs Blender) | Blender read-only enumeration → measured records |
| Converted packs (any depth inside the library) | found by `pharos.py ingest`, which adds their parent folders to `manifest_roots` and builds the model/kit index | recipes/wiring join at import; schemas `pharos.pack.export/v2` and `pharos.kb3d.export/v1` (old `kiosk.*` IDs accepted) |

## 4. Onboarding a fresh library (no crawler output at all)

0. `python pharos.py setup` opens the setup page (`serve` without a config
   serves the same page and prints its URL): folder browser, the survey's
   tree with kind / index / decision per folder, *Start indexing*, then the
   doctor verdict and the engine chains the decisions ask for. Steps 1 and
   2 are the same flow from the shell.
1. `python pharos.py init <root>` surveys the root (structural markers
   first: `Exports/manifest.json`, `kit_manifest.json`, `.uproject`,
   `Assets/` + `ProjectSettings/`, a purchase CSV header; file shares and
   names only as a guess) and writes the **library map**,
   `<root>/_Agent_Files/library_map.csv`: one row per folder that is a
   thing, with kind, state, evidence and the decision its kind needs.
   `pharos_config.json` mirrors the map. `python pharos.py survey <root>`
   prints the tree; `python pharos.py map set "<folder>" kind=...` or
   the server's `/tags` page correct it. User rows survive a re-survey.
2. `python pharos.py ingest` runs every engine-free chain and asks the
   missing decisions. `python pharos.py convert | extract | previews`
   run the engine chains the map allows (`--dry-run` prints the
   commands); without the decision they refuse.
3. `python pharos.py serve` — all importers run at start; **every missing
   source degrades gracefully**: absent sections serve empty results
   (HTTP 200 with `total: 0`), never errors. Verified against a fixture
   library with no audio index, no crawler JSONLs, no manifest packs.

## 5. Scene building (agent-authored scenes)

The generated `SCENE_BUILD.md` beside the agent docs is the agent's
guide: scene-file format, how search results go in, the commands.

- `scene_manifest.py` — `pharos.scene/v1` JSON: assets (a search `key`
  — resolved to the file and, for `#object` keys, the one object — or a
  path; `"materials": "recipe"` pulls the registry's per-slot recipe),
  position,
  Y-up `rotation` [pitch, yaw, roll] — the builder converts to Blender's
  Z-up), scale, per-slot material recipes, texture sets (applied as
  ground planes), audio beds, crowd block; validator + on-disk path
  checker + triangle budget report (`--budget` warns loudly on any
  registry lookup miss — a mis-costed scene must never look confident).
- `scene_builder.py` — headless Blender builder: imports `.fbx`, `.obj`,
  `.glb`/`.gltf` and objects of `.blend` files at manifest scale
  (metres), keeps only the asset's object of a multi-object file,
  converts Y-up rotations to Blender Z-up, positions the true hierarchy
  root (loose parts get one common root), ground-snaps via world-space bbox corners on +Z,
  rebuilds Principled BSDF materials from recipes incl. packed ORM
  channel separation, **emissive (additive emission) and opacity (alpha
  blend)**, **per-slot wiring** from `recipe.slots[]` (multi-slot KitBash
  buildings keep their slots), **repoints dead KitBash texture
  references** at the shipped `.png.2k` folder, applies
  `manifest.textures[]` as ground planes, places crowd with real
  per-instance animation stagger. Runs:
  `blender --background --factory-startup --python scene_builder.py
  -- manifest.json` (`--factory-startup` is REQUIRED: user Blender
  extensions can segfault headless runs). Manifest `audio[]` entries are
  recorded for DOWNSTREAM engines only: the Blender builder
  intentionally does not import audio into scenes (nobody does that in
  practice); UE/Unity and video editors consume those paths.
- `verify_importable.py` — one-file-per-process Blender import probe: an
  FBX that hard-crashes Blender's importer (exit 0xC0000005) becomes a
  five-second answer instead of a debugging session.

## 6. Configuration contract

One JSON file (`pharos_config.json`, created by init or copied from
`pharos_config.example.json`): `library_root`, `registry_dir`,
`previews_dir`, `agent_files`, `thumb_cache_dirs`,
`extension_status_file`, `network`, `dashboard` curation. The keys
`sections`, `scan_folders`, `manifest_roots`, `kitbash_root`,
`indexer_skip_dirs` and `collection_csv` are written by `ingest` from
`_Agent_Files/library_map.csv`; change the map, not these keys.
No owner-specific path exists anywhere in the code; the only absolute
defaults left are STANDARD install locations searched for optional
engines (Blender: `BLENDER_EXE`, then PATH, then the newest version under
Program Files or /Applications; Unreal: `UE_EXE`), every one of them
env-overridable. `python pharos.py
init` defaults the registry and previews to `~/.pharos/`. Asset files are
never written: the only writes inside the library root are generated index
files (`<agent_files>/`, plus `library_index.json` / `library_files.jsonl`
in the audio and texture roots) and the `Exports/` folders the conversion
pipelines own.

## 7. Limits (honest list)

- Triangle/vertex counts from raw FBX are fan-triangulation exact for
  triangle/quad meshes; per-material recipes still require the conversion
  pipeline's manifests (UE) or crawler output.
- `.blend` files are indexed (name/path/container) but not measured;
  `.max` is unreadable.
- Material recipes exist only where manifests joined (~75% of a fully
  converted library); unresolvable chains report `resolved: false`
  rather than guessing. Master-material placeholder fills and default
  maps stay listed in `recipe.slots[]`, flagged (`placeholder`,
  `source: default`); they never reach `recipe.primary`/`hero_textures`
  or the builder's wiring.
- The budget checker reports; it does not fail the build.
- Single-user, localhost-only, no auth, no TLS — by design.
- Animation previews are rendered by a browser once per clip (the server
  only caches webm files); headless servers never render previews.
- The `pipeline/` index writers refuse to overwrite a live index with a
  near-empty one: `build_agent_index.py` (`models.jsonl`),
  `build_availability_catalog.py`, `build_kb3d_index.py`, `gen_index.py`,
  `gen_tex_index.py`, `promote_native.py` and `promote_blends.py` all stop
  with `FATAL: only N ... -- refusing to overwrite the live index` when a
  result of **fewer than 10 records, and under half of the live index**,
  would replace a live index that holds 10 or more. The guard is
  deliberate (a wrong root once overwrote a real library's index with
  fixture data). A first build, a small library, or one that lost a pack
  or two is written normally.
- The audio taxonomy cannot be overridden: `classify()` in
  `pipeline/agent_index/classify.py` consults only its RULES table.
  Hand-checked re-categorisations of known misfiles exist, but they live
  in the GITIGNORED `pipeline/agent_index/audio_overrides.json` (kept
  out of the repo because they carry library-specific file titles) and
  are loaded by nothing -- no entry there reaches a published category.
- Two servers on one port: `python pharos.py serve` does not detect an
  already-running instance. The stdlib server allows address reuse, so on
  Windows a second run binds 8765 successfully (observed: two processes
  LISTENING on 8765 at the same time) and which one answers a request is
  not deterministic. Stop the old server first.
- One library root at a time (by design, owner decision 2026-09-19):
  pack ids are root-relative (`pack::<folder path>`). Cross-root PRUNING
  is scoped correctly (indexing root B cannot delete root A's packs),
  but indexing two roots whose packs share the same relative path
  overwrites one registry row with the other. Pharos assumes a single
  `library_root`; point it at one root and keep it there.

## 8. Regenerating everything

The SQLite registry is derived data. Delete `assets.sqlite` and run
`python pharos.py ingest`: it re-runs the scanner and the animation
indexer, rebuilds the model/kit indexes and every table from the library.
Previews re-render on view.
