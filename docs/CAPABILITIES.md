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
| MCP server | `python -m service.pharos_mcp_server` (stdio, 6 tools) | agents with MCP support (Claude Desktop, Cursor, …) |
| Web dashboard | `http://127.0.0.1:8765` in a browser | human verification of what the agent found |
| CLI | `python pharos.py init/serve`, `scanner.py`, `indexer.py`, `scene_manifest.py` | onboarding, ingestion, validation, builds |
| Agent index files | `<library_root>/_Agent_Files/*.md`, `*.jsonl` | offline reading when the server is down |

## 2. What the agent can ask

- **Geometry**: `/api/meshes` — find meshes by free-text query AND'd over
  name/tags/themes, by metre dimensions (`min_dim/max_dim/min_h/max_h`),
  pack, source, theme, triangle budget (`max_tri`), texture presence.
  Responses carry absolute FBX paths, real bounding boxes, triangle counts,
  material recipes (`recipe`, `hero_textures`), a 3D preview URL, and
  `dim_suspect` flags. `/api/meshes/packs` lists exact pack names first so
  `pack=` filters never fail on a guessed name.
- **Textures**: `/api/textures/items` — PBR sets by group/subcategory/query;
  item detail lists every map file in the set.
- **Audio**: `/api/audio/items` — sounds by category/subcategory/duration;
  in-browser playback via `/audiofile` (HTTP Range).
- **Purchases**: `/api/collection/items` — owned products by store/type,
  with the three-state availability model (`local` = on disk,
  `owned-not-downloaded` = purchasable, never silently substituted).
- **Animations**: `/api/anim/clips` — clip search across packs, played as
  rendered webm previews or live three.js viewer.
- **Search semantics** (all four item endpoints): AND-first over stemmed
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
| Converted packs anywhere | list their parent dirs in `manifest_roots` config | recipes/wiring join at import; schemas `pharos.pack.export/v2` and `pharos.kb3d.export/v1` (old `kiosk.*` IDs accepted) |

## 4. Onboarding a fresh library (no crawler output at all)

1. `python pharos.py init <root>` — classifies root's folders by content,
   writes `pharos_config.json`, prints what it found and what it could not
   classify. Detection is verbose and correctable by editing the JSON.
2. `python pharos.py serve` — all importers run at start; **every missing
   source degrades gracefully**: absent sections serve empty results
   (HTTP 200 with `total: 0`), never errors. Verified against a fixture
   library with no audio index, no crawler JSONLs, no manifest packs.
3. `python service/asset_service/scanner.py <model-folder>` — indexes raw
   mesh folders so `/api/meshes` works without any crawler.

## 5. Scene building (agent-authored scenes)

- `scene_manifest.py` — `pharos.scene/v1` JSON: assets (path, position,
  Y-up `rotation` [pitch, yaw, roll] — the builder converts to Blender's
  Z-up), scale, per-slot material recipes, texture sets (applied as
  ground planes), audio beds, crowd block; validator + on-disk path
  checker + triangle budget report (`--budget` warns loudly on any
  registry lookup miss — a mis-costed scene must never look confident).
- `scene_builder.py` — headless Blender builder: imports FBX at manifest
  scale (metres), converts Y-up rotations to Blender Z-up, positions the
  true hierarchy root, ground-snaps via world-space bbox corners on +Z,
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
`previews_dir`, `sections` (folder names per section), `agent_files`,
`thumb_cache_dirs`, `extension_status_file`, `indexer_skip_dirs`,
`manifest_roots`, `collection_csv`, `network`, `dashboard` curation.
No owner-specific path exists anywhere in the code; the only absolute
defaults left are version-pinned STANDARD install locations for optional
engines (e.g. Blender 5.1 under Program Files, UE 5.7 under the Epic
directory), every one of them env-overridable. `python pharos.py
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
  rather than guessing. Master-material placeholder fills can still
  surface in `recipe.primary` (known, documented).
- The budget checker reports; it does not fail the build.
- Single-user, localhost-only, no auth, no TLS — by design.
- Animation previews are rendered by a browser once per clip (the server
  only caches webm files); headless servers never render previews.
- The `pipeline/` index writers refuse to overwrite a live index with a
  near-empty one: `build_agent_index.py` (`packs.json` and `models.jsonl`),
  `build_availability_catalog.py`, `build_kb3d_index.py`, `gen_index.py`,
  `gen_tex_index.py`, `promote_native.py` and `promote_blends.py` all stop
  with `FATAL: only N records -- refusing to overwrite a live index` when
  they would write **fewer than 10 records**. The threshold is uniform:
  10, not a percentage. The guard is deliberate (a wrong root once
  overwrote a real library's index with fixture data), and it has a real
  cost: a library whose section holds fewer than 10 meshes, owned
  products, kits or native containers cannot build that index with those
  scripts until it grows. The server-side importers have no such floor and
  always run, so `init` / `serve` / `scanner.py` and the API are
  unaffected.
- The audio taxonomy cannot be overridden: `classify.OVERRIDES` in
  `pipeline/agent_index/classify.py` carries hand-checked
  re-categorisations of known misfiles, but `classify()` consults only its
  RULES table, so no entry there reaches a published category.
- Two servers on one port: `python pharos.py serve` does not detect an
  already-running instance. The stdlib server allows address reuse, so on
  Windows a second run binds 8765 successfully (observed: two processes
  LISTENING on 8765 at the same time) and which one answers a request is
  not deterministic. Stop the old server first.

## 8. Regenerating everything

The SQLite registry is derived data. Delete it and restart: importers
rebuild every table from the sources above. The same applies to previews
(re-render on view) and the agent index files (crawler-side builders).
