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


CHECKS = [check_rotation_conjugation, check_texture_set_fallback]


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
