# Changelog

## Unreleased (towards 1.0)

Phase 4 of the 1.0 plan: slim down.

- **The Kiosk-era pack layer is gone.** The `assets` / `asset_files` / FTS tables, the pack indexer and its two-tier classifier, the `/registry` and `/pack` pages, `/api/packs`, `/api/pack_files`, `/api/retag`, `/api/pack_source` and the host-app thumbnail mirror. The clips and folders tables answer everything those did: the animation pages, the tags page, the dashboard, doctor, the agent docs and the MCP tools read them. A registry that still carries the old tables drops them at the next start (schema 6). The owner's domain and style words live in `library_map.py`; the vendor of a folder comes from its name.
- **Warm start.** The server runs its importers only when a source changed since the last start (an ingest, a scan, a clip measure, an edited index, map, CSV or config): a warm start takes well under a second instead of ten; `pharos.py serve --rebuild` forces the importers.
- **HTML out of the server.** The viewer, animation, dashboard and batch-renderer pages are templates; `browse.py` lost 1,200 lines.
- **Retired with their readers:** `pipeline/agent_index/classify.py`, `gen_index.py`, `gen_tex_index.py` (a `library_index.json` left in a section by an earlier crawl is still read as optional category data), `docs/AGENT_SETUP_BRIEF.md` and `docs/STARTING_PROMPT.md` (the README's paste prompt and `docs/AGENT_SETUP.md` are the setup documents; `AGENT_PLAYBOOK.md` keeps the build doctrine).
- **Packaging.** `pyproject.toml` with the `pharos` console script; `PHAROS_CONFIG` points the app at a config outside the package.
- Not in this phase: relative paths in the registry and the per-node materials table (the registry size target), the split of `browse.py` into route/search/file modules, and the pipeline scratch files outside the repo. Listed as follow-ups.

Phase 3 of the 1.0 plan: knowing more.

- **One measure for every model and clip file** (`measure.py`, standard library, every FBX version, OBJ, glTF/GLB, STL, BVH): size and placement corners in metres, triangles, vertices, materials, UV layers, skin and bones, LOD level and group, collision hulls, variant group, the file's units and up axis (a Y-up file with a mesh turned about X keeps its Z-up geometry: height stays height), the pivot class (`base-centre`, `corner`, `centre`, `top`, `outside`), animation stacks with duration, fps and frames, a root-motion flag, whether a clip file also carries a mesh, a scale warning. Cached per folder under `_Agent_Files/index/` by size and mtime: a re-scan reads only changed files (176 clips: 6 s, then 0.04 s). The scanner takes its geometry from it; `pharos.py measure <file>` prints a record.
- **The clips table.** `pharos.py measure --clips <folder>` (ingest runs it for the animation section) writes one row per clip: duration, fps, frames, root motion, skeleton signature and family, bone names, has_mesh. `/api/anim/clips` serves them (`duration_s`, `fps`, `frames`, `root_motion`, `family`, `bones`, `has_mesh`) and filters by `min_dur` / `max_dur` / `root_motion` / `family`; `/api/anim/tree` and the body match use the stored bone names instead of opening files at request time; `doctor`, the docs and the MCP tool count from it. A body file in the clip folder is no longer counted as a clip.
- **Mesh facts on the item** (`pivot`, `units`, `up_axis`, `lods`, `skin` + `bones`, `scale_suspect`, `category`) with filters `pivot=`, `skin=`, `lod=0`, `units=` on `/api/meshes`; scanned rows carry their material names and placement corners; a skinned scanned file is a `SkeletalMesh`.
- **The folders table** (`/api/folders`, MCP `pharos_tree('folders')`): the map's nodes with counts per section, the common units / up axis / pivot, LOD and variant groups, the vendor's own category words, vendor, license file, material-wiring health, Unity prefab count. Built at the end of ingest and at server start.
- **One Blender record shape** (`blender_measure.py`): the native index of .blend files and the KitBash3D kit export measure through it (corners, location, pivot, materials, UV layers); the native driver skips files it already indexed (resumable).

Phase 2 of the 1.0 plan: setup mode and the setup page.

- **`pharos.py setup`** starts the server and opens `/setup`: a folder browser for the library root, the survey's tree (one line per pack, project, kit or texture library, with its evidence) with kind, index and decision per folder, *Accept every guess*, *Start indexing* with a live log, then the doctor verdict, the counts and the engine chains the decisions ask for. The same page adds folders later (*Survey again*).
- **Setup mode.** A server started without a config no longer exits: it serves the setup page and its API, redirects `/` there and answers everything else with a 503 that names the page, until the first indexing ran. `init` and `ingest` run as subprocesses behind the page; nothing with an engine runs from a web request.
- **Roots follow the config at run time.** `config.reload()` re-derives every root and runs the hooks the server and the map register; the importers read their roots when called instead of at import. Setup mode changes the library while the server runs; before this, every path was fixed at import.
- The setup API (`/api/setup/state`, `survey`, `map`, `confirm`, `progress`; `/api/fs/drives`, `/api/fs/list` on loopback binds only) is documented in `docs/AGENT_SETUP.md` so an agent can drive the flow for the user. The fresh-install suite drives it end to end: a new user with a loose-FBX folder and a Unity project reaches READY with no purchase layer in sight.

Phase 1 of the 1.0 plan: the library map.

- **The library map** (`<library>/_Agent_Files/library_map.csv`) replaces `pack_tags.csv` (read once and carried over) and the hand-edited section keys of `pharos_config.json`. One row per folder that is a thing: kind (3D assets, Unreal, Unity, Blender, textures, HDRI, animation, audio, purchases, archive, other, ignore), state, include, the decision its kind needs (`export`/`leave`, `extract`/`leave`, `index-files`/`leave`, `previews`/`no-previews`), confirmed, and the owner's tags. A folder inherits the decision of the folder above it. The survey fills the map and never overwrites a row the owner changed. The config mirrors the map (`pharos.py map apply`); a kind the library lacks has no section, so `doctor` stops asking for it.
- **`pharos.py survey <root>`**: a structural survey of the library (markers first, file shares and names as a guess), one row per pack / project / kit / texture library, with the evidence. An 88,000-file library surveys in about a second. `pharos.py map show | pending | set | refresh | apply` edit the map from the shell; the server's Library map page (`/tags`) edits it in a browser.
- **`pharos.py ingest` asks only what the map lacks**, each question with its answer command, and runs nothing with an engine. **`pharos.py convert | extract | previews`** run the Unreal export, the Blender extraction and the clip previews for the folders whose decision allows it, refuse for the others, and print the commands with `--dry-run`.
- `pharos.py extract` hands the pipeline scripts the approved folders (`PHAROS_MAP_FOLDERS`), so a kit or a .blend folder the owner leaves alone is never opened; `include=no` on a folder keeps everything below it out; a wrong decision word is refused before anything is written; the survey moves a pack's state once it is converted; owner text with quotes survives a long map file; a marker file in the library root no longer turns the whole library into one row.
- `init`, `doctor`, the generated `AGENT_START_HERE.md` and the setup docs (`docs/AGENT_SETUP.md` replaces the decision matrix of `AGENT_SETUP_BRIEF.md`) read the map. `tests/test_survey.py` covers the survey, the map and the gated chains.

Phase 0 of the 1.0 plan: ground truth before the restructure.

- **FBX 7.5 and later parse.** Those files write 64-bit record headers; the stdlib reader knew only the 32-bit layout, so every UE5 / recent Maya / Max file came back empty (all 617 clips of one animation pack). `fbx_dims.fbx_nodes()` reads every version.
- **Folder detection: an animation folder needs motion.** A converted-Unreal folder of static meshes was classified as the animation section because static exports carry an AnimationStack and every exporter writes a `Takes` node. A clip now needs an AnimationStack plus an armature, or plus no geometry.
- **One table of file kinds** (`asset_service/kinds.py`). Six writers carried their own list of what counts as a 3D file and disagreed.
- **A purchase is no longer "local" because its CSV folder exists.** For the reference library that folder is the store-image gallery, so all 259 purchases read as downloaded while 38 were. Availability comes from the availability catalogue, or stays unknown.
- Removed, nothing read them: the `/gltest` page, the `operations` registry table (dropped on the next start), `source_fingerprint.py`, `recon_new.py`, `native_blends.txt`, the pack catalogue `build_agent_index.py` computed and discarded (and its extra library walk). `export_kb3d_rest.py` folded into `export_all_kb3d.py`, which now handles both kit layouts.

## 0.10.0 (2026-10-03)
A review of how an agent actually browses the library (live tests against a 12,280-model library, each finding re-checked by a second reviewer, then a second review of the changes) drove this release: search answers are now short, ordered by relevance and honest about what they did.

**Breaking for callers of `/api/meshes`:** list items are short by default. The material recipe, material names and placement corners moved to `/api/meshes/item?key=` (or `detail=1`). One-record endpoints no longer repeat every field under `item` and answer 404 / 400 instead of HTTP 200 with an error body. `/api/meshes/packs` lists `purchases` (a list) instead of one guessed purchase. The generated agent docs describe the new shape; regenerate them with `python pharos.py docs`.

**Search finds what is there**
- **One matcher for everything.** Models, textures, sounds, animation clips, purchases and the MCP tools cut and compare words through one module (`textmatch.py`). The same helpers used to exist five times with different rules.
- **Plurals, codes and CamelCase.** `benches` found nothing in a library with 38 benches. Both sides now fold plurals and -ing/-ed forms; a code such as `lod0`, `v2` or `9mm` stays one word; `PropFuseBox` is read as three words; a model is also found by its own object name (`SM_Wagon`).
- **Glued words.** Adjacent query words are also tried glued (`street lamp` finds `Streetlamp`, `sci fi door` meets the theme `scifi`), and a pack name that glues two words is searchable by that word.
- **Whole words.** `car` no longer returns cardboard and carpets. A word may start or end a longer word with three letters left over (`chair` in `Armchair`), never sit inside one (`round` is not `Ground`, `male` is not `female`); listed compounds reach their parts (`door` finds `Doorway`).
- **Natural queries.** Connective words (`with`, `of`, `and` ...) are optional: `door with frame` needs `door` and `frame`.
- **Aliases and exclusion.** `couch` finds sofas in every section (aliases used to work for animation only). `chair -office` excludes; it used to return only office chairs. Hyphenated exclusions (`-sci-fi`) and exclusion-only queries work.
- **Best match first.** Model results were sorted by pack name. They are now ordered by match quality, and within one quality every pack contributes its best match first, so one pack cannot fill the first page.
- **More to search by.** Material names, the vendor's folder names and the pack name are searchable context for a model (`rust` found 24 models by name; about 300 more carry a rust material). Scanned models get the folders between the scanned folder and the file as context.
- **Animation search.** Every word must match, as in the other sections, with `mode` and `exact` in the answer; folder words count (`root climb`). It used to match any word as a substring and sort by name.
- **Fallback ranking.** When no row matches every word, textures, sounds and purchases now rank the partial matches by word rarity, as documented; they were listed alphabetically.

**Answers are short and say what they did**
- **Short model items.** A page of 8 models was about 40 KB (one reached 569 KB) because every item carried its full recipe. A short item now holds what an agent needs to choose and nothing it can derive: the absolute `fbx` path (the answer's `root` plus the item's `key`), `max_dim_m` and `height_m` (read off `bbox_m`), the per-import `id` and `on_disk` (1 on every row) are gone from list items; full records keep `fbx` and `id`. `size` goes down to 1; the default page is 20.
- **Owned, not downloaded.** Model search lists files on disk only, so an agent never learned that a purchase it could ask for matched. Page 1 of a search with query words now names the owner's matching not-downloaded purchases under `owned_not_downloaded` (following the search's style, with the product URL; `total: 0` means checked, none; tools, 3D-print models and removed listings are left out). In `/api/meshes/packs`, a linked purchase's image count is now `image_count`, as everywhere else. A model whose file was missing at indexing carries `missing_file`; one outside the library carries its `fbx`. Full records drop the `fbx_path` and `max_dim` copies.
- **Full records on request, within a budget.** `/api/meshes/item?key=` returns the recipes of chosen models and stops at about 20k tokens; keys it had no room for come back in `not_returned`.
- **A stable `key` per model:** the library-relative file path, plus `#object` inside multi-object files. A key cut at its `#` by an HTTP client gets the file's object keys as `candidates`.
- **`warnings`.** An unknown parameter, an unreadable value or a filter value the library does not have is reported; the answer used to come back unfiltered or empty and look authoritative. Filter values ignore letter case; `avail=owned-not-downloaded` works. New filters: `kind`, `max_dur`, `res`, `min_px`, `has`, descending sorts (directions documented).
- **`did_you_mean`.** A query word the section does not contain comes back with the closest words it does contain (`barel` → `barrel`).
- **`packs`.** A model answer counts its matches per pack, and the pack list links each pack to the purchase records whose folder holds its files; their store images show what the pack looks like.
- **Faster.** A model query no longer loads and decodes every recipe in the registry: about 0.3 s instead of 0.8 s on 12,280 models.

**Facts are measured, not guessed**
- **Texture resolution.** The label came from the folder name; 2,000 sets called "4K" hold 2048-pixel files. Pharos now reads the pixel size from the file header (`px`, `res`) and says which file is which map (`maps`, with glossiness kept apart from roughness). The thumbnail is a preview image when the set has one.
- **Animation skeletons.** `/api/anim/tree` lists, per folder, the skeleton its clips use and the character files in the library whose bones match. It used to name one body for all clips, which shared no bone with a third of them.
- **Themes.** Theme keywords matched letters inside words and inside pack names: most "vehicle" models were bushes and cardboard, and a pack with "house" in its name made every one of its models a building. Themes now match whole words; a pack name only contributes the setting it spells out. Sound themes no longer inherit every keyword of their category.
- **Material pick.** A master material's default roughness no longer overrides the pack's own packed channel, and a default height map is never the pick. The per-slot `maps` list is unchanged.
- **One row per file.** A file that a richer source covers (converted pack, Blender-measured model) is served with that row only; its scan row is parked and served again if the richer row goes away. Scanner rows of files nobody else covers survive as before.
- **Purchases.** A thumbnail choice that cannot be an image path is dropped (one registry held a status word on every row); a choice whose drive is unplugged is kept. Two purchases sharing a folder no longer overwrite each other.
- **Pack labels.** The sub-category follows the majority of a pack's files (whole words) instead of the first keyword hit; the pack folder's name breaks a tie. Pack tags keep the most frequent words instead of the alphabetically first twelve.
- **Pack pages.** The pack page, bulk retag and store-URL setter rejected every pack below a section folder (most packs).

**Building scenes from search results**
- **Keys in the scene file.** An asset can name the `key` from a search instead of a path; the validator and the builder look up the file and, for a `#object` key, the one object. `"materials": "recipe"` wires the model's own per-slot recipe from the registry, so nothing is pasted.
- **One object of a multi-object file.** 2,950 models (24%) sit inside 123 files that hold many objects (one kit file holds 719). The builder imported the whole file and moved its first object; it now keeps only the asked-for object. A file without an object named moves as one piece.
- **Placement no longer overwrites the import.** Every asset gets an empty as root, and position, rotation and scale go on it. Written onto the imported object they replaced the importer's own rotation: files whose up-axis conversion is an object rotation were built lying on their side.
- **Crowd clips play in Blender 4.4+.** The walk clip was attached without an action slot, so the body stood still.
- A build that had to skip an asset says `BUILD INCOMPLETE` and exits non-zero.
- **Texture sets get their colour map.** The builder guessed map roles from substrings of the file names: sets named `<name>_Color` / `<name>_NormalDX` / `<name>_NormalGL` were built grey, with the DirectX normal map. It now wires the maps the registry serves for the set (the OpenGL normal of a pair; a set that is one photo is its colour map), and reads the names of unindexed sets with the importer's role reader, all maps from one set. The check prints the maps it found and warns when there is no colour map.
- **Whole-file warning.** A multi-object file given without an object is still imported whole, but the check and the builder now say how many objects and triangles that is.
- **More file types.** The builder imports `.obj`, `.glb`/`.gltf` and objects of `.blend` files besides `.fbx` (1,175 searchable models could not be built). OBJ models are baked upright and centred on their own geometry; the validator rejects files it cannot import.
- **Per-object budget.** `--budget` counts the one object taken from a file, or the whole file when no object is named.
- **`SCENE_BUILD.md`**, generated beside the agent docs: the scene-file format, how search results go in, the check and build commands.

**Search answers that steer less wrong**
- When models match every query word but the filters (size, style, pack ...) remove them all, `warnings` says how many; the partial matches then shown no longer look like the answer.
- `sort=triangles` / `max_dim` order within each relevance tier: "lowest-poly cart" listed logs and stumps that only matched through a material before the first cart. Lower-LOD copies (only where the pack also holds the full model), collision hulls and flat pieces are marked `lod` / `collider` / `card`; LOD copies, colliders and impostors come after real models, also in the default order.
- `-lod1` excluded every LOD row, LOD0 included; a code word now stays whole in an exclusion.
- `unmeasured_excluded` counts only the hidden rows that match the query (it counted the whole library).
- `stall` also finds booths, tents and market stands; `cobblestone` also finds paving (one-way: `tent` does not find stalls).
- Object words no longer make a model `medieval`: `lantern`, `cart`, `market`, `tower` and others gave that theme to every lantern in the library. The theme now comes from the period words and from medieval packs.
- Sound, texture and clip answers carry the `root` their paths start from; texture items their `folder`; clip answers the `bodies` each folder's clips play on.
- AGENT_START_HERE lists what to read when (it said "read the files listed here IN THIS ORDER" without a list) and prints the real repo path.

**Your own pack tags**
- **One list, one file.** Every pack has a row in `<agent_files>/pack_tags.csv`: Photorealistic or Stylized, Low Poly (combines with either), a domain and your own words. Tick them on the new dashboard page `/tags` (linked from home and from the registry page), or edit the file in a spreadsheet or text editor; comma, semicolon and tab files and marks like x, 1 or yes are read. The file lives in the library, so it survives a registry rebuild. New packs get a row at the next start; no row is ever removed. Labels you had set in the registry were copied in.
- **Agents use them.** `/api/meshes?style=Low Poly` (or `Stylized,Low Poly`: all must match) filters models by their pack's tags, query words match the tags (`low poly tree`), and each model and pack in an answer carries `styles` and `pack_tags`. Models of untagged packs have no style; the classifier's guess stays on the pack card only.
- The registry page's bulk retag writes domain and style to the same file.

**Less work, less code**
- The indexer no longer reads and hashes each pack's main file on every run (about 2.9 GB on the reference library); nothing read the result.
- The per-pack texture list is no longer stored per model (13 MB of repeated paths; the recipe holds the real files).
- Removed: the MCP server's own search code, an unreachable classifier tier, seven unused config constants and two unused config keys.
- Index writers write only what something reads. Gone: `packs.json` and `MODELS_AGENT_INDEX.md` (`build_agent_index.py`), `availability.json` and `AVAILABILITY_AGENT_INDEX.md` (`build_availability_catalog.py`), the textures `material_sets.jsonl`, the section `AGENT_INDEX.md` guides (`gen_md.py` and `gen_tex_md.py` removed), the `.md.bak-previous` copies of the agent docs, and the registry indexes on `content_hash` / `sha256` (dropped on the next start). Leftover copies from older versions can be deleted.

**MCP**
- Every tool calls the function behind the matching HTTP endpoint, so both routes give the same answer. New tools: `pharos_tree`, `pharos_get_meshes`, `pharos_get_texture`, `pharos_search_animations`.

**Tests and CI**
- New suite `tests/test_search.py` (48 checks), run in CI as its own step; CI also runs Python 3.13.

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
