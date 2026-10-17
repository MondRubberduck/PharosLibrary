"""pharos convert | extract | previews -- the engine-bound chains, gated by
the library map.

    python pharos.py convert  [--dry-run] [--pack <folder>]   Unreal -> FBX
    python pharos.py extract  [--dry-run]                     .blend -> FBX
    python pharos.py previews [--dry-run]                     clip previews

Nothing here starts without the owner's decision in the map:
    unreal    action=export      (pharos.py map set "<folder>" action=export)
    blender   action=extract
    animation action=previews
A folder without its decision is listed with the command that answers it,
and nothing runs. The commands are the ones pipeline/README.md documents;
this is the one place that runs them, with the consent check in front.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import config, library_map

REPO = Path(__file__).resolve().parents[2]


def _say(text: str) -> None:
    print(text, flush=True)


def _run(cmd: list, cwd: Path, dry: bool, env: dict | None = None) -> int:
    _say("> " + "".join(f"{k}={v!s}  " for k, v in (env or {}).items() if k.startswith("PHAROS_"))
         + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    if dry:
        _say("  (dry-run: skipped)")
        return 0
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd),
                       env={**os.environ, **(env or {})})
    if r.returncode:
        _say(f"  exit {r.returncode}")
    return r.returncode


def _rows(kind: str, action: str, states=None) -> tuple:
    """(ready, undecided): included rows of `kind` whose effective decision
    is `action`, and the ones still waiting for a decision."""
    rows = library_map.load()
    pend = {r["folder"] for r in library_map.pending(rows)}   # top-most only
    ready, undecided = [], []
    for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
        if not library_map.included(r, rows) or r["kind"] != kind:
            continue
        if states is not None and r["state"] not in states:
            continue
        act = library_map.effective_action(r, rows)
        if act == action:
            ready.append(r)
        elif not act and r["folder"] in pend:
            undecided.append(r)
    return ready, undecided


def _refuse(undecided: list, action: str, what: str) -> int:
    if undecided:
        _say(f"nothing runs: {len(undecided)} {what} folder(s) have no decision yet. "
             f"Ask the owner, then answer with:")
        for r in undecided:
            other = [o for o in library_map.decision_options(r["kind"], r["state"])
                     if o != action]
            _say(f'  pharos.py map set "{r["folder"]}" action={action}'
                 + (f"   (or action={other[0]})" if other else ""))
        return 2
    _say(f"nothing to do: no {what} folder in the library map carries action={action}")
    return 0


def _blender() -> str:
    from .doctor import _find_blender
    return _find_blender()


def _ue() -> str:
    from .doctor import _find_ue
    return os.environ.get("UE_EXE", "").strip() or _find_ue()


def convert(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="pharos convert",
                                 description="extract FBX + textures + material recipes "
                                             "from the Unreal packs the owner approved")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pack", help="one map folder only")
    args = ap.parse_args(argv)
    root = config.LIBRARY_ROOT
    ready, undecided = _rows("unreal", "export", states=("raw", "project"))
    if args.pack:
        ready = [r for r in ready if r["folder"].lower() == args.pack.replace("\\", "/").strip("/").lower()]
    todo = [r for r in ready
            if not (root / r["folder"] / "Exports" / "manifest.json").is_file()]
    if not todo:
        if ready:
            _say(f"every approved Unreal pack is converted already ({len(ready)})")
            return 0
        return _refuse(undecided, "export", "Unreal")
    ue = _ue()
    bash = shutil.which("bash")
    if not args.dry_run and (not ue or not bash):
        _say("cannot convert: " + ("Unreal Editor not found (set UE_EXE to "
             "UnrealEditor-Cmd.exe)" if not ue else "") +
             (" and " if not ue and not bash else "") +
             ("Git Bash not on PATH (the conversion driver is a shell script)"
              if not bash else ""))
        return 2
    conv = REPO / "pipeline" / "conversion"
    if not (conv / "sandbox").is_dir() and not args.dry_run:
        _say("one-time: creating the conversion sandbox project")
        if _run([sys.executable, conv / "make_sandbox.py"], conv, False):
            return 4
    failed = 0
    for r in todo:
        pack = root / r["folder"]
        env = {"ASSETS_ROOT": str(pack.parent), "UE_EXE": ue}
        _say(f"\nconverting {r['folder']} (map decision: export)")
        rc = _run([bash or "bash", conv / "convert_packs.sh", "--pack", pack.name,
                   "--assets-root", str(pack.parent)], conv, args.dry_run, env)
        if rc == 0:
            rc = _run([bash or "bash", conv / "relink_pack.sh", "--pack", pack.name,
                       "--assets-root", str(pack.parent)], conv, args.dry_run, env)
        if rc == 0:
            rc = _run([sys.executable, conv / "verify_pack_export.py",
                       pack / "Exports"], conv, args.dry_run)
        failed += 1 if rc else 0
    _say(f"\nconverted {len(todo) - failed} of {len(todo)} pack(s)"
         + ("; run pharos.py ingest to import them" if not args.dry_run else ""))
    return 4 if failed else 0


def extract(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="pharos extract",
                                 description="export the Blender kits and .blend folders "
                                             "the owner approved to FBX + manifests")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    kits, und_k = _rows("blender", "extract", states=("kit",))
    files, und_f = _rows("blender", "extract", states=("files",))
    undecided = und_k + und_f
    if not kits and not files:
        return _refuse(undecided, "extract", "Blender")
    if not args.dry_run and not _blender():
        _say("cannot extract: Blender not found (set BLENDER_EXE, or install Blender 4.x/5.x)")
        return 2
    # the pipeline scripts read kitbash_root / skip dirs from the config: make
    # sure it mirrors the map first
    cfg = config.load()
    library_map.apply_to_config(library_map.load(), cfg, config.LIBRARY_ROOT)
    if not args.dry_run:
        config.save(cfg)
    rc = 0
    # the scripts enumerate from the config (kit root, skip dirs); the
    # map's approved folders go with them, so a kit or a .blend folder the
    # owner leaves alone is never opened
    if kits:
        env = {"PHAROS_MAP_FOLDERS": os.pathsep.join(r["folder"] for r in kits)}
        _say(f"\nexporting {len(kits)} kit(s) under {cfg.get('kitbash_root')} (map decision: extract)")
        kb = REPO / "pipeline" / "kitbash"
        rc |= _run([sys.executable, kb / "export_all_kb3d.py"], kb, args.dry_run, env)
        rc |= _run([sys.executable, kb / "run_kb3d_metadata.py"], kb, args.dry_run, env)
    if files:
        env = {"PHAROS_MAP_FOLDERS": os.pathsep.join(r["folder"] for r in files)}
        _say(f"\nenumerating the objects of {len(files)} .blend folder(s): "
             f"{', '.join(r['folder'] for r in files[:6])}{' ...' if len(files) > 6 else ''}")
        nat = REPO / "pipeline" / "native"
        bash = shutil.which("bash")
        rc |= _run([sys.executable, nat / "make_native_manifest2.py"], nat, args.dry_run, env)
        rc |= _run([bash or "bash", nat / "index_native_all.sh"], nat, args.dry_run, env)
        rc |= _run([sys.executable, nat / "run_blends.py"], nat, args.dry_run, env)
        rc |= _run([sys.executable, nat / "promote_native.py"], nat, args.dry_run, env)
        rc |= _run([sys.executable, nat / "promote_blends.py"], nat, args.dry_run, env)
    _say("\ndone" + ("; run pharos.py ingest to import the records" if not args.dry_run else ""))
    return 4 if rc else 0


def previews(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="pharos previews",
                                 description="record browser previews of the animation "
                                             "folders the owner approved")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ready, undecided = _rows("animation", "previews")
    if not ready:
        return _refuse(undecided, "previews", "animation")
    host = (config.NETWORK or {}).get("host", "127.0.0.1")
    port = (config.NETWORK or {}).get("port", 8765)
    url = f"http://{host}:{port}/batchrender"
    _say(f"clip previews for: {', '.join(r['folder'] for r in ready)}")
    _say(f"the renderer is a browser page: {url}  (it records every clip it "
         f"has not recorded yet and uploads the result; keep the tab open)")
    if args.dry_run:
        return 0
    s = socket.socket()
    s.settimeout(0.5)
    up = s.connect_ex((host, port)) == 0
    s.close()
    if not up:
        _say("the server is not running: start it (pharos.py serve), then open that page")
        return 2
    import webbrowser
    webbrowser.open(url)
    return 0


def run_chain(cmd: str, argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    if not config.is_configured():
        _say("pharos_config.json is not configured yet -- run pharos.py init first")
        return 2
    if not library_map.load():
        _say(f"no library map yet at {library_map.PATH} -- run pharos.py ingest first")
        return 2
    return {"convert": convert, "extract": extract, "previews": previews}[cmd](argv)
