"""Verify an exported group FBX kept its assembly: parent + submeshes."""
import bpy, os, json, sys

src = os.environ.get("KB3D_VERIFY", "")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=src)

objs = list(bpy.context.scene.objects)
meshes = [o for o in objs if o.type == 'MESH']
empties = [o for o in objs if o.type == 'EMPTY']
roots = [o for o in objs if o.parent is None]
tris = 0
for m in meshes:
    try:
        tris += sum(max(0, len(p.vertices) - 2) for p in m.data.polygons)
    except Exception:
        pass

def tree(o, d=0, acc=None):
    acc = acc if acc is not None else []
    acc.append("  " * d + "%s [%s]" % (o.name, o.type))
    for c in o.children:
        tree(c, d + 1, acc)
    return acc

lines = []
for r in roots:
    lines.extend(tree(r))

print("KB3D_VERIFY " + json.dumps({
    "file": os.path.basename(src),
    "objects": len(objs), "meshes": len(meshes), "empties": len(empties),
    "roots": len(roots), "triangles": tris,
    "root_names": [r.name for r in roots],
    "mesh_names_sample": sorted(m.name for m in meshes)[:8],
}))
print("KB3D_TREE")
for l in lines[:14]:
    print("   " + l)
print("   ... total tree lines: %d" % len(lines))
