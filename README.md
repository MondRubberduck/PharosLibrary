[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?hosted_button_id=5XKG8WLLRWZ44)
[![Support on Patreon](https://img.shields.io/badge/Support-Patreon-FF424D?logo=patreon&logoColor=white)](https://www.patreon.com/NGallist)

# Pharos Library

A local server that knows your 3D asset library, for you and your coding
agent. Point it at the folder that holds your models, textures, sounds
and animations. It measures each file it can read once, keeps the facts
in a small database and answers over HTTP and as MCP tools: real sizes,
triangle counts, pivots, units, materials, clip durations, what you own.
Searching needs no engine; Blender and Unreal only do what they alone
can do.

| Dashboard | Textures |
|---|---|
| ![dashboard](docs/screenshots/dashboard.png) | ![textures](docs/screenshots/textures.png) |
| **Animations** | **3D viewer** |
| ![animations](docs/screenshots/animations.png) | ![viewer](docs/screenshots/viewer.png) |

## Get started

You need Python 3.10 or newer, Git, and a coding agent that can run
commands on your computer (Claude Code, Codex, Cursor). Blender 5.x is
optional: contact sheets, scene builds and Blender-kit export need it.
The MCP tools need one package: `pip install "mcp<2"`.

**1. Set up, once.**

```
git clone https://github.com/MondRubberduck/PharosLibrary
cd PharosLibrary
python pharos.py setup
```

A page opens: pick your asset folder, check what the survey found (one
line per pack, project, kit or texture folder, with its evidence),
answer the few decisions that need you, press **Start indexing**. Or
paste this to your agent, which drives the same page:

```
Clone https://github.com/MondRubberduck/PharosLibrary and set up Pharos
for my asset library. Read README.md, then docs/AGENT_SETUP.md, and follow it.
My assets live at: D:\path\to\my\assets
```

To answer what you own, put a CSV with Name and URL or Price columns
into a folder of your library before setup.

**2. Look.** The dashboard at http://127.0.0.1:8765 shows your models
with their measured facts, textures, sounds and animation clips, and
opens FBX, OBJ and glTF models in the 3D viewer. `python pharos.py
previews --sheets` renders a contact sheet per model folder (Blender,
about a minute per folder). Next time: `python pharos.py serve`.
Something wrong? `python pharos.py doctor` prints the fix.

**3. Build.** Tell your agent:

```
Build me a scene with my Pharos library: <your idea>.
Read <your asset folder>/_Agent_Files/AGENT_START_HERE.md first.
```

It searches, picks by measured facts, writes a scene file and builds it
headless in Blender.

**Added assets?** New files in a folder Pharos already indexes are
picked up at the next server start. A new top-level folder needs
`python pharos.py ingest`.

**Your files stay untouched.** Inside your library Pharos writes only
its own `_Agent_Files` folder, plus `Exports` for conversions you
approved. Its database and previews live in `~/.pharos` unless you
choose another folder on the setup page.

## What your agent gets

One short answer with facts, instead of a walk through the folders.
Measured on a library of 81,677 files (12,280 models, 2,438 texture
sets, 4,737 sounds, 1,930 clips):

| Your agent asks for | Pharos answers with | Without Pharos it has to |
|---|---|---|
| crates under 1.2 m | 10 crates with size and triangle count, about 1,050 tokens | read 59 file names, then open and measure each: sizes are in no text file |
| a brick-wall texture set with its maps | 10 sets with pixel size and maps, about 1,200 tokens | read the 727 file names containing "brick", about 25,000 tokens; a folder named "4K" proves nothing |
| a thunder or rain sound, 5 to 30 seconds | 10 sounds with durations, about 480 tokens | read 194 names, then open each file for its length |

A listing of every file path in that library is about 2.4 million tokens.

## With and without engines

| | Python only | + Blender | + Unreal Editor 5.x (Windows) |
|---|---|---|---|
| Search by name, path, tags, themes, ownership | ✅ | ✅ | ✅ |
| Measured facts: size, triangles, pivot, units, LOD, skin; clip duration, fps, root motion | ✅ | ✅ | ✅ |
| Texture sets, sounds with durations, purchase list | ✅ | ✅ | ✅ |
| Contact sheets and pictures of models | – | ✅ | ✅ |
| Blender kits exported to FBX; .blend containers listed and measured | – | ✅ | ✅ |
| Scenes built headless in Blender | – | ✅ | ✅ |
| Unreal packs converted, with engine-exact material recipes | – | – | ✅ |

Unreal conversion, Blender extraction and clip previews never run
without your decision in the library map. A section without content
answers empty, never with an error.

## How it works

One CLI, `pharos.py` (`python pharos.py` lists its commands: setup,
doctor, ingest, serve, previews, convert, extract, map and more). The
survey writes the **library map** (`_Agent_Files/library_map.csv`: one
row per folder that is a thing, with its kind and your decisions).
`ingest` measures everything that needs no engine and writes the agent's
entry files. The server imports it all into a SQLite registry, derived
data rebuilt whenever a source changes, and serves the API and the
pages; `service/pharos_mcp_server.py` gives the same answers as MCP
tools. Every change is tested on Windows, Linux and macOS, with a
headless Blender.

## Documentation

| For | Read |
|---|---|
| Setting up with an agent: the map, the decisions, the steps | [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md) |
| Building scenes with an agent | [docs/AGENT_PLAYBOOK.md](docs/AGENT_PLAYBOOK.md) |
| Every capability and limit | [docs/CAPABILITIES.md](docs/CAPABILITIES.md) |
| MCP client configuration | [docs/MCP_SETUP.md](docs/MCP_SETUP.md) |
| The optional engine chains (Unreal, Blender) | [pipeline/README.md](pipeline/README.md) |

## Support

If Pharos helps your pipeline: [Donate via PayPal](https://www.paypal.com/donate/?hosted_button_id=5XKG8WLLRWZ44) · [Support on Patreon](https://www.patreon.com/NGallist)

## License

MIT. Copyright (c) 2026 MondRubberduck and contributors.
