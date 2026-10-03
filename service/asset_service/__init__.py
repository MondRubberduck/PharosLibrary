"""asset_service - local, non-destructive asset registry (Pharos).

Modules:
    agent_docs          Generate markdown documentation for AI agents
    audio_import        Audio crawler / scanner importer
    auto_classifier     2-tier non-destructive auto-categorizer
    browse              Localhost web UI and REST API
    collection_import   Collection CSV metadata importer
    config              Library configuration and directory paths
    db                  SQLite schema, FTS5 search, migrations
    fbx_dims            Bounding box and dimension extraction for FBX
    imgmeta             Image pixel size from header bytes; map role of a file name
    indexer             Crawler and registry filler
    init                Initial library setup and auto-configuration
    meshes_import       Mesh manifest importer and linker
    scanner             Direct disk scanner for meshes and audio
    scene_builder       Scene assembly and layout primitives
    scene_manifest      Scene description and manifest generator
    skeleton            Bone names and rig family of FBX / BVH files
    textmatch           The one word matcher: tokens, stems, themes, queries
    textures_import     Texture index importer
    uasset_parser       Binary Unreal .uasset/.umap header parser
    verify_importable   Blender FBX import probe for one file

Everything in this package treats the canonical asset roots as read-only.
"""

__version__ = "0.10.0"

import sys as _sys

# Console output must never crash on a name outside the console's code
# page: agents read Pharos through a pipe, which on Windows is cp1252, and
# a CJK/emoji folder name killed init, ingest and the scanner mid-run.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
