"""Index native model geometry for the agent.
Two modes:
  * batch  - AMNATIVE_LIST points at the manifest; import every file in turn.
  * blend  - a .blend was passed on the command line; index whatever loaded.
Emits one JSON line per MESH object with real-world size in metres."""
import bpy, os, json, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "service"))
from asset_service import blender_measure      # noqa: E402  (the one record shape)

LIST = os.environ.get("AMNATIVE_LIST", "")
OUT = os.environ.get("AMNATIVE_OUT", "")
SECTION = os.environ.get("AMNATIVE_SECTION", "")

fh = open(OUT, "a", encoding="utf-8")


def emit(src, section, container_kind, mtime_ns=None):
    """Record every mesh object in the current scene (blender_measure's
    record: corners, location, pivot, materials, UV layers)."""
    scale = blender_measure.scene_scale()
    n = 0
    for o in bpy.context.scene.objects:
        if o.type != 'MESH' or o.data is None:
            continue
        try:
            rec = blender_measure.object_record(o, scale, src, section, container_kind)
        except Exception as exc:
            rec = {"section": section, "source": src, "kind": container_kind,
                   "object": o.name, "error": "%s: %s" % (type(exc).__name__, exc)}
        if mtime_ns is not None:
            rec["mtime_ns"] = mtime_ns
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
    return n


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


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
            total += emit(item["rel"], item["section"], ext.lstrip("."), _mtime(p))
        except Exception as exc:
            fh.write(json.dumps({"section": item["section"], "source": item["rel"],
                                 "kind": ext.lstrip("."), "error": "%s: %s" % (type(exc).__name__, exc)},
                                ensure_ascii=False) + "\n")
    print("NATIVE_DONE importable_meshes=%d" % total)
else:
    src = bpy.data.filepath
    total = emit(src, SECTION, "blend", _mtime(src))
    print("NATIVE_DONE blend=%s meshes=%d" % (os.path.basename(src), total))

fh.close()
