"""measure.py -- one standard-library pass over model and clip files.

Every fact an agent can be given without an engine, from one read of the
file: size and placement corners in metres, triangles and vertices,
materials, UV layers, skin deformers and bones, LOD level and group,
collision hulls, variant group, units and up axis, the local pivot class
(where the origin sits in the box), animation stacks with duration and
fps, a root-motion flag, whether a clip file also carries a mesh, and a
scale warning. FBX (every version), OBJ, glTF/GLB, STL and BVH.

    measure_file(path, rel)       -> one record (never raises)
    measure_folder(folder, root)  -> records for every model/clip file
                                     below `folder`, cached per file in
                                     <agent files>/index/<folder>.jsonl
                                     (size + mtime: unchanged files are
                                     not read again)
    python measure.py <file>      prints the record
    python measure.py --clips <folder>   writes the clips table

The scanner takes its geometry from here; the Blender backend
(blender_measure.py, inside Blender) writes the same record shape with
per-object corners for .blend files and multi-object containers.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import time
from pathlib import Path

# importable as a package module and runnable as a script (ingest calls
# `python measure.py --clips <folder>`, like scanner.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import kinds, skeleton                        # noqa: E402
from asset_service.fbx_dims import _walk, fbx_nodes             # noqa: E402

SCHEMA = 2                      # bump to re-measure every cached file
FBX_TIME = 46186158000          # FBX time units per second
_TIME_MODES = {0: 30.0, 1: 120.0, 2: 100.0, 3: 60.0, 4: 50.0, 5: 48.0, 6: 30.0,
               7: 30.0, 8: 29.97, 9: 29.97, 10: 25.0, 11: 24.0, 12: 1000.0,
               13: 23.976, 15: 96.0, 16: 72.0, 17: 59.94}
_UNITS = ((1.0, "cm"), (100.0, "m"), (2.54, "in"), (0.1, "mm"), (30.48, "ft"),
          (10.0, "dm"), (1000.0, "km"), (91.44, "yd"))
MEASURED_EXTS = frozenset(kinds.MODEL_SCAN | {".bvh"})
_LOD_RE = re.compile(r"(?:^|[^a-z0-9])lod[ _-]?(\d{1,2})(?![0-9])", re.I)
_COLLISION_RE = re.compile(r"^(?:ucx|ubx|ucp|usp)[_\-]", re.I)
_VARIANT_RE = re.compile(r"(?:[ _\-]?\d{1,3}[a-z]?|[ _\-](?:v(?:ar)?\d{1,3}|[a-z]))$", re.I)
_ROOT_BONES = ("root", "hips", "pelvis", "hip", "cog", "reference", "armature",
               "motion")
_GENERIC_DIRS = frozenset((
    "fbx", "obj", "mesh", "meshes", "model", "models", "export", "exports",
    "source", "sources", "lod", "lods", "geometry", "geo", "3d", "files",
    "assets", "content", "textures", "materials", "sm", "sk", "static",
    "staticmesh", "staticmeshes", "skeletal", "data", "game", "generic",
    "low", "high", "lowpoly", "highpoly", "final", "new", "old", "v1", "v2"))
_KEYS = ("rel", "ext", "bytes", "mtime_ns", "kind", "triangles", "vertices",
         "bbox_min_m", "bbox_max_m", "bbox_m", "units", "unit_scale", "up_axis",
         "pivot", "materials", "uv_layers", "skin", "bones", "bone_names",
         "lod_level", "lod_group", "collision", "variant_group", "category_hint",
         "clips", "has_mesh", "scale_suspect", "error", "measured_by",
         "measured_at", "schema")


# ----------------------------------------------------------------- helpers
def _fbx_name(raw) -> str:
    """'Name\\x00\\x01Class' -> 'Name' (an FBX object name)."""
    if not isinstance(raw, bytes):
        return str(raw or "")
    return raw.split(b"\x00\x01", 1)[0].decode("utf-8", "replace")


def _first_number(props):
    for v in props[1:]:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return None


def _units(scale) -> str:
    if not scale or scale <= 0:
        return "unknown"
    for k, label in _UNITS:
        if abs(scale - k) <= k * 0.02:
            return label
    return f"x{scale:g}cm"


def pivot_class(lo, hi, up: int) -> str | None:
    """Where the origin sits in the box: base-centre (the ground contact
    under the middle, what placement wants), corner, centre, top, or
    outside the box. `up` is the index of the vertical axis."""
    if lo is None or hi is None or any(h < l for l, h in zip(lo, hi)):
        return None
    size = [h - l for l, h in zip(lo, hi)]
    if max(size) <= 0:
        return None
    tol = max(size) * 0.05
    pos = []
    for i in range(3):
        if size[i] <= tol:
            # a flat axis (a card, a plane): centred when the origin lies in
            # the slab, else the origin is off the geometry
            if lo[i] - tol <= 0.0 <= hi[i] + tol:
                pos.append(0.5)
                continue
            return "outside"
        pos.append((0.0 - lo[i]) / size[i])
    if any(p < -0.05 or p > 1.05 for p in pos):
        return "outside"
    horiz = [pos[i] for i in range(3) if i != up]
    centred = all(0.35 <= p <= 0.65 for p in horiz)
    v = pos[up]
    if v <= 0.08 and centred:
        return "base-centre"
    if v <= 0.08 and all(p <= 0.12 or p >= 0.88 for p in horiz):
        return "corner"
    if 0.35 <= v <= 0.65 and centred:
        return "centre"
    if v >= 0.92 and centred:
        return "top"
    return "other"


def name_facts(stem: str) -> dict:
    """LOD level and group, collision hull, variant group from a file or
    object name."""
    m = _LOD_RE.search(stem)
    lod_level = int(m.group(1)) if m else None
    base = _LOD_RE.sub("", stem).strip(" _-") if m else stem
    variant = _VARIANT_RE.sub("", base).strip(" _-") or base
    return {"lod_level": lod_level,
            "lod_group": base.lower() if m else None,
            "collision": bool(_COLLISION_RE.match(stem)),
            "variant_group": variant.lower() if variant.lower() != stem.lower() else None}


def category_hint(rel: str) -> list:
    """The vendor's own taxonomy: the folder words between the indexed
    folder and the file, minus the generic ones (FBX, Meshes, Exports)."""
    parts = rel.replace("\\", "/").split("/")[:-1]
    out = []
    for p in parts:
        w = p.strip()
        if not w or w.lower() in _GENERIC_DIRS or re.fullmatch(r"[\d._ -]+", w):
            continue
        if re.fullmatch(r"(?i)(lod\d*|\d+k|v\d+)", w):
            continue
        out.append(w)
    return out[-3:]


def _blank(rel: str, ext: str) -> dict:
    return {"rel": rel, "ext": ext, "bytes": None, "mtime_ns": None,
            "kind": "unknown", "triangles": None, "vertices": None,
            "bbox_min_m": None, "bbox_max_m": None, "bbox_m": None,
            "units": "unknown", "unit_scale": None, "up_axis": None, "pivot": None,
            "materials": [], "uv_layers": None, "skin": False, "bones": 0,
            "bone_names": [], "lod_level": None, "lod_group": None,
            "collision": False, "variant_group": None, "category_hint": [],
            "clips": [], "has_mesh": False, "scale_suspect": False, "error": None,
            "measured_by": "stdlib", "measured_at": "", "schema": SCHEMA}


# --------------------------------------------------------------------- FBX
def _measure_fbx(path: Path, rec: dict) -> None:
    parsed = fbx_nodes(path)
    if parsed is None:
        head = path.read_bytes()[:2_000_000] if path.stat().st_size else b""
        rec["error"] = "not a readable binary FBX"
        # the marker alone still says what the file is for
        if b"AnimationStack" in head or b"AnimationCurve" in head:
            rec["kind"] = "clip"
            rec["clips"] = [{"name": path.stem, "frames": None, "fps": None,
                             "duration_s": None, "root_motion": None}]
        elif b"Kaydara" in head or b"Vertices" in head:
            rec["kind"] = "mesh"
        return
    version, top = parsed
    rec["fbx_version"] = version
    # ---- global settings: units, up axis, frame rate
    gs: dict = {}
    for _, kids in _walk(top, "GlobalSettings"):
        for pr, _ in _walk(kids, "P"):
            if pr and isinstance(pr[0], bytes):
                gs[pr[0]] = _first_number(pr)
    usf = gs.get(b"UnitScaleFactor")
    usf = float(usf) if isinstance(usf, (int, float)) and usf > 0 else 1.0
    rec["unit_scale"] = usf
    rec["units"] = _units(usf)
    up_i = gs.get(b"UpAxis")
    up_i = up_i if up_i in (0, 1, 2) else None       # None: the file does not say
    rec["up_axis"] = "XYZ"[up_i] if up_i is not None else None
    mode = gs.get(b"TimeMode")
    fps = _TIME_MODES.get(mode) if isinstance(mode, int) else None
    if mode == 14:
        cfr = gs.get(b"CustomFrameRate")
        fps = float(cfr) if isinstance(cfr, (int, float)) and cfr > 0 else None
    if fps is None:
        fps = 30.0
    # ---- geometry
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    polys = idx_count = verts = 0
    got = False
    uv_max = 0
    for props, kids in _walk(top, "Geometry"):
        uv_max = max(uv_max, sum(1 for _ in _walk(kids, "LayerElementUV")))
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
        if props and isinstance(props[0], list):
            idx = props[0]
            polys += sum(1 for x in idx if x < 0)
            idx_count += len(idx)
    rec["has_mesh"] = got
    if got:
        tris = idx_count - 2 * polys if idx_count >= 2 * polys else polys
        rec["triangles"], rec["vertices"] = tris, verts
        rec["uv_layers"] = uv_max
    # ---- objects: models, materials, deformers
    models: dict = {}            # id -> (name, type)
    rot90 = plain = 0
    scales: list = []
    for props, kids in _walk(top, "Model"):
        if len(props) >= 3 and isinstance(props[0], int):
            typ = _fbx_name(props[2]) or (props[2].decode("latin-1")
                                          if isinstance(props[2], bytes) else "")
            models[props[0]] = (_fbx_name(props[1]), typ)
            if typ == "Mesh":
                rx = 0.0
                for pr, _ in _walk(kids, "P"):
                    if pr and pr[0] in (b"Lcl Rotation", b"PreRotation"):
                        nums = [v for v in pr[1:] if isinstance(v, (int, float))
                                and not isinstance(v, bool)]
                        if nums:
                            rx += float(nums[0])
                    elif pr and pr[0] == b"Lcl Scaling":
                        nums = [v for v in pr[1:] if isinstance(v, (int, float))
                                and not isinstance(v, bool)]
                        if nums and nums[0] > 0:
                            scales.append(round(float(nums[0]), 6))
                if abs(abs(rx) - 90.0) < 1.0 or abs(abs(rx) - 270.0) < 1.0:
                    rot90 += 1
                else:
                    plain += 1
    # a Mesh model's own scale is part of its size (Blender writes vertices
    # in metres with Lcl Scaling 100 into a centimetre file): the most
    # common scale of the mesh models applies to the file-level box
    s_model = 1.0
    if scales:
        s_model = max(set(scales), key=scales.count)
        if len(set(scales)) > 1:
            rec["scale_mixed"] = True
    if got:
        k = usf / 100.0 * s_model
        rec["bbox_min_m"] = [round(x * k, 5) for x in lo]
        rec["bbox_max_m"] = [round(x * k, 5) for x in hi]
        rec["bbox_m"] = [round((hi[i] - lo[i]) * k, 5) for i in range(3)]
        if s_model != 1.0:
            rec["model_scale"] = s_model
    # the vertices' own vertical axis: a Mesh model turned 90 degrees
    # about X in a Y-up scene keeps Z-up geometry (3ds Max, Blender,
    # Character Creator exports); the box and the pivot follow that
    geo_up = 2 if (up_i == 1 and rot90 > plain) else up_i
    rec["geo_up_axis"] = "XYZ"[geo_up] if geo_up is not None else None
    if got and geo_up is not None:
        rec["pivot"] = pivot_class(lo, hi, geo_up)
    joints = [n for n, t in models.values() if t in ("LimbNode", "Limb", "Root")]
    if not joints:
        joints = [n for n, t in models.values() if t == "Null"] if not got else []
    rec["bones"] = len(joints)
    rec["bone_names"] = list(dict.fromkeys(joints))[:400]
    mats = []
    for props, _ in _walk(top, "Material"):
        if len(props) >= 2:
            n = _fbx_name(props[1])
            if n and n not in mats:
                mats.append(n)
    rec["materials"] = mats[:64]
    rec["skin"] = any(len(p) >= 3 and _fbx_name(p[2]) == "Skin"
                      for p, _ in _walk(top, "Deformer"))
    if got and not rec["collision"]:
        rec["collision"] = any(_COLLISION_RE.match(n) for n, t in models.values()
                               if t == "Mesh") and len(models) <= 2
    # ---- animation stacks
    stacks = []
    for props, kids in _walk(top, "AnimationStack"):
        name = _fbx_name(props[1]) if len(props) >= 2 else path.stem
        start = stop = None
        for pr, _ in _walk(kids, "P"):
            if pr and pr[0] == b"LocalStart":
                start = _first_number(pr)
            elif pr and pr[0] == b"LocalStop":
                stop = _first_number(pr)
            elif pr and pr[0] == b"ReferenceStop" and stop is None:
                stop = _first_number(pr)
        stacks.append([name or path.stem, start or 0, stop])
    if stacks and any(s[2] is None for s in stacks):
        # no stop time written: the last key of the longest curve
        last = 0
        for _, kids in _walk(top, "AnimationCurve"):
            for pr, _ in _walk(kids, "KeyTime"):
                if pr and isinstance(pr[0], list) and pr[0]:
                    last = max(last, pr[0][-1])
        for s in stacks:
            if s[2] is None:
                s[2] = last or None
    clips = []
    for name, start, stop in stacks:
        dur = (stop - start) / FBX_TIME if stop is not None else None
        clips.append({"name": name, "frames": int(round(dur * fps)) if dur else None,
                      "fps": fps, "duration_s": round(dur, 3) if dur else None,
                      "root_motion": None})
    if clips:
        rm = _fbx_root_motion(top, models, up_i, usf)
        for c in clips:
            c["root_motion"] = rm
    rec["clips"] = clips
    # ---- what the file is
    if clips and not got:
        rec["kind"] = "clip"
    elif got and rec["skin"]:
        rec["kind"] = "skeletal"
    elif clips and got:
        rec["kind"] = "mesh+clip"
    elif got:
        rec["kind"] = "mesh"
    elif joints:
        rec["kind"] = "skeleton"


def _fbx_root_motion(top, models: dict, up_i: int, usf: float):
    """True when the root bone ENDS the clip somewhere else than it began
    (net horizontal travel over 2 cm: a walk, not a sway), False when it
    comes back or never moves, None when the file does not say (no
    translation curves on such a bone). The root is the bone typed Root
    or named root / BoneRoot; the hips stand in only for a rig without
    one (a hips sway in an idle is not root motion)."""
    conns = list(_walk(top, "Connections"))
    if not conns:
        return None
    links = [(p[1], p[2], p[3] if len(p) > 3 else b"")
             for p, _ in _walk(conns[0][1], "C") if len(p) >= 3 and isinstance(p[1], int)]
    roots, hips = set(), set()
    for mid, (name, typ) in models.items():
        n = name.rsplit(":", 1)[-1].lower()
        if typ == "Root" or n == "root" or n.endswith(("boneroot", "_root")) \
                or n in ("reference", "motion", "trajectory"):
            roots.add(mid)
        elif n in ("hips", "pelvis", "hip", "cog"):
            hips.add(mid)
    roots = roots or hips
    if not roots:
        return None
    tnodes = {child for child, parent, prop in links
              if parent in roots and prop == b"Lcl Translation"}
    if not tnodes:
        return None
    channel: dict = {}
    for child, parent, prop in links:
        if parent in tnodes and prop in (b"d|X", b"d|Y", b"d|Z"):
            channel[child] = "XYZ".index(prop[2:3].decode())
    if not channel:
        return None
    moved = False
    for props, kids in _walk(top, "AnimationCurve"):
        cid = props[0] if props and isinstance(props[0], int) else None
        if cid not in channel or (up_i is not None and channel[cid] == up_i):
            continue
        for pr, _ in _walk(kids, "KeyValueFloat"):
            vals = pr[0] if pr and isinstance(pr[0], list) else None
            if vals and abs(vals[-1] - vals[0]) * usf > 2.0:     # net travel over 2 cm
                moved = True
    return moved


# --------------------------------------------------------------- OBJ / STL
def _measure_obj(path: Path, rec: dict) -> None:
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    verts = tris = 0
    uv = False
    mats: list = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                    except ValueError:
                        continue
                    verts += 1
                    for a, val in enumerate((x, y, z)):
                        if val < lo[a]:
                            lo[a] = val
                        if val > hi[a]:
                            hi[a] = val
            elif line.startswith("f "):
                n = len(line.split()) - 1
                tris += max(0, n - 2)
            elif line.startswith("vt"):
                uv = True
            elif line.startswith("usemtl"):
                m = line[6:].strip()
                if m and m not in mats:
                    mats.append(m)
    if not verts:
        rec["error"] = "no vertices"
        return
    rec.update(has_mesh=True, kind="mesh", triangles=tris, vertices=verts,
               uv_layers=1 if uv else 0, materials=mats[:64],
               units="unknown", up_axis="Y",       # OBJ carries no units: read as metres
               bbox_min_m=[round(x, 5) for x in lo], bbox_max_m=[round(x, 5) for x in hi],
               bbox_m=[round(hi[i] - lo[i], 5) for i in range(3)],
               pivot=pivot_class(lo, hi, 1))


def _measure_stl(path: Path, rec: dict) -> None:
    data = path.read_bytes()
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    tris = 0
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            for t in struct.iter_unpack("<12fH", data[84:84 + 50 * n]):
                for i in range(3, 12, 3):
                    for a in range(3):
                        v = t[i + a]
                        if v < lo[a]:
                            lo[a] = v
                        if v > hi[a]:
                            hi[a] = v
                tris += 1
    if not tris:
        for line in data.decode("utf-8", "replace").splitlines():
            s = line.strip()
            if s.startswith("facet"):
                tris += 1
            elif s.startswith("vertex"):
                parts = s.split()
                if len(parts) >= 4:
                    try:
                        vals = [float(parts[1]), float(parts[2]), float(parts[3])]
                    except ValueError:
                        continue
                    for a in range(3):
                        lo[a] = min(lo[a], vals[a])
                        hi[a] = max(hi[a], vals[a])
    if not tris:
        rec["error"] = "no triangles"
        return
    rec.update(has_mesh=True, kind="mesh", triangles=tris, vertices=None,
               units="unknown", up_axis="Z",
               bbox_min_m=[round(x, 5) for x in lo], bbox_max_m=[round(x, 5) for x in hi],
               bbox_m=[round(hi[i] - lo[i], 5) for i in range(3)],
               pivot=pivot_class(lo, hi, 2))


# -------------------------------------------------------------------- glTF
def _measure_gltf(path: Path, rec: dict) -> None:
    data = path.read_bytes()
    if data[:4] == b"glTF":
        if len(data) < 20:
            rec["error"] = "truncated GLB"
            return
        clen, ctype = struct.unpack_from("<II", data, 12)
        if ctype != 0x4E4F534A:
            rec["error"] = "GLB without a JSON chunk"
            return
        doc = json.loads(data[20:20 + clen].decode("utf-8", "replace"))
    else:
        doc = json.loads(data.decode("utf-8-sig", "replace"))
    acc = doc.get("accessors") or []
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    tris = verts = 0
    got = False
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            pa = (prim.get("attributes") or {}).get("POSITION")
            if isinstance(pa, int) and pa < len(acc):
                a = acc[pa]
                verts += int(a.get("count") or 0)
                mn, mx = a.get("min"), a.get("max")
                if mn and mx and len(mn) == 3 and len(mx) == 3:
                    got = True
                    for i in range(3):
                        lo[i] = min(lo[i], float(mn[i]))
                        hi[i] = max(hi[i], float(mx[i]))
            mode = prim.get("mode", 4)
            ia = prim.get("indices")
            n = (int(acc[ia].get("count") or 0) if isinstance(ia, int) and ia < len(acc)
                 else int(acc[pa].get("count") or 0) if isinstance(pa, int) and pa < len(acc) else 0)
            if mode == 4:
                tris += n // 3
            elif mode in (5, 6):
                tris += max(0, n - 2)
    rec["has_mesh"] = bool(doc.get("meshes"))
    if got:
        rec.update(triangles=tris, vertices=verts,
                   bbox_min_m=[round(x, 5) for x in lo], bbox_max_m=[round(x, 5) for x in hi],
                   bbox_m=[round(hi[i] - lo[i], 5) for i in range(3)],
                   pivot=pivot_class(lo, hi, 1))
    elif rec["has_mesh"]:
        rec.update(triangles=tris or None, vertices=verts or None)
    rec["units"], rec["unit_scale"], rec["up_axis"] = "m", 100.0, "Y"
    rec["materials"] = [str(m.get("name") or f"material_{i}")
                        for i, m in enumerate(doc.get("materials") or [])][:64]
    rec["uv_layers"] = max((sum(1 for k in (p.get("attributes") or {}) if k.startswith("TEXCOORD"))
                            for m in doc.get("meshes") or [] for p in m.get("primitives") or []),
                           default=None)
    skins = doc.get("skins") or []
    if skins:
        rec["skin"] = True
        joints = skins[0].get("joints") or []
        nodes = doc.get("nodes") or []
        names = [str(nodes[j].get("name") or f"joint_{j}") for j in joints
                 if isinstance(j, int) and j < len(nodes)]
        rec["bones"], rec["bone_names"] = len(joints), names[:400]
    clips = []
    for an in doc.get("animations") or []:
        dur = 0.0
        for s in an.get("samplers") or []:
            ia = s.get("input")
            if isinstance(ia, int) and ia < len(acc):
                mx = acc[ia].get("max")
                if mx:
                    dur = max(dur, float(mx[0]))
        clips.append({"name": str(an.get("name") or f"animation_{len(clips)}"),
                      "frames": None, "fps": None,
                      "duration_s": round(dur, 3) if dur else None, "root_motion": None})
    rec["clips"] = clips
    if clips and not rec["has_mesh"]:
        rec["kind"] = "clip"
    elif rec["has_mesh"] and rec["skin"]:
        rec["kind"] = "skeletal"
    elif rec["has_mesh"] and clips:
        rec["kind"] = "mesh+clip"
    elif rec["has_mesh"]:
        rec["kind"] = "mesh"


# --------------------------------------------------------------------- BVH
def _measure_bvh(path: Path, rec: dict) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    names = skeleton.bone_names(path)
    rec["bones"], rec["bone_names"] = len(names), names[:400]
    head, _, motion = text.partition("MOTION")
    frames = fps = None
    m = re.search(r"Frames:\s*(\d+)", motion)
    if m:
        frames = int(m.group(1))
    m = re.search(r"Frame Time:\s*([0-9.eE+-]+)", motion)
    ft = float(m.group(1)) if m else None
    if ft and ft > 0:
        fps = round(1.0 / ft, 3)
    dur = round(frames * ft, 3) if (frames and ft) else None
    root_motion = None
    m = re.search(r"ROOT\s+\S+\s*\{[^{}]*?CHANNELS\s+(\d+)((?:\s+\S+)+)", head)
    if m and frames and frames > 1:
        chans = m.group(2).split()[:int(m.group(1))]
        pos_idx = [i for i, c in enumerate(chans) if c.lower() in ("xposition", "zposition")]
        lines = [ln for ln in motion.splitlines() if ln.strip() and ln.strip()[0] in "-0123456789."]
        if pos_idx and len(lines) >= 2:
            try:
                first = [float(x) for x in lines[0].split()]
                last = [float(x) for x in lines[-1].split()]
                root_motion = any(abs(last[i] - first[i]) > 2.0 for i in pos_idx
                                  if i < len(first) and i < len(last))
            except ValueError:
                root_motion = None
    rec.update(kind="clip", units="unknown", up_axis="Y",
               clips=[{"name": path.stem, "frames": frames, "fps": fps,
                       "duration_s": dur, "root_motion": root_motion}])


# ------------------------------------------------------------------ driver
_BY_EXT = {".fbx": _measure_fbx, ".obj": _measure_obj, ".stl": _measure_stl,
           ".glb": _measure_gltf, ".gltf": _measure_gltf, ".bvh": _measure_bvh}


def measure_file(path, rel: str | None = None) -> dict:
    """The record of one file. Never raises: an unreadable file carries
    `error` and whatever its name says (LOD, collision, variant)."""
    path = Path(path)
    ext = path.suffix.lower()
    rel = (rel or path.name).replace("\\", "/")
    rec = _blank(rel, ext)
    rec["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        st = path.stat()
        rec["bytes"], rec["mtime_ns"] = st.st_size, st.st_mtime_ns
    except OSError as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec
    rec.update(name_facts(path.stem))
    rec["category_hint"] = category_hint(rel)
    fn = _BY_EXT.get(ext)
    if fn is None:
        rec["error"] = f"no reader for {ext or 'this file'}"
        return rec
    try:
        fn(path, rec)
    except (RecursionError, MemoryError) as exc:
        rec["error"] = f"{type(exc).__name__}"
    except Exception as exc:                              # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    if rec["bbox_m"]:
        big = max(rec["bbox_m"])
        rec["scale_suspect"] = bool(big > 60.0 or (0 < big < 0.01))
    if rec["skin"] and rec["has_mesh"]:
        rec["kind"] = "skeletal"
    return rec


def registry_bbox(rec: dict) -> tuple:
    """(size [x, y, z], min, max) in metres with Z up, the registry's
    convention: a Y-up file's height moves to the third slot."""
    size, lo, hi = rec.get("bbox_m"), rec.get("bbox_min_m"), rec.get("bbox_max_m")
    if not size:
        return None, None, None
    if (rec.get("geo_up_axis") or rec.get("up_axis")) == "Y":
        # (x, y, z) -> (x, -z, y): the min of y comes from the max of z
        size2 = [size[0], size[2], size[1]]
        lo2 = [lo[0], -hi[2], lo[1]] if (lo and hi) else None
        hi2 = [hi[0], -lo[2], hi[1]] if (lo and hi) else None
        return size2, lo2, hi2
    return size, lo, hi


def facts_of(rec: dict) -> dict:
    """The compact per-file facts the registry keeps beside a mesh row
    (everything an agent filters or reads, nothing it can derive)."""
    out = {}
    for k in ("units", "up_axis", "geo_up_axis", "pivot", "uv_layers", "skin", "bones",
              "lod_level", "lod_group", "collision", "variant_group",
              "category_hint", "scale_suspect", "has_mesh", "measured_by"):
        v = rec.get(k)
        if v not in (None, [], False, "unknown", "stdlib"):
            out[k] = v
    if rec.get("clips"):
        out["clips"] = len(rec["clips"])
        out["duration_s"] = max((c.get("duration_s") or 0) for c in rec["clips"]) or None
    if rec.get("bone_names"):
        out["bone_names"] = rec["bone_names"]
    if rec.get("error"):
        out["error"] = rec["error"]
    return out


# ------------------------------------------------------------- folder pass
def index_path(root: Path, folder: Path):
    """<agent files>/index/<folder>.jsonl for a folder below the root;
    None for a folder outside it (a throw-away scan keeps no cache in the
    library)."""
    from asset_service import config
    try:
        rel = Path(folder).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", rel.replace("/", "__")) or "root"
    return Path(config.AGENT_FILES) / "index" / f"{slug}.jsonl"


def load_index(path) -> dict:
    out: dict = {}
    if path is None:
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("rel"):
                    out[r["rel"]] = r
    except OSError:
        pass
    return out


def save_index(path, records: dict) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rel in sorted(records):
            fh.write(json.dumps(records[rel], ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def measure_cached(path, rel: str, cache: dict) -> dict:
    """The file's record from the cache when size and mtime still match,
    else measured now and put there."""
    rel = rel.replace("\\", "/")
    old = cache.get(rel)
    if old is not None and old.get("schema") == SCHEMA:
        try:
            st = Path(path).stat()
            if old.get("bytes") == st.st_size and old.get("mtime_ns") == st.st_mtime_ns:
                return old
        except OSError:
            pass
    rec = measure_file(path, rel)
    cache[rel] = rec
    return rec


def iter_files(folder: Path, root: Path, exts=MEASURED_EXTS, skip_converted: bool = True):
    """(path, rel) of every measurable file below `folder`; converted
    packs (Exports/manifest.json, kit_manifest.json) are the manifests'
    business and skipped; hidden folders and Finder noise too."""
    folder = Path(folder)
    converted = set()
    if skip_converted:
        for pat in ("Exports/manifest.json", "Exports/kit_manifest.json"):
            converted |= {m.parent.parent for m in folder.rglob(pat)}
    for dirpath, dirnames, filenames in os.walk(folder):
        d = Path(dirpath)
        dirnames[:] = sorted(x for x in dirnames
                             if not x.startswith((".", "_", "$"))
                             and (d / x) not in converted)
        for f in sorted(filenames):
            if f.startswith("._") or f == ".DS_Store":
                continue
            if Path(f).suffix.lower() not in exts:
                continue
            p = d / f
            try:
                rel = p.resolve().relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                rel = p.as_posix()
            yield p, rel


def measure_folder(folder, root, exts=MEASURED_EXTS, verbose: bool = False) -> tuple:
    """Every model / clip file below `folder`, through the per-folder
    cache. Returns (records, stats)."""
    folder, root = Path(folder), Path(root)
    t0 = time.perf_counter()
    ipath = index_path(root, folder)
    cache = load_index(ipath)
    seen: dict = {}
    stats = {"files": 0, "measured": 0, "cached": 0, "errors": 0,
             "index": str(ipath) if ipath else None}
    for p, rel in iter_files(folder, root, exts):
        stats["files"] += 1
        before = cache.get(rel)
        rec = measure_cached(p, rel, cache)
        stats["cached" if rec is before else "measured"] += 1
        if rec.get("error"):
            stats["errors"] += 1
            if verbose:
                print(f"  [warn] {rel}: {rec['error']}", flush=True)
        seen[rel] = rec
    # files that are gone leave the index
    save_index(ipath, seen)
    stats["seconds"] = round(time.perf_counter() - t0, 2)
    return list(seen.values()), stats


# --------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pharos measure",
                                 description="measure model and clip files with the standard library")
    ap.add_argument("paths", nargs="*", help="files to print, or folders with --clips / --index")
    ap.add_argument("--clips", action="store_true",
                    help="write the clips table for the given animation folder(s)")
    ap.add_argument("--index", action="store_true",
                    help="write the per-folder index for the given folder(s) (no registry)")
    ap.add_argument("--db", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    from asset_service import config
    if not args.paths:
        ap.print_help()
        return 2
    if args.clips or args.index:
        root = config.LIBRARY_ROOT
        rc = 0
        for p in args.paths:
            folder = Path(p)
            if not folder.is_dir():
                print(f"not a folder: {p}")
                rc = 2
                continue
            if args.clips:
                from asset_service import clips_import
                n, stats = clips_import.write_clips(args.db or config.DB_PATH, root, folder,
                                                    verbose=args.verbose)
                print(f"clips: {n} in {folder.name} "
                      f"({stats['measured']} measured, {stats['cached']} cached, "
                      f"{stats['errors']} unreadable, {stats['seconds']} s) -> {stats['index']}")
            else:
                recs, stats = measure_folder(folder, root, verbose=args.verbose)
                print(f"measured {stats['measured']} of {stats['files']} file(s), "
                      f"{stats['cached']} cached, {stats['errors']} unreadable, "
                      f"{stats['seconds']} s -> {stats['index']}")
        return rc
    for p in args.paths:
        print(json.dumps(measure_file(p), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
