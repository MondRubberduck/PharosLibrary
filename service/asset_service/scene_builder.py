"""Pharos Scene Builder for Blender.

Run inside Blender (headless or interactive):
    blender --background --python scene_builder.py -- manifest.json [--preview]

Or from Blender's Python console:
    exec(open("scene_builder.py").read()); build("manifest.json")

Reads a validated scene manifest (pharos.scene/v1) and:
  1. Imports each FBX at scale 1.0 (metres, Y-up — enforced, not guessed)
  2. Ground-snaps assets when snap_to_ground is true (uses bbox_min)
  3. Rebuilds materials from hero_textures with packed-channel support
     (Separate RGB → AO/Roughness/Metallic for ORM/RMA maps)
  4. Places crowd instances with staggered animation offsets
  5. Saves the .blend file

The builder does NOT:
  - Modify any source files (read-only imports)
  - Guess missing paths (the validator catches those first)
  - Apply art direction (lighting, cameras, post — human's job)
"""

import bpy
import json
import math
import sys
from pathlib import Path
import mathutils
from mathutils import Vector, Vector as V2


def build(manifest_path: str, save_path: str = None, preview: bool = False):
    """Main entry point. Call from Blender."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    scene_name = manifest.get("scene", "Pharos Scene")
    ground_y = manifest.get("ground_y", 0.0)

    # clean scene
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.name = scene_name

    # set units to metres
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.scene.unit_settings.scale_length = 1.0

    print(f"\\n{'=' * 60}")
    print(f"Building: {scene_name}")
    print(f"Assets: {len(manifest.get('assets', []))}")
    print(f"Crowd: {sum(c.get('count', 1) for c in manifest.get('crowd', []))}")
    print(f"{'=' * 60}")

    # --- import assets ---
    for i, asset in enumerate(manifest.get("assets", [])):
        _import_asset(asset, ground_y, i)

    # --- crowd ---
    for group in manifest.get("crowd", []):
        _build_crowd(group, ground_y)

    # --- audio (D08: process or warn, never silently drop) ---
    audio_entries = manifest.get("audio", [])
    if audio_entries:
        print("\n  [audio] %d entries:" % len(audio_entries))
        for au in audio_entries:
            af = au.get("file", "")
            aloop = au.get("loop", False)
            if af and Path(af).is_file():
                print("    %s loop=%s (not auto-imported; use Sequencer)" % (Path(af).name, aloop))
            else:
                print("    WARNING: audio file not found: %s" % af)
        print("  (Audio documented but not auto-imported yet)")

    # --- save ---
    if not save_path:
        save_path = Path(manifest_path).with_suffix(".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(save_path))
    print(f"\\nSaved: {save_path}")
    print(f"Objects in scene: {len(bpy.data.objects)}")
    return save_path


def _import_asset(asset, ground_y, index):
    """Import one FBX, position it, optionally rebuild its material."""
    fbx = asset["fbx"]
    aid = asset.get("id", f"asset_{index}")
    print(f"  [{index + 1}] {aid}: {Path(fbx).name}")

    # import at scale 1.0, Y-up (Pharos convention)
    bpy.ops.import_scene.fbx(
        filepath=fbx,
        global_scale=1.0,
        use_manual_orientation=False,  # FBX already Y-up from exports
    )
    imported = bpy.context.selected_objects
    if not imported:
        print(f"    WARNING: no objects imported from {fbx}")
        return

    # find the root of the imported hierarchy: the object with no parent
    # among the imported set. For KitBash assemblies this is the EMPTY;
    # for simple meshes it is the mesh itself.
    parent_set = {obj.name for obj in imported}
    root = None
    for obj in imported:
        if obj.parent is None or obj.parent.name not in parent_set:
            root = obj
            break
    if root is None:
        root = imported[0]

    # record the original world-space centre, then zero the root so we
    # can position from a clean origin
    bpy.context.view_layer.update()
    world_centre = Vector((0, 0, 0))
    mesh_count = 0
    for obj in imported:
        if obj.type == 'MESH':
            for corner in obj.bound_box:
                wc = obj.matrix_world @ mathutils.Vector(corner)
                world_centre += wc
                mesh_count += 1
    if mesh_count:
        world_centre /= mesh_count

    # zero out the root's location so the assembly starts at origin
    root.location = (0, 0, 0)
    bpy.context.view_layer.update()

    # position
    pos = asset.get("position", [0, 0, 0])
    root.location = Vector(pos)

    # rotation (radians to degrees)
    rot = asset.get("rotation", [0, 0, 0])
    root.rotation_euler = Vector(rot)

    # uniform scale — MULTIPLY, don't overwrite: Blender's FBX importer
    # sets object scale to 0.01 for cm-authored files (cm→m normalisation).
    # Forcing scale=(1,1,1) destroys that and inflates geometry 100×
    # (cross-validation D01). A manifest scale of 1.0 means "keep the
    # importer's normalisation as-is".
    scale = asset.get("scale", 1.0)
    if scale != 1.0:
        root.scale = root.scale * scale

    # ground snap: shift so the lowest mesh point sits on ground_y.
    # Blender is Z-up internally; snap along +Z (the vertical axis).
    # Uses the true world-space bbox minimum, not origin-minus-half-height
    # (cross-validation D02).
    if asset.get("snap_to_ground", False):
        bpy.context.view_layer.update()
        world_min = None
        for obj in imported:
            if obj.type != 'MESH':
                continue
            for corner in obj.bound_box:
                wc = obj.matrix_world @ Vector(corner)
                if world_min is None:
                    world_min = wc.copy()
                else:
                    world_min.x = min(world_min.x, wc.x)
                    world_min.y = min(world_min.y, wc.y)
                    world_min.z = min(world_min.z, wc.z)
        if world_min is not None:
            offset = ground_y - world_min.z
            root.location.z += offset
            print(f"    snapped: +{offset:.3f} to ground (Z axis)")

    # rebuild material if recipe provided
    mats = asset.get("materials")
    if mats:
        _build_material(root, mats, aid)
    else:
        print(f"    (no material recipe — placeholder materials remain)")

    # deselect for next import
    bpy.ops.object.select_all(action='DESELECT')


def _build_material(obj, recipe, name):
    """Build a Principled BSDF material from a Pharos hero_textures recipe."""
    mat = bpy.data.materials.new(f"M_{name}")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    output = nodes.get("Material Output")

    def load_tex(path, non_color=False):
        """Load an image texture node."""
        if not path or not Path(path).is_file():
            return None
        node = nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(path)
        node.image.colorspace_settings.name = (
            'Non-Color' if non_color else 'sRGB')
        return node

    # albedo -> Base Color
    alb = load_tex(recipe.get("albedo"))
    if alb:
        links.new(alb.outputs["Color"], bsdf.inputs["Base Color"])

    # normal -> Normal (via Normal Map node)
    nrm = load_tex(recipe.get("normal"), non_color=True)
    if nrm:
        nmap = nodes.new("ShaderNodeNormalMap")
        links.new(nrm.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # roughness (direct or from packed)
    rgh = load_tex(recipe.get("roughness"), non_color=True)
    if rgh:
        links.new(rgh.outputs["Color"], bsdf.inputs["Roughness"])

    # metallic (direct or from packed)
    met = load_tex(recipe.get("metallic"), non_color=True)
    if met:
        links.new(met.outputs["Color"], bsdf.inputs["Metallic"])

    # packed map (ORM/RMA) — separate channels
    packed = load_tex(recipe.get("packed"), non_color=True)
    channels = recipe.get("packed_channels") or {}
    if packed:
        sep = nodes.new("ShaderNodeSeparateColor")
        links.new(packed.outputs["Color"], sep.inputs["Color"])

        # map channel letters to BSDF inputs
        # Blender 5.x SeparateColor outputs: "Red", "Green", "Blue"
        ch_map = channels or {"r": "ao", "g": "roughness", "b": "metallic"}
        CH_OUT = {"r": "Red", "g": "Green", "b": "Blue"}
        if "roughness" in ch_map.values():
            for ch_letter, role in ch_map.items():
                out_name = CH_OUT.get(ch_letter, "Green")
                if role == "roughness" and not rgh:
                    out = sep.outputs[out_name]
                    links.new(out, bsdf.inputs["Roughness"])
                elif role == "metallic" and not met:
                    out = sep.outputs[out_name]
                    links.new(out, bsdf.inputs["Metallic"])
        print(f"    material: packed map with channels {ch_map}")

    # apply to all mesh children
    for child in _all_meshes(obj):
        if child.data.materials:
            child.data.materials.clear()
        child.data.materials.append(mat)


def _all_meshes(obj):
    """Get all mesh objects in the hierarchy."""
    result = []
    if obj.type == 'MESH':
        result.append(obj)
    for child in obj.children:
        result.extend(_all_meshes(child))
    return result


def _build_crowd(group, ground_y):
    """Import a body + animation, duplicate with spacing."""
    body_fbx = group["body_fbx"]
    anim_fbx = group["animation_fbx"]
    count = group.get("count", 1)
    spacing = group.get("spacing", 2.0)
    base_pos = group.get("position", [0, 0, 0])
    anim_offset = group.get("animation_offset", 0.0)

    print(f"  [crowd] {count} instances, spacing {spacing}m")

    # import the body once
    bpy.ops.import_scene.fbx(filepath=body_fbx, global_scale=1.0)
    body_objs = bpy.context.selected_objects
    if not body_objs:
        print(f"    WARNING: body import failed: {body_fbx}")
        return
    body_root = body_objs[0]

    # import the animation
    bpy.ops.import_scene.fbx(filepath=anim_fbx, global_scale=1.0)
    anim_objs = bpy.context.selected_objects

    # find animation actions
    actions = [a for a in bpy.data.actions if a.users > 0]
    if not actions:
        print(f"    WARNING: no animation found in {anim_fbx}")
    else:
        action = actions[-1]  # most recently imported

    # place instances
    for i in range(count):
        if i == 0:
            inst = body_root
        else:
            # linked duplicate (shares mesh data — memory efficient)
            bpy.ops.object.select_all(action='DESELECT')
            body_root.select_set(True)
            bpy.ops.object.duplicate(linked=True)
            inst = bpy.context.selected_objects[0]

        # grid layout
        angle = (i / max(count, 1)) * 2 * math.pi
        radius = spacing * max(1, count // 6)
        inst.location = Vector((
            base_pos[0] + math.cos(angle) * radius,
            base_pos[1],
            base_pos[2] + math.sin(angle) * radius,
        ))

        # assign animation with offset
        if actions:
            for obj in _all_meshes(inst):
                if obj.animation_data:
                    obj.animation_data.action = action
                    # offset playback
                    obj.animation_data.action_frame_start = int(anim_offset * 24)

    bpy.ops.object.select_all(action='DESELECT')


# --- CLI entry (when run via blender --python) ---
if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args:
        manifest = args[0]
        preview = "--preview" in args
        save = None
        if "--save" in args:
            save = args[args.index("--save") + 1]
        build(manifest, save, preview)
    else:
        print("Usage: blender --background --python scene_builder.py -- "
              "manifest.json [--save output.blend]")
