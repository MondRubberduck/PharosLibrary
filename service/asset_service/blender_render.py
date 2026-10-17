"""Render one tile per model inside Blender (headless): the model fitted
into a unit box, a 3/4 orthographic view, studio light, its key as a
label under it. Workbench, 256 px, BMP out (the caller turns the tiles
into PNGs and sheets with the standard library).

    blender --background --factory-startup --python blender_render.py -- job.json

job.json: {"items": [{"key", "path", "object" | null, "label", "out"}],
           "size": 256, "result": "<path to write {key: {ok, file|error}}>"}
A failing file fails its tile only; the run goes on.
"""

import json
import math
import os
import sys
import traceback

import re

import bpy
import mathutils

_VIEW = mathutils.Vector((1.0, -1.0, 0.75)).normalized()     # 3/4 from the front-right, above
_HELPER_RE = re.compile(r"(?:^|[^a-z0-9])(?:ucx|ubx|ucp|usp|collider|collision|impost[eo]r)"
                        r"(?:[^a-z0-9]|$)", re.I)


def _bake_world(o):
    """Detach `o` from its parent and keep its world transform: the
    transform is evaluated first (a freshly linked or re-parented object
    reads a stale matrix_world until the depsgraph ran)."""
    bpy.context.view_layer.update()
    world = o.matrix_world.copy()
    o.parent = None
    o.matrix_world = world


def _scene_objects():
    return set(bpy.context.scene.objects)


def import_file(path, obj_name=None):
    """Import one model file; returns the objects it added."""
    suffix = os.path.splitext(path)[1].lower()
    before = _scene_objects()
    if suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, global_scale=1.0, use_manual_orientation=False)
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif suffix == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif suffix in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif suffix == ".blend":
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            names = ([obj_name] if obj_name and obj_name in src.objects
                     else ([] if obj_name else list(src.objects)))
            dst.objects = names
        for o in dst.objects:
            if o is not None and o.type not in ("CAMERA", "LIGHT"):
                bpy.context.scene.collection.objects.link(o)
    else:
        raise RuntimeError("no importer for %s" % suffix)
    bpy.context.view_layer.update()          # matrix_world of what was linked
    added = [o for o in bpy.context.scene.objects if o not in before]
    if obj_name and suffix != ".blend":
        # a multi-object container: keep the one object (and its children)
        keep = next((o for o in added if o.name == obj_name), None) \
            or next((o for o in added if o.name.lower() == obj_name.lower()), None) \
            or next((o for o in added if re.sub(r"\.\d{3}$", "", o.name) == obj_name), None)
        if keep is None and len(added) > 1:
            raise RuntimeError("object %r not in %s (it holds e.g. %s)" % (
                obj_name, os.path.basename(path), sorted(o.name for o in added)[:8]))
        if keep is not None:
            if keep.parent is not None:
                _bake_world(keep)          # the root's transform stays with it
            wanted = set()
            stack = [keep]
            while stack:
                o = stack.pop()
                wanted.add(o)
                stack.extend(o.children)
            for o in added:
                if o not in wanted:
                    bpy.data.objects.remove(o, do_unlink=True)
            added = [o for o in added if o in wanted]
    elif not obj_name:
        # a whole file: its collision hulls and impostor cards are not
        # the model (when anything else is left)
        helpers = [o for o in added if o.type == "MESH" and _HELPER_RE.search(o.name)]
        if helpers and any(o.type == "MESH" for o in added if o not in helpers):
            for o in helpers:
                bpy.data.objects.remove(o, do_unlink=True)
            added = [o for o in added if o not in helpers]
    bpy.context.view_layer.update()
    return added


def _world_bbox(objs):
    lo = mathutils.Vector((1e18, 1e18, 1e18))
    hi = mathutils.Vector((-1e18, -1e18, -1e18))
    any_ = False
    for o in objs:
        if o.type != "MESH" or not o.data or not o.data.vertices:
            continue
        for c in o.bound_box:
            w = o.matrix_world @ mathutils.Vector(c)
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
            any_ = True
    if not any_:
        raise RuntimeError("no mesh geometry")
    return lo, hi


def _fit(objs):
    """Parent the imported roots to an empty and scale/move that empty so
    the model's box is a unit box centred on the origin, standing on z=0."""
    lo, hi = _world_bbox(objs)
    size = max(hi - lo)
    if size <= 0:
        raise RuntimeError("zero-size geometry")
    centre = (lo + hi) / 2.0
    empty = bpy.data.objects.new("fit", None)
    bpy.context.scene.collection.objects.link(empty)
    for o in objs:
        if o.parent is None or o.parent not in objs:
            if o.parent is not None:
                _bake_world(o)             # a parent outside the set: keep its effect
            o.parent = empty
            o.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    s = 1.0 / size
    empty.scale = (s, s, s)
    empty.location = (-centre.x * s, -centre.y * s, -lo.z * s)
    return (hi - lo) * s


def _camera(scene):
    cam = bpy.data.cameras.new("tile_cam")
    cam.type = "ORTHO"
    cam.ortho_scale = 2.25
    cam.clip_start = 0.01
    cam.clip_end = 100.0
    obj = bpy.data.objects.new("tile_cam", cam)
    scene.collection.objects.link(obj)
    obj.location = _VIEW * 10.0 + mathutils.Vector((0.0, 0.0, 0.5))
    obj.rotation_euler = _VIEW.to_track_quat("Z", "Y").to_euler()
    scene.camera = obj
    return obj


def _label(scene, cam, text):
    curve = bpy.data.curves.new("tile_label", type="FONT")
    curve.body = text
    curve.align_x = "CENTER"
    curve.align_y = "TOP"
    curve.size = 0.12
    obj = bpy.data.objects.new("tile_label", curve)
    scene.collection.objects.link(obj)
    mat = bpy.data.materials.new("tile_label_mat")
    mat.diffuse_color = (0.92, 0.92, 0.95, 1.0)
    obj.data.materials.append(mat)
    # in the camera's plane, just below the unit box
    rot = cam.rotation_euler.to_matrix()
    up = rot @ mathutils.Vector((0.0, 1.0, 0.0))
    towards = rot @ mathutils.Vector((0.0, 0.0, 1.0))      # out of the screen
    obj.rotation_euler = cam.rotation_euler
    obj.location = mathutils.Vector((0.0, 0.0, 0.5)) - up * 0.98 + towards * 2.0
    return obj


def render_tile(item, size):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    objs = import_file(item["path"], item.get("object"))
    dims = _fit(objs)
    # an image texture whose file is missing paints the model magenta in
    # Workbench: without the node the material's own colour shows
    for mat in bpy.data.materials:
        if mat.use_nodes and mat.node_tree:
            for n in list(mat.node_tree.nodes):
                if n.type == "TEX_IMAGE" and (n.image is None or not n.image.has_data):
                    mat.node_tree.nodes.remove(n)
    cam = _camera(scene)
    _label(scene, cam, item.get("label") or item["key"])
    world = bpy.data.worlds.new("tile_world")
    world.color = (0.11, 0.115, 0.13)
    scene.world = world
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "TEXTURE"
    sh.show_shadows = True
    sh.show_cavity = True
    sh.background_type = "WORLD"
    scene.render.resolution_x = scene.render.resolution_y = int(size)
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "BMP"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = item["out"]
    bpy.ops.render.render(write_still=True)
    return {"ok": True, "file": item["out"],
            "dims": [round(float(d), 4) for d in dims], "objects": len(objs)}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender --background --factory-startup --python blender_render.py -- job.json")
        return 2
    job = json.load(open(argv[0], encoding="utf-8"))
    size = int(job.get("size") or 256)
    results = {}

    def flush():
        with open(job["result"], "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
    for item in job.get("items") or []:
        try:
            results[item["key"]] = render_tile(item, size)
        except Exception as exc:                          # noqa: BLE001
            results[item["key"]] = {"ok": False,
                                    "error": ("%s: %s" % (type(exc).__name__, exc))[:240]}
            traceback.print_exc()
        flush()                     # a crash on a later file keeps these
    flush()
    ok = sum(1 for r in results.values() if r.get("ok"))
    print("RENDER_DONE ok=%d failed=%d" % (ok, len(results) - ok))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    if code:
        sys.exit(code)
