# Pharos

**A local asset-intelligence server for coding agents.**

Point it at any folder of 3D assets, textures, audio, or animations. It
indexes everything into a queryable SQLite database and serves a rich HTTP
API that coding agents (Claude, GPT, Cursor, any MCP client) can use to
find, filter, and select assets by real-world dimensions, materials,
themes, and availability — **without reading a single file from disk**.

```
  Animations        Assets              Textures           Audio
  1,313 clips       259 purchases       2,377 PBR sets     4,737 sounds
  live 3D preview   galleries + links   4K maps            click to play
```

## Quick start

```bash
# 1. Clone and configure
git clone https://github.com/MondRubberduck/PharosLibrary.git
cd PharosLibrary

# 2. Point it at your asset folders (or accept defaults)
python service/asset_service/browse.py --init

# 3. Start the server
python service/asset_service/browse.py
# → http://127.0.0.1:8765
```

## What your agent gets

The MCP server exposes these tools natively:

- `pharos_stats` — library overview
- `pharos_search_meshes` — geometry with real dimensions, themes, budgets
- `pharos_search_textures` — PBR sets
- `pharos_search_audio` — sounds by keyword, category, duration
- `pharos_search_collection` — owned purchases (local vs. not-downloaded)
- `pharos_list_packs` — exact pack names

See `docs/MCP_SETUP.md` for client configuration.

Tell your coding agent:

> "Read `<your-library-root>\_Agent_Files\AGENT_START_HERE.md`, then use the
> HTTP API at http://127.0.0.1:8765 to find assets."

Or with the MCP server (installed via `pip install mcp<2`):

```json
{ "pharos": { "command": "python", "args": ["-m", "service.pharos_mcp_server"], "cwd": "<repo root>" } }
```

The agent can then ask things like:

- *"Find me a door between 1.8 and 2.4 metres with textures"*
- *"What cyberpunk packs do I own?"*
- *"Play me a siren sound"*
- *"Which medieval packs are not downloaded yet?"*

…and get structured JSON with absolute paths, dimensions, material
recipes, and availability — in **~90% fewer tokens** than walking the
filesystem.

## Requirements

- Python 3.10+ (stdlib only — no pip installs for the core server)
- Any folder of assets you want to index

## Architecture

```
service/asset_service/
  browse.py           HTTP server + page routes
  config.py           path configuration (pharos_config.json)
  db.py               SQLite schema (assets, meshes, textures, audio, collection)
  indexer.py          pack discovery + tagging (Animation section)
  collection_import.py  CSV purchase catalog importer
  textures_import.py  texture set indexer
  audio_import.py     audio library importer (from crawl JSONL)
  meshes_import.py    geometry layer importer (from crawl JSONL)
  auto_classifier.py  3-tier domain/style/theme classifier
  templates/          HTML pages (assets, textures, audio, animation, viewer)
  static/             vendored three.js r170 + loaders
tools/                environment inspection, hashing, deployment
docs/                 decision log (every design choice, dated)
tests/                unit tests
```

## Documentation

- `docs/CAPABILITIES.md` — what this system lets a coding agent do (the capability matrix)
- `docs/MCP_SETUP.md` — MCP client configuration
- `docs/tagging-plan.md` — the tagging/meta-tagging architecture

## License

MIT
