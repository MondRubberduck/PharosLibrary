"""Index native model geometry for the agent.
Two modes:
  * batch  - AMNATIVE_LIST points at the manifest; import every file in turn.
  * blend  - a .blend was passed on the command line; index whatever loaded.
Emits one JSON line per MESH object with real-world size in metres."""
import bpy, os, json, sys, mathutils

LIST = os.environ.get("AMNATIVE_LIST", "")
OUT = os.environ.get("AMNATIVE_OUT", "")
LABEL = os.environ.get("AMNATIVE_LABEL", "")
SECTION = os.environ.get("AMNATIVE_SECTION", "")

fh = open(OUT, "a", encoding="utf-8")


def scene_scale():
    try:
        return float(bpy.context.scene.unit_settings.scale_length) or 1.0
    except Exception:
        return 1.0


def emit(src, section, container_kind):
    """Record every mesh object in the current scene."""
    scale = scene_scale()
    n = 0
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or o.data is None:
            continue
        try:
            corners = [o.matrix_world @ mathutils.Vector(c) for c in o.bound_box]
            mn = [min(c[i] for c in corners) for i in range(3)]
            mx = [max(c[i] for c in corners) for i in range(3)]
            dims = [round((mx[i] - mn[i]) * scale, 4) for i in range(3)]
        except Exception:
            mn = mx = dims = None
        me = o.data
        try:
            tris = sum(max(0, len(p.vertices) - 2) for p in me.polygons)
        except Exception:
            tris = None
        mats = [s.material.name for s in o.material_slots if s.material]
        rec = {
            "section": section,
            "source": src,
            "kind": container_kind,
            "object": o.name,
            "collection": (o.users_collection[0].name if o.users_collection else None),
            "vertices": len(me.vertices),
            "triangles": tris,
            "bbox_m": dims,
            "max_dim_m": (round(max(dims), 4) if dims else None),
            "materials": mats,
        }
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
    return n


total = 0
if LIST:
    man = json.load(open(LIST, encoding="utf-8"))
    for item in man["importable"]:
        p, ext = item["path"], item["ext"]
        if ext == ".usd" and item["bytes"] < 20000:
            continue  # material / payload stubs, not geometry
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            if ext == ".fbx":
                bpy.ops.import_scene.fbx(filepath=p)
            elif ext == ".obj":
                bpy.ops.wm.obj_import(filepath=p)
            else:
                bpy.ops.wm.usd_import(filepath=p)
            total += emit(item["rel"], item["section"], ext.lstrip("."))
        except Exception as exc:
            fh.write(json.dumps({"section": item["section"], "source": item["rel"],
                                 "kind": ext.lstrip("."), "error": "%s: %s" % (type(exc).__name__, exc)},
                                ensure_ascii=False) + "\n")
    print("NATIVE_DONE importable_meshes=%d" % total)
else:
    src = bpy.data.filepath
    total = emit(src, SECTION, "blend")
    print("NATIVE_DONE blend=%s meshes=%d" % (os.path.basename(src), total))

fh.close()
