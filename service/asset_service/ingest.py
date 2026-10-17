"""pharos ingest — run every SAFE import chain in the right order, then
hand the decision-y chains to the user as questions.

    python pharos.py ingest [--dry-run]

The LIBRARY MAP (<library>/_Agent_Files/library_map.csv) says what each
folder is and what the owner decided; ingest surveys the library first so
new folders get a row, then runs only what the map allows. A decision the
owner has not made is printed ONCE as a question with the command that
answers it (pharos.py map set <folder> action=...); an answered one is
never asked again.

What runs AUTOMATICALLY (no engine, no consent needed):
    scanner.py   on every included 3d-assets folder AND on the audio section
                 (when no crawl index exists yet)
    indexer.py   on the library root (animation clip packs)
    agent index  build_agent_index.py / build_kb3d_index.py when a converted
                 pack's Exports manifest is newer than its _Agent_Files index
                 (detected manifest roots are ADDED to pharos_config.json)
    importers    collection / textures / audio / meshes (same order the
                 server uses at startup)
    docs         AGENT_START_HERE.md / AGENT_API.md with live counts

What NEVER runs automatically (engines, hours, or judgment calls) --
these are printed as QUESTIONS for the running agent to relay:
    chain 1      UE pack crawl (needs UE 5.x + the local sandbox)
    chain 2      KitBash3D kit export (needs Blender; restructures nothing)
    chain 3      .blend / native container enumeration (needs Blender)
    chain 4      richer audio/texture indexes from file and folder names
                 (pure Python, minutes; writes index files into those sections)

Safety: same rules as the app -- the library is read-only except
_Agent_Files/ and Exports/; the registry is derived data; scanner rows
survive every rebuild.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config, library_map

REPO = str(Path(__file__).resolve().parents[2])


def _run(cmd: list[str], dry: bool) -> int:
    print("\n> " + " ".join(cmd), flush=True)
    if dry:
        print("  (dry-run: skipped)", flush=True)
        return 0
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"  WARNING: exit {r.returncode} -- continuing with the "
              f"remaining steps", flush=True)
    return r.returncode


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _index_packs(index: Path) -> set:
    """The Exports folders an agent index was built from (from each row's
    fbx path; rows without an Exports ancestor are ignored)."""
    out = set()
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            fbx = (json.loads(line) or {}).get("fbx") or ""
        except ValueError:
            continue
        parts = Path(fbx.replace("\\", "/")).parts
        if "Exports" in parts:
            i = len(parts) - 1 - parts[::-1].index("Exports")
            out.add(_norm(Path(*parts[:i + 1])))
    return out


def run_ingest(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    ap = argparse.ArgumentParser(
        prog="pharos ingest",
        description="run the safe import chains in order, print the "
                    "decision questions for the user")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact commands without running them")
    args = ap.parse_args(argv)
    dry = args.dry_run

    cfg = config.load()
    lib = (cfg.get("library_root") or "").strip()
    if not cfg.get("library_root") or not cfg.get("registry_dir"):
        print("pharos_config.json is not configured yet -- run "
              f'pharos.py init <assets-root> first, or run '
              f'pharos.py doctor to see what is missing')
        return 2
    root = Path(lib)
    py = sys.executable

    # a registry left behind by ANOTHER library (the default registry dir
    # is shared machine-wide): stop before any chain writes into it
    from asset_service import db
    owner = db.registry_foreign(config.DB_PATH, root)
    if owner:
        print(db.foreign_registry_message(config.DB_PATH, owner, root))
        return 2

    print(f"pharos ingest -- library: {root}"
          f"{'  (DRY RUN)' if dry else ''}")
    # ---- THE MAP IS THE CONTRACT -----------------------------------------
    # survey the library (1-2 s), give new folders a row, then mirror the
    # map's picks into pharos_config.json (sections, scan folders, manifest
    # roots, skip dirs): the importers still read the config
    res = library_map.refresh(root)
    if "error" in res:
        print(f"  survey failed: {res['error']}")
        return 2
    rows = library_map.load()
    if res.get("map_added") or res.get("map_filled"):
        print(f"  library map: {res.get('map_added', 0)} folder(s) added, "
              f"{res.get('map_filled', 0)} filled in -> {library_map.PATH}")
    library_map.apply_to_config(rows, cfg, root, library_map.weights_of(res))
    if not dry:
        raw_cfg = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8-sig"))
        for key in ("sections", "scan_folders", "manifest_roots", "kitbash_root",
                    "indexer_skip_dirs", "collection_csv"):
            if key in cfg:
                raw_cfg[key] = cfg[key]
            else:
                raw_cfg.pop(key, None)
        config.save(raw_cfg)
        config.MANIFEST_ROOTS[:] = [Path(r).resolve() for r in cfg["manifest_roots"]]
    sec = cfg.get("sections") or {}
    # the importers below run in THIS process: their roots follow the map's
    # picks, not the config the process started with
    dflt = config._DEFAULTS["sections"]
    config.TEX_ROOT = root / (sec.get("textures") or dflt["textures"])
    config.AUDIO_ROOT = root / (sec.get("audio") or dflt["audio"])
    config.COLLECTION_ROOT = root / (sec.get("collection") or dflt["collection"])
    config.CSV_PATH = config.COLLECTION_ROOT / cfg.get("collection_csv",
                                                       config.CSV_PATH.name)

    def _section(kind: str) -> str:
        name = (sec.get(kind) or "").strip()
        return name if name and (root / name).is_dir() else ""
    print(f"  sections: animation={_section('animation') or '-'} | "
          f"textures={_section('textures') or '-'} | "
          f"audio={_section('audio') or '-'} | "
          f"purchases={_section('collection') or '-'}")
    scan_folders = [f for f in cfg.get("scan_folders") or [] if (root / f).is_dir()]
    if scan_folders:
        print(f"  model folders     : {', '.join(scan_folders)}")
    # what the map knows about engine content (the old detector's report)
    report: dict = {"ue_manifests": [], "kit_manifests": [], "raw_ue_packs": [],
                    "manifest_packs_found": [], "blend_folders": [],
                    "kitbash_root": cfg.get("kitbash_root") or ""}
    for r in rows.values():
        if not library_map.included(r, rows):
            continue
        if r["kind"] == "unreal" and r["state"] == "converted":
            m = root / r["folder"] / "Exports" / "manifest.json"
            if m.is_file():
                report["ue_manifests"].append(str(m))
                report["manifest_packs_found"].append(r["folder"].rsplit("/", 1)[-1])
        elif r["kind"] == "unreal" and r["state"] in ("raw", "project"):
            report["raw_ue_packs"].append(r["folder"])
        elif r["kind"] == "blender" and r["state"] == "kit-exported":
            m = root / r["folder"] / "Exports" / "kit_manifest.json"
            if m.is_file():
                report["kit_manifests"].append(str(m))
        elif r["kind"] == "blender" and r["state"] in ("kit", "files"):
            report["blend_folders"].append(r["folder"])
    if report["manifest_packs_found"]:
        print(f"  converted pack folder(s): {len(report['manifest_packs_found'])} "
              f"(recipes import automatically; the scanner skips them)")
    if report["blend_folders"]:
        print(f"  blend folders     : {', '.join(report['blend_folders'][:8])}"
              f"{' ...' if len(report['blend_folders']) > 8 else ''}"
              f"  (not scanned; decision: extract or leave)")
    if res.get("root_files"):
        n = sum(res["root_files"].values())
        print(f"  !! {n} asset file(s) DIRECTLY in the library root are "
              f"invisible to every section -- move them into a subfolder")

    # ---- automatic chains -------------------------------------------------
    from asset_service import collection_import, textures_import, \
        audio_import, meshes_import, agent_docs

    failed_steps = 0
    for folder in scan_folders:
        failed_steps += 1 if _run([py, str(
            Path(REPO) / "service" / "asset_service" / "scanner.py"),
            str(root / folder)], dry) else 0

    audio_dir = _section("audio")
    audio_jsonl = (root / audio_dir / "library_files.jsonl") if audio_dir         else None
    if audio_dir:
        if audio_jsonl and audio_jsonl.is_file():
            print(f"\n  audio: crawl index found ({audio_jsonl.name}) -- "
                  f"skipping the folder scan (the crawl's own metadata is "
                  f"richer; scanner rows survive regardless)")
        else:
            failed_steps += 1 if _run([py, str(
                Path(REPO) / "service" / "asset_service" / "scanner.py"),
                str(root / audio_dir)], dry) else 0

    if _section("animation"):
        # the clips table: every clip measured (duration, fps, root
        # motion, skeleton) from one read of the file, cached per folder
        failed_steps += 1 if _run([py, str(
            Path(REPO) / "service" / "asset_service" / "measure.py"),
            "--clips", str(root / _section("animation"))], dry) else 0
        failed_steps += 1 if _run([py, str(
            Path(REPO) / "service" / "asset_service" / "indexer.py"),
            "--root", str(root)], dry) else 0
    else:
        print("\n  animation: no clip folder detected -- skipping the clip measure")

    # converted packs -> mesh rows: meshes_import creates rows ONLY from the
    # _Agent_Files index files (manifests add the recipes), so rebuild an
    # index whenever a manifest is newer than it (big libraries take minutes)
    for key, out_name, script in (
            ("ue_manifests", "models.jsonl",
             "pipeline/agent_index/build_agent_index.py"),
            ("kit_manifests", "kb3d_models.jsonl",
             "pipeline/kitbash/build_kb3d_index.py")):
        mans = report.get(key) or []
        out = config.AGENT_FILES / out_name
        # no manifests left: rebuild only if the index still lists packs
        # (the LAST pack was removed -- its rows must go too)
        if not mans and not (out.is_file() and _index_packs(out)):
            continue
        # fresh = newer than every manifest AND built from exactly these
        # packs: a pack COPIED in keeps its older mtimes, a removed pack
        # would keep its rows
        try:
            fresh = (bool(mans) and out.is_file()
                     and out.stat().st_mtime > max(
                         Path(m).stat().st_mtime for m in mans)
                     and _index_packs(out)
                     == {_norm(Path(m).parent) for m in mans})
        except OSError:
            fresh = False
        if fresh:
            print(f"\n  {out_name}: newer than all {len(mans)} manifest(s) "
                  f"-- index rebuild skipped", flush=True)
            continue
        failed_steps += 1 if _run([py, str(Path(REPO) / script)], dry) else 0

    print("", flush=True)
    if not dry:
        # one missing/moved source (e.g. no purchase CSV yet) must skip
        # THAT importer, never kill the chain -- browse.py guards the same
        # calls at server startup; ingest must match it
        for name, fn in (("collection", lambda d: collection_import.import_collection(d, config.CSV_PATH)),
                         ("textures", lambda d: textures_import.import_textures(d, config.TEX_ROOT)),
                         ("audio", lambda d: audio_import.import_audio(d, config.AUDIO_ROOT)),
                         ("meshes", meshes_import.import_meshes)):
            try:
                n = fn(config.DB_PATH)
                print(f"  {name} imported: {n}", flush=True)
            except Exception as exc:                  # noqa: BLE001
                # an absent source is a SKIP (a library without a
                # purchase CSV is a valid state); anything else (corrupt
                # file, locked DB) is a failed step
                if name == "collection" and isinstance(exc, FileNotFoundError):
                    print(f"  {name} import skipped: {exc}", flush=True)
                else:
                    failed_steps += 1
                    print(f"  {name} import FAILED: {exc}", flush=True)
        try:
            from asset_service import folders_import
            n_f = folders_import.build(config.DB_PATH, root, library_map.load())
            print(f"  folders: {n_f} node(s) with measured facts", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"  folders skipped: {exc}", flush=True)
        try:
            for p in agent_docs.generate(config.DB_PATH):
                print(f"  wrote {p}", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"  docs skipped: {exc}", flush=True)

    # ---- decisions: agent relays these to the user ------------------------
    # only what the map does not know yet; an answer is written with
    # `pharos.py map set` and never asked again
    rows = library_map.load()
    pend = library_map.pending(rows)
    print("\n" + "-" * 24 + " ASK YOUR USER " + "-" * 24)
    qs = []
    if not pend:
        print("  nothing pending: every decision is in the library map")
    else:
        print("  (setup is not complete until these are answered; every 'yes'")
        print("   needs the engine named next to it -- see pipeline/README.md)")
    qs.append("1. Are these ALL your asset folders? (other drives / "
              "external disks?) surveyed so far:")
    qs.append(f"   {root}  ({res.get('files', 0):,} files; map: {library_map.PATH})")
    raw = [r for r in pend if r["kind"] == "unreal"]
    done = report.get("manifest_packs_found") or []
    if raw:
        qs.append(f"2. {len(raw)} Unreal pack(s) with raw .uasset content "
                  f"and NO conversion yet: {', '.join(r['folder'] for r in raw)} -- "
                  f"convert them with chain 1? It extracts FBX + textures + "
                  f"material recipes; needs UE 5.x + the pipeline/conversion "
                  f"sandbox; slow: roughly 1-30 min of engine time per pack")
        for r in raw:
            qs.append(f'   answer: pharos.py map set "{r["folder"]}" action=export'
                      f'   (or action=leave)')
    if done:
        qs.append(f"   already converted, nothing to do: {len(done)} "
                  f"pack(s) -- their recipes import automatically")
    blend = [r for r in pend if r["kind"] == "blender"]
    if blend:
        qs.append(f"3. {len(blend)} Blender folder(s): extract their objects to "
                  f"FBX + manifest (needs Blender; chains 2+3) or leave them as "
                  f"they are? {', '.join(r['folder'] for r in blend[:8])}"
                  f"{' ...' if len(blend) > 8 else ''}")
        for r in blend[:8]:
            qs.append(f'   answer: pharos.py map set "{r["folder"]}" action=extract'
                      f'   (or action=leave)')
    unity = [r for r in pend if r["kind"] == "unity"]
    if unity:
        qs.append(f"4. {len(unity)} Unity project(s)/package(s): index the files "
                  f"under Assets/ (no prefab graph yet) or leave them? "
                  f"{', '.join(r['folder'] for r in unity)}")
        for r in unity:
            qs.append(f'   answer: pharos.py map set "{r["folder"]}" action=index-files'
                      f'   (or action=leave)')
    anim = [r for r in pend if r["kind"] == "animation"]
    if anim:
        qs.append(f"5. Record browser previews for the animation folder(s) "
                  f"{', '.join(r['folder'] for r in anim)}? (a browser tab, "
                  f"minutes; optional)")
        for r in anim:
            qs.append(f'   answer: pharos.py map set "{r["folder"]}" action=previews'
                      f'   (or action=no-previews)')
    stray = [r["folder"] for r in rows.values()
             if "/" not in r["folder"] and r["folder"] != "." and r["include"]
             and r["kind"] in ("other", "")]
    if stray:
        qs.append(f"6. These top-level folders match no section (nothing "
                  f"Pharos recognises): {', '.join(stray)} -- what are they? "
                  f'(pharos.py map set "<folder>" kind=<kind>, or include=no '
                  f"to leave them out)")
    for q in qs:
        print("  " + q)
    print("-" * 66)
    print("\nnext steps:")
    print(f'  verify setup   : "{sys.executable}" "{REPO}/pharos.py" doctor')
    print(f'  start serving  : "{sys.executable}" "{REPO}/pharos.py" '
          f"serve --no-open")
    print(f'  review the map : http://{(cfg.get("network") or {}).get("host", "127.0.0.1")}:'
          f'{(cfg.get("network") or {}).get("port", 8765)}/tags  (after serve)')
    # ---- exit codes are the agent's signal (no prose parsing) ----------
    if dry:
        return 0
    if failed_steps:
        print(f"\npharos ingest: {failed_steps} step(s) FAILED -- "
              f"see the warnings above", flush=True)
        return 4
    try:
        db.stamp_registry(config.DB_PATH, root)    # this library's registry
    except Exception as exc:                              # noqa: BLE001
        print(f"  registry stamp skipped: {exc}", flush=True)
    try:
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        totals = {}
        for tbl in ("meshes", "textures", "audio", "collection"):
            try:
                totals[tbl] = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                totals[tbl] = 0
        try:      # the clips table, else the legacy pack table
            anim = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        except sqlite3.OperationalError:
            anim = 0
        if not anim:
            try:
                anim = conn.execute(
                    "SELECT COUNT(*) FROM assets WHERE id LIKE 'pack::%'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                anim = 0
        conn.close()
    except Exception:                                     # noqa: BLE001
        return 0
    if all(v == 0 for v in totals.values()) and anim == 0:
        print("\npharos ingest: NOTHING WAS INDEXED -- the library shape "
              "was not recognized (loose files at the root? unknown "
              "layouts?). Run doctor and check pharos_config.json.",
              flush=True)
        return 3
    return 0
