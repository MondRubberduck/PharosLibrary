# Agent setup contract

You are the coding agent setting Pharos up for a user. This file is the
whole contract: what you run, what you ask, what "done" means. The
build-phase doctrine (scenes) is in `AGENT_PLAYBOOK.md`.

## Ground rules

- The library is **read-only** for you. Pharos itself writes only
  `_Agent_Files/` (its index files and the library map), `Exports/`
  (conversion output) and preview files. Everything else lives in the
  registry, which is derived and rebuildable.
- **Nothing with an engine runs without the user's decision in the
  library map.** `pharos.py convert`, `extract` and `previews` refuse
  otherwise and print the command that records the decision.
- `pharos.py doctor` never writes. Start with it, end with it.
- If code misbehaves: diagnose, fix if you can, add a regression check
  in the spirit of `tests/`, and note it in
  `<library>/_Agent_Files/SETUP_NOTES.md`. Code worked around silently
  is a bug twice.

## The library map

`<library>/_Agent_Files/library_map.csv` is the truth about what each
folder is. `pharos.py init` and `pharos.py ingest` survey the library
and fill it: one row per folder that is a thing (a pack, a project, a
kit, a texture library), never one per file.

| column | meaning |
|---|---|
| `kind` | `3d-assets` `unreal` `unity` `blender` `textures` `hdri` `animation` `audio` `purchases` `archive` `other` `ignore` |
| `state` | what the survey saw (leave it as written): Unreal `raw` / `project` / `converted`, Unity `project` / `package`, Blender `kit` / `kit-exported` / `files`; empty for every other kind |
| `include` | yes/no: index this folder at all |
| `action` | the decision its kind needs (below). A folder inherits the action of the nearest folder above it: one answer on the folder of packs covers every pack in it |
| `confirmed` | the user looked at the row. Survey guesses stay unconfirmed |
| `name`, `photorealistic`, `stylized`, `low_poly`, `domain`, `tags`, `note` | the user's labels per pack, shown in search answers |

The survey never overwrites a row the user changed. Edit the file in a
spreadsheet, on the server's Library map page (`/tags`), or with

```
python pharos.py map show
python pharos.py map pending
python pharos.py map set "<folder>" kind=<kind> include=yes|no action=<decision> confirmed=yes
```

`pharos_config.json` mirrors the map (sections, scan folders, manifest
roots, kit root, skipped folders). Change the map, not the config.

## The decisions

| kind, state | decision | what it means |
|---|---|---|
| unreal, raw / project | `export` or `leave` | export: FBX + textures + material recipes through the Unreal Editor (5.x, Windows; minutes to half an hour of engine time per pack). leave: the pack is listed from its purchase and folder data only |
| blender, kit / files | `extract` or `leave` | extract: per-object FBX + manifest through headless Blender (minutes per file). leave: nothing touches the .blend files |
| unity, project | `index-files` or `leave` | index-files: the project's `Assets` folder is scanned like a mesh folder; Unity itself is never started |
| animation | `previews` or `no-previews` | previews: a browser tab renders a short clip of each animation once. no-previews: clip list and bone facts only |

## The steps

0. Read `README.md`, this file, `AGENT_PLAYBOOK.md`.
1. `python pharos.py doctor --json`: what the machine has (Blender,
   Unreal, registry). Report every gap with the fixing command it
   prints.
2. Two ways to pick and survey the library; the result is the same map.
   - **The setup page** (prefer it when the user is at the machine):
     `python pharos.py setup` starts the server and opens `/setup`. The
     user picks the folder in a folder browser, sees one line per thing
     the survey found with its evidence, corrects kinds, unticks folders
     to leave out, answers the decisions, presses *Start indexing*. Tell
     them the page is open and wait; `GET /api/setup/state` tells you
     where they are (`configured`, `pending`, `progress.state`), and
     `pharos.py doctor --json` says READY when it is done. A server
     started without a config serves only this page (setup mode) until
     the first indexing ran.
   - **The shell**: `python pharos.py init "<library root>" --force`
     writes the config and the first map (if the user gave no folder,
     ask for it first). `python pharos.py survey "<library root>"`
     prints the tree the way the survey read it. Show the user the
     top-level lines (kind, evidence, file counts) and ask which are
     wrong before anything is indexed. Correct them with
     `map set "<folder>" kind=<kind>` (`include=no` for folders to
     skip), `confirmed=yes` for the rows they looked at.
   An agent can also drive the page's API itself: `POST /api/setup/survey`
   `{"root": "<folder>"}`, `GET /api/setup/map` and `POST /api/setup/map`
   (one row: `{"folder", "kind"?, "include"?, "action"?, "confirmed"?}`),
   `POST /api/setup/confirm` `{}` (refused with the pending list while
   decisions are missing), `GET /api/setup/progress` (the running job's
   log, then its result with the doctor verdict and the next steps).
3. `python pharos.py ingest` runs everything that needs no engine
   (the measure of every model and clip file, the importers, the
   folders table, the agent docs) and prints an
   **ASK YOUR USER** block: the folders whose decision is missing, each
   with its answer command. Relay the questions in **one** message, in
   plain words, with the cost and a safe default ("if unsure, leave it;
   it can be done later"). Also ask: is the purchase CSV current (it
   drives owned-but-not-downloaded answers)? Are there other drives
   (`init` sees one root)? Record every answer with
   `python pharos.py map set "<folder>" action=<answer>`.
4. Run the consented chains: `python pharos.py convert`, `extract`,
   `previews`. Each takes `--dry-run`, which prints the commands it
   would run. Then `python pharos.py ingest` again to import the
   results. With Blender present, offer the pictures too:
   `python pharos.py previews --sheets` renders one contact sheet per
   model folder (up to 36 models each, about a minute per folder);
   `--keys <key> ...` renders the models of a shortlist. Agents and the
   `/models` page show them; a library without them works, it just
   cannot be looked at.
5. `python pharos.py serve --no-open`; wait for **INGESTION COMPLETE**;
   report the per-section counts. `python pharos.py doctor` must say
   **READY**, or **READY-WITH-GAPS** where every gap is a kind the user
   confirmed the library does not have.
6. Write what was asked, answered, run and skipped into
   `<library>/_Agent_Files/SETUP_NOTES.md`.

## Known limits (do not rediscover these)

- `.max` files are unreadable without 3ds Max: catalogued as gaps.
- ASCII FBX is not read anywhere (binary only).
- Two servers can bind one port on Windows (stdlib `SO_REUSEADDR`): kill
  by PID from `netstat -ano | findstr <port>` before restarting.
- PowerShell aliases `curl`; use `curl.exe`.
- Blender always runs with `--background --factory-startup`.
- The suites are the contract: `tests/test_db.py`,
  `test_uasset_parser.py`, `test_pack_verify.py`, `test_search.py`,
  `test_survey.py`, `test_fresh_install.py` stay green; a fix without a
  regression check is not done.
