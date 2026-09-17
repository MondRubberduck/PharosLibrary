"""Pharos — central configuration.

Loads `pharos_config.json` from the service folder. Every module imports
paths from here — no hardcoded machine paths anywhere. Create the file
with `python -m asset_service init` (auto-detects folders under a root
you point it at) or copy `pharos_config.example.json` from the repo root
and edit it. Change the JSON, restart, done.
"""

import json
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parent / "pharos_config.json"

# Machine-neutral defaults: the roots start EMPTY and are filled by init
# or by hand. Section names are just relative folder names — importers
# skip gracefully when a folder is absent, so keep them as hints.
_DEFAULTS = {
    "library_root": "",
    "registry_dir": "",
    "previews_dir": "",
    "sections": {
        "animation": "Animation",
        "textures": "Textures_Materials",
        "textures_main": "Textures_Materials/4K_Textures_Gumroad",
        "audio": "Audio_Assets",
        "collection": "Collected Files",
        "collected_galleries": "Collected Files",
    },
    "agent_files": "_Agent_Files",
    # external thumbnail cache dir NAMES mirrored into the thumb index
    # (e.g. a host library app's caches); empty = no thumbnail mirroring
    "thumb_cache_dirs": [],
    # optional path to a host-app extension status file (shown on /registry)
    "extension_status_file": "",
    # extra top-level dir names the pack indexer must skip
    "indexer_skip_dirs": [],
    # directories whose CHILDREN may carry Exports/manifest.json (pack
    # exports) or Exports/kit_manifest.json (kit exports) produced by the
    # conversion pipeline; empty means no manifest join at all
    "manifest_roots": [],
    "network": {"host": "127.0.0.1", "port": 8765},
    # optional dashboard curation; every value may be empty, pages fall
    # back to generic picks (thumb index / first available image)
    "dashboard": {
        "animation_hero": "",
        "asset_hero_names": [],
        "texture_hero_patterns": [],
    },
}

_NESTED = ("sections", "network", "dashboard")


def _resolve(base: str, rel: str) -> str:
    return str((Path(base) / rel).resolve()) if rel else ""


def load() -> dict:
    """Load config, filling any missing key from the defaults."""
    if _CONFIG_FILE.is_file():
        try:
            cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
    else:
        cfg = {}
    for k, v in _DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
    for key in _NESTED:
        if not isinstance(cfg.get(key), dict):
            cfg[key] = dict(_DEFAULTS[key])
        else:
            for k, v in _DEFAULTS[key].items():
                cfg[key].setdefault(k, v)
    if not isinstance(cfg.get("manifest_roots"), list):
        cfg["manifest_roots"] = []
    return cfg


def save(cfg: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                            encoding="utf-8")


def is_configured() -> bool:
    """True once the two roots every entry point needs are set."""
    return bool(_CFG.get("library_root")) and bool(_CFG.get("registry_dir"))


_CFG = load()
LIBRARY_ROOT = Path(_CFG["library_root"] or ".").resolve()
REGISTRY_DIR = Path(_CFG["registry_dir"] or ".").resolve()
DB_PATH = str(REGISTRY_DIR / "assets.sqlite")
# previews default to a sibling of the registry DB (both are derived data)
PREVIEW_DIR = Path(_CFG["previews_dir"] or (REGISTRY_DIR / "previews")).resolve()
SECTIONS = _CFG["sections"]
AGENT_FILES = (LIBRARY_ROOT / _CFG["agent_files"]).resolve()
MANIFEST_ROOTS = [Path(r).resolve() for r in _CFG["manifest_roots"]]
THUMB_CACHE_DIRS = list(_CFG["thumb_cache_dirs"])
EXTENSION_STATUS_FILE = _CFG["extension_status_file"]
INDEXER_SKIP_DIRS = list(_CFG["indexer_skip_dirs"])
NETWORK = _CFG["network"]
DASHBOARD = _CFG["dashboard"]

# per-section absolute roots
ANIM_ROOT = LIBRARY_ROOT / SECTIONS["animation"]
TEX_ROOT = LIBRARY_ROOT / SECTIONS["textures"]
TEX_MAIN = LIBRARY_ROOT / SECTIONS["textures_main"]
AUDIO_ROOT = LIBRARY_ROOT / SECTIONS["audio"]
COLLECTION_ROOT = LIBRARY_ROOT / SECTIONS["collection"]

# Deliberate owner-default filename, kept verbatim: init.py only writes
# `collection_csv` when the detected name DIFFERS from this literal
# (init.py:179-183), so every config whose CSV is called this has no key at
# all and relies on this default -- including the owner's live config.
# Renaming it here alone would leave those installs pointing at a file that
# does not exist; a neutral name would need an init.py change plus a config
# migration. Reported as a documented exception, not a leftover.
CSV_PATH = COLLECTION_ROOT / _CFG.get("collection_csv",
                                      "3D_Assets_Overview.csv")
AVAILABILITY_JSONL = AGENT_FILES / "availability.jsonl"
AUDIO_JSONL = AUDIO_ROOT / "library_files.jsonl"
AUDIO_INDEX = AUDIO_ROOT / "library_index.json"
MODELS_JSONL = AGENT_FILES / "models.jsonl"
KB3D_JSONL = AGENT_FILES / "kb3d_models.jsonl"
NATIVE_JSONL = AGENT_FILES / "native_models.jsonl"
