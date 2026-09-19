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
import sys
from pathlib import Path

from . import config

CLIP_EXTS = {".fbx", ".bvh"}
MESH_EXTS = {".obj", ".glb", ".gltf", ".stl", ".blend", ".usd", ".usda",
             ".usdc", ".usdz", ".uasset", ".umodel", ".max"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aif", ".aiff",
              ".aac", ".wma"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp",
              ".exr", ".hdr", ".dds", ".tga"}
SAMPLE_LIMIT = 2000          # files censused per folder; classification
                             # does not need the whole tree


def _census(folder: Path) -> dict[str, int]:
    """Bounded extension census of a folder tree."""
    counts: dict[str, int] = {}
    n = 0
    try:
        for p in folder.rglob("*"):
            if n >= SAMPLE_LIMIT:
                break
            if p.is_file():
                ext = p.suffix.lower()
                counts[ext] = counts.get(ext, 0) + 1
                n += 1
    except OSError:
        pass
    return counts


def _dominant(counts: dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    clips = sum(counts.get(e, 0) for e in CLIP_EXTS)
    audio = sum(counts.get(e, 0) for e in AUDIO_EXTS)
    images = sum(counts.get(e, 0) for e in IMAGE_EXTS)
    meshes = sum(counts.get(e, 0) for e in MESH_EXTS)
    # model files present -> model folder, regardless of image counts:
    # 3D packs ship 4-8 texture maps per mesh, so images ALWAYS outnumber
    # meshes and a dominance rule misfiles every model pack as "textures"
    # (laptop-run finding). Animation clips keep priority for fbx/bvh.
    if meshes > 0 and meshes >= clips:
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
                    "unknown": []}
    if not root.is_dir():
        report["error"] = f"root does not exist: {root}"
        return report

    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        counts = _census(child)
        kind = _dominant(counts)
        if counts.get(".blend"):
            # .blend files need an explicit decision (enumerate containers,
            # export kits, or leave them alone) -- surface the candidates
            report.setdefault("blend_folders", []).append(child.name)
        if kind == "meshes":
            report["mesh_folders"].append(child.name)
        elif kind:
            report["sections"].setdefault(kind, []).append(child.name)
        else:
            # a folder may still be a manifest root even when its census is
            # mixed (converted packs are .uasset + .fbx + .png)
            report["unknown"].append(child.name)

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
            if is_kit:
                kit_roots.add(p.parent.parent.parent)
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

    report["catalog_csvs"] = [str(p) for p in _find_catalog_csvs(root)]
    return report


def _pick(sections: dict, kind: str) -> str | None:
    cands = sections.get(kind) or []
    return cands[0] if cands else None


def build_config(report: dict, registry_dir: Path) -> dict:
    """Turn a detect() report into a pharos_config.json payload."""
    root = Path(report["root"])
    cfg = config.load()
    cfg["library_root"] = str(root)
    cfg["registry_dir"] = str(registry_dir)
    cfg["previews_dir"] = str(registry_dir / "previews")
    for kind, key in (("animation", "animation"), ("audio", "audio"),
                      ("textures", "textures")):
        pick = _pick(report["sections"], kind)
        if pick:
            cfg["sections"][key] = pick
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
    if report["manifest_roots"]:
        cfg["manifest_roots"] = list(report["manifest_roots"])
    if report.get("kitbash_root"):
        cfg["kitbash_root"] = report["kitbash_root"]
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
    for kind in ("animation", "textures", "audio"):
        picks = report["sections"].get(kind) or []
        print(f"  {kind:<10} -> {picks[0] if picks else '(not found)'}"
              + (f"   [also: {', '.join(picks[1:])}]" if len(picks) > 1 else ""))
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
    if report["mesh_folders"]:
        print("  model folders (index via scanner, not a section): "
              + ", ".join(report["mesh_folders"]))
    if report.get("blend_folders"):
        print("  blend folders (decision needed: enumerate containers, "
              "export kits, or leave as-is): "
              + ", ".join(report["blend_folders"]))
    if report["unknown"]:
        print("  unclassified (mixed/other content): "
              + ", ".join(report["unknown"]))

    cfg_file = config._CONFIG_FILE
    if cfg_file.is_file() and not args.force:
        print(f"\nconfig already exists: {cfg_file}"
              "\n(re-run with --force to overwrite, or edit it by hand)")
        return 0

    cfg = build_config(report, registry_dir)
    config.save(cfg)
    print(f"\nconfig written: {cfg_file}")

    # ---- INGESTION SUMMARY (the user must never wonder "did it work?") --
    print("\n" + "-" * 24 + " INGESTION SUMMARY " + "-" * 24)
    picks = report["sections"]
    print(f"  sections      : animation={picks.get('animation', ['-'])[0]}"
          f" | textures={picks.get('textures', ['-'])[0]}"
          f" | audio={picks.get('audio', ['-'])[0]}")
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
              f"service/asset_service/scanner.py \"{root / f}\"")
    not_indexed = [u for u in report["unknown"] if u not in covered]
    if not_indexed:
        print(f"  NOT INDEXED (matched no section): {', '.join(not_indexed)}")
        print("    -> point a section at them in pharos_config.json, "
              "or scan them with scanner.py")
    if picks.get("animation"):
        print(f"  animation     : run indexer.py to index the clip packs ->"
              f" python service/asset_service/indexer.py"
              f" --root \"{root}\"")
    if picks.get("audio"):
        ap = root / picks["audio"][0]
        print(f"  audio         : scanner.py indexes durations + folder"
              f" categories -> python service/asset_service/scanner.py"
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
    print("  check setup     : python pharos.py doctor")
    print("  auto-run chains : python pharos.py ingest  (relays the "
          "decision questions to your user)")
    print("  start the server: python pharos.py serve"
          "   (or python service/asset_service/browse.py)")
    print("  agent setup flow: docs/STARTING_PROMPT.md + "
          "docs/AGENT_SETUP_BRIEF.md")
    if report["mesh_folders"]:
        print("  index model folders: python service/asset_service/scanner.py "
              f"\"{root / report['mesh_folders'][0]}\"")
    return 0
