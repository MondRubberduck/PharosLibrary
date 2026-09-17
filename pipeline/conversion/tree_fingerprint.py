#!/usr/bin/env python3
"""
tree_fingerprint.py -- prove a directory tree did not change.

Computes, for every file under a root (sorted by relative path):
  * size in bytes
  * SHA-256 of the contents
and folds the lot into one digest.  Two runs that agree on `tree_sha256` and
`files` mean not one byte under that root changed between them.

Built for the TASK 1 acceptance check: "FBX and Textures on disk must stay
BYTE-IDENTICAL" when relink_materials.py rewrites manifest.json.  Reading is the
only thing it does.

Usage:
  python tree_fingerprint.py <dir> [<dir> ...] [--json out.json] [--skip-hash]
                             [--threads N]

--skip-hash falls back to a size/metadata-only fingerprint, which is much faster
on multi-GB trees but cannot prove byte equality on its own.
Exit code 0 = fingerprint produced (it says nothing about change: compare two runs).
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def walk_files(root):
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError as exc:
                out.append((p, -1, "stat-error:%s" % exc))
                continue
            out.append((p, st.st_size, None))
    out.sort(key=lambda r: os.path.normcase(r[0]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--json", default=None)
    ap.add_argument("--skip-hash", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    result = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "trees": {}}
    for root in args.dirs:
        root = os.path.abspath(root)
        t0 = time.time()
        files = walk_files(root)
        entries = []
        digests = []
        bad = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {}
            for p, size, err in files:
                rel = os.path.relpath(p, root).replace("\\", "/")
                rec = {"rel": rel, "bytes": size}
                if not args.skip_hash and err is None:
                    futures[pool.submit(sha256_file, p)] = (rec, p)
                else:
                    if err:
                        rec["error"] = err
                        bad.append(rel)
                    entries.append(rec)
            for fut in concurrent.futures.as_completed(futures):
                rec, p = futures[fut]
                try:
                    rec["sha256"] = fut.result()
                except Exception as exc:
                    rec["error"] = "%s: %s" % (type(exc).__name__, exc)
                    bad.append(rec["rel"])
                entries.append(rec)
        entries.sort(key=lambda r: r["rel"])

        folded = hashlib.sha256()
        total_bytes = 0
        for r in entries:
            total_bytes += max(r.get("bytes") or 0, 0)
            folded.update(("%s|%s|%s\n" % (r["rel"], r.get("bytes"),
                                           r.get("sha256", "-"))).encode("utf-8"))
        result["trees"][root] = {
            "files": len(entries),
            "bytes": total_bytes,
            "hash_mode": "size-only" if args.skip_hash else "sha256",
            "tree_sha256": folded.hexdigest(),
            "unreadable": bad[:20],
            "unreadable_count": len(bad),
            "seconds": round(time.time() - t0, 1),
            "entries": entries,
        }
        t = result["trees"][root]
        print("%s\n  files=%d bytes=%d mode=%s\n  tree_sha256=%s  (%.1fs)"
              % (root, t["files"], t["bytes"], t["hash_mode"], t["tree_sha256"], t["seconds"]))
        if bad:
            print("  UNREADABLE: %d  %s" % (len(bad), bad[:5]))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
        print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
