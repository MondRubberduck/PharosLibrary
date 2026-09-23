# Pharos Pipeline — optional conversion & indexing tools

These tools produce the RICH index data (material recipes, real
dimensions, wiring, availability) that the base scanner cannot extract
alone. Everything here is **optional**: the app runs fine without ever
touching this folder. Engines required per chain are listed below —
without them, use `service/asset_service/scanner.py` instead.

All Python tools read the SAME `pharos_config.json` as the app
(`PHAROS_CONFIG` env var overrides the path); every root also has an
env-var override. Shell drivers take the same values as environment
variables (`ASSETS_ROOT`, `UE_EXE`, `BLENDER_EXE`).

**Safety invariant (never violate):** source asset libraries are
read-only. Conversion works on a disposable sandbox copy; outputs land
in `<pack>/Exports/`; fingerprints prove byte-identity of sources
before/after.

## Chains

### 1. `conversion/` — Unreal `.uasset` packs → FBX + textures + manifest
**Requires: Unreal Editor 5.x (command line), Blender (optional
round-trip check), Git Bash.**

| Tool | What it does |
|---|---|
| `convert_packs.sh` | THE driver: one pack → `Exports/{FBX,Textures}/` + `manifest.json` (7 steps: discovery, sandbox sync, engine export, verify, Blender round-trip, status) |
| `export_pack.py` | runs INSIDE UnrealEditor (via `AMCONV_JOB` env JSON set by the driver) |
| `relink_pack.sh` + `relink_materials.py` | re-wire materials with engine-exact texture params → manifest v2 (`--preview` dry-runs against a copy first) |
| `verify_pack_export.py` | independent verifier (no engine): every manifest link + FBX header + texture dims |
| `source_fingerprint.py` | SHA-256 proof that pack sources are byte-identical before/after an engine run |
| `pack_discovery.py` | filesystem-only pack/content-root discovery |
| `blender_inspect_fbx.py` | headless Blender import measurement (round-trip proof) |
| `batch_packs.sh` | batch driver over a pack root (resume, skip-if-done, failure isolation) |

```bash
export UE_EXE="/c/Program Files/Epic Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
export ASSETS_ROOT="D:/path/to/packs"          # children = packs
cd pipeline/conversion
python make_sandbox.py                         # ONE-TIME: generate the local
                                               # conversion sandbox (see below)
bash ./convert_packs.sh --pack "MyPack"        # adds --skip-meshes NAME for
                                               # exporter-crashing assets
bash ./relink_pack.sh --pack "MyPack" --preview # engine-exact wiring, dry run
bash ./relink_pack.sh --pack "MyPack"           # APPLY: manifest v2 = recipes
python verify_pack_export.py "<pack>/Exports"  # must print PASS
cd ../.. && python pharos.py ingest            # imports the pack + recipes
```
Manifest schema: `pharos.pack.export/v2` (v1 = inventory only: meshes
import, but WITHOUT material recipes until the relink is applied). Legacy
`kiosk.*` schema IDs are accepted everywhere on read. Chain 1 runs on
Windows (Unreal Editor + Git Bash); measured engine time is roughly
1–30 minutes per pack.

**The sandbox** (`sandbox/Sandbox.uproject`) is NOT shipped in the repo —
it is an empty throwaway UE project whose EngineAssociation must match
YOUR UE install, so `make_sandbox.py` generates it locally (deriving the
version from `UE_EXE`, or pass `--engine-version 5.7`). It enables
`PythonScriptPlugin`, packs are robocopied into its `Content/` for the
headless engine run, and your sources stay read-only. Delete and
regenerate any time. If a driver reports "sandbox project missing", it
prints exactly this command.

### 2. `kitbash/` — KitBash-style `.blend` kits → per-assembly FBX
**Requires: Blender.** One FBX per root assembly (`*_grp`), textures
resolved by basename fallback, `kit_manifest.json` per kit.

```bash
cd pipeline/kitbash
python export_all_kb3d.py            # newer packaging (.blender.native)
python export_kb3d_rest.py           # older packaging (.blend in kit root)
python run_kb3d_metadata.py          # material names + textures (needed for recipes)
cd ../.. && python pharos.py ingest  # builds _Agent_Files/kb3d_models.jsonl + imports
```
Blender is found automatically (`BLENDER_EXE`, then PATH, then the newest
installed version).
Kits root: `kitbash_root` in pharos_config.json or `PHAROS_KB3D_ROOT`.
In-Blender scripts (`export_kb3d.py`, `kb3d_metadata.py`) are launched
by the drivers — one Blender process per file (threaded FBX importer
crashes when batched).

### 3. `native/` — raw FBX/OBJ/BLEND containers → measured records
**Requires: Blender (for .blend/.fbx container enumeration).**

```bash
cd pipeline/native
python recon_new.py                  # read-only scout of a downloads folder
python make_native_manifest2.py      # build the indexing worklist
bash index_native_all.sh             # measure the model files (1 Blender session)
python run_blends.py                 # .blend objects pass (1 Blender proc each)
python promote_native.py && python promote_blends.py   # -> _Agent_Files
cd ../.. && python pharos.py ingest  # import the new records
```

What chain 3 covers: every top-level folder EXCEPT the ones `pharos.py
ingest` already handles -- the animation section, the configured
`scan_folders`, converted packs and exported kits (their `Exports/`).
In practice that means the `.blend` folders ingest asked about. To leave
a folder out, add its name to `indexer_skip_dirs` in pharos_config.json
BEFORE running chain 3; to narrow a run that was too broad, delete
`<library>/_Agent_Files/native_models.jsonl` first (the near-empty guard
refuses to shrink a large live index on its own).

`recon_new.py` and `promote_blends.py` find their sections in the library
itself (the pack folders carrying `Exports/manifest.json`, the kit folders
carrying `Exports/kit_manifest.json`) — nothing is hardcoded to a folder
name. Two optional overrides: `PHAROS_CONVERTED_ROOT` pins the converted
section when a library has more than one, `PHAROS_RECON_LEFTOVER=<folder>`
makes the scout report whether a specific pack was already removed.

### 4. `agent_index/` — regenerate the app's index files (pure Python)
```bash
cd pipeline/agent_index
python build_agent_index.py          # packs.json + models.jsonl + md
python build_availability_catalog.py # owned vs on-disk availability
python gen_index.py && python gen_md.py             # audio (folder root)
python gen_tex_index.py && python gen_tex_md.py     # textures
```
Audio/texture roots come from the config sections
(`AGENT_AUDIO_ROOT` / `AGENT_TEX_ROOT` override). After any of these:
restart the app — importers rebuild the registry from the new files.
Every writer here refuses to let a near-empty result (fewer than 10
records, under half of the live index) replace a live index of 10 or more
(`FATAL: only N ...`); first builds and small libraries are written — see
`docs/CAPABILITIES.md` §7. `pharos.py ingest` runs `build_agent_index.py`
and `build_kb3d_index.py` itself whenever converted packs or kits exist.

## Order of operations for brand-new downloads

scout (`recon_new.py`) → convert packs (chain 1) and/or export kits
(chain 2) → enumerate natives (chain 3) → optional richer audio/texture
indexes (chain 4) → `python pharos.py ingest` (builds the model/kit
indexes, imports everything, rewrites the agent docs) → restart the server.

## Standing rules carried over from production

- Never modify anything under the library root except `Exports/` folders
  and `_Agent_Files/` index files.
- Manifest backups inside `Exports/` are `manifest.v1.bak` — never
  `*.json` (a glob must not pick them up).
- Verify (verifier + fingerprints) after every engine run; the source
  tree fingerprint must be identical before/after.
- Packs whose master materials expose no texture parameters can verify
  with 0/N wiring — that is an authoring choice, not a pipeline failure:
  the verifier warns on low coverage, it never hard-fails on it.
