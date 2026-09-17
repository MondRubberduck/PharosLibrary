#!/usr/bin/env python3
"""Non-destructive corpus audit for the canonical asset root.

Walks the asset tree WITHOUT modifying anything:
  - classifies every file by extension into coarse buckets
      (mesh / animation / texture / hdri / unreal native / blend native /
       archive / audio / video / metadata / other)
  - attributes files+bytes to top-level library folders
  - detects vendor signatures in paths (Quixel/Megascans, Fab, CGTrader,
    TurboSquid, Reallusion, Mixamo, ...)
  - counts crude taxonomy signal tokens (anim/walk/run/idle/mocap/...)
  - parses a deterministic sample of .uasset/.umap headers with the local
    parser and reports the AssetClass / engine-version distribution
  - records license-named files

Output: reports/corpus_stats.json plus a console summary.
Files are opened 'rb' only, and only the sampled headers. Nothing is written
inside the asset root.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from asset_service.uasset_parser import UassetParseError, parse_uasset  # noqa: E402

DEFAULT_ROOT = ""   # pass --root or set library_root
DEFAULT_OUT = REPO / "reports" / "corpus_stats.json"

BUCKETS: dict[str, set[str]] = {
    "mesh": {".fbx", ".obj", ".glb", ".gltf", ".ply", ".stl", ".3ds", ".dae",
             ".abc", ".usd", ".usda", ".usdc", ".usdz", ".max", ".ma", ".mb"},
    "animation": {".bvh", ".c3d", ".trc", ".anim"},
    "blend_native": {".blend", ".blend1"},
    "unreal_native": {".uasset", ".umap", ".uexp", ".ubulk", ".upk", ".pak", ".unitypackage"},
    "texture": {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".dds",
                ".psd", ".exr", ".webp", ".jp2"},
    "hdri": {".hdr"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "audio": {".wav", ".mp3", ".ogg", ".flac", ".aac"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "metadata": {".json", ".xml", ".yaml", ".yml", ".md", ".txt"},
    "other": set(),
}

EXT_TO_BUCKET = {ext: name for name, exts in BUCKETS.items() for ext in exts}

VENDOR_TOKENS = {
    "quixel": "Quixel", "megascans": "Quixel", "bridge": "Quixel",
    "fab": "Fab", "epicgames": "Epic", "cgtrader": "CGTrader",
    "turbosquid": "TurboSquid", "reallusion": "Reallusion", "actorcore": "Reallusion",
    "mixamo": "Mixamo", "adobe": "Adobe", "substance": "Adobe",
    "sketchfab": "Sketchfab", "artstation": "ArtStation", "gumroad": "Gumroad",
    "kitbash3d": "KitBash3D", "polyhaven": "PolyHaven", "ambientcg": "ambientCG",
}
# "bridge"/"fab" are generic words -- only count them as path segments to
# avoid flagging every "fabric" or "bridge" model
SEGMENT_ONLY_TOKENS = {"bridge", "fab"}

SIGNAL_TOKENS = (
    "anim", "walk", "run", "idle", "mocap", "bvh", "rig", "skeleton",
    "blendspace", "rootmotion", "cmu", "mixamo", "handpainted", "lowpoly",
    "stylized", "toon", "archviz", "interior", "kitchen", "facade",
)

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "$Windows.~WS"}
MAX_ERRORS = 200
UASSET_LIST_CAP = 50_000


def bucket_for(ext: str) -> str:
    return EXT_TO_BUCKET.get(ext, "other")


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:,.1f} TiB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Non-destructive corpus audit")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--uasset-sample", type=int, default=60,
                        help="how many .uasset/.umap headers to parse (0 = none)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: root {root} does not exist", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    ext_counter: Counter[str] = Counter()
    ext_bytes: Counter[str] = Counter()
    bucket_files: Counter[str] = Counter()
    bucket_bytes: Counter[str] = Counter()
    topdir_files: Counter[str] = Counter()
    topdir_bytes: Counter[str] = Counter()
    vendor_files: Counter[str] = Counter()
    signal_files: Counter[str] = Counter()
    license_named: list[str] = []
    uasset_paths: list[str] = []
    total_files = total_dirs = 0
    total_bytes = 0
    errors: list[dict] = []
    progress_every = 10_000

    import os

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        top_dir = "." if rel_dir == "." else rel_dir.split(os.sep)[0]
        total_dirs += 1

        for filename in filenames:
            total_files += 1
            if total_files % progress_every == 0 and not args.quiet:
                print(f"  ... {total_files:,} files", flush=True)
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root)
            rel_norm = rel.replace("\\", "/")
            lower = (rel_norm).lower()
            ext = Path(filename).suffix.lower()

            try:
                size = os.stat(full).st_size
            except OSError as exc:
                if len(errors) < MAX_ERRORS:
                    errors.append({"path": rel_norm, "error": str(exc)})
                size = 0
            total_bytes += size

            ext_counter[ext or "<none>"] += 1
            ext_bytes[ext or "<none>"] += size
            bucket = bucket_for(ext)
            bucket_files[bucket] += 1
            bucket_bytes[bucket] += size
            topdir_files[top_dir] += 1
            topdir_bytes[top_dir] += size

            segments = set(lower.replace("/", " ").split())
            seg_tokens = {s.strip() for s in lower.split("/")}
            for token, vendor in VENDOR_TOKENS.items():
                if token in SEGMENT_ONLY_TOKENS:
                    if token in seg_tokens or any(p == token for p in seg_tokens):
                        vendor_files[vendor] += 1
                elif token in lower:
                    vendor_files[vendor] += 1
            for token in SIGNAL_TOKENS:
                if token in segments or token in lower.replace("_", " ").split():
                    signal_files[token] += 1

            if any(k in filename.lower() for k in ("license", "eula", "readme")):
                if len(license_named) < 500:
                    license_named.append(rel_norm)
            if ext in (".uasset", ".umap") and len(uasset_paths) < UASSET_LIST_CAP:
                uasset_paths.append(full)

    duration = time.perf_counter() - t0

    # deterministic stride sample of uasset headers
    uasset_stats: dict = {
        "discovered": len(uasset_paths), "sampled": 0, "parsed_ok": 0,
        "class_distribution": {}, "engine_versions": {}, "failures": {},
        "failure_samples": [], "warning_counts": 0,
    }
    sample_n = min(args.uasset_sample, len(uasset_paths))
    for i in range(sample_n):
        path = uasset_paths[i * len(uasset_paths) // sample_n]
        try:
            s = parse_uasset(path)
            uasset_stats["parsed_ok"] += 1
            uasset_stats["class_distribution"][s.asset_class or "<unresolved>"] = \
                uasset_stats["class_distribution"].get(s.asset_class or "<unresolved>", 0) + 1
            uasset_stats["engine_versions"][s.engine_version or "<unknown>"] = \
                uasset_stats["engine_versions"].get(s.engine_version or "<unknown>", 0) + 1
            uasset_stats["warning_counts"] += len(s.warnings)
        except (UassetParseError, OSError) as exc:
            reason = str(exc)[:120]
            uasset_stats["failures"][reason] = uasset_stats["failures"].get(reason, 0) + 1
            if len(uasset_stats["failure_samples"]) < 5:
                uasset_stats["failure_samples"].append(
                    {"path": str(Path(path).relative_to(root)).replace("\\", "/"),
                     "error": reason})
        uasset_stats["sampled"] += 1

    vendors = {v: c for v, c in vendor_files.items() if c > 0}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root).replace("\\", "/"),
        "duration_seconds": round(duration, 2),
        "totals": {
            "files": total_files,
            "directories": total_dirs,
            "bytes": total_bytes,
            "bytes_human": human_bytes(total_bytes),
            "distinct_extensions": len(ext_counter),
        },
        "by_bucket": {
            b: {"files": bucket_files.get(b, 0), "bytes": bucket_bytes.get(b, 0)}
            for b in sorted(BUCKETS)
        },
        "by_extension": [
            {"ext": ext, "count": ext_counter[ext], "bytes": ext_bytes[ext],
             "bucket": bucket_for(ext if ext != "<none>" else "")}
            for ext, _ in ext_counter.most_common()
        ],
        "by_top_level_dir": [
            {"dir": d, "files": topdir_files[d], "bytes": topdir_bytes[d],
             "share_of_files": round(topdir_files[d] / max(total_files, 1), 4)}
            for d, _ in topdir_files.most_common(40)
        ],
        "vendor_signatures": dict(sorted(vendors.items(), key=lambda kv: -kv[1])),
        "taxonomy_signal_tokens": dict(signal_files.most_common()),
        "license_named_files": {"count": len(license_named), "sample": license_named[:10]},
        "uasset_sample": uasset_stats,
        "errors": {"count": len(errors), "sample": errors[:20]},
        "notes": [
            "Read-only audit: no file under the root was created, modified, moved or deleted.",
            "Extension buckets are coarse; .fbx/.abc can be mesh OR animation "
            "(Sprint 3 classifier resolves this per file).",
            ".exr counted as texture; .hdr counted as hdri.",
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        t = report["totals"]
        print(f"\nCorpus audit: {t['files']:,} files / {t['directories']:,} dirs / "
              f"{t['bytes_human']} / {report['duration_seconds']}s")
        print("\nTop buckets:")
        for b in sorted(BUCKETS, key=lambda b: -bucket_bytes.get(b, 0)):
            if bucket_files.get(b):
                print(f"  {b:<14} {bucket_files[b]:>8,} files  {human_bytes(bucket_bytes[b]):>12}")
        print("\nTop extensions:")
        for row in report["by_extension"][:15]:
            print(f"  {row['ext']:<10} {row['count']:>8,}  {human_bytes(row['bytes']):>12}  [{row['bucket']}]")
        print("\nTop-level folders:")
        for row in report["by_top_level_dir"][:12]:
            print(f"  {row['dir']:<40} {row['files']:>8,} files  {human_bytes(row['bytes']):>12}")
        if vendors:
            print(f"\nVendor signatures: {vendors}")
        if uasset_stats["sampled"]:
            print(f"\nuasset sample: {uasset_stats['parsed_ok']}/{uasset_stats['sampled']} parsed "
                  f"(of {uasset_stats['discovered']} discovered)")
            print(f"  classes: {dict(sorted(uasset_stats['class_distribution'].items(), key=lambda kv: -kv[1]))}")
            print(f"  engines: {uasset_stats['engine_versions']}")
            if uasset_stats["failures"]:
                print(f"  failures: {uasset_stats['failures']}")
        if errors:
            print(f"\n{len(errors)} stat errors (see report)")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
