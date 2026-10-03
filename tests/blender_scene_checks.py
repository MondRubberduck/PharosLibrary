"""scene_builder regression checks that need Blender (plan W0.3).

Runs INSIDE Blender, never under plain Python:

    blender --background --factory-startup --python-exit-code 1 \
        --python tests/blender_scene_checks.py

(`tests/test_pack_verify.py::test_scene_builder_in_blender` runs exactly
this; CI runs it in the `blender` job.)

Every fixture is generated here: primitives exported with
bpy.ops.export_scene.fbx, 1x1 PNGs from test_pack_verify._png(). No
library assets. Each check builds through the real scene_builder.build(),
SAVES the .blend, REOPENS it headless and asserts on the reopened file.
Any failed check raises at the end, so --python-exit-code 1 makes the
Blender process exit non-zero.
"""

import json
import math
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
from mathutils import Euler, Matrix, Vector

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "tests"))

from test_pack_verify import _png  # noqa: E402  (shared PNG fixture)
from asset_service import config as _config  # noqa: E402
from asset_service import scene_builder  # noqa: E402

# hermetic: the builder's KitBash remap pass globs the configured library
# roots; the fixtures live in temp dirs, so it gets no roots at all
_config.load = lambda: {}


def _export_cube(path: Path, name: str) -> None:
    """A cube primitive named `name`, exported alone to an FBX file."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    bpy.context.active_object.name = name
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True)


def _build_and_reopen(tmp: Path, name: str, manifest: dict) -> None:
    """Build via the real builder, save, then reopen the saved .blend."""
    mpath = tmp / f"{name}.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    blend = tmp / f"{name}.blend"
    scene_builder.build(str(mpath), str(blend))
    assert blend.is_file(), f"builder saved no .blend at {blend}"
    bpy.ops.wm.open_mainfile(filepath=str(blend))


def _images(obj) -> set:
    """Basenames of every image wired into obj's materials."""
    # bpy.path.basename: a '//x/y.png' blend-relative path is UNC to pathlib
    return {bpy.path.basename(n.image.filepath)
            for slot in obj.material_slots
            if slot.material and slot.material.node_tree
            for n in slot.material.node_tree.nodes
            if n.type == 'TEX_IMAGE' and n.image}


def _base_color_image(obj) -> str:
    for slot in obj.material_slots:
        if slot.material and slot.material.node_tree:
            for link in slot.material.node_tree.links:
                if (link.to_socket.name == "Base Color"
                        and link.from_node.type == 'TEX_IMAGE'):
                    return bpy.path.basename(link.from_node.image.filepath)
    return ""


def check_rotation_conjugation(tmp: Path) -> None:
    """Invariant #7: a Y-up manifest rotation R reaches Blender (Z-up) as
    the CONJUGATION C.R.C^-1 (C = Rx90). Left-multiplying (C.R) once laid
    every asset on its side, identity rotation included."""
    cases = {"FX_RotIdentity": [0.0, 0.0, 0.0],
             "FX_RotYaw": [0.0, 0.7, 0.0],
             "FX_RotRoll": [0.0, 0.0, 0.4]}
    assets = []
    for i, (name, rot) in enumerate(cases.items()):
        fbx = tmp / f"{name}.fbx"
        _export_cube(fbx, name)
        assets.append({"id": name.lower(), "fbx": str(fbx),
                       "position": [i * 5.0, 0.0, 0.0], "rotation": rot})
    _build_and_reopen(tmp, "rotation", {"schema": "pharos.scene/v1",
                                        "scene": "rotation",
                                        "assets": assets})
    conv = Matrix.Rotation(math.radians(90.0), 3, 'X')
    for name, rot in cases.items():
        obj = bpy.data.objects.get(name)
        assert obj is not None, \
            f"{name} missing from the reopened .blend: " \
            f"{sorted(bpy.data.objects.keys())}"
        got = obj.matrix_world.to_3x3().normalized()
        want = conv @ Euler(rot, 'XYZ').to_matrix() @ conv.inverted()
        err = max(abs(got[r][c] - want[r][c])
                  for r in range(3) for c in range(3))
        assert err < 1e-4, \
            f"{name} rotation {rot}: reopened orientation is not C.R.C^-1 " \
            f"(max element error {err:.4f})"
    # what yaw MEANS in the Z-up frame: a turn about the vertical axis
    yaw = bpy.data.objects["FX_RotYaw"].matrix_world.to_3x3().normalized()
    assert (yaw @ Vector((0, 0, 1)) - Vector((0, 0, 1))).length < 1e-4, \
        "yaw tilted the vertical axis"


def check_texture_set_fallback(tmp: Path) -> None:
    """A textures[] set whose name matches no filename falls back to ALL
    maps in its folder, on the ground plane AND on a named asset (found by
    live verification: the silent skip lost the ground plane). Control: a
    name that DOES match uses only the matching files."""
    fb = tmp / "tex" / "FX_Folder_Only"
    fb_maps = ["FX_Surface_albedo.png", "FX_Surface_normal.png",
               "FX_Surface_roughness.png"]
    ctl = tmp / "tex" / "FX_Mixed"
    # FX_Alpha sorts first: without name matching it would take albedo
    ctl_maps = ["FX_Alpha_albedo.png", "FX_Brick_albedo.png",
                "FX_Brick_normal.png"]
    for folder, names in ((fb, fb_maps), (ctl, ctl_maps)):
        folder.mkdir(parents=True)
        for n in names:
            (folder / n).write_bytes(_png())
    fbx = tmp / "FX_TexTarget.fbx"
    _export_cube(fbx, "FX_TexTarget")
    _build_and_reopen(tmp, "texture_sets", {
        "schema": "pharos.scene/v1", "scene": "texture_sets",
        "assets": [{"id": "tex_target", "fbx": str(fbx)}],
        "textures": [
            {"set": "Display Name Only", "folder": str(fb),
             "apply_to": ["ground", "tex_target"]},
            {"set": "FX Brick", "folder": str(ctl), "apply_to": "ground"}]})

    plane = bpy.data.objects.get("ground_Display Name Only")
    assert plane is not None, \
        "fallback: no ground plane for a set name that matches no filename"
    assert _images(plane) == set(fb_maps), \
        f"fallback: ground plane maps {sorted(_images(plane))}"
    assert _base_color_image(plane) == "FX_Surface_albedo.png", \
        f"fallback: base colour is {_base_color_image(plane)!r}"
    target = bpy.data.objects.get("FX_TexTarget")
    assert target is not None, "texture target missing after reopen"
    assert _images(target) == set(fb_maps), \
        f"fallback: apply_to asset got maps {sorted(_images(target))}"
    ctl_plane = bpy.data.objects.get("ground_FX Brick")
    assert ctl_plane is not None, "control: matching set built no plane"
    assert _images(ctl_plane) == {"FX_Brick_albedo.png",
                                  "FX_Brick_normal.png"}, \
        f"control: matching set used {sorted(_images(ctl_plane))}"


def _layout_scene() -> None:
    """Objects laid out like a kit file: a tall 'Chair' off the origin, a
    flat 'Desk', a 'Lamp', a 'Pole' that LIES (an object rotation, as the
    up-axis conversion of some files is) and a 'Crate.001' (a name that
    already carries Blender's copy suffix)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name, size, loc, rot in (
            ("Chair", (0.5, 0.5, 1.0), (3, 2, 0.5), 0.0),
            ("Desk", (2.0, 1.0, 0.1), (-2, 1, 1), 0.0),
            ("Lamp", (0.3, 0.3, 1.5), (0, -3, 1), 0.0),
            ("Pole", (0.2, 0.2, 2.0), (5, 5, 1), math.radians(90.0)),
            ("Crate.001", (0.4, 0.4, 0.4), (-5, -5, 0.2), 0.0)):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
        o = bpy.context.active_object
        o.name = name
        o.scale = size
        bpy.ops.object.transform_apply(location=False, rotation=False,
                                       scale=True)
        o.rotation_euler = (rot, 0.0, 0.0)


def _world_box(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector([min(p[k] for p in pts) for k in range(3)])
    hi = Vector([max(p[k] for p in pts) for k in range(3)])
    return lo, hi


def _at(objs, x, y, tol=0.01):
    """The objects whose footprint centre is (x, y) in Blender's frame."""
    out = []
    for o in objs:
        lo, hi = _world_box(o)
        if abs((lo.x + hi.x) / 2 - x) < tol and abs((lo.y + hi.y) / 2 - y) < tol:
            out.append(o)
    return out


def _size(o):
    lo, hi = _world_box(o)
    return tuple(round(v, 3) for v in (hi - lo))


def check_one_object_of_a_multi_object_file(tmp: Path) -> None:
    """A search key 'file#Chair' must place the chair ALONE: the builder
    imported the whole file and moved its first object (one chair brought
    a whole office). For every format the builder reads (.fbx, .obj, .glb,
    objects of a .blend):
      - the picked object arrives alone, at its position, in its size;
      - the rotation the importer gave it survives placement (writing the
        manifest rotation onto the object laid such models on their side);
      - a glTF object takes the manifest rotation (quaternion mode ignored
        it);
      - the same key used twice gives two objects, also for a name that
        ends in '.001' (the second one was skipped);
      - a file without an object named moves as one piece."""
    _layout_scene()
    files = {"fbx": tmp / "kit.fbx", "obj": tmp / "kit.obj",
             "glb": tmp / "kit.glb", "blend": tmp / "kit_src.blend"}
    bpy.ops.export_scene.fbx(filepath=str(files["fbx"]))
    bpy.ops.wm.obj_export(filepath=str(files["obj"]))
    bpy.ops.export_scene.gltf(filepath=str(files["glb"]), export_format='GLB')
    bpy.ops.wm.save_as_mainfile(filepath=str(files["blend"]), copy=True)
    assets = []
    for i, (fmt, path) in enumerate(files.items()):
        assets.append({"id": f"chair_{fmt}", "fbx": str(path),
                       "object": "Chair", "position": [i * 5.0, 0.0, 0.0]})
        assets.append({"id": f"pole_{fmt}", "fbx": str(path),
                       "object": "Pole", "position": [i * 5.0, 0.0, 10.0]})
    for k, fmt in enumerate(("blend", "blend", "obj", "obj")):
        assets.append({"id": f"crate_{k}", "fbx": str(files[fmt]),
                       "object": "Crate.001",
                       "position": [30.0 + 2 * k, 0.0, 0.0]})
    assets.append({"id": "desk_turned", "fbx": str(files["glb"]),
                   "object": "Desk", "position": [40.0, 0.0, 0.0],
                   "rotation": [0.0, math.radians(90.0), 0.0]})
    assets.append({"id": "whole", "fbx": str(files["fbx"]),
                   "position": [0.0, 0.0, 20.0]})
    _build_and_reopen(tmp, "multi", {"schema": "pharos.scene/v1",
                                     "scene": "multi", "assets": assets})
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    # manifest [x, height, z] -> Blender (x, -z, height)
    for i, fmt in enumerate(files):
        hit = _at(meshes, i * 5.0, 0.0)
        assert len(hit) == 1, f"{fmt}: objects at the chair spot: {hit}"
        assert _size(hit[0]) == (0.5, 0.5, 1.0), \
            f"{fmt}: chair is {_size(hit[0])}, want (0.5, 0.5, 1.0) upright"
        hit = _at(meshes, i * 5.0, -10.0)
        assert len(hit) == 1, f"{fmt}: objects at the pole spot: {hit}"
        assert _size(hit[0]) == (0.2, 2.0, 0.2), \
            f"{fmt}: pole is {_size(hit[0])}: the rotation it has in its " \
            f"file was overwritten (want (0.2, 2.0, 0.2), lying)"
    for k in range(4):
        hit = _at(meshes, 30.0 + 2 * k, 0.0)
        assert len(hit) == 1 and _size(hit[0]) == (0.4, 0.4, 0.4), \
            f"crate copy {k} (same key used twice) missing: {hit}"
    hit = _at(meshes, 40.0, 0.0)
    assert len(hit) == 1 and _size(hit[0]) == (1.0, 2.0, 0.1), \
        f"glb desk ignored its 90 degree turn: {[_size(o) for o in hit]}"
    # nothing nobody asked for: 4 chairs + 4 poles + 4 crates + 1 desk
    # + the 5 objects of the whole file
    assert len(meshes) == 18, \
        f"{len(meshes)} meshes, want 18: {sorted(o.name for o in meshes)}"
    # the whole file moved as one: its parts keep their distances
    desk, lamp = _at(meshes, -2.0, -19.0), _at(meshes, 0.0, -23.0)
    assert len(desk) == 1 and len(lamp) == 1, \
        f"the parts of the whole file drifted: desk {desk} lamp {lamp}"
    assert desk[0].parent is not None and desk[0].parent == lamp[0].parent, \
        "loose parts of one file got no common root"


def check_crowd_clip_plays(tmp: Path) -> None:
    """A crowd body got its clip attached but, in Blender 4.4+, no action
    slot: the 'walking' character stood still in every frame."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.object.armature_add()
    arm = bpy.context.active_object
    arm.name = "FX_Rig"
    bpy.ops.mesh.primitive_cube_add(size=0.2)
    cube = bpy.context.active_object
    cube.parent = arm
    cube.parent_type = 'BONE'
    cube.parent_bone = arm.data.bones[0].name
    body, clip = tmp / "body.fbx", tmp / "clip.fbx"
    bpy.ops.export_scene.fbx(filepath=str(body), bake_anim=False)
    bone = arm.pose.bones[0]
    bone.rotation_mode = 'XYZ'
    bone.keyframe_insert("rotation_euler", frame=1)
    bone.rotation_euler = (1.2, 0.0, 0.0)
    bone.keyframe_insert("rotation_euler", frame=20)
    bpy.ops.export_scene.fbx(filepath=str(clip), bake_anim=True)
    _build_and_reopen(tmp, "crowd", {
        "schema": "pharos.scene/v1", "scene": "crowd", "assets": [],
        "crowd": [{"body_fbx": str(body), "animation_fbx": str(clip),
                   "position": [0.0, 0.0, 0.0], "count": 1}]})
    rigs = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    assert len(rigs) == 1, f"armatures: {[o.name for o in rigs]}"
    rig = rigs[0]
    assert rig.animation_data and rig.animation_data.action, "no clip attached"
    scene = bpy.context.scene
    poses = []
    for frame in (1, 20):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        poses.append(rig.pose.bones[0].matrix.copy())
    moved = max(abs(poses[0][r][c] - poses[1][r][c])
                for r in range(4) for c in range(4))
    assert moved > 0.1, \
        f"the clip is attached but the bone does not move (max change " \
        f"{moved:.4f}): no action slot assigned"


CHECKS = [check_rotation_conjugation, check_texture_set_fallback,
          check_one_object_of_a_multi_object_file, check_crowd_clip_plays]


def main() -> None:
    failed = []
    for check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="pharos_bchk_",
                                         ignore_cleanup_errors=True) as tmp:
            try:
                check(Path(tmp))
                print(f"PASS {check.__name__}")
            except (Exception, SystemExit) as exc:
                # SystemExit: the builder rejected the manifest (exit 2)
                failed.append(f"{check.__name__}: {exc}")
                traceback.print_exc()
                print(f"FAIL {check.__name__}: {exc}")
            finally:
                sys.stdout.flush()
                bpy.ops.wm.read_factory_settings(use_empty=True)
    print(f"blender_scene_checks: {len(CHECKS) - len(failed)}/{len(CHECKS)}"
          f" passed")
    sys.stdout.flush()
    if failed:
        raise RuntimeError(f"blender_scene_checks failed: {failed}")


if __name__ == "__main__":
    main()
