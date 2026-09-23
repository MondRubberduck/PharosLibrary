"""pharos ingest — run every SAFE import chain in the right order, then
hand the decision-y chains to the user as questions.

    python pharos.py ingest [--dry-run]

What runs AUTOMATICALLY (no engine, no consent needed):
    scanner.py   on every detected model folder AND on the audio section
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

from . import config
from .init import _not_indexed_dirs, detect

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
    report = detect(root)
    picks = report["sections"]
    # the CONFIG names the sections (init weighed the candidates; the user
    # may have edited them); detection only fills a key that points nowhere
    sec = cfg.get("sections") or {}

    def _section(kind: str) -> str:
        name = (sec.get(kind) or "").strip()
        if name and (root / name).is_dir():
            return name
        return (picks.get(kind) or [""])[0]
    print(f"  sections: animation={_section('animation') or '-'} | "
          f"textures={_section('textures') or '-'} | "
          f"audio={_section('audio') or '-'}")

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
    # converted packs JOIN the config (additive: nothing is ever removed).
    # meshes_import reads recipes only under manifest_roots and chain 2
    # needs kitbash_root: a pack converted after init was ignored until the
    # user hand-edited pharos_config.json (laptop run)
    # a non-list value (hand-typed string) is ignored, as config.load() does
    have_roots = cfg.get("manifest_roots")
    have_roots = have_roots if isinstance(have_roots, list) else []
    add_roots = [r for r in report.get("manifest_roots") or []
                 if not any(db.path_under(r, have) for have in have_roots)]
    add_kit = ("" if (cfg.get("kitbash_root") or "").strip()
               else report.get("kitbash_root") or "")
    if add_roots or add_kit:
        cfg["manifest_roots"] = have_roots + add_roots
        if add_kit:
            cfg["kitbash_root"] = add_kit
        # applied in-process too: meshes_import shares this list object
        config.MANIFEST_ROOTS.extend(Path(r).resolve() for r in add_roots)
        verb = "would add (dry-run, not written):" if dry else "added"
        if not dry:
            raw = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8-sig"))
            raw_roots = raw.get("manifest_roots")
            raw["manifest_roots"] = ((raw_roots if isinstance(raw_roots, list)
                                      else []) + add_roots)
            if add_kit:
                raw["kitbash_root"] = add_kit
            config.save(raw)
        for r in add_roots:
            print(f"  config: {verb} manifest_roots entry {r}")
        if add_kit:
            print(f"  config: {verb} kitbash_root {add_kit}")
        print("    (detected converted packs/kits; pharos_config.json only "
              "gains entries here, it never loses any)")
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
        failed_steps += 1 if _run([py, str(
            Path(REPO) / "service" / "asset_service" / "indexer.py"),
            "--root", str(root)], dry) else 0
    else:
        print("\n  animation: no clip folder detected -- skipping indexer")

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
    # raw (unconverted) packs are the question; converted packs are done
    raw = report.get("raw_ue_packs") or []
    done = report.get("manifest_packs_found") or []
    if raw:
        qs.append(f"2. {len(raw)} Unreal pack(s) with raw .uasset content "
                  f"and NO conversion yet: {', '.join(raw)} -- convert them "
                  f"with chain 1? It extracts FBX + textures + material "
                  f"recipes; needs UE 5.x + the pipeline/conversion "
                  f"sandbox; slow: roughly 1-30 min of engine time per pack")
    elif report.get("uasset_total") and not done:
        qs.append(f"2. {report['uasset_total']} RAW .uasset/.umap files "
                  f"detected in {', '.join(report.get('uasset_folders', []))} "
                  f"-- convert them with chain 1? (needs UE 5.x)")
    else:
        qs.append("2. Any Unreal .uasset packs to convert? (none detected; "
                  "chain 1 needs UE 5.x)")
    if done:
        qs.append(f"   already converted, nothing to do: {len(done)} "
                  f"pack(s) -- their recipes import automatically")
    if report.get("kitbash_root") or report.get("blend_folders"):
        qs.append("3. Export KitBash3D kits to per-assembly FBX and/or "
                  "enumerate .blend containers? (needs Blender; chains 2+3; "
                  f"blend folders: {', '.join(report.get('blend_folders', [])) or 'none'})")
    else:
        qs.append("3. Any .blend kits/containers to index, or leave them "
                  "as-is? (none detected; chains 2+3 need Blender)")
    qs.append("4. Is there a purchase CSV for the collection? (ground "
              "truth for owned vs on-disk)")
    qs.append("5. Build richer audio/texture indexes (categories, keywords "
              "from file and folder names; pure Python, minutes; chain 4 "
              "writes index files into those folders) -- or keep the "
              "scanner-only metadata?")
    # blend folders and raw UE packs already have their own question
    asked = set(report.get("blend_folders") or []) | {
        p.split("/")[0] for p in raw}
    stray = [d for d in _not_indexed_dirs(root, cfg, config.DB_PATH)
             if d not in asked]
    if stray:
        qs.append(f"6. These top-level folders match no section and hold no "
                  f"indexed assets: {', '.join(stray)} -- what are they? "
                  f"(scan them with scanner.py, point a section at them in "
                  f"pharos_config.json, or leave them out)")
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
        try:      # `assets` only exists once indexer.py ran (animation)
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
