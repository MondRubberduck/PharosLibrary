"""Minimal binary FBX 7.x walker for per-file dimensions — stdlib only.

Reads a binary FBX and reports file-level geometry WITHOUT any external
tool or engine:

- bbox extents over every Geometry's Vertices array (file units),
- polygon count (PolygonVertexIndex high-bit terminators),
- UnitScaleFactor from GlobalSettings.

Per-model transforms are deliberately IGNORED: most single-asset FBX carry
baked or identity transforms, but scene-style containers do not — callers
treat results as file-level. "Indicative, not fabricated": anything
unparseable (ASCII FBX, truncation, trailing junk) returns None.

Format notes (verified against Blender-measured ground truth on 8k+ UE
exporter files): record layout is [endOffset:u32][numProps:u32]
[propLen:u32][nameLen:u8][name][props][children]; pre-7400 files put a
13-byte null record BETWEEN top-level records; child lists end where the
next candidate stops being a valid record. A record's props sit BETWEEN
the name and its children — parse positions must track both.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

_ELEM_FMT = {"d": ("d", 8), "f": ("f", 4), "i": ("i", 4),
             "l": ("q", 8), "b": ("b", 1)}


def _read_property(data: bytes, pos: int):
    """Read one property at pos. Returns (value, new_pos); (None, pos+1)
    for unknown type codes (cannot advance safely -> caller stops)."""
    t = chr(data[pos])
    pos += 1
    if t == "Y":
        return struct.unpack_from("<h", data, pos)[0], pos + 2
    if t == "C":
        return bool(data[pos]), pos + 1
    if t == "I":
        return struct.unpack_from("<i", data, pos)[0], pos + 4
    if t == "F":
        return struct.unpack_from("<f", data, pos)[0], pos + 4
    if t == "D":
        return struct.unpack_from("<d", data, pos)[0], pos + 8
    if t == "L":
        return struct.unpack_from("<q", data, pos)[0], pos + 8
    if t in _ELEM_FMT:
        n, enc, clen = struct.unpack_from("<III", data, pos)
        pos += 12
        raw = data[pos:pos + clen]
        pos += clen
        if enc == 1:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                return None, pos
        fmt, size = _ELEM_FMT[t]
        if len(raw) < n * size:
            return None, pos
        return list(struct.unpack_from(f"<{n}{fmt}", raw, 0)), pos
    if t in ("S", "R"):
        n = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        return data[pos:pos + n], pos + n
    return None, pos


def _read_record(data: bytes, pos: int):
    """Read one record (header + props). Children are the caller's job.

    Returns (name, props, end_offset, pos_after_props) or None when the
    candidate at pos is not a valid record (junk / null terminator)."""
    if pos + 13 > len(data):
        return None
    end_offset, num_props, prop_len = struct.unpack_from("<III", data, pos)
    name_len = data[pos + 12]
    if name_len == 0 or end_offset <= pos + 13 or end_offset > len(data):
        return None
    name = data[pos + 13:pos + 13 + name_len].decode("latin-1")
    pos += 13 + name_len
    prop_end = pos + prop_len
    if prop_end > end_offset:
        return None
    props = []
    for _ in range(num_props):
        v, pos = _read_property(data, pos)
        props.append(v)
    return name, props, end_offset, prop_end


def _parse_nodes(data: bytes, pos: int, limit: int, top: bool = False):
    """Parse successive records up to limit. Returns (nodes, pos)."""
    out = []
    while pos + 13 <= limit:
        peek = struct.unpack_from("<I", data, pos)[0]
        name_len = data[pos + 12]
        if peek == 0 and name_len == 0:
            pos += 13
            if top:
                continue        # pre-7400 separator between top records
            break
        if peek <= pos or peek > limit or name_len == 0:
            break               # props tail / junk — not a record
        rec = _read_record(data, pos)
        if rec is None:
            break
        name, props, end, after = rec
        kids, _ = _parse_nodes(data, after, end)
        out.append((name, props, kids))
        pos = end
    return out, pos


def _walk(nodes, name):
    for nm, props, kids in nodes:
        if nm == name:
            yield props, kids
        yield from _walk(kids, name)


def fbx_file_info(path: str | Path) -> dict | None:
    """File-level geometry info from a binary FBX, in FILE UNITS.

    Returns {"bbox_min", "bbox_max", "polygons", "vertices",
    "unit_scale_factor", "fbx_version"} or None when unparseable.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data.startswith(b"Kaydara FBX Binary") or len(data) < 27:
        return None
    version = struct.unpack_from("<I", data, 23)[0]
    try:
        top, _ = _parse_nodes(data, 27, len(data), top=True)
    except (struct.error, zlib.error):
        return None

    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    polys = 0
    verts = 0
    idx_count = 0
    got = False
    for props, _ in _walk(top, "Vertices"):
        if not props or not isinstance(props[0], list) or len(props[0]) < 3:
            continue
        v = props[0]
        got = True
        verts += len(v) // 3
        for i in range(0, len(v) - 2, 3):
            for a in range(3):
                x = v[i + a]
                if x < lo[a]:
                    lo[a] = x
                if x > hi[a]:
                    hi[a] = x
    for props, _ in _walk(top, "PolygonVertexIndex"):
        if not props or not isinstance(props[0], list):
            continue
        idx = props[0]
        polys += sum(1 for x in idx if x < 0)   # high bit = last index
        idx_count += len(idx)
    if not got:
        return None
    # fan triangulation of n-gons: every polygon with k indices -> k-2 tris
    tris = idx_count - 2 * polys if idx_count >= 2 * polys else polys

    usf = None
    for props, _ in _walk(top, "P"):
        # P layout varies by exporter: [name, type, label, flags, value]
        # (value at 4) or [name, type, flags, value] (value at 3). The
        # first FLOAT after the name wins -- type/label/flags are bytes.
        if len(props) >= 4 and props[0] == b"UnitScaleFactor":
            for v in props[3:]:
                if isinstance(v, float):
                    usf = v
                    break
            break
    return {"bbox_min": lo, "bbox_max": hi, "polygons": polys,
            "triangles": tris, "vertices": verts,
            "unit_scale_factor": usf if usf and usf > 0 else None,
            "fbx_version": version}


def fbx_bbox_m(path: str | Path) -> dict | None:
    """Convenience: file-level geometry in METRES (truth-verified against
    Blender measurements: raw * UnitScaleFactor/100, default factor 1)."""
    info = fbx_file_info(path)
    if info is None:
        return None
    k = (info["unit_scale_factor"] or 1.0) / 100.0
    return {"bbox_m": [(info["bbox_max"][i] - info["bbox_min"][i]) * k
                       for i in range(3)],
            "triangles": info["triangles"],
            "vertices": info["vertices"]}
