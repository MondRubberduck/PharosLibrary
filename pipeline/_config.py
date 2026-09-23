"""Shared config for pipeline tools.

Standalone scripts read the SAME pharos_config.json the app uses.
Resolution order: PHAROS_CONFIG env var, then the repo's
service/asset_service/pharos_config.json. Env overrides beat config:
PHAROS_LIBRARY_ROOT beats library_root, PHAROS_AUDIO_ROOT beats the
audio section, and so on. Scripts stay runnable without the app
installed -- only this file + a config json are needed.
"""

import glob
import json
import os
import re
import shutil
from pathlib import Path

import sys as _sys

# Console output must never crash on a name outside the console's code
# page: agents read Pharos through a pipe, which on Windows is cp1252, and
# a CJK/emoji folder name killed init, ingest and the scanner mid-run.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass


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
            return json.loads(p.read_text(encoding="utf-8-sig"))
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


NEAR_EMPTY = 10


def _live_rows(target: str) -> int:
    """Rows in an existing index file (jsonl lines, or the longest list in
    a json document); 0 when absent or unreadable."""
    try:
        text = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if str(target).endswith(".jsonl"):
        return sum(1 for line in text.splitlines() if line.strip())
    try:
        doc = json.loads(text)
    except ValueError:
        return 0
    if isinstance(doc, list):
        return len(doc)
    if isinstance(doc, dict):
        return max((len(v) for v in doc.values() if isinstance(v, list)),
                   default=0)
    return 0


def refuse_near_empty(n: int, target: str, what: str = "records") -> None:
    """Guard for LIVE index writers: a wrong root once REPLACED a real
    index with fixture data. Refuse only that -- a near-empty result
    (< 10 rows, and under half of what is live) about to replace a live
    index that holds >= 10. A first build, a library that is simply
    small, or one that lost a pack or two, is written."""
    if n >= NEAR_EMPTY:
        return
    live = _live_rows(target)
    if live >= NEAR_EMPTY and n * 2 < live:
        raise SystemExit(
            "FATAL: only %d %s -- refusing to overwrite the live index %s "
            "(%d rows) with a near-empty scan (wrong root?)"
            % (n, what, target, live))


def db_path() -> str:
    env = os.environ.get("PHAROS_DB")
    if env:
        return env
    reg = os.environ.get("PHAROS_REGISTRY_DIR") or load().get("registry_dir") or ""
    if reg:
        return str(Path(reg) / "assets.sqlite")
    return ""


# install locations searched by blender_exe() (module constants so a test
# can point them at a fake tree)
BLENDER_WIN_ROOT = r"C:\Program Files\Blender Foundation"
BLENDER_MAC_APP = "/Applications/Blender.app/Contents/MacOS/Blender"


def _blender_version(exe: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)*)", os.path.basename(os.path.dirname(exe)))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def blender_exe() -> str:
    """Blender executable for the pipeline drivers, or "" when none found.
    BLENDER_EXE (when it points at a file) > `blender` on PATH > the
    highest-versioned <BLENDER_WIN_ROOT>/Blender X.Y/blender.exe (5.10
    beats 5.9) > the macOS app bundle."""
    env = os.environ.get("BLENDER_EXE", "")
    if env and os.path.isfile(env):
        return env
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    found = [p for p in glob.glob(os.path.join(BLENDER_WIN_ROOT, "Blender *",
                                               "blender.exe"))
             if os.path.isfile(p)]
    if found:
        return max(found, key=_blender_version)
    if os.path.isfile(BLENDER_MAC_APP):
        return BLENDER_MAC_APP
    return ""
