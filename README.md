# Pharos Library

A local asset-intelligence server for coding agents. Point it at a
folder of 3D assets, textures, audio, or animations. Pharos indexes
everything into a queryable SQLite registry and serves an HTTP API, a
dashboard, and MCP tools, so a coding agent can find, filter, and
select assets by real-world dimensions, materials, themes, and
availability — without walking the filesystem.

| Dashboard | Textures |
|---|---|
| ![dashboard](docs/screenshots/dashboard.png) | ![textures](docs/screenshots/textures.png) |
| **Animations** | **3D viewer** |
| ![animations](docs/screenshots/animations.png) | ![viewer](docs/screenshots/viewer.png) |

## Why it pays off

Measured on a 446 GiB, 92k-file library across three end-to-end runs
and two independent QA agents:

| | With Pharos | Filesystem crawling | Saved |
|---|---|---|---|
| Tool calls per scene | ~320 | ~1,610 | 5× |
| Content tokens per scene | ~1.3 M | ~9.5 M | ~86% |
| Wall clock per scene | 1.5–3 h | 14–24 h | ~8× |
| Novel-scene discovery* | 16–24k tokens | 180–250k tokens | ~90% |

\* A scene type that exists nowhere in the library (a Brazilian
favela), routed across four asset sections.

These numbers come from recorded runs on one private library. They are
directional evidence, not a controlled benchmark; a reproducible
`bench/` harness is on the roadmap.

## What you do

Three steps. Everything else is agent work.

**1. Get the app.**

```bash
git clone https://github.com/MondRubberduck/PharosLibrary.git
```

**2. Paste the setup prompt into your coding agent.** Edit the two
paths first.

```
Set up Pharos for my asset library. Setup only.
Read <repo>/README.md, then <repo>/docs/STARTING_PROMPT.md and follow it
exactly. My assets live at <D:/path/to/your/assets>.
```

**3. Whenever you want a scene, paste the build prompt with your idea.**

```
Build me a scene with my Pharos library: <your one-line idea>.
Read <library>/_Agent_Files/AGENT_START_HERE.md first and follow
docs/AGENT_PLAYBOOK.md phase 2.
```

The dashboard opens at http://127.0.0.1:8765 whenever the server runs.

## What your agent does

**Setup** — driven by `docs/STARTING_PROMPT.md`, checked by
`docs/AGENT_SETUP_BRIEF.md`:

1. `pharos.py doctor` probes the machine and reports every gap with the
   exact command that fixes it.
2. `pharos.py init` detects your asset folders and writes
   `pharos_config.json`.
3. `pharos.py ingest` indexes everything that needs no engine, then
   asks you the decisions that are yours: crawl the Unreal packs for
   material recipes? export KitBash3D kits? enumerate .blend files or
   leave them alone? where is the purchase CSV?
4. Nothing with an engine runs without your answer. After your answers
   the server starts and prints INGESTION COMPLETE with per-section
   counts.

**Building** — `docs/AGENT_PLAYBOOK.md`, phase 2: query the API, author
a scene manifest, validate it, check the triangle budget, build
headless in Blender, reopen the .blend and verify the result. The
priority ladder: on disk → use it; owned but not downloaded → ask you;
absent → model it and say so.

## What works with and without engines

| Capability | Python only | + Blender | + Unreal Editor 5.x |
|---|---|---|---|
| Search: names, paths, tags, themes, availability | ✅ | ✅ | ✅ |
| FBX dimensions + triangle counts | ✅ | ✅ | ✅ |
| Texture sets, audio durations, purchase catalog | ✅ | ✅ | ✅ |
| OBJ/GLB/STL measured geometry | + trimesh | ✅ | ✅ |
| .blend / USD container enumeration | — | ✅ | ✅ |
| Kits → per-assembly FBX + manifests | — | ✅ | ✅ |
| Engine-exact material recipes | — | — | ✅ |
| Scene manifest → headless Blender build | — | ✅ | ✅ |

A section without content or without its engine serves empty results,
never errors.

## Requirements

- Python 3.10+. The core server is stdlib-only; the optional MCP
  integration adds one package (`pip install "mcp<2"`).
- Optional: Blender 5.x, Unreal Editor 5.x, Git Bash — only for the
  pipeline chains.
- Windows, Linux and macOS are covered by CI.

## How it fits together

```
service/asset_service/    HTTP server, importers, MCP, scanner, doctor, ingest
                          (templates/ + static/ live inside it)
pipeline/                 optional conversion and index chains (UE, Blender)
tools/ tests/ docs/       PII gate, test suite, documentation
pharos.py                 CLI: init | doctor | ingest | serve | docs
```

The SQLite registry is derived data: importers rebuild it from crawler
outputs and disk on every start. Crawler-sourced tables rebuild
automatically; scanner-indexed rows return on the next
`pharos.py ingest`.
Asset files are never modified — the only writes into a library are
generated index files.

## Documentation

| For | Read |
|---|---|
| The paste-ready setup prompt | [docs/STARTING_PROMPT.md](docs/STARTING_PROMPT.md) |
| The agent's setup contract: decisions and interview | [docs/AGENT_SETUP_BRIEF.md](docs/AGENT_SETUP_BRIEF.md) |
| The two-phase doctrine and priority ladders | [docs/AGENT_PLAYBOOK.md](docs/AGENT_PLAYBOOK.md) |
| Every capability and limit, verifiable | [docs/CAPABILITIES.md](docs/CAPABILITIES.md) |
| MCP client configuration | [docs/MCP_SETUP.md](docs/MCP_SETUP.md) |

## License

MIT
