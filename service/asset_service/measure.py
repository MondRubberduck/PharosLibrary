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

SCHEMA = 5                      # bump to re-measure every cached file (5: skins, instancing, scene roots)
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
# ------------------------------------------------- node transforms (4x4)
def _mat_identity() -> list:
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _mat_mul(a: list, b: list) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _mat_apply(m: list, p) -> list:
    return [m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3)]


def _mat_t(t) -> list:
    m = _mat_identity()
    m[0][3], m[1][3], m[2][3] = float(t[0]), float(t[1]), float(t[2])
    return m


def _mat_s(s) -> list:
    m = _mat_identity()
    m[0][0], m[1][1], m[2][2] = float(s[0]), float(s[1]), float(s[2])
    return m


def _mat_rot_axis(axis: str, deg: float) -> list:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    m = _mat_identity()
    if axis == "X":
        m[1][1], m[1][2], m[2][1], m[2][2] = c, -s, s, c
    elif axis == "Y":
        m[0][0], m[0][2], m[2][0], m[2][2] = c, s, -s, c
    else:
        m[0][0], m[0][1], m[1][0], m[1][1] = c, -s, s, c
    return m


_FBX_ORDERS = {0: "XYZ", 1: "XZY", 2: "YZX", 3: "YXZ", 4: "ZXY", 5: "ZYX"}


def _mat_euler(deg, order: int = 0) -> list:
    """FBX Euler angles (degrees): the first axis of the order turns first."""
    m = _mat_identity()
    for axis in _FBX_ORDERS.get(order, "XYZ"):
        m = _mat_mul(_mat_rot_axis(axis, float(deg["XYZ".index(axis)])), m)
    return m


def _mat_transpose3(m: list) -> list:
    """The inverse of a pure rotation."""
    out = _mat_identity()
    for i in range(3):
        for j in range(3):
            out[i][j] = m[j][i]
    return out


def _mat_quat(q) -> list:
    """glTF quaternion [x, y, z, w] to a rotation matrix."""
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = _mat_identity()
    m[0][0], m[0][1], m[0][2] = 1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)
    m[1][0], m[1][1], m[1][2] = 2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)
    m[2][0], m[2][1], m[2][2] = 2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)
    return m


_FBX_XFORM_KEYS = (b"Lcl Translation", b"Lcl Rotation", b"Lcl Scaling", b"PreRotation", b"PostRotation",
                   b"RotationOrder", b"RotationActive", b"RotationOffset", b"RotationPivot", b"ScalingOffset",
                   b"ScalingPivot", b"GeometricTranslation", b"GeometricRotation", b"GeometricScaling")


def _fbx_xform_props(kids) -> dict:
    """The properties that place a Model node, name -> numbers."""
    out: dict = {}
    for pr, _ in _walk(kids, "P"):
        if pr and pr[0] in _FBX_XFORM_KEYS:
            nums = [float(v) for v in pr[1:] if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if nums:
                out[pr[0].decode("latin-1")] = nums
    return out


def _fbx_node_matrix(x: dict, rotation_active: bool = False) -> list:
    """A Model node's local transform: T Roff Rp Rpre R Rpost^-1 Rp^-1 Soff Sp S Sp^-1
    (the FBX formula; pivots and offsets are zero in most exports). Like
    Blender's importer, Pre/PostRotation and the rotation order count
    only when RotationActive is set (on the node or its template)."""
    three = lambda k, d: (x.get(k) or [d, d, d])[:3]                     # noqa: E731
    active = bool((x.get("RotationActive") or [1 if rotation_active else 0])[0])
    order = int((x.get("RotationOrder") or [0])[0]) if active else 0
    m = _mat_t(three("Lcl Translation", 0.0))
    m = _mat_mul(m, _mat_t(three("RotationOffset", 0.0)))
    rp = three("RotationPivot", 0.0)
    m = _mat_mul(m, _mat_t(rp))
    if active and "PreRotation" in x:
        m = _mat_mul(m, _mat_euler(three("PreRotation", 0.0), 0))
    m = _mat_mul(m, _mat_euler(three("Lcl Rotation", 0.0), order))
    if active and "PostRotation" in x:
        m = _mat_mul(m, _mat_transpose3(_mat_euler(three("PostRotation", 0.0), 0)))
    m = _mat_mul(m, _mat_t([-v for v in rp]))
    m = _mat_mul(m, _mat_t(three("ScalingOffset", 0.0)))
    sp = three("ScalingPivot", 0.0)
    m = _mat_mul(m, _mat_t(sp))
    m = _mat_mul(m, _mat_s(three("Lcl Scaling", 1.0)))
    m = _mat_mul(m, _mat_t([-v for v in sp]))
    return m


def _fbx_geometric(x: dict) -> list:
    """The geometric offset a node applies to its own geometry only."""
    three = lambda k, d: (x.get(k) or [d, d, d])[:3]                     # noqa: E731
    m = _mat_t(three("GeometricTranslation", 0.0))
    if "GeometricRotation" in x:
        m = _mat_mul(m, _mat_euler(three("GeometricRotation", 0.0), 0))
    return _mat_mul(m, _mat_s(three("GeometricScaling", 1.0)))


def _axis_aligned(m: list) -> bool:
    """True when the 3x3 part only permutes, flips and scales the axes (a
    box stays a box through it); a turn by another angle needs the points."""
    for i in range(3):
        if sum(1 for j in range(3) if abs(m[i][j]) > 1e-6) > 1:
            return False
    return True


def _box_through(m: list, lo, hi, plo, phi, verts=None) -> None:
    """Widen (plo, phi) by the box (lo, hi) taken through m: its 8 corners
    when m keeps boxes boxes, else every vertex (a sphere turned 40 degrees
    once grew to 1.69 m through its corners)."""
    if verts is not None and not _axis_aligned(m):
        for i in range(0, len(verts) - 2, 3):
            p = _mat_apply(m, (verts[i], verts[i + 1], verts[i + 2]))
            for a in range(3):
                plo[a] = min(plo[a], p[a])
                phi[a] = max(phi[a], p[a])
        return
    for cx in (lo[0], hi[0]):
        for cy in (lo[1], hi[1]):
            for cz in (lo[2], hi[2]):
                p = _mat_apply(m, (cx, cy, cz))
                for a in range(3):
                    plo[a] = min(plo[a], p[a])
                    phi[a] = max(phi[a], p[a])


def _zero_translation(m: list) -> list:
    out = [row[:] for row in m]
    out[0][3] = out[1][3] = out[2][3] = 0.0
    return out


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
    # ---- geometry: one box per Geometry node, in its own frame
    geoms: dict = {}             # geometry id -> (lo, hi) raw
    gverts: dict = {}            # geometry id -> its flat vertex list
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    polys = idx_count = verts = 0
    got = False
    uv_max = 0
    for props, kids in _walk(top, "Geometry"):
        uv_max = max(uv_max, sum(1 for _ in _walk(kids, "LayerElementUV")))
        gid = props[0] if props and isinstance(props[0], int) else None
        gtype = _fbx_name(props[2]) if len(props) >= 3 else ""
        if gtype == "Shape":
            continue                 # blend-shape deltas: no object, no box
        for vp, _ in _walk(kids, "Vertices"):
            if not vp or not isinstance(vp[0], list) or len(vp[0]) < 3:
                continue
            v = vp[0]
            got = True
            verts += len(v) // 3
            glo = [min(v[a::3]) for a in range(3)]
            ghi = [max(v[a::3]) for a in range(3)]
            for a in range(3):
                lo[a] = min(lo[a], glo[a])
                hi[a] = max(hi[a], ghi[a])
            if gid is not None:
                old = geoms.get(gid)
                geoms[gid] = (glo, ghi) if old is None else \
                    ([min(a, b) for a, b in zip(old[0], glo)], [max(a, b) for a, b in zip(old[1], ghi)])
                gverts[gid] = v if old is None else gverts.get(gid, []) + list(v)
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
    # ---- objects: models, their transforms, the connections
    models: dict = {}            # id -> (name, type)
    xforms: dict = {}            # id -> the properties that place the node
    for props, kids in _walk(top, "Model"):
        if len(props) >= 3 and isinstance(props[0], int):
            typ = _fbx_name(props[2]) or (props[2].decode("latin-1")
                                          if isinstance(props[2], bytes) else "")
            models[props[0]] = (_fbx_name(props[1]), typ)
            xforms[props[0]] = _fbx_xform_props(kids)
    # the scene graph: a node's parent is the Model (or the root, 0) it is
    # OO-linked to -- never its cluster or display layer, which the exporter
    # lists after the parent (the last link once cut a sword off its rig);
    # a geometry can hang under several models (linked duplicates)
    parent_of: dict = {}
    geo_models: dict = {}
    skins = {props[0] for props, _k in _walk(top, "Deformer")
             if len(props) >= 3 and isinstance(props[0], int) and _fbx_name(props[2]) == "Skin"}
    skinned_geos: set = set()
    had_conns = False
    for _cp, conns_kids in _walk(top, "Connections"):
        had_conns = True
        for p, _ in _walk(conns_kids, "C"):
            if len(p) >= 3 and p[0] == b"OO" and isinstance(p[1], int) and isinstance(p[2], int):
                child, par = p[1], p[2]
                if child in geoms and par in models:
                    geo_models.setdefault(child, []).append(par)
                elif child in models and (par == 0 or par in models):
                    parent_of[child] = par
                elif child in skins and par in geoms:
                    skinned_geos.add(par)
        break
    # the template's RotationActive (Blender reads Pre/PostRotation only then)
    rot_active = False
    for _dp, dkids in _walk(top, "Definitions"):
        for p2, k2 in _walk(dkids, "ObjectType"):
            if p2 and p2[0] == b"Model":
                for pr, _ in _walk(k2, "P"):
                    if pr and pr[0] == b"RotationActive":
                        nums = [v for v in pr[1:] if isinstance(v, (int, float)) and not isinstance(v, bool)]
                        rot_active = bool(nums and nums[0])
        break
    bone_types = ("LimbNode", "Limb", "Root")
    bone_ids = {mid for mid, (_n, t) in models.items() if t in bone_types}
    mesh_ids = [mid for mid, (_n, t) in models.items() if t == "Mesh"]
    # a Null (or Root) that holds the bones becomes Blender's armature
    # object; loose bones become one too. A skinned mesh is parented under
    # that armature by the importer, wherever the exporter put it (Blender
    # writes it as a root sibling with its world transform baked)
    arm_nulls = {parent_of[b] for b in bone_ids if parent_of.get(b, 0) in models
                 and parent_of[b] not in bone_ids}
    has_rig = bool(bone_ids)
    skinned_models = {m for g in skinned_geos for m in geo_models.get(g, ())} if has_rig else set()
    # the loose objects the importer makes: every parentless model of any
    # type (a mesh beside an empty or a camera keeps its place in the
    # builder, like two meshes do) plus the armature of loose bones
    roots = [mid for mid, (_n, t) in models.items()
             if mid not in bone_ids and mid not in skinned_models and parent_of.get(mid, 0) not in models]
    loose_bones = has_rig and not arm_nulls
    rec["mesh_models"] = (len(roots) + (1 if loose_bones else 0)) if had_conns else (len(mesh_ids) or 1)
    rec["nested_mesh"] = any(parent_of.get(mid, 0) in models for mid in mesh_ids)
    # ---- the box where the builder puts the file: every geometry taken
    # through its node chain (a KitBash assembly's mesh sits under a Null
    # scaled 100 and offset: its raw box was 100 times too small and in
    # the wrong place), the one loose root's translation dropped as the
    # builder drops it (several roots keep their layout)
    placed = None
    if got and geoms and had_conns:
        single = len(roots) == 1 and not loose_bones
        plo = [float("inf")] * 3
        phi = [float("-inf")] * 3

        def _full(node, zero_root=True):
            # Blender folds a node's geometric offset into its matrix and
            # passes it on to the children; the one loose root loses its
            # whole translation column when the builder zeroes its location
            m = _mat_mul(_fbx_node_matrix(xforms.get(node, {}), rot_active), _fbx_geometric(xforms.get(node, {})))
            if zero_root and single and parent_of.get(node, 0) not in models and node not in skinned_models:
                m = _zero_translation(m)      # a skinned root sibling is no loose object: it moves with the armature
            return m

        def _chain(mid, zero_root=True):
            m = _mat_identity()
            node, depth, through = mid, 0, set()
            while node in models and depth < 64:
                through.add(node)
                m = _mat_mul(_full(node, zero_root), m)
                node, depth = parent_of.get(node, 0), depth + 1
            return m, through
        # the armature's place: what the builder's zeroing takes off a
        # skinned mesh that the importer re-parents under it
        arm_t = [0.0, 0.0, 0.0]
        if single and len(arm_nulls) == 1:
            am, _ = _chain(next(iter(arm_nulls)), zero_root=False)
            arm_t = [am[0][3], am[1][3], am[2][3]]
        for gid, (glo, ghi) in geoms.items():
            for mid in geo_models.get(gid, ()):       # no model: no object, no box
                m, through = _chain(mid)
                if mid in skinned_models and single and not (through & arm_nulls):
                    m = _mat_mul(_mat_t([-v for v in arm_t]), m)
                _box_through(m, glo, ghi, plo, phi, gverts.get(gid))
        if plo[0] < float("inf"):
            placed = (plo, phi)
    # without connections (a bare file): the old reading -- the most common
    # Mesh scale applies, and a Mesh model turned 90 degrees about X in a
    # Y-up scene keeps Z-up geometry (the pivot follows that)
    rot90 = plain = 0
    scales: list = []
    for mid in mesh_ids:
        x = xforms.get(mid, {})
        rx = (x.get("Lcl Rotation") or [0.0])[0] + (x.get("PreRotation") or [0.0])[0]
        if abs(abs(rx) - 90.0) < 1.0 or abs(abs(rx) - 270.0) < 1.0:
            rot90 += 1
        else:
            plain += 1
        sc = x.get("Lcl Scaling")
        if sc and sc[0] > 0:
            scales.append(round(sc[0], 6))
    s_model = 1.0
    if placed is None and scales:
        s_model = max(set(scales), key=scales.count)
        if len(set(scales)) > 1:
            rec["scale_mixed"] = True
    if got:
        blo, bhi = placed if placed is not None else (lo, hi)
        k = usf / 100.0 * s_model
        rec["bbox_min_m"] = [round(x * k, 5) for x in blo]
        rec["bbox_max_m"] = [round(x * k, 5) for x in bhi]
        rec["bbox_m"] = [round((bhi[i] - blo[i]) * k, 5) for i in range(3)]
        if s_model != 1.0:
            rec["model_scale"] = s_model
    # the box's vertical axis: the file's once the node turns are applied;
    # the vertex heuristic only for a file without connections
    geo_up = up_i if placed is not None else (2 if (up_i == 1 and rot90 > plain) else up_i)
    rec["geo_up_axis"] = "XYZ"[geo_up] if geo_up is not None else None
    if got and geo_up is not None:
        rec["pivot"] = pivot_class(blo, bhi, geo_up)
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
    verts = tris = objs = 0
    lead = False            # faces before the first 'o': the importer's object named after the file
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
                if not objs:
                    lead = True
            elif line.startswith("vt"):
                uv = True
            elif line.startswith("usemtl"):
                m = line[6:].strip()
                if m and m not in mats:
                    mats.append(m)
            elif line.startswith("o "):
                objs += 1                       # the importer splits objects here
    if not verts:
        rec["error"] = "no vertices"
        return
    rec.update(has_mesh=True, kind="mesh", triangles=tris, vertices=verts,
               uv_layers=1 if uv else 0, materials=mats[:64],
               units="unknown", up_axis="Y",       # OBJ carries no units: read as metres
               bbox_min_m=[round(x, 5) for x in lo], bbox_max_m=[round(x, 5) for x in hi],
               bbox_m=[round(hi[i] - lo[i], 5) for i in range(3)],
               pivot=pivot_class(lo, hi, 1), mesh_models=max(objs + (1 if lead else 0), 1))


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
    mesh_boxes: dict = {}          # mesh index -> (lo, hi) in the mesh's frame
    for mi, mesh in enumerate(doc.get("meshes") or []):
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
                    old = mesh_boxes.get(mi)
                    mesh_boxes[mi] = ([float(v) for v in mn], [float(v) for v in mx]) if old is None else \
                        ([min(o, float(v)) for o, v in zip(old[0], mn)], [max(o, float(v)) for o, v in zip(old[1], mx)])
            mode = prim.get("mode", 4)
            ia = prim.get("indices")
            n = (int(acc[ia].get("count") or 0) if isinstance(ia, int) and ia < len(acc)
                 else int(acc[pa].get("count") or 0) if isinstance(pa, int) and pa < len(acc) else 0)
            if mode == 4:
                tris += n // 3
            elif mode in (5, 6):
                tris += max(0, n - 2)
    rec["has_mesh"] = bool(doc.get("meshes"))
    nodes = doc.get("nodes") or []
    mesh_nodes = [i for i, n in enumerate(nodes) if isinstance(n, dict) and "mesh" in n]
    kids = {c for n in nodes if isinstance(n, dict) for c in (n.get("children") or []) if isinstance(c, int)}
    parent = {c: i for i, n in enumerate(nodes) if isinstance(n, dict)
              for c in (n.get("children") or []) if isinstance(c, int)}
    # the loose objects the importer shows: the DEFAULT scene's root nodes
    # of any type (an empty beside the mesh counts, as in the builder);
    # other scenes and orphan nodes land in excluded collections and the
    # builder leaves them out
    scenes = [sc for sc in (doc.get("scenes") or []) if isinstance(sc, dict)]
    default = doc.get("scene") if isinstance(doc.get("scene"), int) else 0
    scene_roots = [r for r in ((scenes[default] if 0 <= default < len(scenes) else {}).get("nodes") or [])
                   if isinstance(r, int) and r < len(nodes)]
    roots = scene_roots or [i for i in range(len(nodes)) if i not in kids]
    shown: set = set()
    stack = list(roots)
    while stack:
        i = stack.pop()
        if i in shown or not isinstance(nodes[i], dict):
            continue
        shown.add(i)
        stack.extend(c for c in (nodes[i].get("children") or []) if isinstance(c, int) and c < len(nodes))
    mesh_nodes = [i for i in mesh_nodes if i in shown]
    rec["mesh_models"] = len(roots) or len(mesh_nodes) or len(doc.get("meshes") or [])
    rec["nested_mesh"] = any(i in kids for i in mesh_nodes)

    def node_matrix(n: dict, drop_translation: bool) -> list:
        if isinstance(n.get("matrix"), list) and len(n["matrix"]) == 16:
            c = n["matrix"]                      # column-major
            m = [[float(c[j * 4 + i]) for j in range(4)] for i in range(4)]
            if drop_translation:
                m[0][3] = m[1][3] = m[2][3] = 0.0
            return m
        t = [0.0, 0.0, 0.0] if drop_translation else [float(v) for v in (n.get("translation") or [0, 0, 0])]
        m = _mat_mul(_mat_t(t), _mat_quat(n.get("rotation") or [0, 0, 0, 1]))
        return _mat_mul(m, _mat_s([float(v) for v in (n.get("scale") or [1, 1, 1])]))
    # the box where the builder puts the file: each mesh node through its
    # chain, the one loose root's translation dropped
    placed = None
    if got and mesh_nodes:
        single = len(roots) == 1
        plo = [float("inf")] * 3
        phi = [float("-inf")] * 3
        for i in mesh_nodes:
            box = mesh_boxes.get(nodes[i].get("mesh"))
            if box is None:
                continue
            if "skin" in nodes[i]:
                # a skinned mesh is placed by its joints, not by its node
                # chain: its vertices are written in bind (world) space; the
                # importer parents it under the armature node, whose
                # translation the builder zeroes when it is the one loose object
                m = _mat_identity()
                arm = parent.get(i)
                if single and arm is not None:
                    am = _mat_identity()
                    node, depth = arm, 0
                    while node is not None and depth < 64:
                        am = _mat_mul(node_matrix(nodes[node], False), am)
                        node, depth = parent.get(node), depth + 1
                    m = _mat_t([-am[0][3], -am[1][3], -am[2][3]])
                _box_through(m, box[0], box[1], plo, phi)
                continue
            m = _mat_identity()
            node, depth = i, 0
            while node is not None and depth < 64:
                is_root = node not in parent
                m = _mat_mul(node_matrix(nodes[node], is_root and single), m)
                node, depth = parent.get(node), depth + 1
            _box_through(m, box[0], box[1], plo, phi)
        if plo[0] < float("inf"):
            placed = (plo, phi)
    if got:
        blo, bhi = placed if placed is not None else (lo, hi)
        rec.update(triangles=tris, vertices=verts,
                   bbox_min_m=[round(x, 5) for x in blo], bbox_max_m=[round(x, 5) for x in bhi],
                   bbox_m=[round(bhi[i] - blo[i], 5) for i in range(3)],
                   pivot=pivot_class(blo, bhi, 1))
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
_READERS = {".fbx": _measure_fbx, ".obj": _measure_obj, ".stl": _measure_stl,
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
    fn = _READERS.get(ext)
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
              "category_hint", "scale_suspect", "has_mesh", "measured_by",
              "mesh_models", "nested_mesh"):
        v = rec.get(k)
        if v not in (None, [], False, "unknown", "stdlib"):
            out[k] = v
    if out.get("mesh_models") == 1:
        out.pop("mesh_models")          # the common case is implied
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
                from asset_service import clips_import, db
                dbp = args.db or config.DB_PATH
                owner = db.registry_foreign(dbp, root)
                if owner:          # a registry of another library: never written
                    print(db.foreign_registry_message(dbp, owner, root))
                    return 2
                n, stats = clips_import.write_clips(dbp, root, folder, verbose=args.verbose)
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
