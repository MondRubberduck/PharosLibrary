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
import re
import sys
from pathlib import Path
import mathutils
from mathutils import Vector


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
        sys.stdout.flush()      # a crash mid-build must not lose the log
                               # (Blender buffers stdout in --background)

    # --- ground-covering texture sets (manifest.textures[]) ---
    if manifest.get("textures"):
        _apply_ground_textures(manifest, ground_y)

    # --- crowd ---
    for group in manifest.get("crowd", []):
        _build_crowd(group, ground_y)
        sys.stdout.flush()

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

    # drop orphaned image/material datablocks (dead kit references that
    # were replaced leave hundreds of unassigned 0x0 images behind)
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False,
                               do_recursive=True)
    except Exception as exc:                              # noqa: BLE001
        print(f"  (orphan purge skipped: {exc})")

    # --- save ---
    if not save_path:
        save_path = Path(manifest_path).with_suffix(".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(save_path))
    print(f"\nSaved: {save_path}")
    print(f"Objects in scene: {len(bpy.data.objects)}")
    sys.stdout.flush()
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

    # rotation: the manifest declares Y-up euler [pitch, yaw, roll]
    # (pharos.scene/v1 contract); Blender is Z-up. Convert the rotation
    # into Blender's frame exactly: R_blender = Rx(+90°) · R_manifest.
    # Single-axis check: pitch→X, yaw→Z, roll→−Y, exactly as a Y-up
    # author expects (V5 cross-validation D3: applying the raw euler
    # turned yaw into roll and buried assets in the ground).
    rot = asset.get("rotation", [0, 0, 0])
    r_mat = mathutils.Euler((rot[0], rot[1], rot[2]), 'XYZ') \
        .to_matrix().to_4x4()
    conv = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
    root.rotation_euler = (conv @ r_mat).to_euler('XYZ')

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
        if mats.get("slots"):
            # per-slot recipes (API recipe.slots[]): one material per slot
            # name, assigned in place -- a 33-slot KitBash building keeps
            # its 33 materials instead of collapsing to one representative
            # hero pick (V5 D8)
            _apply_recipe_slots(root, mats["slots"], aid)
        else:
            _build_material(root, mats, aid)
            multi = sum(len(m.data.materials) for m in _all_meshes(root))
            if multi > 1:
                print(f"    WARNING: hero recipe is representative "
                      f"({multi} existing slots kept hero material only; "
                      f"pass recipe 'slots[]' for per-slot wiring)")
    else:
        print(f"    (no material recipe — placeholder materials remain)")

    # repoint dead KitBash texture references (the FBX files point at an
    # empty 'kb3d_*.blender.native/KB3DTextures/4k' folder; the real
    # textures live in the sibling 'kb3d_*.png.2k' folder)
    _remap_dead_kb3d_images()

    # deselect for next import
    bpy.ops.object.select_all(action='DESELECT')


def _make_material(name, recipe):
    """Build a Principled BSDF material from a recipe dict
    (hero_textures shape or a per-slot maps dict). Returns the material."""
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

    # emissive -> additive Emission shader (V5 D4: neon/signage/screens
    # rendered dark and dead without it)
    emi = load_tex(recipe.get("emissive"))
    if emi:
        emish = nodes.new("ShaderNodeEmission")
        emish.inputs["Strength"].default_value = float(
            recipe.get("emissive_strength", 3.0))
        links.new(emi.outputs["Color"], emish.inputs["Color"])
        mix = nodes.new("ShaderNodeAddShader")
        links.new(bsdf.outputs["BSDF"], mix.inputs[0])
        links.new(emish.outputs["Emission"], mix.inputs[1])
        links.new(mix.outputs["Shader"], output.inputs["Surface"])

    # opacity/alpha -> Principled Alpha + blend
    opa = load_tex(recipe.get("opacity") or recipe.get("alpha"),
                   non_color=True)
    if opa:
        links.new(opa.outputs["Color"], bsdf.inputs["Alpha"])
        try:
            mat.blend_method = 'BLEND'
        except Exception:
            pass                      # API renamed in newer Blender
    return mat


def _build_material(obj, recipe, name):
    """Make a hero-textures material and assign it to every mesh slot."""
    mat = _make_material(name, recipe)
    for child in _all_meshes(obj):
        if child.data.materials:
            child.data.materials.clear()
        child.data.materials.append(mat)


def _apply_recipe_slots(root, slots, aid):
    """Per-slot material wiring (API recipe.slots[]): build one material
    per slot and replace existing slots BY NAME, leaving unmatched slots
    untouched (never collapse a 33-slot building to one material)."""
    # slot shape: {slot, material, base, maps: [{role, param, file, channels}]}
    built = {}
    for s in slots:
        maps = {m.get("role"): m.get("file")
                for m in (s.get("maps") or []) if m.get("file")}
        # collapse packed-channel info for the first packed map
        for m in (s.get("maps") or []):
            if (m.get("role") or "").startswith("packed"):
                maps["packed"] = m.get("file")
                if m.get("channels"):
                    maps["packed_channels"] = {
                        k: v for k, v in m["channels"].items()}
        if not maps:
            continue
        slot_name = s.get("material") or s.get("slot") or ""
        built[slot_name] = _make_material(f"{aid}_{slot_name}"[:60], maps)
    replaced = 0
    for child in _all_meshes(root):
        for i, existing in enumerate(child.data.materials):
            if existing is None:
                continue
            key = existing.name
            # importers may prefix/suffix; match loosely by containment
            match = built.get(key)
            if match is None:
                for bname, bmat in built.items():
                    if bname and (bname in key or key in bname):
                        match = bmat
                        break
            if match is not None:
                child.data.materials[i] = match
                replaced += 1
    print(f"    per-slot materials: {len(built)} built, {replaced} slots "
          f"replaced (unmatched slots kept their import materials)")


def _all_meshes(obj):
    """Get all mesh objects in the hierarchy."""
    result = []
    if obj.type == 'MESH':
        result.append(obj)
    for child in obj.children:
        result.extend(_all_meshes(child))
    return result


_KB3D_DEAD = re.compile(
    r"kb3d_[^\\/]+\.blender\.native[\\/]KB3DTextures[\\/][^\\/]*[\\/]",
    re.IGNORECASE)


def _remap_dead_kb3d_images():
    """KitBash FBX files reference an EMPTY
    'kb3d_<kit>.blender.native/KB3DTextures/4k' folder; the shipped
    textures live in the sibling 'kb3d_<kit>.png.2k' folder. Repoint any
    missing image whose path matches the dead pattern (V5 D8)."""
    fixed = 0
    for img in bpy.data.images:
        src = bpy.path.abspath(img.filepath)
        if not src or Path(src).is_file():
            continue
        p = Path(src.replace("\\", "/"))
        # ancestor index of the 'kb3d_<kit>.blender.native' segment
        seg = next((i for i, anc in enumerate(p.parents)
                    if anc.name.lower().startswith("kb3d_")
                    and ".blender.native" in anc.name.lower()), None)
        if seg is None:
            continue
        kit_dir = p.parents[seg + 1] if seg + 1 < len(p.parents) else None
        kit = p.parents[seg].name.split(".blender.native")[0]
        # the FBX records the library-of-origin path; when the library
        # lives elsewhere (copied drive, new machine) that dir does not
        # exist -> fall back to the configured kitbash/library roots
        candidates = []
        if kit_dir is not None:
            candidates.append(kit_dir / f"{kit}.png.2k" / p.name)
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from asset_service import config as _cfg
            _c = _cfg.load()
            for key in ("kitbash_root", "library_root"):
                if _c.get(key):
                    candidates.append(
                        Path(_c[key]) / f"{kit}.png.2k" / p.name)
        except Exception:
            pass
        hit = next((c for c in candidates if c.is_file()), None)
        if hit is None and kit_dir is not None:
            # last resort: bounded search for the kit folder anywhere
            # under the kitbash root (copied libraries rename parents)
            try:
                import sys as _sys2
                _sys2.path.insert(
                    0, str(Path(__file__).resolve().parents[1]))
                from asset_service import config as _cfg2
                _kr = _cfg2.load().get("kitbash_root")
                if _kr:
                    for found in Path(_kr).glob(
                            f"**/kb3d_{kit}.png.2k"):
                        cand = found / p.name
                        if cand.is_file():
                            hit = cand
                            break
            except Exception:
                pass
        if hit is not None:
            img.filepath = str(hit)
            fixed += 1
    if fixed:
        print(f"    remapped {fixed} dead KitBash texture reference(s) "
              f"to the shipped .png.2k folder")


def _apply_ground_textures(manifest, ground_y):
    """manifest.textures[] — apply each set as a ground plane (V5 D5: the
    block was validated but never did anything). Entry: {folder, set,
    apply_to ('ground'|'ground_plane'|omit), size (m, default 20)}."""
    for tex in manifest.get("textures", []) or []:
        apply_to = (tex.get("apply_to") or "ground").lower()
        if apply_to not in ("ground", "ground_plane", "floor"):
            continue                     # e.g. apply_to: asset-specific
        folder = Path(tex.get("folder") or "")
        setname = tex.get("set") or ""
        if not folder.is_dir():
            print(f"    WARNING: texture folder missing: {folder}")
            continue
        files = [p for p in sorted(folder.rglob("*"))
                 if p.is_file() and p.suffix.lower()
                 in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")]
        if setname:
            # display names use spaces, filenames use underscores --
            # compare on alphanumerics only
            norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
            key = norm(setname)
            files = [p for p in files if key in norm(p.stem)]
        recipe = {}
        for p in files:
            n = p.name.lower()
            if "albedo" in n or "basecolor" in n or "base_color" in n or \
                    "diffuse" in n or "_alb" in n or "_d." in n:
                recipe.setdefault("albedo", str(p))
            elif "normal" in n or "_nrm" in n or "_n." in n:
                recipe.setdefault("normal", str(p))
            elif "roughness" in n or "_rgh" in n or "_r." in n:
                recipe.setdefault("roughness", str(p))
            elif "metallic" in n or "_met" in n or "_m." in n:
                recipe.setdefault("metallic", str(p))
        if not recipe:
            print(f"    WARNING: no map channels recognised in {folder} "
                  f"(set {setname!r})")
            continue
        size = float(tex.get("size", 20.0))
        bpy.ops.mesh.primitive_plane_add(size=size, location=(
            tex.get("position", [0, 0, 0])[0],
            tex.get("position", [0, 0, 0])[1],
            ground_y - 0.002))
        plane = bpy.context.active_object
        plane.name = f"ground_{setname or 'tex'}"[:60]
        mat = _make_material(plane.name, recipe)
        if plane.data.materials:
            plane.data.materials.clear()
        plane.data.materials.append(mat)
        print(f"    ground plane '{plane.name}' {size}x{size}m: "
              f"{sorted(recipe.keys())}")


def _build_crowd(group, ground_y):
    """Import a body + animation, duplicate with spacing."""
    body_fbx = group["body_fbx"]
    anim_fbx = group["animation_fbx"]
    count = group.get("count", 1)
    spacing = group.get("spacing", 2.0)
    base_pos = group.get("position", [0, 0, 0])
    anim_offset = group.get("animation_offset", 0.0)

    print(f"  [crowd] {count} instances, spacing {spacing}m")
    sys.stdout.flush()

    # import the body once
    bpy.ops.import_scene.fbx(filepath=body_fbx, global_scale=1.0)
    body_objs = bpy.context.selected_objects
    if not body_objs:
        print(f"    WARNING: body import failed: {body_fbx}")
        return
    body_root = body_objs[0]

    # import the animation, take its action, then DELETE the animation
    # import's own objects -- otherwise a ghost armature stays parked at
    # the origin (V5 D6: 5-instance crowd produced 6 armatures)
    bpy.ops.import_scene.fbx(filepath=anim_fbx, global_scale=1.0)
    anim_objs = list(bpy.context.selected_objects)
    action = None
    for a in reversed(bpy.data.actions):
        if a.users > 0:
            action = a
            break
    if action is None:
        print(f"    WARNING: no animation found in {anim_fbx}")
    bpy.ops.object.select_all(action='DESELECT')
    for o in anim_objs:
        if o.name in bpy.data.objects:
            o.select_set(True)
    if bpy.context.selected_objects:
        bpy.ops.object.delete()

    def _offset_action(src, frames):
        """Copy an action with keyframes shifted — linked duplicates share
        one action, so per-instance stagger needs per-instance copies
        (V5 D6: documented stagger resolved to 0 for every instance)."""
        new = src.copy()

        def fcurves_of(act):
            if hasattr(act, "fcurves"):          # pre-4.4 legacy API
                return list(act.fcurves)
            out = []                             # slotted actions (4.4+)
            for layer in act.layers:
                for strip in layer.strips:
                    get_bag = (getattr(strip, "channel_bag", None)
                               or getattr(strip, "channelbag", None))
                    if get_bag is None:
                        continue
                    for slot in act.slots:
                        bag = get_bag(slot)
                        if bag:
                            out.extend(bag.fcurves)
            return out

        for fc in fcurves_of(new):
            for kp in fc.keyframe_points:
                kp.co.x += frames
                kp.handle_left.x += frames
                kp.handle_right.x += frames
        new.name = f"{src.name}_+{frames}f"
        return new

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

        # assign animation to the ARMATURE with a real per-instance offset
        if action:
            arm = inst if inst.type == 'ARMATURE' else next(
                (o for o in inst.children_recursive
                 if o.type == 'ARMATURE'), None)
            if arm:
                if not arm.animation_data:
                    arm.animation_data_create()
                arm.animation_data.action = (
                    action if anim_offset == 0
                    else _offset_action(action, int(anim_offset * i * 24)))

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
