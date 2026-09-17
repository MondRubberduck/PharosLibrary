"""
blender_inspect_fbx.py -- round-trip proof for exported FBX files.

Run headless, e.g.:
  blender.exe --background --factory-startup --python blender_inspect_fbx.py -- file1.fbx file2.fbx ...

For every file it clears the scene, imports the FBX, and reports:
  object count, mesh count, vertex count, triangle count,
  world-space bounding-box dimensions (Blender units == metres for UE-exported FBX),
  material count and the FBX unit scale factor read from the file.

Prints a JSON document between AMCONV_JSON_BEGIN / AMCONV_JSON_END markers.
"""

import json
import sys
import traceback

import bpy
from mathutils import Vector

SEP = "--"


def parse_args():
    argv = sys.argv
    if SEP in argv:
        return argv[argv.index(SEP) + 1:]
    return []


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_fbx(path):
    """Import via whichever operator this Blender exposes."""
    if hasattr(bpy.ops.import_scene, "fbx"):
        return bpy.ops.import_scene.fbx(filepath=path)
    if hasattr(bpy.ops.wm, "fbx_import"):
        return bpy.ops.wm.fbx_import(filepath=path)
    raise RuntimeError("no FBX importer available in this Blender build")


def world_bbox(objs):
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    found = False
    for o in objs:
        if o.type != "MESH" or not o.bound_box:
            continue
        for corner in o.bound_box:
            w = o.matrix_world @ Vector(corner)
            found = True
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
    if not found:
        return None
    return {"min": [round(v, 4) for v in lo],
            "max": [round(v, 4) for v in hi],
            "dims_m": [round(hi[i] - lo[i], 4) for i in range(3)]}


def inspect(path):
    rec = {"file": path}
    try:
        clear_scene()
        import_fbx(path)
    except Exception as exc:
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        rec["traceback"] = traceback.format_exc()
        return rec

    scene = bpy.context.scene
    all_objs = list(scene.objects)
    mesh_objs = [o for o in all_objs if o.type == "MESH"]
    arm_objs = [o for o in all_objs if o.type == "ARMATURE"]

    verts = 0
    tris = 0
    mats = set()
    for o in mesh_objs:
        me = o.data
        verts += len(me.vertices)
        me.calc_loop_triangles()
        tris += len(me.loop_triangles)
        for slot in o.material_slots:
            if slot.material:
                mats.add(slot.material.name)

    rec.update({
        "objects_total": len(all_objs),
        "objects_mesh": len(mesh_objs),
        "objects_armature": len(arm_objs),
        "object_names": [o.name for o in all_objs][:12],
        "vertices": verts,
        "triangles": tris,
        "materials": len(mats),
        "bbox": world_bbox(mesh_objs),
    })
    # SKM exports should carry a skeleton; report bone count when present.
    if arm_objs:
        rec["bones"] = sum(len(a.data.bones) for a in arm_objs)
    return rec


def main():
    files = parse_args()
    out = {
        "blender_version": bpy.app.version_string,
        "scene_unit_scale": bpy.context.scene.unit_settings.scale_length,
        "scene_unit_system": bpy.context.scene.unit_settings.system,
        "count": len(files),
        "results": [],
    }
    for f in files:
        print("[amconv-blender] inspecting %s" % f)
        r = inspect(f)
        print("[amconv-blender] -> %s" % json.dumps(r))
        out["results"].append(r)

    print("AMCONV_JSON_BEGIN")
    print(json.dumps(out, indent=2))
    print("AMCONV_JSON_END")


main()
