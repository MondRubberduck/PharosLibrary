# AGENT SETUP BRIEF — what you must decide, and what you must ask

Read this before running any setup command. It is the contract between
you (the coding agent), the user, and the Pharos code. The doctrine is
in `AGENT_PLAYBOOK.md`; this file is the concrete setup-time version:
what runs automatically, what needs a decision, and the questions that
are never optional.

## 0. Ground rules

- The asset library is **read-only** for you. The only writes allowed
  inside it: `_Agent_Files/` index files and `Exports/` conversion
  output. Everything else lives in the registry (derived, rebuildable).
- `pharos.py doctor` is your first and last command. It never writes.
  Start with it; end with it; its `--json` output is yours to parse.
- `pharos.py ingest` runs only what is safe automatically. Everything
  with an engine or a judgment call comes back to you as a question —
  relay it to the user verbatim, with the engine cost stated.
- Empty is a valid state. A zero section means "no such content" only
  if the user confirms it; otherwise it means "not indexed yet" and
  `doctor` prints the filling command.
- If code misbehaves: diagnose, fix if you can, add a regression check
  in the spirit of `tests/`, and log what you found and decided in
  `<library>/_Agent_Files/SETUP_NOTES.md`. Flawed code that gets worked
  around silently is a bug twice.

## 1. The decision matrix — what the census can find

`pharos.py init` prints what it detected; `pharos.py ingest --dry-run`
prints what it would run. Map every finding through this table:

| Census finding | Chain | Runs automatically? | Needs | Ask the user? |
|---|---|---|---|---|
| Loose mesh folders (FBX/OBJ/GLB/STL) | `scanner.py` | YES — dims, tri counts | nothing | no |
| Animation clip packs | `indexer.py` | YES — clip grid | nothing | no |
| Audio, no crawl index yet | `scanner.py` | YES — WAV durations only | nothing | no |
| Audio with `library_files.jsonl` | importer | YES — crawl metadata wins | nothing | no |
| Purchase CSV | `collection_import` | YES — owned vs on-disk | nothing | no (but confirm the CSV is current) |
| Packs WITH `Exports/manifest.json` | importers join recipes | YES — wiring + recipes | nothing | no |
| UE `.uasset` packs WITHOUT Exports | chain 1 (`pipeline/conversion`) | **NO** | UE 5.x + local sandbox (`make_sandbox.py`); hours of engine time | **YES** — crawl or skip? |
| KitBash3D kits (`*.blender.native`) | chain 2 (`pipeline/kitbash`) | **NO** | Blender | **YES** — export to per-assembly FBX? |
| `.blend` containers (non-kit) | chain 3 (`pipeline/native`) | **NO** | Blender | **YES** — enumerate objects, or leave the files alone? |
| Textures/Audio crawl-quality indexes (categories, keywords) | chain 4 (`pipeline/agent_index`) | texture index self-builds its cache, but the WRITE goes into the section roots | nothing (but writes into the library) | **YES** — scanner-only metadata, or full index? |
| owned-not-downloaded items | `build_availability_catalog.py` | after the registry exists | nothing | tell the user it produces the requisition list |
| `.max`, other unreadable formats | — | never | — | **tell the user** these are cataloged as gaps, not parsed |
| MCP for the agent client | `pip install "mcp<2"` | **NO** — installs a package | pip | **YES** if not already installed |

Two standing exceptions you will see in the code and must not "fix":
the owner-default collection CSV filename and the pre-rename `kiosk.*`
schema IDs are documented migration artifacts (see `config.py`,
`docs/CAPABILITIES.md`).

## 2. The interview (never optional)

Ask, at minimum — more if the census triggers rows in the matrix:

1. **Are these all the asset folders?** Other drives, external disks,
   NAS? `init` only sees what it was pointed at.
2. **Crawl the Unreal packs for material recipes?** State the cost:
   needs UE 5.x, generates FBX+textures per pack, takes real time.
   No answer = do not run it.
3. **Blender files: enumerate, kit-export, or leave as-is?** Nothing
   touches `.blend` files without an answer.
4. **Is there a purchase CSV?** (Name/URL/Price columns.) It is the
   ground truth for the requisition list of owned-not-downloaded items.
5. **Crawl-quality indexes for audio/textures?** The scanner gives
   filenames and dimensions; the crawl gives categories and keywords.
6. **Animation previews** render once in a browser tab — acceptable?

Relay answers into actions. Never silently substitute: an asset the
user owns but that is not on disk becomes a requisition line, never a
replacement; content absent from the library gets modelled from scratch
and SAID SO — that is the build-phase ladder, but the same honesty
governs setup.

## 3. What "setup complete" means

- `pharos.py doctor` prints **READY**, or **READY-WITH-GAPS** where every
  remaining gap is a section the user confirmed is genuinely empty.
- The server printed **INGESTION COMPLETE** and the per-section numbers
  were reported to the user.
- Every decision is recorded in `<library>/_Agent_Files/SETUP_NOTES.md`
  (what was asked, what was answered, what was run, what was skipped).
- `pharos.py docs` has (re)written `AGENT_START_HERE.md` /
  `AGENT_API.md` into the library — from there, the build phase
  (`AGENT_PLAYBOOK.md` PHASE 2) takes over.

## 4. Known limits (do not rediscover these the hard way)

- `.max` files are unreadable without 3ds Max — documented gap.
- ASCII FBX is not supported anywhere (binary only).
- Two servers can bind the same port on Windows (stdlib SO_REUSEADDR):
  kill by PID from `netstat -ano | findstr <port>` before restarting.
- PowerShell aliases `curl` — use `curl.exe`.
- Blender commands always carry `--background --factory-startup`.
- The suite is the contract: `tests/test_db.py`, `test_uasset_parser.py`,
  `test_pack_verify.py`, `test_fresh_install.py` must stay green; a bug
  fix without a regression check is not done.
