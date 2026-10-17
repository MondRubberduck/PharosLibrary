"""Measuring inside Blender: the one record shape every Blender-side
script writes (the native index of .blend files and multi-object
containers, the KitBash3D kit export). Importable only under Blender's
Python (bpy); nothing here runs in the server.

    object_record(o, scale, source, section, kind)  one mesh object
    group_record(objects, scale)                    an assembly of objects

Both carry world-space corners in metres (`bbox_min_m` / `bbox_max_m`),
the object's location, the size (`bbox_m`), triangles, vertices,
materials, UV layers, the pivot class of the object's origin in its own
box, and `measured_by: "blender"`, so the stdlib pass (measure.py) and
the Blender pass are interchangeable rows for the importer.
"""

import bpy      # noqa: F401  (Blender only)
import mathutils

_PIVOT_TOL = 0.05


def scene_scale() -> float:
    try:
        return float(bpy.context.scene.unit_settings.scale_length) or 1.0
    except Exception:                                     # noqa: BLE001
        return 1.0


def pivot_class(lo, hi, origin, up=2):
    """Where `origin` sits in the box [lo, hi]: base-centre, corner,
    centre, top, outside, other (the same classes as measure.py)."""
    if lo is None or hi is None:
        return None
    size = [hi[i] - lo[i] for i in range(3)]
    if max(size) <= 0:
        return None
    tol = max(size) * _PIVOT_TOL
    pos = []
    for i in range(3):
        if size[i] <= tol:
            # a flat axis: centred when the origin lies in the slab
            if lo[i] - tol <= origin[i] <= hi[i] + tol:
                pos.append(0.5)
                continue
            return "outside"
        pos.append((origin[i] - lo[i]) / size[i])
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


def _corners(objects, scale):
    lo = [1e18] * 3
    hi = [-1e18] * 3
    any_ = False
    for o in objects:
        try:
            for c in o.bound_box:
                w = o.matrix_world @ mathutils.Vector(c)
                for i in range(3):
                    lo[i] = min(lo[i], w[i] * scale)
                    hi[i] = max(hi[i], w[i] * scale)
                any_ = True
        except Exception:                                 # noqa: BLE001
            continue
    if not any_:
        return None, None
    return [round(x, 4) for x in lo], [round(x, 4) for x in hi]


def _tris(me) -> int | None:
    try:
        return sum(max(0, len(p.vertices) - 2) for p in me.polygons)
    except Exception:                                     # noqa: BLE001
        return None


def object_record(o, scale=None, source="", section="", kind="blend") -> dict:
    """One MESH object: the shared record shape (see module docstring)."""
    scale = scene_scale() if scale is None else scale
    me = o.data
    lo, hi = _corners([o], scale)
    dims = [round(hi[i] - lo[i], 4) for i in range(3)] if lo else None
    loc = [round(v * scale, 4) for v in o.matrix_world.translation]
    return {
        "section": section, "source": source, "kind": kind,
        "object": o.name,
        "collection": (o.users_collection[0].name if o.users_collection else None),
        "vertices": len(me.vertices), "triangles": _tris(me),
        "bbox_m": dims, "max_dim_m": (round(max(dims), 4) if dims else None),
        "bbox_min_m": lo, "bbox_max_m": hi, "location_m": loc,
        "materials": [s.material.name for s in o.material_slots if s.material],
        "uv_layers": len(me.uv_layers), "units": "m", "up_axis": "Z",
        "pivot": pivot_class(lo, hi, loc, 2) if lo else None,
        "measured_by": "blender",
    }


def group_record(objects, scale=None) -> dict:
    """An assembly (a kit group): totals and the world box of all its
    mesh objects."""
    scale = scene_scale() if scale is None else scale
    meshes = [o for o in objects if o.type == "MESH" and o.data]
    tris = verts = 0
    for m in meshes:
        verts += len(m.data.vertices)
        tris += _tris(m.data) or 0
    lo, hi = _corners(meshes, scale)
    dims = [round(hi[i] - lo[i], 4) for i in range(3)] if lo else None
    mats = sorted({s.material.name for m in meshes for s in m.material_slots if s.material})
    return {"meshes": meshes, "triangles": tris, "vertices": verts, "bbox_m": dims,
            "materials": mats, "bbox_min_m": lo, "bbox_max_m": hi,
            "uv_layers": max((len(m.data.uv_layers) for m in meshes), default=0),
            "units": "m", "up_axis": "Z", "measured_by": "blender"}
