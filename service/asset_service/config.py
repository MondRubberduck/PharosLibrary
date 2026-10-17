"""Pharos — central configuration.

Loads `pharos_config.json` from the service folder. Every module imports
paths from here — no hardcoded machine paths anywhere. Create the file
with `python pharos.py init` (auto-detects folders under a root
you point it at) or copy `pharos_config.example.json` from the repo root
and edit it. Change the JSON, restart, done.
"""

import json
import os
import sys
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
        "audio": "Audio_Assets",
        "collection": "Collected Files",
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
        # utf-8-sig: Windows PowerShell 5.1 writes a BOM; an unreadable file
        # is reported, never silently treated as "not configured"
        try:
            cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            print(f"WARNING: {_CONFIG_FILE} is not valid JSON ({exc}) -- "
                  f"fix it by hand; running as NOT CONFIGURED", file=sys.stderr)
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
    """Whole file or nothing: written beside, then swapped in."""
    tmp = _CONFIG_FILE.with_name(_CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _CONFIG_FILE)


def is_configured() -> bool:
    """True once the two roots every entry point needs are set."""
    return bool(_CFG.get("library_root")) and bool(_CFG.get("registry_dir"))


_HOOKS: list = []


def on_reload(fn):
    """Register a callable that runs after every reload(): a module that
    derives paths from the roots re-derives them there."""
    _HOOKS.append(fn)
    return fn


# the list object is shared with the importers (they alias it): reload
# fills it in place
MANIFEST_ROOTS: list = []


def _bind(cfg: dict) -> None:
    """Derive every root from `cfg` into the module globals."""
    global _CFG, LIBRARY_ROOT, REGISTRY_DIR, DB_PATH, PREVIEW_DIR, SECTIONS
    global AGENT_FILES, THUMB_CACHE_DIRS, EXTENSION_STATUS_FILE, INDEXER_SKIP_DIRS
    global NETWORK, DASHBOARD, TEX_ROOT, AUDIO_ROOT, COLLECTION_ROOT, CSV_PATH
    global AVAILABILITY_JSONL
    _CFG = cfg
    LIBRARY_ROOT = Path(cfg["library_root"] or ".").resolve()
    REGISTRY_DIR = Path(cfg["registry_dir"] or ".").resolve()
    DB_PATH = str(REGISTRY_DIR / "assets.sqlite")
    # previews default to a sibling of the registry DB (both are derived data)
    PREVIEW_DIR = Path(cfg["previews_dir"] or (REGISTRY_DIR / "previews")).resolve()
    SECTIONS = cfg["sections"]
    AGENT_FILES = (LIBRARY_ROOT / cfg["agent_files"]).resolve()
    MANIFEST_ROOTS[:] = [Path(r).resolve() for r in cfg["manifest_roots"]]
    THUMB_CACHE_DIRS = list(cfg["thumb_cache_dirs"])
    EXTENSION_STATUS_FILE = cfg["extension_status_file"]
    INDEXER_SKIP_DIRS = list(cfg["indexer_skip_dirs"])
    NETWORK = cfg["network"]
    DASHBOARD = cfg["dashboard"]
    # per-section absolute roots
    TEX_ROOT = LIBRARY_ROOT / SECTIONS["textures"]
    AUDIO_ROOT = LIBRARY_ROOT / SECTIONS["audio"]
    COLLECTION_ROOT = LIBRARY_ROOT / SECTIONS["collection"]
    # Deliberate owner-default filename, kept verbatim: init.py only writes
    # `collection_csv` when the detected name DIFFERS from this literal, so
    # every config whose CSV is called this has no key at all and relies on
    # this default -- including the owner's live config. Renaming it here
    # alone would leave those installs pointing at a file that does not
    # exist. Reported as a documented exception, not a leftover.
    CSV_PATH = COLLECTION_ROOT / cfg.get("collection_csv", "3D_Assets_Overview.csv")
    AVAILABILITY_JSONL = AGENT_FILES / "availability.jsonl"


_bind(load())


def reload() -> dict:
    """Re-read pharos_config.json and re-derive every root, then run the
    registered hooks. Setup mode writes the config while the server runs;
    before this, every root was fixed at import."""
    _bind(load())
    for fn in list(_HOOKS):
        fn()
    return _CFG
