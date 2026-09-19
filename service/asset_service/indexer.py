"""Non-destructive crawler + registry filler.

Walks the canonical asset root, groups files into LOGICAL PACKS, probes
binary headers (.uasset classes, FBX animation markers), runs the 3-tier
auto-classifier and upserts everything into the SQLite registry.

Pack rule (v1): a top-level folder that directly contains asset files is one
pack; a top-level folder with no direct asset files splits into one pack per
child folder that contains asset files anywhere below it. This matches
marketplace layouts (a converted-pack section -> one pack per pack folder,
Animation -> Actor / daily-activities) without ever touching the disk layout.

Safety: files are opened 'rb' only; the only writes are to the registry
database under the pipeline root. Re-runs are idempotent (upserts keyed by
pack path; content_hash from the hero file).

CLI:
    python indexer.py [--root <asset root>] [--db ...] [--limit N]
                      [--dry-run] [-v]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
# this must run BEFORE the first asset_service import: as a direct script
# (python service/asset_service/indexer.py --root <lib> -- the form the app's
# own INGESTION SUMMARY prints) the package is not on sys.path yet
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_service import config                                # noqa: E402
from asset_service import db                                    # noqa: E402
from asset_service.auto_classifier import (                     # noqa: E402
    PackInput, classify_pack, get_vision_classifier, probe_fbx_markers,
)
from asset_service.uasset_parser import UassetParseError, parse_uasset  # noqa: E402

FILE_TYPES: dict[str, str] = {}
for ext in (".fbx", ".obj", ".glb", ".gltf", ".ply", ".stl", ".3ds", ".dae",
            ".abc", ".usd", ".usda", ".usdc", ".usdz", ".max", ".ma", ".mb"):
    FILE_TYPES[ext] = "mesh"
for ext in (".bvh", ".c3d", ".trc", ".anim"):
    FILE_TYPES[ext] = "anim"
for ext in (".uasset", ".umap", ".uexp", ".ubulk", ".upk"):
    FILE_TYPES[ext] = "uasset"
FILE_TYPES[".blend"] = "blend"
FILE_TYPES[".hdr"] = "hdri"
for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".dds",
            ".psd", ".exr", ".webp"):
    FILE_TYPES[ext] = "texture"

ASSET_TYPES = {"mesh", "anim", "uasset", "blend", "hdri", "texture"}

HERO_EXT_PRIORITY = (".blend", ".fbx", ".uasset", ".obj", ".gltf", ".glb",
                     ".bvh", ".hdr", ".exr")
HERO_UASSET_PREFIXES = ("SM_", "SK_", "AS_", "LS_", "BP_", "M_")
SIDEcar_EXTS = {".json", ".txt", ".md"}
SKIP_DIRS = ({"$RECYCLE.BIN", "System Volume Information"}
             | set(config.INDEXER_SKIP_DIRS))

FBX_PROBE_BYTES = 16 * 1024 * 1024
HASH_CHUNK = 4 * 1024 * 1024
HASH_FULL_LIMIT = 512 * 1024 * 1024   # above this: partial hash + size salt


def sha256_file(path: Path) -> tuple[str, bool]:
    """(hash, was_partial). Full hash; >512 MiB files get a 64 MiB prefix hash."""
    h = hashlib.sha256()
    partial = path.stat().st_size > HASH_FULL_LIMIT
    with path.open("rb") as fh:
        if partial:
            h.update(fh.read(64 * 1024 * 1024))
        else:
            for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
                h.update(chunk)
    if partial:
        h.update(str(path.stat().st_size).encode())
    return h.hexdigest(), partial


def _file_entry(path: Path, root: Path) -> Optional[dict]:
    ext = path.suffix.lower()
    ftype = FILE_TYPES.get(ext)
    if ftype is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    rel = path.relative_to(root).as_posix()
    # handover 2026-09-15: Exports folders hold DERIVED FBX/PNG output of
    # the UE->FBX conversion pipeline -- real files, but a separate kind so
    # source counts and derived counts never blur
    if "/Exports/" in "/" + rel:
        ftype = "export_" + ftype
    return {"relpath": rel, "ext": ext,
            "size": size, "file_type": ftype}


def _skippable(path: Path) -> bool:
    # host apps may write thumbnail caches into added sources; that is
    # their own data -- never indexed as a pack, never touched.
    return path.name in SKIP_DIRS or path.name.startswith(".")


def discover_packs(root: Path) -> list[tuple[str, list[dict]]]:
    """Return [(pack_rel_path, [file entries...]), ...] using the v1 pack rule."""
    packs: list[tuple[str, list[dict]]] = []
    top_dirs = sorted(p for p in root.iterdir()
                      if p.is_dir() and not _skippable(p))
    for top in top_dirs:
        top_files: list[dict] = []
        child_subtrees: list[tuple[str, list[dict]]] = []
        for child in sorted(top.iterdir()):
            if not child.is_dir() or _skippable(child):
                continue
            subtree = [e for e in (_file_entry(p, root)
                                  for p in child.rglob("*")
                                  if p.is_file()) if e]
            if any(e["file_type"] in ASSET_TYPES for e in subtree):
                child_subtrees.append((child.relative_to(root).as_posix(), subtree))
        # direct files in the top folder itself
        for p in top.iterdir():
            if p.is_file():
                e = _file_entry(p, root)
                if e:
                    top_files.append(e)
        if any(e["file_type"] in ASSET_TYPES for e in top_files):
            # top folder IS the pack (its subtree belongs to it)
            everything = top_files + [e for _, sub in child_subtrees for e in sub]
            packs.append((top.relative_to(root).as_posix(), everything))
        else:
            packs.extend(child_subtrees)
    return packs


def pick_hero(files: list[dict]) -> Optional[dict]:
    asset_files = [f for f in files if f["file_type"] in ASSET_TYPES]
    if not asset_files:
        return None
    for ext in HERO_EXT_PRIORITY:
        candidates = [f for f in asset_files if f["ext"] == ext]
        if candidates:
            if ext == ".uasset":
                for prefix in HERO_UASSET_PREFIXES:
                    for f in candidates:
                        if Path(f["relpath"]).name.startswith(prefix):
                            return f
            return max(candidates, key=lambda f: f["size"])
    return max(asset_files, key=lambda f: f["size"])


def prettify_name(folder_name: str) -> str:
    """'MansionExample_5.0' -> 'Mansion Example 5.0' (camelCase-aware) so FTS
    tokens match natural-language searches."""
    import re
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", folder_name)
    spaced = spaced.replace("_", " ").replace("-", " ")
    return " ".join(spaced.split()).strip()


def build_pack_input(pack_path: str, files: list[dict], root: Path) -> PackInput:
    name = prettify_name(Path(pack_path).name)
    pack = PackInput(name=name, rel_path=pack_path, files=files)

    # Layer 2: uasset class probes (up to 3, mesh/anim-prefixed first)
    uassets = [f for f in files if f["ext"] in (".uasset", ".umap")]
    ranked = sorted(
        uassets,
        key=lambda f: next((i for i, p in enumerate(HERO_UASSET_PREFIXES)
                            if Path(f["relpath"]).name.startswith(p)), 99))
    pack.engine_hint = None  # type: ignore[attr-defined]
    engine_versions: set[str] = set()
    for f in ranked[:3]:
        try:
            s = parse_uasset(root / f["relpath"])
            if s.asset_class:
                pack.uasset_classes.append(s.asset_class)
            if s.engine_version:
                engine_versions.add(s.engine_version)
        except (UassetParseError, OSError):
            pass
    pack.sidecar_text = ""
    sidecars = sorted((f for f in files
                       if Path(f["relpath"]).suffix.lower() in SIDEcar_EXTS),
                      key=lambda f: f["size"])[:3]
    for f in sidecars:
        try:
            with (root / f["relpath"]).open("rb") as fh:
                pack.sidecar_text += fh.read(4096).decode("utf-8", "replace") + "\n"
        except OSError:
            pass

    # Layer 2: FBX marker probes (up to 3 smallest files)
    fbxs = sorted((f for f in files if f["ext"] == ".fbx"),
                  key=lambda f: f["size"])[:3]
    for f in fbxs:
        try:
            with (root / f["relpath"]).open("rb") as fh:
                head = fh.read(64)
                blob = head + fh.read(FBX_PROBE_BYTES - 64)
            pack.fbx_signals.append(probe_fbx_markers(blob))
        except OSError:
            pass
    pack.engine_versions = sorted(engine_versions)  # type: ignore[attr-defined]
    return pack


def index_root(root: Path, db_path: Path, limit: int = 0, dry_run: bool = False,
               verbose: bool = False, config: Optional[dict] = None,
               subtree: Optional[str] = None) -> dict:
    t0 = time.perf_counter()
    packs = discover_packs(root)
    if subtree:
        # accept "Animation/daily-activities" or an absolute path under root
        sub = Path(subtree)
        if sub.is_absolute():
            try:
                subtree = sub.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                print(f"error: subtree {subtree} is not under {root}", file=sys.stderr)
                return {}
        packs = [p for p in packs
                 if p[0] == subtree or p[0].startswith(subtree.rstrip("/") + "/")]
        if not packs:
            print(f"no packs found under {subtree}")
            return {"packs": 0, "subtree": subtree}
    if limit:
        packs = packs[:limit]

    vision = get_vision_classifier(config or {})
    conn = db.init_db(db_path) if not dry_run else None

    domain_dist: Counter = Counter()
    style_dist: Counter = Counter()
    vendor_dist: Counter = Counter()
    review_queue: list[str] = []
    indexed = 0

    print(f"indexing {len(packs)} packs under {root}\n")
    for pack_path, files in packs:
        pack = build_pack_input(pack_path, files, root)
        hero = pick_hero(files) or (files[0] if files else None)
        if hero is None:
            continue
        hero_abs = root / hero["relpath"]
        try:
            content_hash, hashed_partially = sha256_file(hero_abs)
        except OSError as exc:
            print(f"  SKIP {pack_path}: hero unreadable ({exc})")
            continue

        preview = None  # previews arrive with host-app thumbnails
        result = classify_pack(pack, vision=vision, preview_path=preview)

        license_files = [f for f in files
                         if any(k in Path(f["relpath"]).name.lower()
                                for k in ("license", "eula"))]
        by_type = Counter(f["file_type"] for f in files)
        technical_meta = {
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "by_type": dict(by_type),
            "engine_versions": getattr(pack, "engine_versions", []),
            "hashed_partially": hashed_partially,
        }

        domain_dist[result.domain] += 1
        style_dist[result.style] += 1
        if result.vendor:
            vendor_dist[result.vendor] += 1
        if result.validation_status == "needs_review":
            review_queue.append(pack_path)

        flag = "REVIEW" if result.validation_status == "needs_review" else "ok"
        print(f"  [{flag}] {pack_path}")
        print(f"         {result.domain} / {result.sub_category} / "
              f"{result.style} / conf={result.confidence:.2f}"
              f"{' / ' + result.vendor if result.vendor else ''}")

        if not dry_run and conn is not None:
            db.upsert_asset(conn, {
                "id": f"pack::{pack_path}",
                "name": pack.name,
                "canonical_root": str(root).replace("\\", "/"),
                "hero_file_path": str(hero_abs).replace("\\", "/"),
                "vendor": result.vendor,
                "license_path": (str(root / license_files[0]["relpath"]).replace("\\", "/")
                                 if license_files else None),
                "license_status": "found" if license_files else None,
                "style": result.style,
                "domain": result.domain,
                "sub_category": result.sub_category,
                "confidence_score": result.confidence,
                "validation_status": result.validation_status,
                "formats": result.formats,
                "targets": result.targets,
                "technical_meta": technical_meta,
                "host_category": result.host_category,
                "host_tags": result.host_tags,
                "content_hash": content_hash,
            })
            db.replace_asset_files(
                conn, f"pack::{pack_path}",
                [{"relative_path": f["relpath"], "file_type": f["file_type"],
                  "file_size": f["size"],
                  "sha256": content_hash if f is hero else None}
                 for f in files])
        if verbose:
            print(f"         evidence: {json.dumps(result.evidence)[:300]}")
        indexed += 1

    elapsed = time.perf_counter() - t0
    print(f"\n{indexed} packs classified in {elapsed:.1f}s")
    # handover 2026-09-15 §7.1: drop registry rows for packs whose folder
    # no longer exists (e.g. a pack the owner deleted from the library)
    if not dry_run and conn is not None:
        # guard: pruning is per-file Path.exists(); if the library root
        # itself is unreachable (unmounted NAS / removed drive) EVERY row
        # looks stale and one pass would delete the entire registry
        if not root.is_dir():
            print(f"  WARNING: library root unreachable ({root}) -- "
                  "pruning SKIPPED so the registry is not wiped; "
                  "re-run when the drive is mounted", flush=True)
            stale = []
        else:
            stale = [r["id"] for r in conn.execute(
                "SELECT id, hero_file_path FROM assets")
                if not Path(r["hero_file_path"]).exists()]
        for pack_id in stale:
            conn.execute("DELETE FROM assets WHERE id = ?", (pack_id,))
        if stale:
            conn.commit()
            print(f"  removed {len(stale)} stale packs (folder gone):")
            for pack_id in stale[:10]:
                print(f"    - {pack_id}")

    print(f"  domains: {dict(domain_dist.most_common())}")
    print(f"  styles:  {dict(style_dist.most_common())}")
    print(f"  vendors: {dict(vendor_dist.most_common())}")
    if review_queue:
        print(f"  needs review ({len(review_queue)}):")
        for p in review_queue[:10]:
            print(f"    - {p}")
    if conn is not None:
        conn.close()
    return {"packs": indexed, "domains": dict(domain_dist),
            "styles": dict(style_dist), "vendors": dict(vendor_dist)}


def load_config() -> dict:
    """The indexer shares pharos_config.json with the rest of the app
    (the old config/library.yaml track is retired)."""
    try:
        from asset_service import config as _c
        return {
            "canonical_roots": ([str(_c.LIBRARY_ROOT)]
                                if _c.is_configured() else []),
            "registry_db": _c.DB_PATH,
        }
    except Exception:
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Index + classify the asset corpus")
    parser.add_argument("--root", default=(cfg.get("canonical_roots") or [""])[0])
    parser.add_argument("--db", default=cfg.get("registry_db", config.DB_PATH))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--subtree", default=None,
                        help="only index packs under this folder (relative to "
                             "root, or absolute); used for per-source syncs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.root:
        print("error: no --root given and pharos_config.json sets no "
              "library_root; run `python -m asset_service init` first or "
              "pass --root <folder>", file=sys.stderr)
        return 2
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: root {root} does not exist", file=sys.stderr)
        return 2
    index_root(root, Path(args.db), args.limit, args.dry_run, args.verbose, cfg,
               subtree=args.subtree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
