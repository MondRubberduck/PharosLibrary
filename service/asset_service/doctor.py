"""pharos doctor — one command that says what is set up, what is missing,
and the exact command that fixes each gap.

    python pharos.py doctor [--json]

Designed for BOTH audiences: the human reading a console and the coding
agent that needs a machine-readable verdict (--json) plus paste-ready
fix commands. Read-only: doctor never writes anything.

Checks, in order:
  python          version >= 3.10
  config          pharos_config.json exists, roots set, dirs exist
  registry        SQLite exists, schema version, per-section row counts
                  (a zero section is reported as NOT INDEXED, with the
                  command that fills it -- never as an error)
  library         configured section folders actually exist on disk
  agent files     _Agent_Files + generated docs stamp
  server          is anything already listening on the configured port?
  blender         BLENDER_EXE env / PATH / common install dirs (optional)
  unreal          UE_EXE env / common install dirs (optional, chain 1)
  git bash        bash on PATH (optional, shell drivers)
  mcp             the optional MCP client package (pip install "mcp<2")

Exit codes: 1 = NOT CONFIGURED (no config / roots missing); 0 otherwise
(READY and READY-WITH-GAPS are both success -- the gaps carry commands).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

from . import config


def _check(name: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _section_counts(db_path: str) -> tuple[dict, str]:
    """Row counts per section table + the registry schema version."""
    out: dict[str, object] = {}
    if not Path(db_path).is_file():
        return out, "missing"
    conn = sqlite3.connect(db_path)
    try:
        out["schema"] = conn.execute("PRAGMA user_version").fetchone()[0]
        for tbl in ("meshes", "textures", "audio", "collection"):
            try:
                out[tbl] = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                out[tbl] = None          # table not created yet
        try:
            out["anim_packs"] = conn.execute(
                "SELECT COUNT(*) FROM assets WHERE id LIKE 'pack::Animation/%'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            out["anim_packs"] = None
    finally:
        conn.close()
    return out, "ok"


def _find_blender() -> str:
    cand = shutil.which("blender")
    if cand:
        return cand
    for pat in ("C:/Program Files/Blender Foundation/Blender */blender.exe",
                "/usr/bin/blender", "/Applications/Blender.app/Contents/MacOS/Blender"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


def _find_ue() -> str:
    for pat in ("C:/Program Files/Epic Games/UE_*/Engine/Binaries/Win64/"
                "UnrealEditor-Cmd.exe",):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


def run_doctor(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    ap = argparse.ArgumentParser(prog="pharos doctor",
                                 description="check setup, print exact fixes")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report (for agents)")
    args = ap.parse_args(argv)

    checks: list[dict] = []
    cfg = config.load()
    lib = (cfg.get("library_root") or "").strip()
    reg = (cfg.get("registry_dir") or "").strip()
    repo = str(Path(__file__).resolve().parents[2])

    # ---- python ----------------------------------------------------------
    ok = sys.version_info >= (3, 10)
    checks.append(_check(
        "python", "ok" if ok else "fail",
        f"{sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}",
        "" if ok else "install Python 3.10+"))

    # ---- config ----------------------------------------------------------
    cfg_file = config._CONFIG_FILE
    if not cfg_file.is_file():
        checks.append(_check(
            "config", "missing", str(cfg_file),
            f'python "{repo}/pharos.py" init <path-to-your-assets-root>'))
    elif not lib or not reg:
        checks.append(_check(
            "config", "incomplete",
            f"library_root={lib or '(unset)'} registry_dir={reg or '(unset)'}",
            'python pharos.py init <assets-root> --force, or edit '
            'pharos_config.json by hand'))
    else:
        missing = [p for p in (lib, reg) if not Path(p).is_dir()]
        checks.append(_check(
            "config", "fail" if missing else "ok",
            f"library_root={lib} registry_dir={reg}"
            + (f"  MISSING DIRS: {', '.join(missing)}" if missing else ""),
            "" if not missing else "fix library_root/registry_dir in "
                                   "pharos_config.json"))

    # ---- registry + sections ---------------------------------------------
    db_path = str(Path(reg or ".") / "assets.sqlite")
    counts, reg_status = _section_counts(db_path)
    if reg_status == "missing":
        checks.append(_check(
            "registry", "missing", db_path,
            f'python "{repo}/pharos.py" serve   '
            f"(first start creates + fills it)"))
    else:
        empty_fill = {
            "meshes": 'python service/asset_service/scanner.py "<mesh folder>"',
            "textures": 'python pharos.py serve (importer runs at startup); '
                        'or the pipeline/agent_index texture chain',
            "audio": 'python service/asset_service/scanner.py "<audio folder>"',
            "collection": "provide the purchase CSV named in "
                          "pharos_config.json (collection_csv)",
            "anim_packs": f'python service/asset_service/indexer.py '
                          f'--root "{lib}"',
        }
        for key in ("meshes", "textures", "audio", "collection",
                    "anim_packs"):
            n = counts.get(key)
            if n is None:
                checks.append(_check(
                    f"section:{key}", "not-created",
                    "table absent (importer never ran)",
                    f'python "{repo}/pharos.py" serve'))
            elif n == 0:
                checks.append(_check(
                    f"section:{key}", "not-indexed",
                    "0 rows -- empty is a real state, but if you EXPECT "
                    "content here, fill it:", empty_fill[key]))
            else:
                checks.append(_check(f"section:{key}", "ok", f"{n} rows"))
        checks.append(_check("registry", "ok",
                             f"schema v{counts.get('schema')} at {db_path}"))

    # ---- library sections on disk ----------------------------------------
    if lib and Path(lib).is_dir():
        sections = cfg.get("sections") or {}
        for key in ("animation", "textures", "audio", "collection"):
            rel = sections.get(key) or ""
            if not rel:
                continue
            p = Path(lib) / rel
            checks.append(_check(
                f"folder:{rel}", "ok" if p.is_dir() else "missing",
                str(p),
                "" if p.is_dir() else
                f"point sections.{key} at a real folder in "
                f"pharos_config.json"))

    # ---- agent files ------------------------------------------------------
    af = (Path(lib) / (cfg.get("agent_files") or "_Agent_Files")
          if lib else None)
    start = af / "AGENT_START_HERE.md" if af else None
    if af is None or not af.is_dir():
        checks.append(_check(
            "agent-files", "missing", str(af) if af else "(no library_root)",
            f'python "{repo}/pharos.py" docs   (after imports)'))
    elif not start.is_file():
        checks.append(_check(
            "agent-files", "missing", "AGENT_START_HERE.md absent",
            f'python "{repo}/pharos.py" docs'))
    else:
        import datetime
        stamp = datetime.datetime.fromtimestamp(
            start.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        checks.append(_check(
            "agent-files", "ok", f"generated {stamp}",
            'regenerate after imports: python pharos.py docs'))

    # ---- server -----------------------------------------------------------
    host = (cfg.get("network") or {}).get("host", "127.0.0.1")
    port = (cfg.get("network") or {}).get("port", 8765)
    s = socket.socket()
    s.settimeout(0.5)
    busy = s.connect_ex((host, port)) == 0
    s.close()
    checks.append(_check(
        "server", "running" if busy else "stopped",
        f"http://{host}:{port}",
        "" if busy else f'python "{repo}/pharos.py" serve --no-open'))

    # ---- optional engines -------------------------------------------------
    blender = _find_blender()
    checks.append(_check(
        "blender", "found" if blender else "absent",
        blender or "scene building disabled",
        "" if blender else
        "install Blender 5.x and/or set BLENDER_EXE (headless builder)"))
    ue = os.environ.get("UE_EXE", "").strip() or _find_ue()
    checks.append(_check(
        "unreal", "found" if ue else "absent",
        ue or "chain 1 (UE pack crawl) disabled -- optional",
        "" if ue else "set UE_EXE to UnrealEditor-Cmd.exe to enable chain 1"))
    bash = shutil.which("bash")
    checks.append(_check(
        "git-bash", "found" if bash else "absent",
        bash or "shell drivers (pipeline/*.sh) unavailable",
        "" if bash else "install Git for Windows (shell drivers only)"))
    try:
        import importlib.util as _ilu
        has_mcp = _ilu.find_spec("mcp") is not None
    except Exception:
        has_mcp = False
    checks.append(_check(
        "mcp", "found" if has_mcp else "absent",
        "6 agent tools available" if has_mcp
        else 'optional: pip install "mcp<2"'))

    # ---- verdict ----------------------------------------------------------
    hard_fail = any(c["status"] in ("fail", "missing")
                    and c["name"] in ("python", "config", "registry")
                    for c in checks)
    if hard_fail:
        verdict = "NOT CONFIGURED"
    elif any(c["status"] in ("not-indexed", "not-created", "missing")
             for c in checks):
        verdict = "READY-WITH-GAPS"
    else:
        verdict = "READY"

    if args.json:
        print(json.dumps({"verdict": verdict, "checks": checks,
                          "library_root": lib, "registry_dir": reg,
                          "server": f"http://{host}:{port}"},
                         ensure_ascii=False, indent=1))
    else:
        print(f"pharos doctor -- {verdict}")
        print("-" * 62)
        for c in checks:
            mark = {"ok": "+", "found": "+", "running": "+",
                    "not-indexed": "!", "not-created": "!",
                    "missing": "x", "fail": "x", "absent": "-",
                    "stopped": "-", "incomplete": "!"}.get(
                c["status"], "?")
            print(f" [{mark}] {c['name']:<18} {c['status']:<12} "
                  f"{c['detail']}")
            if c["fix"] and c["status"] not in ("ok", "found", "running"):
                print(f"       fix: {c['fix']}")
        print("-" * 62)
        print("verdict: " + verdict)
        if verdict == "READY-WITH-GAPS":
            print("(empty sections are valid if the library has no such "
                  "content -- fill them only if you expect data there)")
    return 1 if hard_fail else 0
