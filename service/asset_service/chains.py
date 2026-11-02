"""pharos convert | extract | previews -- the engine-bound chains, gated by
the library map.

    python pharos.py convert  [--dry-run] [--pack <folder>]   Unreal -> FBX
    python pharos.py extract  [--dry-run]                     .blend -> FBX
    python pharos.py previews [--dry-run]                     clip previews
    python pharos.py previews --sheets [--folder F] [--force] contact sheets (Blender)
    python pharos.py previews --keys K [K ...]                renders on demand (Blender)

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
import json
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


def _needs_relink(manifest) -> bool:
    """A manifest without a wiring block (a v1 manifest, or an export whose
    relink step failed) needs the relink; one that carries its wiring does
    not, whatever it resolved (a pack whose masters hardcode their
    textures resolves 0 of 493 and is right), and the engine is not
    started for it."""
    try:
        m = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(m.get("wiring"), dict)


def _texture_loss(manifest) -> str:
    """The pack_status texture-loss reason for a manifest, '' when the
    textures came through (or the manifest cannot be read)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pack_status", str(REPO / "pipeline" / "conversion" / "pack_status.py"))
        ps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ps)
        m = json.loads(Path(manifest).read_text(encoding="utf-8"))
        fails = m.get("failures") or []
        tex = [f for f in fails if isinstance(f, dict) and (f.get("stage") or "").startswith("texture")]
        return ps.texture_loss(m, tex)
    except Exception:                                     # noqa: BLE001
        return ""


def _pack_key(folder: str) -> str:
    return folder.replace("\\", "/").strip("/").lower()


def _explain_pack(pack: str, rows: dict, relink: bool) -> int:
    """Why --pack names nothing that runs: said about THAT folder (the
    answer once listed another folder's missing decision, and following
    it would have approved a full engine conversion the owner never
    asked about)."""
    r = rows.get(_pack_key(pack))
    if r is None:
        _say(f"nothing runs: {pack!r} is no folder of the library map "
             f"(pharos.py map show lists them)")
    elif r["kind"] != "unreal":
        _say(f"nothing runs: {r['folder']} is a {r['kind'] or 'unclassified'} folder, "
             f"not an Unreal pack")
    elif not library_map.included(r, rows):
        _say(f"nothing runs: {r['folder']} is excluded (include=no on it or on a parent)")
    elif r["state"] == "converted" and not relink:
        _say(f"nothing runs: {r['folder']} is converted already; "
             f"pharos.py convert --relink-only --pack \"{r['folder']}\" rewires its recipes")
    elif r["state"] not in ("raw", "project", "converted"):
        _say(f"nothing runs: {r['folder']} is an Unreal folder in state "
             f"{r['state'] or '(a folder of packs)'}, not a pack")
    elif library_map.effective_action(r, rows) == "leave":
        _say(f"nothing runs: the map's decision for {r['folder']} is leave; "
             f"pharos.py map set \"{r['folder']}\" action=export changes it")
    else:
        return _refuse([r], "export", "Unreal")
    return 2


def convert(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="pharos convert",
                                 description="extract FBX + textures + material recipes "
                                             "from the Unreal packs the owner approved")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pack", help="one map folder only")
    ap.add_argument("--relink-only", action="store_true",
                    help="no engine export: rewire and verify the converted packs that "
                         "have a manifest (recipes missing after a failed relink)")
    args = ap.parse_args(argv)
    root = config.LIBRARY_ROOT
    rows = library_map.load()
    ready, undecided = _rows("unreal", "export", states=("raw", "project"))
    if args.relink_only:
        # a converted pack was approved when it was exported; the survey
        # drops that spent decision with the state change, and the pack
        # stays the owner's to rewire (engine-free: the relink writes the
        # manifest's recipes and nothing else)
        seen = {r["folder"].lower() for r in ready}
        for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
            if r["kind"] == "unreal" and r["state"] == "converted" \
                    and library_map.included(r, rows) and r["folder"].lower() not in seen \
                    and library_map.effective_action(r, rows) != "leave":
                ready.append(r)
    if args.pack:
        key = _pack_key(args.pack)
        ready = [r for r in ready if r["folder"].lower() == key]
        undecided = [r for r in undecided if r["folder"].lower() == key]
        if not ready and not undecided:
            return _explain_pack(args.pack, rows, args.relink_only)
    has_manifest = lambda r: (root / r["folder"] / "Exports" / "manifest.json").is_file()  # noqa: E731
    if args.relink_only:
        # every pack whose manifest lacks recipes; the one --pack names
        # in any case (the owner asks for it: rewired and verified again)
        todo = [r for r in ready if has_manifest(r)
                and (args.pack or _needs_relink(root / r["folder"] / "Exports" / "manifest.json"))]
        if not todo:
            with_man = [r for r in ready if has_manifest(r)]
            if with_man and not args.pack:
                _say(f"every converted pack's manifest carries its recipes ({len(with_man)}); "
                     f"pharos.py convert --relink-only --pack \"<folder>\" rewires one anyway")
                return 0
            _say("no converted pack with an Exports/manifest.json to relink"
                 + (f" (candidates: {len(ready)})" if ready else ""))
            return 0 if ready else _refuse(undecided, "export", "Unreal")
    else:
        todo = [r for r in ready if not has_manifest(r)]
    if not todo:
        if ready:
            _say(f"every approved Unreal pack is converted already ({len(ready)}); "
                 f"pharos.py convert --relink-only rewires one whose recipes are missing; "
                 f"to export one again, move its Exports folder away first")
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
        rc = 0
        if not args.relink_only:
            _say(f"\nconverting {r['folder']} (map decision: export)")
            rc = _run([bash or "bash", conv / "convert_packs.sh", "--pack", pack.name,
                       "--assets-root", str(pack.parent)], conv, args.dry_run, env)
            if rc and not args.dry_run and has_manifest(r):
                loss = _texture_loss(root / r["folder"] / "Exports" / "manifest.json")
                if loss:
                    # the models are there, their textures are not: a relink
                    # cannot bring them; the export has to run again
                    _say(f"  {loss}: the pack would import untextured. Fix the engine's "
                         f"texture exporter, move {r['folder']}/Exports away (the conversion's "
                         f"own output) and run pharos.py convert --pack \"{r['folder']}\" again")
                else:
                    # the engine wrote the pack but the driver's verdict was FAIL:
                    # the manifest is there, the recipes are not
                    _say(f"  the export left a manifest; after the cause is fixed: "
                         f"pharos.py convert --relink-only --pack \"{r['folder']}\"")
        else:
            _say(f"\nrelinking {r['folder']} (manifest present, "
                 + ("converted pack" if r["state"] == "converted" else "map decision: export")
                 + ")")
        if rc == 0:
            # relink-only: the sandbox copy the manifest was exported from
            # may be stale or gone (a failed conversion, a cleaned sandbox);
            # --sync refreshes it from the pack before the engine reads it
            rc = _run([bash or "bash", conv / "relink_pack.sh", "--pack", pack.name,
                       "--assets-root", str(pack.parent)]
                      + (["--sync"] if args.relink_only else []), conv, args.dry_run, env)
        if rc == 0:
            rc = _run([sys.executable, conv / "verify_pack_export.py",
                       pack / "Exports"], conv, args.dry_run)
        failed += 1 if rc else 0
    what = ("would relink" if args.dry_run else "relinked") if args.relink_only \
        else ("would convert" if args.dry_run else "converted")
    done = len(todo) - failed
    _say(f"\n{what} {done} of {len(todo)} pack(s)"
         + ("; run pharos.py ingest to import them" if done and not args.dry_run else ""))
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
        _say(f"\nexporting {len(kits)} kit(s) (map decision: extract): "
             f"{', '.join(r['folder'] for r in kits[:6])}{' ...' if len(kits) > 6 else ''}")
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
                                             "folders the owner approved; render contact "
                                             "sheets of the model folders (--sheets) or "
                                             "pictures of given models (--keys) in Blender")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sheets", action="store_true",
                    help="one contact sheet per folder node that holds models")
    ap.add_argument("--keys", nargs="+", metavar="KEY",
                    help="render these mesh keys (as /api/meshes serves them)")
    ap.add_argument("--folder", action="append", metavar="FOLDER",
                    help="with --sheets: only this map folder (repeatable)")
    ap.add_argument("--force", action="store_true", help="render again what exists")
    ap.add_argument("--limit", type=int, default=36, help="models per sheet (max 36)")
    args = ap.parse_args(argv)
    if args.sheets or args.keys:
        return _render_models(args)
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


def _render_models(args) -> int:
    """Contact sheets per folder node, or tiles for given keys: one
    Blender pass per run, resumable (rendered tiles are kept)."""
    from . import sheets
    blender = sheets.find_blender()
    if not blender and not args.dry_run:
        _say("cannot render: Blender not found (set BLENDER_EXE, or install Blender 4.x/5.x)")
        return 2
    root = config.LIBRARY_ROOT
    if args.keys:
        if args.dry_run:
            todo = [k for k in args.keys if args.force or not sheets.tile_path(k).is_file()]
            _say(f"{len(todo)} of {len(args.keys)} picture(s) would be rendered -> {sheets.tiles_dir()}")
            for k in todo:
                _say(f"  {k}")
            return 0
        res = sheets.render_keys(args.keys, root, blender=blender, log=_say, force=args.force)
        for k, v in res.items():
            _say(f"  {'ok     ' if v.get('ok') else 'FAILED '} {k}"
                 + ("" if v.get("ok") else f"  ({v.get('error')})")
                 + ("  (cached)" if v.get("cached") else ""))
        n_ok = sum(1 for v in res.values() if v.get("ok"))
        _say(f"{n_ok} of {len(res)} picture(s) under {sheets.tiles_dir()}; "
             f"served as /render/<hash>.png (the mesh item's `render`)")
        return 0 if n_ok == len(res) else 4
    stats = sheets.build_sheets(config.DB_PATH, root, folders=args.folder, force=args.force,
                                limit=min(max(args.limit, 1), sheets.LIMIT), blender=blender,
                                log=_say, dry_run=args.dry_run)
    if args.dry_run:
        _say(f"{len(stats['nodes'])} folder(s) would get a sheet; {stats['skipped']} have one")
        return 0
    _say(f"sheets: {stats['sheets']} rendered, {stats['skipped']} kept, "
         f"{stats['tiles']} new tile(s), {stats['failed']} model(s) failed -> {sheets.sheets_dir()}")
    if stats["failed"] and not stats["tiles"] and not stats["skipped"]:
        return 4                    # nothing came out of Blender
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
