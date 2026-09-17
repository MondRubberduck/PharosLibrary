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
| `tree_fingerprint.py`, `source_fingerprint.py` | SHA-256 tree proofs (byte-identity of sources) |
| `pack_discovery.py` | filesystem-only pack/content-root discovery |
| `blender_inspect_fbx.py` | headless Blender import measurement (round-trip proof) |
| `batch_packs.sh`, `convert_all.sh` | batch drivers over a pack root |

```bash
export UE_EXE="/c/Program Files/Epic Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
export ASSETS_ROOT="D:/path/to/packs"          # children = packs
cd pipeline/conversion
bash ./convert_packs.sh --pack "MyPack"        # adds --skip-meshes NAME for
                                               # exporter-crashing assets
bash ./relink_pack.sh --pack "MyPack" --preview # engine-exact wiring, dry run
python verify_pack_export.py "<pack>/Exports"  # must print PASS
```
Manifest schema: `pharos.pack.export/v2` (v1 = inventory only). Legacy
`kiosk.*` schema IDs are accepted everywhere on read.

### 2. `kitbash/` — KitBash-style `.blend` kits → per-assembly FBX
**Requires: Blender.** One FBX per root assembly (`*_grp`), textures
resolved by basename fallback, `kit_manifest.json` per kit.

```bash
cd pipeline/kitbash
python export_all_kb3d.py            # newer packaging (.blender.native)
python export_kb3d_rest.py           # older packaging (.blend in kit root)
python run_kb3d_metadata.py          # enrich materials/textures (read-only .blend)
python build_kb3d_index.py           # fold into _Agent_Files/kb3d_models.jsonl
```
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
bash index_native_all.sh             # enumerate containers (1 Blender proc each)
python run_blends.py                 # .blend objects pass
python promote_native.py && python promote_blends.py   # -> _Agent_Files
```

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

## Order of operations for brand-new downloads

scout (`recon_new.py`) → convert packs (chain 1) and/or export kits
(chain 2) → enumerate natives (chain 3) → rebuild indexes (chain 4) →
`python pharos.py docs` → restart the server.

## Standing rules carried over from production

- Never modify anything under the library root except `Exports/` folders
  and `_Agent_Files/` index files.
- Manifest backups inside `Exports/` are `manifest.v1.bak` — never
  `*.json` (a glob must not pick them up).
- Verify (verifier + fingerprints) after every engine run; the source
  tree fingerprint must be identical before/after.
- HorrorMansion-class packs (0/493 wiring because master materials
  expose no texture params) are authoring choices: the verifier warns
  on low coverage, it never hard-fails on it.
