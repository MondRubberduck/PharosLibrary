"""pharos ingest — run every SAFE import chain in the right order, then
hand the decision-y chains to the user as questions.

    python pharos.py ingest [--dry-run]

What runs AUTOMATICALLY (no engine, no consent needed):
    scanner.py   on every detected model folder AND on the audio section
                 (when no crawl index exists yet)
    indexer.py   on the library root (animation clip packs)
    importers    collection / textures / audio / meshes (same order the
                 server uses at startup)
    docs         AGENT_START_HERE.md / AGENT_API.md with live counts

What NEVER runs automatically (engines, hours, or judgment calls) --
these are printed as QUESTIONS for the running agent to relay:
    chain 1      UE pack crawl (needs UE 5.x + the local sandbox)
    chain 2      KitBash3D kit export (needs Blender; restructures nothing)
    chain 3      .blend / native container enumeration (needs Blender)
    chain 4      crawl-quality audio/texture indexes (needs a vision crawl)

Safety: same rules as the app -- the library is read-only except
_Agent_Files/ and Exports/; the registry is derived data; scanner rows
survive every rebuild.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import config
from .init import detect

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

    print(f"pharos ingest -- library: {root}"
          f"{'  (DRY RUN)' if dry else ''}")
    report = detect(root)
    picks = report["sections"]
    print(f"  sections detected: "
          f"animation={picks.get('animation', ['-'])[0] if picks.get('animation') else '-'} | "
          f"textures={picks.get('textures', ['-'])[0] if picks.get('textures') else '-'} | "
          f"audio={picks.get('audio', ['-'])[0] if picks.get('audio') else '-'}")

    # ---- THE CONFIG IS THE CONTRACT ---------------------------------------
    # scan folders come from pharos_config.json (written by init); a fresh
    # detection only fills the gap for configs that predate the key. When
    # both exist and disagree, that is DRIFT: report it, prefer the config
    # (the user may have hand-curated it), never silently re-detect.
    cfg_scan = [f for f in (cfg.get("scan_folders") or [])
                if (root / f).is_dir()]
    det_scan = [f for f in report["mesh_folders"] if (root / f).is_dir()]
    scan_folders = cfg_scan or det_scan
    if cfg_scan and sorted(cfg_scan) != sorted(det_scan):
        print(f"  config/detect drift on model folders "
              f"(config wins): {', '.join(cfg_scan)} | "
              f"detector now says: {', '.join(det_scan) or '-'}")
    elif not cfg_scan and det_scan:
        print(f"  note: config has no scan_folders key; using the "
              f"detector's picks (re-run init --force to pin them)")
    if scan_folders:
        print(f"  model folders     : {', '.join(scan_folders)}")
    if report.get("manifest_roots"):
        print(f"  manifest packs    : "
              f"{len(report.get('manifest_packs_found', []))} "
              f"under {', '.join(report['manifest_roots'])}")
    if report.get("blend_folders"):
        print(f"  blend folders     : {', '.join(report['blend_folders'])}"
              f"  (decision needed -- NOT scanned)")
    if report.get("root_files"):
        n = sum(report["root_files"].values())
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

    audio_jsonl = (root / (picks.get("audio", [""])[0])
                   / "library_files.jsonl") if picks.get("audio") else None
    if picks.get("audio"):
        if audio_jsonl and audio_jsonl.is_file():
            print(f"\n  audio: crawl index found ({audio_jsonl.name}) -- "
                  f"skipping the folder scan (the crawl's own metadata is "
                  f"richer; scanner rows survive regardless)")
        else:
            failed_steps += 1 if _run([py, str(
                Path(REPO) / "service" / "asset_service" / "scanner.py"),
                str(root / picks["audio"][0])], dry) else 0

    if picks.get("animation"):
        failed_steps += 1 if _run([py, str(
            Path(REPO) / "service" / "asset_service" / "indexer.py"),
            "--root", str(root)], dry) else 0
    else:
        print("\n  animation: no clip folder detected -- skipping indexer")

    print("", flush=True)
    if not dry:
        # one missing/moved source (e.g. no purchase CSV yet) must skip
        # THAT importer, never kill the chain -- browse.py guards the same
        # calls at server startup; ingest must match it
        for name, fn in (("collection", collection_import.import_collection),
                         ("textures", textures_import.import_textures),
                         ("audio", audio_import.import_audio),
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
        # owned vs on-disk: the purchase CSV's Local Folder is on-disk
        # evidence -- mark rows 'local' when the folder REALLY exists,
        # then the (optional) availability catalog refines the rest via
        # name matching. Without this, every purchase read as
        # owned-not-downloaded even when the files sit on disk.
        try:
            import sqlite3
            conn = sqlite3.connect(config.DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, folder FROM collection "
                "WHERE folder IS NOT NULL AND folder != ''").fetchall()
            local_ids = [r["id"] for r in rows if Path(r["folder"]).is_dir()]
            if local_ids:
                conn.executemany(
                    "UPDATE collection SET availability='local' WHERE id=?",
                    [(i,) for i in local_ids])
                conn.commit()
            conn.close()
            if local_ids:
                print(f"  availability: {len(local_ids)} purchase(s) with "
                      f"a folder on disk marked local", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"  availability marking skipped: {exc}", flush=True)
        try:
            for p in agent_docs.generate(config.DB_PATH):
                print(f"  wrote {p}", flush=True)
        except Exception as exc:                      # noqa: BLE001
            print(f"  docs skipped: {exc}", flush=True)

    # ---- decisions: agent relays these to the user ------------------------
    print("\n" + "-" * 24 + " ASK YOUR USER " + "-" * 24)
    print("  (setup is not complete until these are answered; every 'yes'")
    print("   needs the engine named next to it -- see pipeline/README.md)")
    qs = []
    qs.append("1. Are these ALL your asset folders? (other drives / "
              "external disks?) detected so far:")
    qs.append(f"   {root}")
    if report.get("manifest_packs_found"):
        qs.append(f"2. Crawl the {len(report['manifest_packs_found'])} Unreal "
                  f"pack(s) for material recipes? (needs UE 5.x + "
                  f"pipeline/conversion sandbox; chain 1)")
    elif report.get("uasset_total"):
        qs.append(f"2. {report['uasset_total']} RAW .uasset/.umap files "
                  f"detected in {', '.join(report.get('uasset_folders', []))} "
                  f"-- convert them with chain 1? (needs UE 5.x)")
    else:
        qs.append("2. Any Unreal .uasset packs to convert? (none detected; "
                  "chain 1 needs UE 5.x)")
    if report.get("kitbash_root") or report.get("blend_folders"):
        qs.append("3. Export KitBash3D kits to per-assembly FBX and/or "
                  "enumerate .blend containers? (needs Blender; chains 2+3; "
                  f"blend folders: {', '.join(report.get('blend_folders', [])) or 'none'})")
    else:
        qs.append("3. Any .blend kits/containers to index, or leave them "
                  "as-is? (none detected; chains 2+3 need Blender)")
    qs.append("4. Is there a purchase CSV for the collection? (ground "
              "truth for owned vs on-disk)")
    qs.append("5. Audio/texture crawl-quality indexes (categories, "
              "keywords) need a vision crawl pass -- run it, or ship with "
              "scanner-only metadata?")
    for q in qs:
        print("  " + q)
    print("-" * 66)
    print("\nnext steps:")
    print(f'  verify setup   : "{sys.executable}" "{REPO}/pharos.py" doctor')
    print(f'  start serving  : "{sys.executable}" "{REPO}/pharos.py" '
          f"serve --no-open")
    print("  build prompts  : docs/STARTING_PROMPT.md + "
          "docs/AGENT_SETUP_BRIEF.md")
    # ---- exit codes are the agent's signal (no prose parsing) ----------
    if dry:
        return 0
    if failed_steps:
        print(f"\npharos ingest: {failed_steps} step(s) FAILED -- "
              f"see the warnings above", flush=True)
        return 4
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
        anim = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE id LIKE 'pack::%'"
        ).fetchone()[0]
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
