# Pharos

**A local asset-intelligence server for coding agents.**

Point it at any folder of 3D assets, textures, audio, or animations. It
indexes everything into a queryable SQLite registry and serves an HTTP
API + dashboard that coding agents (Claude, GPT, Cursor, any MCP client)
use to find, filter, and select assets by real-world dimensions,
materials, themes, and availability — **without reading a single file
from disk**.

> *"Find me a door between 1.8 and 2.4 metres with textures"* →
> structured JSON with absolute paths, triangle counts, material
> recipes, and a 3D preview URL. In seconds.

## Why: agents burn tokens crawling filesystems

Measured on a 446 GiB / 92k-file library (three independent end-to-end
dry runs plus an external QA agent):

| | With Pharos API | Filesystem crawling | Saving |
|---|---|---|---|
| Tool calls per scene | ~320 | ~1,610 | **5×** |
| Content tokens per scene | ~1.3 M | ~9.5 M | **~86%** |
| Wall clock per scene | 1.5–3 h | 14–24 h | **~8×** |
| Novel-scene discovery* | 6–9 calls / 16–24k tokens | 35–50 calls / 180–250k tokens | **~90%** |

\* building a scene type that does not exist as a folder in the
library (a Brazilian favela), routing across four asset sections —
the generalization proof. An independent QA agent's own measurement of
the geometry battery: ~14k tokens via the API vs 1.2 M+ for directory
listings alone, before any binary parsing (~98.8%).

## Screenshots

| Dashboard | Textures |
|---|---|
| ![dashboard](docs/screenshots/dashboard.png) | ![textures](docs/screenshots/textures.png) |

| Animations (self-rendered previews) | 3D viewer (three.js) |
|---|---|
| ![animations](docs/screenshots/animations.png) | ![viewer](docs/screenshots/viewer.png) |

## Quick start

```bash
git clone https://github.com/MondRubberduck/PharosLibrary.git
cd PharosLibrary

# 1. Point it at your asset folders (auto-detects sections)
python pharos.py init "D:/path/to/your/assets"

# 2. Start the server
python pharos.py serve
#    → http://127.0.0.1:8765

# 3. Index raw mesh folders (real FBX dimensions, no engines needed)
python service/asset_service/scanner.py "D:/path/to/models"

# 4. Generate the agent entry files in YOUR library
python pharos.py docs
```

Then tell your coding agent:

> "Read `<your-library-root>/_Agent_Files/AGENT_START_HERE.md`, then use
> the HTTP API at http://127.0.0.1:8765 to find assets."

Or connect via MCP (`pip install "mcp<2"`, see `docs/MCP_SETUP.md`):

```json
{ "mcpServers": { "pharos": {
    "command": "python", "args": ["-m", "service.pharos_mcp_server"],
    "cwd": "<repo root>" } } }
```

## What you get, with and without engines

| Capability | Base (Python only) | + Blender | + Unreal Editor 5.x |
|---|---|---|---|
| Search: names, paths, tags, themes, availability | ✅ | ✅ | ✅ |
| FBX dimensions + triangle counts (`scanner`) | ✅ | ✅ | ✅ |
| Texture sets, audio with durations, purchase catalog | ✅ | ✅ | ✅ |
| OBJ/GLB/STL measured geometry | + trimesh | ✅ | ✅ |
| Native container enumeration (.blend/USD) | — | ✅ (`pipeline/native/`) | ✅ |
| KitBash-style kits → per-assembly FBX + manifests | — | ✅ (`pipeline/kitbash/`) | ✅ |
| Engine-exact material recipes & wiring (`hero_textures`) | — | — | ✅ (`pipeline/conversion/`) |
| Scene manifest → Blender build | — | ✅ | ✅ |

Everything degrades gracefully: a section without content (or without
its engine) serves empty results, never errors.

## The agent contract

- `docs/CAPABILITIES.md` — the full capability matrix (what the system
  can and cannot do; the honest-limits list)
- `AGENT_START_HERE.md` / `AGENT_API.md` — generated into your library
  by `pharos docs`, with live counts (never hand-maintained)
- Search semantics: AND-first with ranked OR fallback, rarity-ranked,
  tier/mode/exact disclosed per response; short tokens match on word
  boundaries; all-dropped-token queries return zero, never everything
- Safety: file routes path-jailed to configured roots; the library on
  disk is never modified by the app

## Requirements

- Python 3.10+ (stdlib only for the core server — no pip installs)
- Optional: Blender 5.x, UE 5.x, Git Bash — only for the `pipeline/` chains
- Windows / macOS / Linux (paths are config-driven)

## Architecture

```
service/asset_service/    HTTP server + importers + MCP + scanner
templates/ static/        dashboard pages, vendored three.js
pipeline/                 optional conversion & index chains (UE/Blender)
tools/ docs/ tests/       utilities, capability matrix, test suite
pharos.py                 CLI: init | serve | docs
```

The SQLite registry is **derived data**: importers rebuild it from
crawler outputs and disk on every start. Delete it; restart; it's back.

## License

MIT
