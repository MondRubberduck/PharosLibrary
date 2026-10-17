"""Contact sheets and renders of models, with the standard library around
one headless Blender pass.

A tile is one model (256 px, 3/4 view, labelled with its file name), rendered
by blender_render.py and kept under <previews_dir>/models/<hash>.png.
A contact sheet is a grid of up to 36 tiles for one folder node of the
library map, kept under <previews_dir>/sheets/<name>.png (the name: the
node, readable and bounded, plus a short hash) with index.json beside it. Both are served by the server (/render/<hash>.png,
/sheet/<name>.png) and named in the agent docs: an agent picking among
shortlisted models can look.

    pharos.py previews --sheets [--folder F] [--force]   every node with models
    pharos.py previews --keys K [K ...]                  renders on demand
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config, library_map, measure        # noqa: E402

TILE = 256
COLUMNS = 6
LIMIT = 36
_SCRIPT = Path(__file__).resolve().parent / "blender_render.py"
_SHEET_KINDS = ("3d-assets", "unreal", "blender", "unity")
_COLLIDER_RE = re.compile(r"(?:^|[^a-z0-9])(?:ucx|ubx|ucp|usp|collider|collision)(?:[^a-z0-9]|$)", re.I)
_CARD_RE = re.compile(r"impost[eo]r", re.I)


# ------------------------------------------------------------------- paths
def slug(folder: str) -> str:
    """A file name for a folder node: readable, bounded, and unique (two
    folders that differ only in characters the name cannot carry get
    different hashes)."""
    f = (folder or "").replace("\\", "/").strip("/")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", f.replace("/", "__"))
    base = re.sub(r"\.{2,}", ".", base).strip("._") or "root"
    return f"{base[:80]}-{hashlib.sha1(f.lower().encode('utf-8')).hexdigest()[:8]}"


def tile_hash(key: str) -> str:
    return hashlib.sha1(key.replace("\\", "/").lower().encode("utf-8")).hexdigest()[:20]


def tiles_dir() -> Path:
    return Path(config.PREVIEW_DIR) / "models"


def sheets_dir() -> Path:
    return Path(config.PREVIEW_DIR) / "sheets"


def tile_path(key: str) -> Path:
    return tiles_dir() / f"{tile_hash(key)}.png"


def sheet_path(folder: str) -> Path:
    return sheets_dir() / f"{slug(folder)}.png"


def tile_url(key: str):
    return f"/render/{tile_hash(key)}.png" if tile_path(key).is_file() else None


def sheet_url(folder: str):
    return f"/sheet/{slug(folder)}.png" if sheet_path(folder).is_file() else None


def load_index() -> dict:
    try:
        return json.loads((sheets_dir() / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_index(index: dict) -> None:
    sheets_dir().mkdir(parents=True, exist_ok=True)
    tmp = sheets_dir() / "index.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, sheets_dir() / "index.json")


# -------------------------------------------------------------- images
def read_bmp(path) -> tuple:
    """(width, height, rows) of a 24-bit uncompressed BMP, rows top-down RGB."""
    data = Path(path).read_bytes()
    if data[:2] != b"BM":
        raise ValueError("not a BMP")
    off = struct.unpack_from("<I", data, 10)[0]
    w, h = struct.unpack_from("<ii", data, 18)
    bits = struct.unpack_from("<H", data, 28)[0]
    comp = struct.unpack_from("<I", data, 30)[0]
    if bits not in (24, 32) or comp != 0:
        raise ValueError(f"unsupported BMP ({bits} bits, compression {comp})")
    bpp = bits // 8
    stride = (w * bpp + 3) & ~3
    flip = h > 0
    h = abs(h)
    rows = []
    for y in range(h):
        src = off + (h - 1 - y if flip else y) * stride
        line = data[src:src + w * bpp]
        if bpp == 4:
            line = bytes(b for i, b in enumerate(line) if i % 4 != 3)
        # BGR -> RGB
        rgb = bytearray(w * 3)
        rgb[0::3] = line[2::3]
        rgb[1::3] = line[1::3]
        rgb[2::3] = line[0::3]
        rows.append(bytes(rgb))
    return w, h, rows


def write_png(path, w: int, h: int, rows) -> None:
    """8-bit RGB PNG, filter 0, from top-down RGB rows."""
    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)


def read_png(path) -> tuple:
    """(width, height, rows) of an 8-bit non-interlaced PNG as RGB rows."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = []
    w = h = ctype = None
    while pos + 8 <= len(data):
        n, tag = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", body)
            if depth != 8 or inter != 0 or ctype not in (0, 2, 4, 6):
                raise ValueError("unsupported PNG layout")
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    if w is None:
        raise ValueError("no IHDR")
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    rows = []
    prev = bytearray(stride)
    at = 0
    for _ in range(h):
        ftype = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        if ftype == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                ul = prev[i - ch] if i >= ch else 0
                line[i] = (line[i] + _paeth(left, prev[i], ul)) & 0xFF
        prev = line
        if ch == 3:
            rgb = bytes(line)
        elif ch == 4:
            rgb = bytes(b for i, b in enumerate(line) if i % 4 != 3)
        elif ch == 1:
            rgb = bytes(v for v in line for _ in range(3))
        else:
            rgb = bytes(line[i] for i in range(0, stride, 2) for _ in range(3))
        rows.append(rgb)
    return w, h, rows


def _blank_tile(w: int, h: int, shade: int = 40) -> list:
    return [bytes([shade] * (w * 3)) for _ in range(h)]


def compose(keys: list, out_png, columns: int = COLUMNS, tile: int = TILE) -> int:
    """Lay the keys' tiles out in a grid (missing tiles stay dark); the
    number of tiles placed."""
    keys = list(keys)[:LIMIT]
    cols = max(1, min(columns, len(keys)))
    rows_n = (len(keys) + cols - 1) // cols
    W, H = cols * tile, rows_n * tile
    canvas = [bytearray([24] * (W * 3)) for _ in range(H)]
    placed = 0
    for i, key in enumerate(keys):
        cx, cy = (i % cols) * tile, (i // cols) * tile
        p = tile_path(key)
        try:
            w, h, trows = read_png(p)
        except (OSError, ValueError):
            w, h, trows = tile, tile, _blank_tile(tile, tile)
        else:
            placed += 1
        for y in range(min(h, tile)):
            row = trows[y][:tile * 3]
            canvas[cy + y][cx * 3:cx * 3 + len(row)] = row
    write_png(out_png, W, H, canvas)
    return placed


# ---------------------------------------------------------- picking keys
def _lib_rel(root: Path, path: str) -> str:
    p = (path or "").replace("\\", "/")
    base = root.as_posix().rstrip("/") + "/"
    return p[len(base):] if p.lower().startswith(base.lower()) else p


def key_file(root: Path, key: str) -> tuple:
    """(absolute path, object name or None) of a mesh key."""
    rel, _, obj = key.partition("#")
    p = Path(rel)
    return (p if p.is_absolute() else Path(root) / rel), (obj or None)


def mesh_keys(db_path, root: Path, rows_map: dict | None = None) -> dict:
    """folder node -> [(key, name, triangles, facts, subfolder)] for every
    served mesh row on disk, keyed like the API keys its files."""
    import sqlite3
    rows_map = rows_map if rows_map is not None else library_map.load()
    look = library_map.Lookup(rows_map)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
        if not cols:
            return {}
        fcol = "facts" if "facts" in cols else "NULL"
        ocol = "object_name" if "object_name" in cols else "NULL"
        rows = [dict(r) for r in conn.execute(
            f"SELECT name, fbx, {ocol} AS object_name, triangles, {fcol} AS facts "
            "FROM meshes WHERE on_disk = 1 AND fbx != ''")]
    finally:
        conn.close()
    count: dict = {}
    for r in rows:
        r["_rel"] = _lib_rel(root, r["fbx"])
        count[r["_rel"].lower()] = count.get(r["_rel"].lower(), 0) + 1
    out: dict = {}
    for r in rows:
        key = r["_rel"] + ("#" + (r["object_name"] or r["name"] or "")
                           if count[r["_rel"].lower()] > 1 else "")
        node = look.node(r["_rel"])
        if not node:
            continue
        try:
            facts = json.loads(r["facts"] or "{}") or {}
        except ValueError:
            facts = {}
        inner = r["_rel"][len(node["folder"]) + 1:] if r["_rel"].lower().startswith(
            node["folder"].lower() + "/") else r["_rel"]
        sub = inner.split("/")[0] if "/" in inner else ""
        out.setdefault(node["folder"], []).append((key, r["name"] or "", r["triangles"] or 0, facts, sub))
    return out


def pick_keys(entries: list, limit: int = LIMIT) -> list:
    """Up to `limit` representative keys of one node: full models (no
    lower LOD, no collision hull, no impostor card), one per variant
    group, the biggest first, dealt out across the node's sub-folders so
    every part of the folder is on the sheet."""
    seen_groups: set = set()
    good = []
    for key, name, tris, facts, sub in entries:
        text = f"{name} {key.partition('#')[0].rsplit('/', 1)[-1]} {key.partition('#')[2]}"
        if facts.get("lod_level") or _COLLIDER_RE.search(text) or _CARD_RE.search(text) \
                or facts.get("collision"):
            continue
        group = facts.get("variant_group") or facts.get("lod_group") or re.sub(
            r"[ _\-]?(?:\d{1,3}[a-z]?|[a-z])$", "", name.lower()) or name.lower()
        if group in seen_groups:
            continue
        seen_groups.add(group)
        good.append((key, tris or 0, sub))
    by_sub: dict = {}
    for key, tris, sub in good:
        by_sub.setdefault(sub, []).append((tris, key))
    for lst in by_sub.values():
        lst.sort(key=lambda t: -t[0])
    order = sorted(by_sub, key=lambda s: -len(by_sub[s]))
    picked: list = []
    while len(picked) < limit and any(by_sub.values()):
        for s in order:
            if by_sub[s]:
                picked.append(by_sub[s].pop(0)[1])
                if len(picked) >= limit:
                    break
    return picked


# ------------------------------------------------------------- rendering
def find_blender() -> str:
    from asset_service.doctor import _find_blender
    return _find_blender()


_MAX_RESTARTS = 6           # Blender runs per batch after crashes


def render_tiles(keys: list, root: Path, blender: str, log=print, timeout: int = 1800,
                 force: bool = False) -> dict:
    """Render the tiles the keys lack (one Blender run; one more after
    every file Blender crashes on, up to _MAX_RESTARTS), convert them to
    PNG. Returns key -> {"ok", "file" | "error"}."""
    todo = [k for k in keys if force or not tile_path(k).is_file()]
    results = {k: {"ok": True, "file": str(tile_path(k)), "cached": True}
               for k in keys if k not in todo}
    if not todo:
        return results
    if not blender:
        for k in todo:
            results[k] = {"ok": False, "error": "Blender not found (set BLENDER_EXE)"}
        return results
    tiles_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pharos_render_") as tmp:
        items = []
        for k in todo:
            path, obj = key_file(root, k)
            if not path.is_file():
                results[k] = {"ok": False, "error": "file not on disk"}
                continue
            items.append({"key": k, "path": str(path), "object": obj,
                          "label": (k.rsplit("/", 1)[-1])[:28],
                          "out": str(Path(tmp) / f"{tile_hash(k)}.bmp")})
        if not items:
            return results
        job = Path(tmp) / "job.json"
        result_file = Path(tmp) / "result.json"
        cmd = [blender, "--background", "--factory-startup", "--python", str(_SCRIPT),
               "--", str(job)]
        log(f"  blender: {len(items)} tile(s) ...")
        got = {}
        pending = list(items)
        restarts = 0
        while pending:
            job.write_text(json.dumps({"items": pending, "size": TILE, "result": str(result_file)},
                                      ensure_ascii=False), encoding="utf-8")
            run_error = ""
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding="utf-8", errors="replace", timeout=timeout)
                if r.returncode:
                    run_error = f"blender exit {r.returncode}: {(r.stdout or '')[-300:]}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                run_error = f"blender: {exc}"
            # the result file is rewritten after every tile: a crash or a
            # timeout on a later file keeps the tiles rendered before it
            try:
                got.update(json.loads(result_file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
            try:
                result_file.unlink()
            except OSError:
                pass
            left = [it for it in pending if it["key"] not in got]
            if not left:
                break
            # Blender stopped (an importer crashed, the run timed out): the
            # first file without a result is the one it stopped on; it is
            # marked and the files after it get a run of their own
            culprit, pending = left[0], left[1:]
            got[culprit["key"]] = ({"ok": True} if Path(culprit["out"]).is_file()
                                   else {"ok": False, "error": run_error or "no result from Blender"})
            restarts += 1
            if pending and restarts >= _MAX_RESTARTS:
                for it in pending:
                    got[it["key"]] = {"ok": False, "error": f"not tried: Blender stopped "
                                      f"{restarts} times in this batch"}
                break
            if pending:
                log(f"  blender stopped on {culprit['key']}; {len(pending)} tile(s) in a new run ...")
        for it in items:
            res = got.get(it["key"]) or {"ok": False, "error": "no result from Blender"}
            if res.get("ok"):
                try:
                    w, h, rows = read_bmp(it["out"])
                    write_png(tile_path(it["key"]), w, h, rows)
                    res = {"ok": True, "file": str(tile_path(it["key"])),
                           "dims": res.get("dims"), "objects": res.get("objects")}
                except (OSError, ValueError) as exc:
                    res = {"ok": False, "error": f"tile conversion: {exc}"}
            results[it["key"]] = res
    return results


def sheet_nodes(rows_map: dict) -> list:
    return [r["folder"] for r in rows_map.values()
            if r["include"] and r["kind"] in _SHEET_KINDS and r["folder"] != "."]


def build_sheets(db_path, root, folders=None, force: bool = False, limit: int = LIMIT,
                 blender: str | None = None, log=print, dry_run: bool = False) -> dict:
    """One contact sheet per folder node that holds models. Resumable:
    rendered tiles are kept, a node with a sheet is skipped unless
    `force`. Returns {"sheets", "skipped", "tiles", "failed", "nodes"}."""
    root = Path(root)
    rows_map = library_map.load()
    per_node = mesh_keys(db_path, root, rows_map)
    nodes = [f for f in sheet_nodes(rows_map) if per_node.get(f)]
    if folders:
        want = {f.replace("\\", "/").strip("/").lower() for f in folders}
        nodes = [f for f in nodes if f.lower() in want]
    blender = blender if blender is not None else find_blender()
    index = load_index()
    stats = {"sheets": 0, "skipped": 0, "tiles": 0, "failed": 0, "nodes": [], "missing": []}
    for folder in sorted(nodes, key=str.lower):
        keys = pick_keys(per_node[folder], limit)
        if not keys:
            continue
        done = index.get(folder) or {}
        if sheet_path(folder).is_file() and not force and done.get("keys") == keys \
                and done.get("placed") and not done.get("failed"):
            stats["skipped"] += 1
            continue
        stats["nodes"].append(folder)
        if dry_run:
            log(f"  {folder}: {len(keys)} model(s) -> {sheet_path(folder)}")
            continue
        log(f"{folder}: {len(keys)} model(s)")
        res = render_tiles(keys, root, blender, log=log, force=force)
        failed = [k for k, v in res.items() if not v.get("ok")]
        stats["tiles"] += sum(1 for v in res.values() if v.get("ok") and not v.get("cached"))
        stats["failed"] += len(failed)
        if not any(tile_path(k).is_file() for k in keys):
            log(f"  no sheet: all {len(keys)} tile(s) failed")
            continue
        placed = compose(keys, sheet_path(folder))
        index[folder] = {"file": sheet_path(folder).name, "keys": keys,
                         "failed": {k: res[k].get("error") for k in failed},
                         "placed": placed, "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_index(index)
        stats["sheets"] += 1
        log(f"  sheet: {placed} of {len(keys)} tiles -> {sheet_path(folder)}"
            + (f"  ({len(failed)} failed)" if failed else ""))
    if not blender and stats["nodes"] and not dry_run:
        stats["missing"].append("blender")
    return stats


def render_keys(keys: list, root, blender: str | None = None, log=print,
                force: bool = False) -> dict:
    """Tiles for the given keys (on demand), key -> result."""
    blender = blender if blender is not None else find_blender()
    return render_tiles(list(keys), Path(root), blender, log=log, force=force)
