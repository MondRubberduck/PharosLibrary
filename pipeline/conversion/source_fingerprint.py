#!/usr/bin/env python3
"""
source_fingerprint.py -- prove the source packs were not touched.

Two modes:

  --root <dir> --out <json>
      Walk the tree, record (relpath, size, mtime_ns) for every asset-ish file
      (.uasset/.umap/.ubulk/.uexp), skipping `Exports`.  Writes the record list
      plus a sha256 over the canonical record list.  Cheap: metadata only.

  --root <dir> --sha256 <relpath_prefix> --out <json>
      Full content sha256 of every .uasset/.umap under the tree (optionally
      limited to a prefix).  Slow but byte-exact; use on one small pack.

  --compare <before.json> <after.json>
      Report added / removed / changed files and the overall verdict.

Usage:
  python source_fingerprint.py --root "C:/path/to/assets/..." --out before.json
  python source_fingerprint.py --compare before.json after.json
"""

import argparse
import hashlib
import json
import os
import sys

ASSET_EXT = (".uasset", ".umap", ".ubulk", ".uexp", ".uptnl")
SKIP_DIRS = {"exports"}


def walk_files(root, with_sha=False, prefix=None):
    out = []
    root = os.path.abspath(root)
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                ents = list(it)
        except OSError:
            continue
        for e in ents:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() in SKIP_DIRS:
                        continue
                    stack.append(e.path)
                    continue
                if not e.name.lower().endswith(ASSET_EXT):
                    continue
                rel = os.path.relpath(e.path, root).replace("\\", "/")
                if prefix and not rel.startswith(prefix):
                    continue
                st = e.stat()
                rec = {"p": rel, "s": st.st_size, "m": st.st_mtime_ns}
                if with_sha:
                    h = hashlib.sha256()
                    with open(e.path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    rec["h"] = h.hexdigest()
                out.append(rec)
            except OSError:
                continue
    out.sort(key=lambda r: r["p"])
    return out


def digest(records):
    h = hashlib.sha256()
    for r in records:
        h.update(("%s|%d|%d|%s\n" % (r["p"], r["s"], r["m"], r.get("h", ""))).encode("utf-8"))
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--out")
    ap.add_argument("--sha256", action="store_true",
                    help="also hash file contents (slow)")
    ap.add_argument("--prefix", default=None, help="only files whose relpath starts with this")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()

    if args.compare:
        a = json.load(open(args.compare[0], encoding="utf-8"))
        b = json.load(open(args.compare[1], encoding="utf-8"))
        am = {r["p"]: r for r in a["files"]}
        bm = {r["p"]: r for r in b["files"]}
        added = sorted(set(bm) - set(am))
        removed = sorted(set(am) - set(bm))
        changed = sorted(p for p in set(am) & set(bm) if am[p] != bm[p])
        same = len(set(am) & set(bm)) - len(changed)
        res = {
            "before": args.compare[0], "after": args.compare[1],
            "root": a.get("root"),
            "count_before": len(am), "count_after": len(bm),
            "unchanged": same, "added": added, "removed": removed, "changed": changed,
            "digest_before": a.get("digest"), "digest_after": b.get("digest"),
            "identical": (not added and not removed and not changed
                          and a.get("digest") == b.get("digest")),
        }
        json.dump(res, sys.stdout, indent=1)
        print()
        print("SOURCE UNCHANGED" if res["identical"] else "SOURCE CHANGED (!)", file=sys.stderr)
        return 0 if res["identical"] else 1

    if not args.root or not args.out:
        ap.error("--root and --out required (or use --compare)")

    recs = walk_files(args.root, with_sha=args.sha256, prefix=args.prefix)
    doc = {
        "root": os.path.abspath(args.root).replace("/", os.sep),
        "mode": "sha256" if args.sha256 else "metadata",
        "prefix": args.prefix,
        "count": len(recs),
        "total_bytes": sum(r["s"] for r in recs),
        "digest": digest(recs),
        "files": recs,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    print("%s: %d files, %d bytes, digest=%s"
          % (args.out, doc["count"], doc["total_bytes"], doc["digest"][:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
