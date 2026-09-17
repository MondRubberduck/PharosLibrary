import bpy, os, glob, random, json, sys

OUT = r"D:\Pipeline\kiosk\exports\AlbertMansion_5.0\FBX"
files = sorted(glob.glob(os.path.join(OUT, "**", "*.fbx"), recursive=True))
print("TOTAL_FBX_FOUND=%d" % len(files))

random.seed(20260915)
pick = random.sample(files, 5)
report = []
for p in pick:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=p)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    verts = sum(len(o.data.vertices) for o in meshes)
    tris = sum(sum(max(0, len(poly.vertices) - 2) for poly in o.data.polygons) for o in meshes)
    slots = sum(len(o.material_slots) for o in meshes)
    named_mats = set()
    for o in meshes:
        for s in o.material_slots:
            if s.material:
                named_mats.add(s.material.name)
    bones = sum(len(a.data.bones) for a in arms)
    dims = [0.0, 0.0, 0.0]
    if meshes:
        import mathutils
        mn = [1e9] * 3
        mx = [-1e9] * 3
        for o in meshes:
            for c in o.bound_box:
                w = o.matrix_world @ mathutils.Vector(c)
                for i in range(3):
                    mn[i] = min(mn[i], w[i])
                    mx[i] = max(mx[i], w[i])
        dims = [round(mx[i] - mn[i], 4) for i in range(3)]
    report.append({
        "file": os.path.relpath(p, OUT),
        "meshes": len(meshes),
        "verts": verts,
        "tris": tris,
        "mat_slots": slots,
        "unique_material_names": len(named_mats),
        "armatures": len(arms),
        "bones": bones,
        "bbox_m": dims,
    })

print("RESULT_JSON=" + json.dumps(report, indent=1))
