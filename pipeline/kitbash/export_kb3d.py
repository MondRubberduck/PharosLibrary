"""
Export a KitBash3D .blend to FBX, ONE FILE PER GROUP ASSEMBLY.

The kits are laid out as:  <Name>_grp  (EMPTY)  ->  N mesh submeshes (children).
Exporting per group preserves the assembly (a building imports as one object with
its submeshes intact) instead of exploding the kit into thousands of loose meshes.

Writes:
  <kit>\\Exports\\FBX\\<grp>\\<grp>.fbx
  <kit>\\Exports\\kit_manifest.json
"""
import bpy, os, json, time, mathutils

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
    meshes = [n for n in nodes if n.type == 'MESH' and n.data]
    tris = verts = 0
    for m in meshes:
        verts += len(m.data.vertices)
        try:
            tris += sum(max(0, len(p.vertices) - 2) for p in m.data.polygons)
        except Exception:
            pass
    lo = [1e18] * 3
    hi = [-1e18] * 3
    for m in meshes:
        for c in m.bound_box:
            w = m.matrix_world @ mathutils.Vector(c)
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
    dims = [round(hi[i] - lo[i], 4) for i in range(3)] if meshes else None
    mats = sorted({s.material.name for m in meshes for s in m.material_slots if s.material})
    # world-space AABB corners (metres): `dims` says how big the assembly is, the
    # corners say where it sits, which is what snapping to a ground plane needs.
    bbox_min = [round(lo[i], 4) for i in range(3)] if meshes else None
    bbox_max = [round(hi[i], 4) for i in range(3)] if meshes else None
    return meshes, tris, verts, dims, mats, bbox_min, bbox_max


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
