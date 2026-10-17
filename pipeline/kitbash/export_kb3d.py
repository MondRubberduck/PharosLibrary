"""
Export a KitBash3D .blend to FBX, ONE FILE PER GROUP ASSEMBLY.

The kits are laid out as:  <Name>_grp  (EMPTY)  ->  N mesh submeshes (children).
Exporting per group preserves the assembly (a building imports as one object with
its submeshes intact) instead of exploding the kit into thousands of loose meshes.

Writes:
  <kit>\\Exports\\FBX\\<grp>\\<grp>.fbx
  <kit>\\Exports\\kit_manifest.json
"""
import bpy, os, json, time, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "service"))
from asset_service import blender_measure      # noqa: E402  (the one record shape)

SRC = bpy.data.filepath
KIT = os.path.splitext(os.path.basename(SRC))[0]
KIT_DIR = os.path.dirname(SRC)
# .../<Kit>/<x>.blender.native/<file>.blend  ->  put Exports beside the kit folder
while os.path.basename(KIT_DIR).endswith(".blender.native") and os.path.dirname(KIT_DIR) != KIT_DIR:
    KIT_DIR = os.path.dirname(KIT_DIR)
EXPORTS = os.path.join(KIT_DIR, "Exports")
FBX_OUT = os.path.join(EXPORTS, "FBX")
MANIFEST = os.path.join(EXPORTS, "kit_manifest.json")
os.makedirs(FBX_OUT, exist_ok=True)

scene = bpy.context.scene
roots = [o for o in scene.objects if o.parent is None]


def tree(o):
    out = [o]
    for c in o.children:
        out.extend(tree(c))
    return out


def group_stats(nodes):
    """(meshes, triangles, vertices, dims, materials, bbox_min, bbox_max)
    of an assembly, measured by blender_measure (the same corners the
    native index writes: `dims` says how big, the corners say where)."""
    rec = blender_measure.group_record(nodes)
    return (rec["meshes"], rec["triangles"], rec["vertices"], rec["bbox_m"],
            rec["materials"], rec["bbox_min_m"], rec["bbox_max_m"])


records = []
failed = []
t0 = time.time()
for r in roots:
    nodes = tree(r)
    meshes, tris, verts, dims, mats, bbox_min, bbox_max = group_stats(nodes)
    if not meshes:
        continue
    safe = r.name.replace("/", "_").replace("\\", "_")
    out_dir = os.path.join(FBX_OUT, safe)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, safe + ".fbx")

    bpy.ops.object.select_all(action='DESELECT')
    for n in nodes:
        try:
            n.select_set(True)
        except Exception:
            pass
    bpy.context.view_layer.objects.active = r

    try:
        bpy.ops.export_scene.fbx(
            filepath=path,
            use_selection=True,
            object_types={'MESH', 'EMPTY'},
            apply_unit_scale=True,
            global_scale=1.0,
            axis_forward='-Z',
            axis_up='Y',
            path_mode='AUTO',
            embed_textures=False,
            mesh_smooth_type='FACE',
            use_tspace=True,
            use_mesh_modifiers=False,
        )
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        if size == 0:
            failed.append({"group": r.name, "error": "no file written"})
            continue
        records.append({
            "group": r.name,
            "fbx": os.path.relpath(path, EXPORTS).replace("\\", "/"),
            "bytes": size,
            "submeshes": len(meshes),
            "triangles": tris,
            "vertices": verts,
            "bbox_m": dims,
            "bbox_min": bbox_min,
            "bbox_max": bbox_max,
            "materials": len(mats),
        })
    except Exception as exc:
        failed.append({"group": r.name, "error": "%s: %s" % (type(exc).__name__, exc)})

if not records:
    # a total failure must NOT leave a manifest behind: the drivers skip
    # on the manifest's existence, so an all-failed export used to hide
    # the kit FOREVER on re-runs
    print("KB3D_EXPORT_FAILED " + json.dumps(
        {"kit": KIT, "failed": len(failed), "errors": failed[:5]}))
    print("no groups exported -- kit_manifest.json NOT written")
    import sys as _sys
    _sys.exit(1)

man = {
    "schema": "pharos.kb3d.export/v1",
    "kit": KIT,
    "source_blend": SRC,
    "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "blender": bpy.app.version_string,
    "units": {"blend_unit_scale": scene.unit_settings.scale_length if hasattr(scene, "unit_settings") else None,
              "export": "metres (FBX, scale 1.0 assumed)"},
    "counts": {"groups_exported": len(records), "groups_failed": len(failed),
               "submeshes_total": sum(r["submeshes"] for r in records),
               "triangles_total": sum(r["triangles"] for r in records)},
    "groups": sorted(records, key=lambda x: x["group"]),
    "failures": failed,
}
with open(MANIFEST, "w", encoding="utf-8") as fh:
    json.dump(man, fh, indent=1, ensure_ascii=False)

print("KB3D_EXPORT " + json.dumps({"kit": KIT, "groups": len(records), "failed": len(failed),
                                   "submeshes": man["counts"]["submeshes_total"],
                                   "tris": man["counts"]["triangles_total"],
                                   "secs": round(time.time() - t0, 1)}))
