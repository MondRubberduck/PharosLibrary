"""Shared config for pipeline tools.

Standalone scripts read the SAME pharos_config.json the app uses.
Resolution order: PHAROS_CONFIG env var, then the repo's
service/asset_service/pharos_config.json. Env overrides beat config:
PHAROS_LIBRARY_ROOT beats library_root, PHAROS_AUDIO_ROOT beats the
audio section, and so on. Scripts stay runnable without the app
installed -- only this file + a config json are needed.
"""

import json
import os
from pathlib import Path


def _cfg_file() -> Path:
    env = os.environ.get("PHAROS_CONFIG")
    if env:
        return Path(env)
    return (Path(__file__).resolve().parents[1] / "service" / "asset_service"
            / "pharos_config.json")


def load() -> dict:
    p = _cfg_file()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def library_root() -> str:
    return (os.environ.get("PHAROS_LIBRARY_ROOT")
            or load().get("library_root") or "")


def section_root(section: str, env_var: str = "") -> str:
    """Absolute root of a configured section (audio, textures, ...) or an
    arbitrary top-level config key (kitbash_root)."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    cfg = load()
    if cfg.get(f"{section}_root"):
        return cfg[f"{section}_root"]
    lib = library_root()
    rel = (cfg.get("sections") or {}).get(section, "")
    return f"{lib}/{rel}" if lib and rel else ""


def agent_files() -> str:
    lib = library_root()
    if not lib:
        return ""
    return f"{lib}/{load().get('agent_files', '_Agent_Files')}"
