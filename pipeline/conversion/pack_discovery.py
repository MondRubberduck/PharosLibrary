#!/usr/bin/env python3
"""
pack_discovery.py -- find a pack's real UE project layout on disk.

Answers, from the filesystem alone (no engine needed):
  * where is the pack's `Content` root?
  * which top-level folders directly under `Content` actually hold `.uasset` files?
    (this is the ONLY reliable way to learn the real `/Game/<Top>` path -- the
    folder name, the project name and the package folder all disagree in practice,
    e.g. `Oriantel Building/OriantelBuilding.uproject` really contains
    `Content\\OriantalBuilding`)
  * what is the project name, as a cross-check

Usage:
  python pack_discovery.py --pack <pack_dir>            -> JSON on stdout
  python pack_discovery.py --scan-all <assets_root>     -> JSON array on stdout
  python pack_discovery.py --pack <pack_dir> --verbose  -> human summary

Exit code is non-zero when a pack has zero .uasset files, so a wrong guess
fails loudly instead of producing a silently empty export.
"""

import argparse
import json
import os
import sys

ASSET_EXT = (".uasset", ".umap")

# Folders directly under Content that never hold exportable pack content.
# `__ExternalActors__` / `__ExternalObjects__` are World-Partition sidecars,
# `Collections` / `Developers` / `Splash` are editor bookkeeping.
NON_CONTENT_DIRS = {
    "collections",
    "developers",
    "splash",
    "__externalactors__",
    "__externalobjects__",
    "movies",
    "audio",
}


def rel(p, base):
    return os.path.relpath(p, base).replace("/", "\\")


def count_assets(root):
    """Recursive count of .uasset/.umap under root (never follows reparse points)."""
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.lower().endswith(ASSET_EXT):
                            n += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return n


def find_content_roots(pack_dir, max_depth=2):
    """Every directory named `Content` at or below pack_dir (depth <= max_depth)."""
    out = []
    pack_dir = os.path.abspath(pack_dir)

    def walk(d, lvl):
        try:
            with os.scandir(d) as it:
                ents = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for e in ents:
            low = e.name.lower()
            if low == "content":
                out.append(e.path)
            elif lvl < max_depth and low not in ("exports", "saved", "deriveddatacache",
                                                 "intermediate", "binaries"):
                walk(e.path, lvl + 1)

    walk(pack_dir, 0)
    return sorted(out)


def find_uproject(pack_dir, max_depth=2):
    pack_dir = os.path.abspath(pack_dir)
    hits = []

    def walk(d, lvl):
        try:
            with os.scandir(d) as it:
                ents = list(it)
        except OSError:
            return
        for e in ents:
            try:
                if e.is_file(follow_symlinks=False) and e.name.lower().endswith(".uproject"):
                    hits.append(e.path)
                elif e.is_dir(follow_symlinks=False) and lvl < max_depth:
                    if e.name.lower() not in ("exports", "saved", "deriveddatacache",
                                              "intermediate", "binaries", "content"):
                        walk(e.path, lvl + 1)
            except OSError:
                continue

    walk(pack_dir, 0)
    return sorted(hits)


def discover(pack_dir, verbose=False):
    pack_dir = os.path.abspath(pack_dir)
    rec = {
        "pack": os.path.basename(pack_dir.rstrip("\\/")),
        "pack_dir": pack_dir.replace("/", os.sep),
        "content_root": None,
        "content_root_rel": None,
        "content_candidates": [],
        "project_file": None,
        "project_name": None,
        "tops": [],
        "game_roots": [],
        "primary_game_root": None,
        "uasset_total": 0,
        "excluded_dirs": sorted(NON_CONTENT_DIRS),
        "notes": [],
        "ok": False,
    }

    if not os.path.isdir(pack_dir):
        rec["notes"].append("pack directory does not exist")
        return rec

    cands = find_content_roots(pack_dir)
    rec["content_candidates"] = [rel(c, pack_dir) for c in cands]
    if not cands:
        rec["notes"].append("no directory named 'Content' found at depth <= 2")
        return rec

    # pick the candidate holding the most assets
    counted = [(c, count_assets(c)) for c in cands]
    counted.sort(key=lambda kv: (-kv[1], len(kv[0])))
    content_root, total = counted[0]
    rec["content_root"] = content_root.replace("/", os.sep)
    rec["content_root_rel"] = rel(content_root, pack_dir)
    if len(counted) > 1:
        rec["notes"].append("multiple Content dirs found; chose the one with most assets: %s"
                            % json.dumps([{"rel": rel(c, pack_dir), "assets": n} for c, n in counted]))
    rec["uasset_total"] = total

    ups = find_uproject(pack_dir)
    if ups:
        rec["project_file"] = rel(ups[0], pack_dir)
        rec["project_name"] = os.path.splitext(os.path.basename(ups[0]))[0]
    else:
        rec["notes"].append("no .uproject found; project_name falls back to nothing")

    # ---- enumerate folders directly under Content that actually hold assets ----
    tops = []
    try:
        with os.scandir(content_root) as it:
            kids = sorted((e for e in it if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
    except OSError as exc:
        rec["notes"].append("cannot list Content: %s" % exc)
        return rec

    for e in kids:
        low = e.name.lower()
        n = count_assets(e.path)
        entry = {"name": e.name, "uassets": n,
                 "excluded_as_non_content": low in NON_CONTENT_DIRS}
        if n > 0 and not entry["excluded_as_non_content"]:
            entry["game_root"] = "/Game/" + e.name
            tops.append(entry)
        else:
            rec.setdefault("non_content_tops", []).append(entry)

    tops.sort(key=lambda t: (-t["uassets"], t["name"]))
    rec["tops"] = tops
    rec["game_roots"] = [t["game_root"] for t in tops]
    if tops:
        rec["primary_game_root"] = tops[0]["game_root"]

    # ---- fall back to the .uproject name only if enumeration found nothing ----
    if not tops and rec["project_name"]:
        cand = os.path.join(content_root, rec["project_name"])
        rec["notes"].append(
            "no asset-bearing folder directly under Content; probing .uproject name '%s'" % rec["project_name"])
        if os.path.isdir(cand) and count_assets(cand) > 0:
            tops.append({"name": rec["project_name"], "uassets": total,
                         "game_root": "/Game/" + rec["project_name"],
                         "source": "uproject_fallback"})
            rec["tops"] = tops
            rec["game_roots"] = [tops[0]["game_root"]]
            rec["primary_game_root"] = tops[0]["game_root"]

    if not tops:
        rec["notes"].append("FAIL: zero exportable top-level folders found")
        return rec

    # a project name that disagrees with the package folder is worth shouting about
    if rec["project_name"] and rec["project_name"] != tops[0]["name"]:
        rec["notes"].append(
            "project name '%s' != package top folder '%s' -- using the FOLDER (%s)"
            % (rec["project_name"], tops[0]["name"], tops[0]["game_root"]))

    rec["ok"] = total > 0 and bool(tops)

    if verbose:
        # verbose goes to stderr so stdout stays pure JSON for the driver
        w = sys.stderr.write
        w("[discovery] pack            : %s\n" % rec["pack"])
        w("[discovery] pack_dir        : %s\n" % rec["pack_dir"])
        w("[discovery] Content root    : %s  (%d assets)\n" % (rec["content_root"], total))
        w("[discovery] project file    : %s\n" % rec["project_file"])
        w("[discovery] project name    : %s\n" % rec["project_name"])
        w("[discovery] /Game tops      : %s\n"
          % ", ".join("%s=%d %s" % (t["name"], t["uassets"], t["game_root"]) for t in tops))
        w("[discovery] primary game root: %s\n" % rec["primary_game_root"])
        for n in rec["notes"]:
            w("[discovery] note            : %s\n" % n)
        sys.stderr.flush()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", help="pack directory")
    ap.add_argument("--scan-all", help="assets root; discover every subdirectory")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.scan_all:
        root = os.path.abspath(args.scan_all)
        out = []
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if os.path.isdir(d):
                out.append(discover(d, verbose=False))
        json.dump(out, sys.stdout, indent=1)
        print()
        return 0

    if not args.pack:
        ap.error("--pack or --scan-all required")

    rec = discover(args.pack, verbose=args.verbose)
    json.dump(rec, sys.stdout, indent=1)
    print()
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
