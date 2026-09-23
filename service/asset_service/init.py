"""pharos init — detect asset folders under a root and write pharos_config.json.

Usage:
    python pharos.py init [ROOT] [--registry-dir DIR] [--force]

Scans ROOT's immediate children (file-extension census, bounded sampling)
and maps each folder to a Pharos section by dominant content:

    FBX/BVH clips            -> sections.animation
    image sets               -> sections.textures
    WAV/OGG/MP3/... sounds   -> sections.audio
    folder with a catalog CSV-> sections.collection
    children with Exports/manifest.json or kit_manifest.json
                             -> manifest_roots (material recipes join)

Detection is best-effort and deliberately VERBOSE: init prints everything
it decided so the user (or their agent) can correct the JSON by hand —
the config file is the contract, not the detector.

The registry (SQLite DB) defaults to ~/.pharos/registry so nothing is
ever written inside the asset library itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config

_REPO = Path(__file__).resolve().parents[2]

CLIP_EXTS = {".fbx", ".bvh"}
MESH_EXTS = {".fbx", ".obj", ".glb", ".gltf", ".stl", ".blend", ".usd", ".usda",
             ".usdc", ".usdz", ".uasset", ".umodel", ".max"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aif", ".aiff",
              ".aac", ".wma"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp",
              ".exr", ".hdr", ".dds", ".tga"}
SAMPLE_LIMIT = 2000          # files censused per folder; classification
                             # does not need the whole tree
FBX_PROBE_LIMIT = 3          # FBXs opened for animation evidence when a
                             # folder's classification is fbx-ambiguous


def _census(folder: Path) -> tuple[dict[str, int], list[Path]]:
    """Bounded extension census of a folder tree. Also returns up to
    FBX_PROBE_LIMIT fbx paths -- the animation-vs-mesh evidence probe
    needs real files when a folder's classification is fbx-ambiguous."""
    counts: dict[str, int] = {}
    fbx_samples: list[Path] = []
    n = 0
    try:
        for p in folder.rglob("*"):
            if n >= SAMPLE_LIMIT:
                break
            if p.is_file():
                ext = p.suffix.lower()
                counts[ext] = counts.get(ext, 0) + 1
                n += 1
                if ext == ".fbx" and len(fbx_samples) < FBX_PROBE_LIMIT:
                    fbx_samples.append(p)
    except OSError:
        pass
    return counts, fbx_samples


def _fbx_is_animation(samples: list[Path]) -> bool:
    """Binary evidence: FBX files carrying AnimStack/AnimationCurve
    nodes are animation clips, static geometry is not. A folder of FBX
    models must classify as MESHES even though .fbx also counts as a
    clip extension (and an animation library must stay animation)."""
    try:
        from asset_service.auto_classifier import probe_fbx_markers
    except Exception:
        return False
    for p in samples:
        try:
            with open(p, "rb") as fh:
                blob = fh.read(16 * 1024 * 1024)
            marks = probe_fbx_markers(blob) or {}
            if marks.get("has_anim"):
                return True
        except OSError:
            continue
    return False


def _dominant(counts: dict[str, int], fbx_samples: list[Path]) -> str:
    total = sum(counts.values()) or 1
    clips = sum(counts.get(e, 0) for e in CLIP_EXTS)
    audio = sum(counts.get(e, 0) for e in AUDIO_EXTS)
    images = sum(counts.get(e, 0) for e in IMAGE_EXTS)
    meshes = sum(counts.get(e, 0) for e in MESH_EXTS)
    # .fbx counts as BOTH mesh and clip extension; break the ambiguity
    # with binary evidence (bvh presence or AnimStack markers)
    if meshes > 0 and meshes >= clips:
        if clips > 0 and meshes == clips and counts.get(".fbx"):
            return "animation" if _fbx_is_animation(fbx_samples) else "meshes"
        return "meshes"
    best = max(clips, audio, images, meshes)
    if best == 0:
        return ""
    if best / total < 0.4:
        return ""
    if best == clips:
        return "animation"
    if best == audio:
        return "audio"
    if best == images:
        return "textures"
    return "meshes"          # model library: feed via scanner.py, no section


def _find_catalog_csvs(root: Path) -> list[Path]:
    """CSVs directly under root or one level deep that look like a
    purchase catalog (header row containing name + url/price/seller)."""
    hits = []
    for p in sorted(root.glob("*.csv")) + sorted(root.glob("*/*.csv")):
        try:
            with open(p, encoding="utf-8-sig", errors="replace",
                      newline="") as f:
                header = (f.readline() or "").lower()
        except OSError:
            continue
        if "name" in header and any(
                k in header for k in ("url", "price", "seller", "store")):
            hits.append(p)
    return hits


def detect(root: Path) -> dict:
    """Classify root's children. Returns a report dict (also printed)."""
    report: dict = {"root": str(root), "sections": {}, "mesh_folders": [],
                    "manifest_roots": [], "catalog_csvs": [],
                    "agent_files": (root / "_Agent_Files").is_dir(),
                    "unknown": [], "folder_counts": {}}
    if not root.is_dir():
        report["error"] = f"root does not exist: {root}"
        return report

    # census of files DIRECTLY in the root (not in any subfolder): these
    # are invisible to sections and the scanner -- they must be reported
    # loudly, never silently dropped (fresh-library finding: a flat
    # folder of assets indexed NOTHING with every signal green)
    root_counts: dict[str, int] = {}
    try:
        for p in sorted(root.iterdir()):
            if p.is_file() and not p.name.startswith((".", "_")):
                ext = p.suffix.lower()
                if ext:
                    root_counts[ext] = root_counts.get(ext, 0) + 1
    except OSError:
        pass
    if root_counts:
        report["root_files"] = root_counts

    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        counts, fbx_samples = _census(child)
        report["folder_counts"][child.name] = counts
        kind = _dominant(counts, fbx_samples)
        if counts.get(".blend"):
            # .blend files need an explicit decision (enumerate containers,
            # export kits, or leave them alone) -- the folder is surfaced
            # as that decision and never auto-scanned as a model folder
            report.setdefault("blend_folders", []).append(child.name)
        if kind == "meshes":
            if child.name not in report.get("blend_folders", []):
                report["mesh_folders"].append(child.name)
        elif kind:
            report["sections"].setdefault(kind, []).append(child.name)
        else:
            # a folder may still be a manifest root even when its census is
            # mixed (converted packs are .uasset + .fbx + .png)
            report["unknown"].append(child.name)
        if sum(counts.get(e, 0) for e in (".uasset", ".umap")):
            report.setdefault("uasset_folders", []).append(child.name)
            report["uasset_total"] = report.get("uasset_total", 0) + \
                sum(counts.get(e, 0) for e in (".uasset", ".umap"))

    # manifest roots: packs carrying Exports/manifest.json at ANY depth
    # (fresh libraries nest packs under category folders like "UE Packs/")
    manifest_parents = set()
    mroots = set()
    kit_roots = set()
    for pat, is_kit in (("Exports/manifest.json", False),
                        ("Exports/kit_manifest.json", True)):
        for p in root.rglob(pat):
            manifest_parents.add(p.parent.parent.name)
            mroots.add(p.parent.parent.parent)   # dir CONTAINING the pack
            # ingest rebuilds the agent index a manifest is newer than
            report.setdefault("kit_manifests" if is_kit else "ue_manifests",
                              []).append(str(p))
            if is_kit:
                kit_roots.add(p.parent.parent.parent)
    # UNEXPORTED KitBash3D kits: the kit exporter (export_all_kb3d.py) globs
    # <kitbash_root>/<Kit>/*.blender.native/*.blend -- detect that shape, or
    # chain 2 cannot start without hand-editing kitbash_root
    for b in root.rglob("*.blender.native/*.blend"):
        kit = b.parent.parent
        if kit != root and kit.is_relative_to(root):
            kit_roots.add(kit.parent)
    # keep only the shallowest roots (a root inside another root is noise)
    mroots = {r for r in mroots
              if not any(o != r and r.is_relative_to(o) for o in mroots)}
    if manifest_parents:
        report["manifest_roots"] = sorted(str(r) for r in mroots)
        report["manifest_packs_found"] = sorted(manifest_parents)
    if kit_roots:
        kits = {r for r in kit_roots
                if not any(o != r and r.is_relative_to(o)
                           for o in kit_roots)}
        report["kitbash_root"] = sorted(str(r) for r in kits)[0]

    # raw UE packs, PER PACK: a pack is the folder holding a `Content` root
    # (pipeline/conversion/pack_discovery.py finds it up to 3 levels below
    # the pack, e.g. <pack>/<project>/Content). It is CONVERTED once any
    # folder above that Content holds Exports/manifest.json -- converted
    # packs keep their .uasset files, so a raw-file count can't tell them
    # apart. Plugin Content (<project>/Plugins/<x>/Content) is its project's.
    raw_packs = set()
    for c in root.rglob("Content"):
        rel = c.relative_to(root)
        if (not c.is_dir() or rel.parts[0].startswith(("_", "."))
                or "content" in (p.lower() for p in rel.parts[:-1])):
            continue
        if not any(f.suffix.lower() in (".uasset", ".umap")
                   for f in c.rglob("*")):
            continue
        if any((a / "Exports" / "manifest.json").is_file()
               for a in c.parents if a != root and a.is_relative_to(root)):
            continue
        raw_packs.add(c.parent)
    raw_packs = {p for p in raw_packs
                 if not any(o != p and p.is_relative_to(o) for o in raw_packs)}
    if raw_packs:
        report["raw_ue_packs"] = sorted(p.relative_to(root).as_posix()
                                        for p in raw_packs)

    report["catalog_csvs"] = [str(p) for p in _find_catalog_csvs(root)]
    return report


def _not_indexed_dirs(root: Path, cfg: dict, db_path=None) -> list[str]:
    """Top-level library folders nothing claims: no section, scan folder,
    skip dir or manifest/kit root, and no indexed row lives under them.
    Used by ingest's ASK block and the server's NOT INDEXED banner."""
    # config/DB paths are usually resolved, the root as typed may not be
    # (macOS /var -> /private/var, Windows 8.3 short names): try both
    roots = (Path(root), Path(root).resolve())

    def top(p) -> str:
        for r in roots:
            try:
                return Path(p).relative_to(r).parts[0].lower()
            except (ValueError, IndexError):
                continue
        return ""
    # section / scan paths may be relative to the root OR absolute
    claimed = {top(root / str(v))
               for v in (cfg.get("sections") or {}).values() if v}
    claimed.update(top(root / str(f)) for f in cfg.get("scan_folders") or [])
    claimed.update(str(d).lower() for d in cfg.get("indexer_skip_dirs") or [])
    claimed.update(top(r) for r in (cfg.get("manifest_roots") or [])
                   + [cfg.get("kitbash_root") or ""])
    if db_path and Path(db_path).is_file():
        import sqlite3
        # plain path, SELECT only: a file:// URI can't open a UNC registry
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            conn = None
        for sql in ("SELECT DISTINCT fbx FROM meshes",
                    "SELECT DISTINCT folder FROM textures") if conn else ():
            try:
                claimed.update(top(p) for (p,) in conn.execute(sql) if p)
            except sqlite3.Error:
                pass
        if conn:
            conn.close()
    try:
        return [d.name for d in sorted(root.iterdir())
                if d.is_dir() and not d.name.startswith((".", "_"))
                and d.name.lower() not in claimed]
    except OSError:
        return []


def _pick(sections: dict, kind: str, counts_by_folder: dict,
          exclude: set = ()) -> str | None:
    """Pick a section's folder by ASSET WEIGHT, not alphabetical order.

    Alphabetical picks once let a purchase-CSV folder (2 screenshots)
    hijack the textures section from the real PBR library. Weight =
    number of files of the section's own kind, censused per folder;
    excluded folders (e.g. the one carrying the catalog CSV) never win."""
    weights = {"animation": CLIP_EXTS, "textures": IMAGE_EXTS,
               "audio": AUDIO_EXTS}
    exts = weights.get(kind, MESH_EXTS)
    best, best_name = -1, None
    for name in sections.get(kind) or []:
        if name in exclude:
            continue
        w = sum(counts_by_folder.get(name, {}).get(e, 0) for e in exts)
        if w > best:
            best, best_name = w, name
    return best_name


def build_config(report: dict, registry_dir: Path) -> dict:
    """Turn a detect() report into a pharos_config.json payload."""
    root = Path(report["root"])
    cfg = config.load()
    # detection-owned keys start from the defaults: `init --force` onto a
    # library must never inherit the PREVIOUS library's picks (stale
    # manifest_roots / kitbash_root survived a switch); hand-set keys
    # (network, dashboard, indexer_skip_dirs, thumb dirs, ...) are kept.
    # Re-initialising the SAME library keeps manifest/kit roots the user set
    # OUTSIDE it (converted packs on another drive can't be re-detected)
    from .db import path_under
    old_lib = cfg.get("library_root") or ""
    same = bool(old_lib) and path_under(old_lib, root) and path_under(root, old_lib)
    old_roots = cfg.get("manifest_roots")
    keep_roots = [r for r in (old_roots if isinstance(old_roots, list) else [])
                  if same and not path_under(r, root)]
    old_kit = cfg.get("kitbash_root") or ""
    keep_kit = old_kit if same and old_kit and not path_under(old_kit, root) else ""
    cfg["sections"] = dict(config._DEFAULTS["sections"])
    cfg["manifest_roots"] = []
    for key in ("scan_folders", "kitbash_root", "collection_csv"):
        cfg.pop(key, None)
    cfg["library_root"] = str(root)
    cfg["registry_dir"] = str(registry_dir)
    cfg["previews_dir"] = str(registry_dir / "previews")
    counts_by_folder = report.get("folder_counts", {})
    # the folder carrying the catalog CSV is the COLLECTION -- it must
    # never win a content-section pick (alphabetical order once let a
    # 2-screenshot CSV folder become the textures section)
    csv_folders = {Path(c).parent.name for c in report["catalog_csvs"]}
    for kind, key in (("animation", "animation"), ("audio", "audio"),
                      ("textures", "textures")):
        pick = _pick(report["sections"], kind, counts_by_folder,
                     exclude=csv_folders)
        if pick:
            cfg["sections"][key] = pick
    # scanned model folders become a CONFIG KEY: ingest must drive the
    # scanner from the config (the contract), not from re-detection
    cfg["scan_folders"] = [f for f in report["mesh_folders"]
                           if f not in csv_folders]
    # textures_main: keep the default relative name; harmless if absent
    coll_csv = report["catalog_csvs"][0] if report["catalog_csvs"] else None
    if coll_csv:
        cfg["sections"]["collection"] = Path(coll_csv).parent.name
        cfg["sections"]["collected_galleries"] = Path(coll_csv).parent.name
    # CSV filename: config.CSV_PATH reads `collection_csv` and defaults to
    # "3D_Assets_Overview.csv" (config.py:123). Record the detected name only
    # when it DIFFERS from that default -- so a config whose CSV carries the
    # default name has no `collection_csv` key at all and depends on the code
    # default. The literal here must therefore keep matching config.py:124;
    # changing one without the other silently unpairs them.
    if coll_csv and Path(coll_csv).name != "3D_Assets_Overview.csv":
        cfg["collection_csv"] = Path(coll_csv).name
    cfg["manifest_roots"] = keep_roots + list(report["manifest_roots"])
    if report.get("kitbash_root") or keep_kit:
        cfg["kitbash_root"] = report.get("kitbash_root") or keep_kit
    return cfg


def run_init(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    parser = argparse.ArgumentParser(
        prog="pharos init",
        description="detect asset folders under ROOT and write pharos_config.json")
    parser.add_argument("root", nargs="?", default=".",
                        help="folder to scan (default: current directory)")
    parser.add_argument("--registry-dir", default=None,
                        help="where the SQLite registry lives "
                             "(default: ~/.pharos/registry)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing pharos_config.json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    registry_dir = (Path(args.registry_dir).resolve()
                    if args.registry_dir
                    else Path.home() / ".pharos" / "registry")
    report = detect(root)

    print(f"scanning: {root}")
    if "error" in report:
        print(f"  ERROR: {report['error']}")
        return 1
    py = sys.executable
    # print the picks the config will really get (weighted, CSV folder
    # excluded) -- not detect()'s alphabetical order
    chosen = build_config(report, registry_dir)["sections"]
    for kind in ("animation", "textures", "audio"):
        picks = report["sections"].get(kind) or []
        pick = chosen.get(kind) if picks else ""
        print(f"  {kind:<10} -> {pick or '(not found)'}"
              + (f"   [also: {', '.join(x for x in picks if x != pick)}]"
                 if len(picks) > 1 else ""))
    csvs = report["catalog_csvs"]
    print(f"  collection -> "
          + (f"{Path(csvs[0]).parent.name} (csv: {Path(csvs[0]).name})"
             if csvs else "(no catalog CSV found)"))
    print(f"  manifest packs: "
          + (f"{len(report.get('manifest_packs_found', []))} found"
              + (f" under {root}" if report["manifest_roots"] else "")
              if report["manifest_roots"] else "(none)"))
    print(f"  agent index (_Agent_Files): "
          + ("present" if report["agent_files"] else "absent"))
    if report.get("uasset_total"):
        print(f"  raw UE packs : {report['uasset_total']} .uasset/.umap files "
              f"in {', '.join(report.get('uasset_folders', []))} "
              f"(chain 1 candidates -- ASK before crawling)")
    if report["mesh_folders"]:
        print("  model folders (index via scanner, not a section): "
              + ", ".join(report["mesh_folders"]))
    if report.get("blend_folders"):
        print("  blend folders (decision needed: enumerate containers, "
              "export kits, or leave as-is): "
              + ", ".join(report["blend_folders"]))
    if report.get("root_files"):
        n = sum(report["root_files"].values())
        print(f"  !! {n} asset file(s) DIRECTLY in the library root -- "
              f"sections and the scanner only see SUBFOLDERS.")
        print("     move them into a subfolder or they will never be indexed")
    if report["unknown"]:
        print("  unclassified (mixed/other content): "
              + ", ".join(report["unknown"]))

    cfg_file = config._CONFIG_FILE
    if cfg_file.is_file() and not args.force:
        current_root = ""
        try:
            current_root = json.loads(
                cfg_file.read_text(encoding="utf-8-sig")).get("library_root", "")
        except (ValueError, OSError):
            pass
        if current_root and Path(current_root) != root:
            print(f"\nERROR: config already points at a DIFFERENT library:"
                  f"\n  config : {current_root}"
                  f"\n  asked  : {root}"
                  f"\nRe-run with --force to switch libraries. The registry "
                  f"is NOT rebuilt for the new root: give the new library "
                  f"its own with --registry-dir <folder>, or delete the "
                  f"old registry file assets.sqlite (derived data).")
            return 2
        print(f"\nconfig already exists: {cfg_file}"
              "\n(re-run with --force to overwrite, or edit it by hand)")
        return 0

    cfg = build_config(report, registry_dir)
    config.save(cfg)
    print(f"\nconfig written: {cfg_file}")
    # init only writes the config; a registry another library left in that
    # folder would make ingest/serve refuse -- say so NOW
    from . import db
    db_file = registry_dir / "assets.sqlite"
    owner = db.registry_foreign(db_file, root)
    if owner:
        print("\n" + db.foreign_registry_message(
            db_file, owner, root, lead="WARNING (ingest and serve will stop)"))

    # ---- INGESTION SUMMARY (the user must never wonder "did it work?") --
    print("\n" + "-" * 24 + " INGESTION SUMMARY " + "-" * 24)
    picks = report["sections"]
    print(f"  sections      : " + " | ".join(
        f"{k}={cfg['sections'].get(k) if picks.get(k) else '-'}"
        for k in ("animation", "textures", "audio")))
    csvs = report["catalog_csvs"]
    print(f"  collection csv: {csvs[0] if csvs else '(none found)'}")
    if report["manifest_roots"]:
        print(f"  manifest packs: {len(report.get('manifest_packs_found', []))}"
              f" under {', '.join(report['manifest_roots'])}"
              f" -> material recipes will import")
    covered = set()
    for kind in ("animation", "textures", "audio"):
        covered.update(picks.get(kind) or [])
    if report["catalog_csvs"]:
        covered.add(Path(report["catalog_csvs"][0]).parent.name)
    covered.update(Path(x).name for x in report["manifest_roots"])
    for f in report["mesh_folders"]:
        covered.add(f)
        print(f"  to scan       : {f}   ->  python "
              f"\"{_REPO / 'service' / 'asset_service' / 'scanner.py'}\" \"{root / f}\"")
    not_indexed = [u for u in report["unknown"] if u not in covered]
    if not_indexed:
        print(f"  NOT INDEXED (matched no section): {', '.join(not_indexed)}")
        print("    -> point a section at them in pharos_config.json, "
              "or scan them with scanner.py")
    if picks.get("animation"):
        print(f"  animation     : run indexer.py to index the clip packs ->"
              f" \"{sys.executable}\" \"{_REPO / 'service' / 'asset_service' / 'indexer.py'}\""
              f" --root \"{root}\"")
    if picks.get("audio"):
        ap = root / picks["audio"][0]
        print(f"  audio         : scanner.py indexes durations + folder"
              f" categories -> \"{sys.executable}\" \"{_REPO / 'service' / 'asset_service' / 'scanner.py'}\""
              f" \"{ap}\"")
    print("-" * 67)

    # questions the RUNNING AGENT must relay to its user before building
    print("\nASK YOUR USER now (setup is not complete until you did):")
    print("  1. Are these ALL your asset folders? (other drives? external?)")
    print("  2. Crawl the Unreal packs for material recipes? "
          "(needs UE 5.x; see pipeline/README.md chain 1)")
    print("  3. Do you have a purchase CSV for the Assets section "
          "(Name/URL/Price columns)?")
    print("  4. Animation previews render once in a browser tab -- OK?")

    print("\nnext steps:")
    py = sys.executable
    print(f'  check setup     : "{py}" "{_REPO / "pharos.py"}" doctor')
    print(f'  auto-run chains : "{py}" "{_REPO / "pharos.py"}" ingest  (relays the '
          "decision questions to your user)")
    print(f'  start the server: "{py}" "{_REPO / "pharos.py"}" serve'
          "   (or python service/asset_service/browse.py)")
    print("  agent setup flow: docs/STARTING_PROMPT.md + "
          "docs/AGENT_SETUP_BRIEF.md")
    if report["mesh_folders"]:
        print(f'  index model folders: "{py}" '
              f"\"{_REPO / 'service' / 'asset_service' / 'scanner.py'}\" "
              f'"{root / report["mesh_folders"][0]}"')
    return 0
