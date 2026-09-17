"""
Populate materials + texture files for KitBash3D group assemblies.

READ-ONLY on the .blend files; no FBX is re-exported.

Why the fallback is needed: in the newer KitBash3D packaging the .blend points at
  <kit>\\kb3d_<kit>.blender.native\\KB3DTextures\\4k\\*.png
but that 4k folder is EMPTY - the delivered textures live in a sibling
  <kit>\\kb3d_<kit>.png.2k\\*.png
So a texture is resolved by (1) the .blend's own relative path, then (2) a
basename index of every image under the kit folder.
"""
import bpy, os, json, sys, time

SRC = bpy.data.filepath
KIT_DIR = os.path.dirname(SRC)
while os.path.basename(KIT_DIR).endswith(".blender.native"):
    KIT_DIR = os.path.dirname(KIT_DIR)
MAN = os.path.join(KIT_DIR, "Exports", "kit_manifest.json")
if not os.path.isfile(MAN):
    print("KB3D_META skip (no manifest): %s" % os.path.basename(SRC))
    sys.exit(0)

IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".tga"}
INDEX = {}
for dp, dn, fn in os.walk(KIT_DIR):
    for f in fn:
        if os.path.splitext(f)[1].lower() in IMG_EXT:
            INDEX.setdefault(f.lower(), os.path.join(dp, f))
print("KB3D_META image index: %d files under %s" % (len(INDEX), os.path.basename(KIT_DIR)))

man = json.load(open(MAN, encoding="utf-8"))
by_name = {g["group"]: g for g in man.get("groups") or []}


def tree(o):
    out = [o]
    for c in o.children:
        out.extend(tree(c))
    return out


def resolve(rel):
    if not rel:
        return None
    ap = bpy.path.abspath(rel)
    if ap and os.path.isfile(ap):
        return ap
    return INDEX.get(os.path.basename(rel.replace("\\", "/")).lower())


scene = bpy.context.scene
updated = with_tex = via_fallback = 0
total_tex = 0
for r in [o for o in scene.objects if o.parent is None]:
    g = by_name.get(r.name)
    if g is None:
        continue
    meshes = [n for n in tree(r) if n.type == 'MESH' and n.data]
    if not meshes:
        continue

    mats, imgs = set(), set()
    for m in meshes:
        for s in m.material_slots:
            mat = s.material
            if not mat:
                continue
            mats.add(mat.name)
            try:
                if mat.use_nodes and mat.node_tree:
                    for n in mat.node_tree.nodes:
                        if n.type == 'TEX_IMAGE' and n.image and n.image.filepath:
                            got = resolve(n.image.filepath)
                            if got:
                                imgs.add(got)
                                if not os.path.isfile(bpy.path.abspath(n.image.filepath)):
                                    via_fallback += 1
            except Exception:
                pass

    g["materials"] = sorted(mats)
    g["material_count"] = len(mats)
    g["texture_files"] = sorted(imgs)
    g["texture_count"] = len(imgs)
    updated += 1
    if imgs:
        with_tex += 1
    total_tex += len(imgs)

man["materials_pass"] = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "groups_updated": updated,
    "groups_with_textures": with_tex,
    "texture_files_total": total_tex,
    "resolved_via_basename_fallback": via_fallback,
    "note": ("4k folder referenced by the .blend was empty for this kit; textures were "
             "resolved from the sibling png.2k folder by basename"
             if via_fallback else "all texture paths resolved directly"),
}
json.dump(man, open(MAN, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("KB3D_META " + json.dumps({"kit": os.path.basename(KIT_DIR), "groups": updated,
                                 "with_textures": with_tex, "texfiles": total_tex,
                                 "fallback_hits": via_fallback}))
