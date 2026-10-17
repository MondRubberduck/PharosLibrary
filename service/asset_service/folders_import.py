"""The folders table: the library map plus what was measured under each
folder node -- counts per section, the units and up axis the files use,
the common pivot, LOD and variant groups, the vendor's own category
words, vendor and license file, material-wiring health, Unity prefabs.

Built from the registry's other tables (cheap, no file reads apart from
a shallow license lookup) at the end of ingest and at server start;
served at /api/folders.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from asset_service import config, library_map

FOLDERS_DDL = """
CREATE TABLE IF NOT EXISTS folders (
  folder TEXT PRIMARY KEY,
  name TEXT, kind TEXT, state TEXT, include INTEGER, action TEXT, confirmed INTEGER,
  meshes INTEGER, clips INTEGER, textures INTEGER, audio INTEGER,
  units TEXT, up_axis TEXT, pivot TEXT,
  lod_groups INTEGER, variant_groups INTEGER,
  category_hints TEXT,           -- JSON [[word, n], ...]
  vendor TEXT, license TEXT,
  wiring_ratio REAL,
  prefabs INTEGER,
  facts TEXT,                    -- JSON: the rest
  measured_at TEXT
);
"""
_LICENSE_WORDS = ("license", "licence", "eula", "terms")
# the vendor from the folder words: a creator's name says what a pack
# is, a marketplace's where it was bought (the creator wins)
_VENDOR_TOKENS = {
    "Quixel": ("quixel", "megascans"), "Gumroad": ("gumroad",), "CGTrader": ("cgtrader",),
    "TurboSquid": ("turbosquid",), "Reallusion": ("reallusion", "actorcore", "actor-core"),
    "Mixamo": ("mixamo",), "Sketchfab": ("sketchfab", "uploads_files"),
    "KitBash3D": ("kitbash3d", "kb3d"), "PolyHaven": ("polyhaven", "poly haven"),
    "Leartes": ("leartes",), "Fab": ("fab",)}
_MARKETPLACES = {"Gumroad", "Sketchfab", "Fab", "CGTrader", "TurboSquid"}


def vendor_of(folder: str):
    """The vendor a folder path names, or None."""
    low = (folder or "").replace("\\", "/").lower()
    segments = {seg for seg in re.split(r"[/_\-. ]+", low) if seg}
    creators, markets = [], []
    for vendor, toks in _VENDOR_TOKENS.items():
        hit = any((tok in segments) if vendor == "Fab" else (tok in low) for tok in toks)
        if hit:
            (markets if vendor in _MARKETPLACES else creators).append(vendor)
    return (creators or markets or [None])[0]


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").lower()


def _under(rel: str, folder: str) -> bool:
    f = folder.lower()
    r = rel.lower()
    return r == f or r.startswith(f + "/")


def _license_file(root: Path, folder: str):
    base = root / folder
    try:
        for depth, d in enumerate((base, *[p for p in base.iterdir() if p.is_dir()][:40])):
            for f in d.iterdir():
                if f.is_file() and any(w in f.name.lower() for w in _LICENSE_WORDS) \
                        and f.suffix.lower() in (".txt", ".md", ".pdf", ".html", ".rtf", ""):
                    return f.relative_to(root).as_posix()
            if depth > 40:
                break
    except OSError:
        pass
    return None


def _prefabs(root: Path, folder: str) -> int:
    n = 0
    base = root / folder / "Assets"
    if not base.is_dir():
        return 0
    for dp, dn, fn in os.walk(base):
        dn[:] = [d for d in dn if not d.startswith(".")]
        n += sum(1 for f in fn if f.lower().endswith(".prefab"))
        if n > 200_000:
            break
    return n


def _lib_rel(root: Path, path: str) -> str:
    p = (path or "").replace("\\", "/")
    r = root.as_posix().rstrip("/") + "/"
    return p[len(r):] if p.lower().startswith(r.lower()) else p


def build(db_path, root, rows=None) -> int:
    """(Re)build `folders` for every row of the map. Returns the count."""
    root = Path(root)
    rows = rows if rows is not None else library_map.load()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(FOLDERS_DDL)
        look = library_map.Lookup(rows)

        def node_of(rel: str):
            r = look.node(rel)
            return r["folder"] if r else None

        per: dict = {}

        def bucket(folder):
            return per.setdefault(folder, {
                "meshes": 0, "clips": 0, "textures": 0, "audio": 0,
                "units": Counter(), "up": Counter(), "pivot": Counter(),
                "lod": {}, "variant": {}, "cats": Counter(), "wiring": [],
                "packs": Counter()})

        def table_cols(tbl):
            try:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
            except sqlite3.OperationalError:
                return set()

        mcols = table_cols("meshes")
        if mcols:
            fcol = "facts" if "facts" in mcols else "NULL"
            for r in conn.execute(f"SELECT fbx, pack, name, {fcol} AS facts FROM meshes"):
                node = node_of(_lib_rel(root, r["fbx"]))
                if not node:
                    continue
                b = bucket(node)
                b["meshes"] += 1
                b["packs"][r["pack"] or ""] += 1
                try:
                    f = json.loads(r["facts"] or "{}") or {}
                except ValueError:
                    f = {}
                if f.get("units"):
                    b["units"][f["units"]] += 1
                up = f.get("geo_up_axis") or f.get("up_axis")
                if up:
                    b["up"][up] += 1
                if f.get("pivot"):
                    b["pivot"][f["pivot"]] += 1
                if f.get("lod_group"):
                    b["lod"].setdefault(f["lod_group"], set()).add(f.get("lod_level"))
                if f.get("variant_group"):
                    b["variant"].setdefault(f["variant_group"], set()).add(r["name"])
                for w in f.get("category_hint") or []:
                    b["cats"][w] += 1
        if table_cols("clips"):
            for r in conn.execute("SELECT rel FROM clips"):
                node = node_of(r["rel"])
                if node:
                    bucket(node)["clips"] += 1
        if table_cols("textures"):
            for r in conn.execute("SELECT folder FROM textures"):
                node = node_of(_lib_rel(root, r["folder"]))
                if node:
                    bucket(node)["textures"] += 1
        if table_cols("audio"):
            # audio.rel is relative to the audio SECTION (or absolute for a
            # file outside it): the section's own path goes in front
            try:
                asec = Path(config.AUDIO_ROOT).resolve().relative_to(root.resolve()).as_posix()
            except (ValueError, OSError):
                asec = ""
            for r in conn.execute("SELECT rel FROM audio"):
                p = (r["rel"] or "").replace("\\", "/")
                if re.match(r"^([A-Za-z]:)?/", p):
                    lib = _lib_rel(root, p)
                else:
                    lib = f"{asec}/{p}" if asec else p
                node = node_of(lib)
                if node:
                    bucket(node)["audio"] += 1
        wiring = {}
        if table_cols("mesh_pack_wiring"):
            wiring = {r["pack"]: r["wiring_ratio"] for r in
                      conn.execute("SELECT pack, wiring_ratio FROM mesh_pack_wiring")}
        # a child's facts count for its parents too (a folder of packs
        # reports its packs' units and categories): deepest first, one
        # depth at a time, so a parent that only appears through its
        # children still passes them on to its own parent
        by_lower = {r["folder"].lower(): r["folder"] for r in rows.values()}
        depth = max((f.count("/") for f in per), default=0)
        while depth > 0:
            for folder in [f for f in list(per) if f.count("/") == depth]:
                b = per[folder]
                parent = by_lower.get(folder.rsplit("/", 1)[0].lower())
                if not parent:
                    continue
                pb = bucket(parent)
                for k in ("meshes", "clips", "textures", "audio"):
                    pb[k] += b[k]
                for k in ("units", "up", "pivot", "cats", "packs"):
                    pb[k].update(b[k])
                for k in ("lod", "variant"):
                    for g, s in b[k].items():
                        pb[k].setdefault(g, set()).update(s)
            depth -= 1
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("DELETE FROM folders")
        n = 0
        for key, r in sorted(rows.items()):
            if r["folder"] == ".":
                continue
            b = per.get(r["folder"]) or bucket(r["folder"])
            lod_groups = sum(1 for s in b["lod"].values() if len(s) >= 2)
            variant_groups = sum(1 for s in b["variant"].values() if len(s) >= 2)
            wr = [wiring[p] for p in b["packs"] if p in wiring and wiring[p] is not None]
            facts = {
                "packs": b["packs"].most_common(12),
                "units_dist": dict(b["units"]), "pivot_dist": dict(b["pivot"]),
                "lod_groups": sorted(g for g, s in b["lod"].items() if len(s) >= 2)[:50],
                "variant_groups": sorted(g for g, s in b["variant"].items() if len(s) >= 2)[:50],
                "state": r["state"], "effective_action": library_map.effective_action(r, rows),
                "styles": r["styles"], "domain": r["domain"], "tags": r["tags"],
            }
            conn.execute(
                "INSERT OR REPLACE INTO folders (folder,name,kind,state,include,action,"
                "confirmed,meshes,clips,textures,audio,units,up_axis,pivot,lod_groups,"
                "variant_groups,category_hints,vendor,license,wiring_ratio,prefabs,facts,"
                "measured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["folder"], r["name"], r["kind"], r["state"], int(bool(r["include"])),
                 r["action"], int(bool(r["confirmed"])),
                 b["meshes"], b["clips"], b["textures"], b["audio"],
                 (b["units"].most_common(1) or [(None, 0)])[0][0],
                 (b["up"].most_common(1) or [(None, 0)])[0][0],
                 (b["pivot"].most_common(1) or [(None, 0)])[0][0],
                 lod_groups, variant_groups,
                 json.dumps(b["cats"].most_common(8), ensure_ascii=False),
                 vendor_of(r["folder"]),
                 _license_file(root, r["folder"]) if r["include"] else None,
                 round(sum(wr) / len(wr), 3) if wr else None,
                 _prefabs(root, r["folder"]) if r["kind"] == "unity" and r["include"] else 0,
                 json.dumps(facts, ensure_ascii=False), now))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, folder: str | None = None) -> list:
    """Served rows: every folder, or one (with its facts)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(folders)")}
    except sqlite3.OperationalError:
        cols = set()
    if not cols:
        return []
    sql = "SELECT * FROM folders"
    args: tuple = ()
    if folder is not None:
        sql += " WHERE lower(folder) = ?"
        args = (folder.replace("\\", "/").strip("/").lower(),)
    out = []
    for r in conn.execute(sql + " ORDER BY folder", args):
        d = dict(r)
        for k in ("category_hints", "facts"):
            try:
                d[k] = json.loads(d.get(k) or "null")
            except ValueError:
                d[k] = None
        d["include"], d["confirmed"] = bool(d["include"]), bool(d["confirmed"])
        if folder is None:
            d.pop("facts", None)
        out.append(d)
    return out
