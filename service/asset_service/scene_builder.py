"""Pharos Scene Builder for Blender.

Run inside Blender (headless or interactive):
    blender --background --python scene_builder.py -- manifest.json

Or from Blender's Python console:
    exec(open("scene_builder.py").read()); build("manifest.json")

Reads a validated scene manifest (pharos.scene/v1) and:
  1. Imports each model file at scale 1.0 (metres, Y-up — enforced, not
     guessed): .fbx, .obj, .glb/.gltf, or the named objects of a .blend.
     From a multi-object file it keeps only the asset's `object`.
  2. Ground-snaps assets when snap_to_ground is true (uses bbox_min)
  3. Rebuilds materials from hero_textures with packed-channel support
     (Separate RGB → AO/Roughness/Metallic for ORM/RMA maps)
  4. Places crowd instances with staggered animation offsets
  5. Saves the .blend file (texture paths absolute, or packed with --pack)

A key that appears several times is imported once: the later assets are
linked duplicates (one mesh, one set of materials). After the imports the
images that name the same file and the materials that are the same
material are merged. A texture file that is missing after the build is
reported loudly: the build still saves, and exits 5.

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


def build(manifest_path: str, save_path: str = None, pack_textures: bool = False):
    """Main entry point. Call from Blender."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    # validate BEFORE building: agents author manifests, and a bad one
    # used to fail half-way through with objects already in the scene
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from scene_manifest import validate_manifest, check_paths, resolve_keys
    problems = validate_manifest(manifest)
    problems += resolve_keys(manifest)       # search keys -> files/objects
    problems += check_paths(manifest)
    if problems:
        print("MANIFEST INVALID -- nothing was built:")
        for p in problems[:25]:
            print(f"  - {p}")
        sys.exit(2)
    from scene_manifest import whole_file_notes
    for note in whole_file_notes(manifest):
        print(f"WARNING: {note}")
    _SKIPPED.clear()
    _IMPORTED.clear()
    _BODIES.clear()
    scene_name = manifest.get("scene", "Pharos Scene")
    ground_y = manifest.get("ground_y", 0.0)

    # clean scene
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.name = scene_name

    # set units to metres
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.scene.unit_settings.scale_length = 1.0

    print(f"\n{'=' * 60}")
    print(f"Building: {scene_name}")
    print(f"Assets: {len(manifest.get('assets', []))}")
    from scene_manifest import crowd_count
    print(f"Crowd: {sum(crowd_count(c) for c in manifest.get('crowd', []))}")
    print(f"{'=' * 60}")

    # --- import assets ---
    roots_by_id: dict = {}
    for i, asset in enumerate(manifest.get("assets", [])):
        _import_asset(asset, ground_y, i, roots_by_id)
        sys.stdout.flush()      # a crash mid-build must not lose the log
                               # (Blender buffers stdout in --background)

    # dead KitBash texture refs are repointed ONCE here with a cached
    # kit-folder index (per-asset invocation was O(assets x images x
    # drive) and cost 45+ min on a real build)
    _remap_dead_kb3d_images()
    _glass_by_name()
    # one datablock per image file and per material: every import brought
    # its own copies (a street of 165 assets held 8,366 images and 2,052
    # materials; Cycles uploads each datablock on its own)
    _dedup_datablocks()

    # --- texture sets: ground planes AND/OR onto named assets ---
    if manifest.get("textures"):
        _apply_texture_sets(manifest, ground_y, roots_by_id)

    # --- crowd ---
    for group in manifest.get("crowd", []):
        _build_crowd(group, ground_y)
        sys.stdout.flush()
    if manifest.get("crowd"):
        _dedup_datablocks()          # the bodies' copies too

    # --- audio: recorded for downstream engines only. Blender does not
    # import audio into scenes by design (nobody does this in 15 years
    # of Blender practice); UE/Unity and video editors consume these
    # paths. This is NOT a failure state and is never reported as one.
    audio_entries = manifest.get("audio", [])
    if audio_entries:
        print("\n  [audio] %d entries recorded for downstream engines "
              "(Blender does not import scene audio by design)"
              % len(audio_entries))

    # drop orphaned image/material datablocks (dead kit references that
    # were replaced leave hundreds of unassigned 0x0 images behind)
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False,
                               do_recursive=True)
    except Exception as exc:                              # noqa: BLE001
        print(f"  (orphan purge skipped: {exc})")

    # a texture that is not on disk stays a 0x0 image and renders magenta
    # or black; said once, loudly, with the files (a whole kit once rendered
    # untextured without a word)
    missing = _missing_textures()

    # --- save ---
    if not save_path:
        save_path = _writable_or_fallback(
            default_save_path(manifest_path, manifest.get("scene") or ""), manifest_path)
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    packed = ""
    if pack_textures:
        try:
            bpy.ops.file.pack_all()
            packed = " (textures packed)"
        except RuntimeError as exc:
            # the operator packs what it finds, reports every file it cannot
            # find and raises at the end: the build went on to exit 0 with
            # no .blend at all
            print(f"  WARNING: pack: {str(exc).strip()[:300]}")
            packed = f" (textures packed except {len(missing)} missing)"
    # absolute texture paths: Blender's default rewrites them relative to
    # the .blend, and a file moved out of the folder it was built in lost
    # every texture (236 of 247 images missing on the first copy)
    bpy.ops.wm.save_as_mainfile(filepath=str(save_path), relative_remap=False)
    print(f"\nSaved: {save_path}{packed}")
    print(f"Objects in scene: {len(bpy.data.objects)}; images: {len(bpy.data.images)}; "
          f"materials: {len(bpy.data.materials)}; meshes: {len(bpy.data.meshes)}")
    sys.stdout.flush()
    if missing:
        print(f"BUILD WITH MISSING TEXTURES: {len(missing)} image file(s) not on disk or empty "
              f"(the .blend is saved; those surfaces render untextured): "
              + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
    if _SKIPPED:
        # a scene missing an asset must not look like a finished build
        skipped = list(_SKIPPED)
        _SKIPPED.clear()
        print(f"BUILD INCOMPLETE: {len(skipped)} asset(s) skipped: "
              f"{', '.join(skipped)} (see ERROR lines above)")
        sys.stdout.flush()
        sys.exit(3)
    if missing:
        sys.stdout.flush()
        sys.exit(5)
    return save_path


_SKIPPED = []       # asset ids the build could not place
_IMPORTED = {}      # (file, object) -> the root of the first import of that key
_BODIES = {}        # body file -> the root of its imported, re-bound body


def default_save_path(manifest_path, scene_name: str) -> Path:
    """Where a build goes when --save is not given: the library's own
    output folder, <library>/<agent_files>/builds/<scene>/<scene>.blend
    (the one place inside a read-only library an agent may write), else
    next to the manifest. The folder is named after the scene as written
    (any script; the ASCII-only slug once sent every non-Latin scene to
    'scene/scene.blend'), or after the manifest file when the manifest
    names no scene."""
    name = (scene_name or "").strip() or Path(manifest_path).stem
    slug = re.sub(r"[^\w.-]+", "_", name).strip("._") or Path(manifest_path).stem
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from asset_service import config as _cfg
        cfg = _cfg.load()
        root = cfg.get("library_root")
        if root and Path(root).is_dir():
            return Path(root) / (cfg.get("agent_files") or "_Agent_Files") / "builds" / slug / f"{slug}.blend"
    except Exception:                                     # noqa: BLE001
        pass
    return Path(manifest_path).with_suffix(".blend")


def _writable_or_fallback(save_path, manifest_path) -> Path:
    """The default folder must take a file: on a read-only share, for a
    sandboxed agent, or with a file where the folder should be, the
    build goes next to the manifest instead of being lost after every
    import ran (it was: an OSError at the save, exit 0, no .blend)."""
    save_path = Path(save_path)
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        probe = save_path.parent / ".pharos_write_test"
        probe.write_bytes(b"")
        probe.unlink()
        return save_path
    except OSError as exc:
        fallback = Path(manifest_path).resolve().with_suffix(".blend")
        print(f"  NOTE: {save_path.parent} cannot be written ({type(exc).__name__}): "
              f"saving next to the manifest, {fallback}")
        return fallback


def _missing_textures() -> list:
    """The image files the scene refers to that are not on disk, or are
    empty (0 bytes: an image that loads as 0x0 and renders black); UDIM
    images are checked tile by tile."""
    out = []
    nopath = 0
    for img in bpy.data.images:
        if not img.users or img.packed_file or img.source not in ('FILE', 'TILED'):
            continue
        src = bpy.path.abspath(img.filepath) if img.filepath else ""
        if not src:
            # an image with no file at all (an engine's sampler reference
            # in an FBX): nothing can be missing from disk; 30 of 326 clean
            # builds once exited 5 over one
            nopath += 1
            continue
        if img.source == 'TILED':
            for tile in img.tiles:
                n = int(tile.number)
                tp = (src.replace("<UDIM>", str(n))
                      .replace("<UVTILE>", f"u{(n - 1001) % 10 + 1}_v{(n - 1001) // 10 + 1}")) if src else ""
                if not tp or not Path(tp).is_file():
                    out.append(tp or img.name)
                elif Path(tp).stat().st_size == 0:
                    out.append(tp + " (empty file)")
            continue
        if not Path(src).is_file():
            out.append(src)
        elif Path(src).stat().st_size == 0:
            out.append(src + " (empty file)")
    if out:
        print(f"  WARNING: {len(out)} texture file(s) missing or empty (0x0 images): "
              + ", ".join(Path(x).name for x in out[:6]) + (" ..." if len(out) > 6 else ""))
    if nopath:
        print(f"  note: {nopath} image(s) without a file path (sampler references), not counted as missing")
    return out


def _sock_value(v):
    """A socket's default value as something repr-stable: strings as they
    are (a Principled Volume's attribute names once raised ValueError
    through float()), vectors rounded, scalars rounded."""
    if isinstance(v, str):
        return v
    try:
        return tuple(round(float(x), 4) for x in v)
    except (TypeError, ValueError):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return str(v)


def _tree_fingerprint(tree, depth: int = 0) -> list:
    """[sorted node signatures, sorted link signatures] of a node tree:
    each node's type, every unlinked input's value, every output's value
    (RGB and Value nodes keep theirs there), its image with colour space,
    its ramp stops and curve points, its settings, and -- for a group node
    -- the group tree's own fingerprint (two levels)."""
    by_name, nodes = {}, []
    for n in tree.nodes:
        parts = [n.bl_idname]
        for inp in n.inputs:
            if inp.is_linked or not hasattr(inp, "default_value"):
                continue
            parts.append(("in", inp.identifier, _sock_value(inp.default_value)))
        for out in n.outputs:
            if hasattr(out, "default_value"):
                parts.append(("out", out.identifier, _sock_value(out.default_value)))
        img = getattr(n, "image", None)
        if img is not None:
            parts.append(("image", (bpy.path.abspath(img.filepath) if img.filepath else img.name).lower(),
                          img.colorspace_settings.name, img.alpha_mode))
        ramp = getattr(n, "color_ramp", None)
        if ramp is not None:
            parts.append(("ramp", ramp.interpolation, ramp.color_mode,
                          tuple((round(e.position, 4), _sock_value(e.color)) for e in ramp.elements)))
        mapping = getattr(n, "mapping", None)
        if mapping is not None and hasattr(mapping, "curves"):
            parts.append(("curves", tuple(tuple(_sock_value(p.location) for p in c.points)
                                          for c in mapping.curves)))
        sub = getattr(n, "node_tree", None)
        if sub is not None and n.bl_idname == "ShaderNodeGroup":
            parts.append(("group", sub.name, repr(_tree_fingerprint(sub, depth + 1)) if depth < 2 else ""))
        for attr in ("blend_type", "operation", "interpolation", "projection", "extension",
                     "uv_map", "distribution", "subsurface_method", "data_type", "space",
                     "use_clamp", "vector_type", "attribute_name", "attribute_type"):
            if hasattr(n, attr):
                parts.append((attr, str(getattr(n, attr))))
        by_name[n.name] = repr(tuple(parts))
        nodes.append(by_name[n.name])
    links = sorted(repr((by_name[l.from_node.name], l.from_socket.identifier,
                         by_name[l.to_node.name], l.to_socket.identifier))
                   for l in tree.links)
    return [sorted(nodes), links]


def _material_fingerprint(mat) -> str:
    """What a material IS, so that only true copies merge: its node tree
    (types, values, images with colour space, groups, ramps, links) and
    blend settings; colour, metallic and roughness for a material without
    nodes. Two kit files' 'Glass' and 'Metal' that shared a base colour
    were once merged by name, and one became chrome and see-through."""
    sig = [str(getattr(mat, "blend_method", "")), round(float(getattr(mat, "alpha_threshold", 0.0)), 4)]
    if mat.use_nodes and mat.node_tree:
        sig += _tree_fingerprint(mat.node_tree)
    else:
        sig += [tuple(round(c, 4) for c in mat.diffuse_color),
                round(float(mat.metallic), 4), round(float(mat.roughness), 4)]
    return repr(sig)


def _dedup_datablocks() -> None:
    """Merge images that name the same file in the same colour space and
    materials that are the same material (same name before Blender's .001
    suffix and the same fingerprint). Linked duplicates already share
    meshes; this covers the copies that separate imports of the same kit
    bring along."""
    first: dict = {}
    merged_img = 0
    for img in list(bpy.data.images):
        path = (bpy.path.abspath(img.filepath) if img.filepath else "").lower()
        if not path or img.packed_file:
            continue
        # a Non-Color copy of a colour image is a different image (a normal
        # map decoded as sRGB is wrong), so the colour space is part of it
        key = (path, img.colorspace_settings.name, img.alpha_mode)
        if key in first:
            img.user_remap(first[key])
            merged_img += 1
        else:
            first[key] = img

    keep: dict = {}
    merged_mat = 0
    for mat in sorted(bpy.data.materials, key=lambda m: (re.sub(r"\.\d{3}$", "", m.name), m.name)):
        base = re.sub(r"\.\d{3}$", "", mat.name)
        key = (base, _material_fingerprint(mat))
        if key in keep:
            mat.user_remap(keep[key])
            merged_mat += 1
        else:
            keep[key] = mat
    if merged_img or merged_mat:
        try:
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
        except Exception:                                 # noqa: BLE001
            pass
        print(f"  merged {merged_img} duplicate image(s) and {merged_mat} duplicate "
              f"material(s); left: {len(bpy.data.images)} images, "
              f"{len(bpy.data.materials)} materials")


def _scene_objects():
    return set(bpy.context.scene.objects)


def _import_file(path, obj_name=None):
    """Import one model file; returns the objects it added to the scene.
    Every importer turns its file's Y-up into Blender's Z-up itself."""
    suffix = Path(path).suffix.lower()
    before = _scene_objects()
    if suffix == ".fbx":
        try:
            bpy.ops.import_scene.fbx(
                filepath=path,
                global_scale=1.0,
                use_manual_orientation=False,  # FBX already Y-up from exports
            )
        except Exception as exc:                          # noqa: BLE001
            # Blender 5.1's importer raises on a light inside the file;
            # the asset is skipped, the rest of the scene still builds
            print(f"    ERROR: the FBX importer refused {path}: {str(exc).strip()[:200]}")
            for o in [o for o in bpy.context.scene.objects if o not in before]:
                bpy.data.objects.remove(o, do_unlink=True)
            return []
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
        # this importer turns Y-up into Z-up with an object ROTATION, and
        # placement replaces rotations: bake it into the mesh first, or the
        # model lies on its side (a 0.10 m flat box stood 0.41 m tall)
        for o in bpy.context.scene.objects:
            if o not in before and o.type == 'MESH' and o.parent is None:
                o.data.transform(o.rotation_euler.to_matrix().to_4x4())
                o.rotation_euler = (0.0, 0.0, 0.0)
                # an OBJ object has no pivot: its vertices sit where the
                # file's layout put them. Give it one at the centre of its
                # footprint, on its base, or the placement keeps that
                # layout offset
                vs = [v.co for v in o.data.vertices]
                if vs:
                    c = Vector([(min(v[k] for v in vs) + max(v[k] for v in vs))
                                / 2 for k in range(2)]
                               + [min(v[2] for v in vs)])
                    o.data.transform(mathutils.Matrix.Translation(-c))
                    o.location = o.location + c
    elif suffix in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
        # the importer's bone-shape sphere and whatever it put in excluded
        # collections (other scenes, orphan nodes) are not the asset: they
        # counted as loose objects and lifted a snapped body by 1 m
        shown = set(bpy.context.view_layer.objects)
        for o in [o for o in bpy.context.scene.objects if o not in before]:
            shape = o.type == 'MESH' and any(
                pb.custom_shape is o for a in bpy.context.scene.objects if a.type == 'ARMATURE'
                for pb in a.pose.bones)
            if shape or o not in shown:
                for coll in list(o.users_collection):
                    coll.objects.unlink(o)
                if not shape:
                    bpy.data.objects.remove(o, do_unlink=True)
    elif suffix == ".blend":
        # load only the named object (a kit file holds hundreds: one
        # barrel must not pull in 700 objects), or every object
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            names = ([obj_name] if obj_name and obj_name in src.objects
                     else ([] if obj_name else list(src.objects)))
            dst.objects = names
        for o in dst.objects:
            if o is not None and o.type not in ("CAMERA", "LIGHT"):
                bpy.context.scene.collection.objects.link(o)
    else:
        print(f"    WARNING: cannot import {suffix} files: {path}")
    return [o for o in bpy.context.scene.objects if o not in before]


def _pick_object(objects, name):
    """The imported object called `name`: exactly (earlier holders of the
    name are parked during the import), else the only object there is,
    else ignoring case, else with a '.001' Blender appended."""
    if len(objects) == 1:
        return objects[0]
    for test in (lambda o: o.name == name,
                 lambda o: o.name.lower() == name.lower(),
                 lambda o: re.sub(r"\.\d{3}$", "", o.name) == name):
        hit = next((o for o in objects if test(o)), None)
        if hit is not None:
            return hit
    return None


_MOD_TARGETS = ("object", "object_from", "object_to", "target", "mirror_object", "offset_object",
                "origin", "start_cap", "end_cap", "curve", "auxiliary_target", "object_center")


def _duplicate_hierarchy(root):
    """A linked duplicate of a hierarchy made with the data API.
    bpy.ops.object.duplicate copies the SELECTED bases only: a hidden or
    unselectable part stayed behind (a copied building lost its roof, a
    chimney stayed at the original), and an object outside the view layer
    (a light parented under a model) raised and ended the build with exit
    0 and no file. Every object is copied sharing its data, linked where
    its original is, re-parented inside the copy; modifier and constraint
    targets inside the hierarchy point at the copies. Returns the root."""
    objs = [root] + list(root.children_recursive)
    mapping = {}
    for o in objs:
        n = o.copy()
        for coll in o.users_collection:
            coll.objects.link(n)
        mapping[o] = n
    for o, n in mapping.items():
        if o.parent is not None and o.parent in mapping:
            n.parent = mapping[o.parent]
            n.matrix_parent_inverse = o.matrix_parent_inverse.copy()
        for mod in getattr(n, "modifiers", ()):
            for attr in _MOD_TARGETS:
                try:
                    tgt = getattr(mod, attr, None)
                    if tgt is not None and tgt in mapping:
                        setattr(mod, attr, mapping[tgt])
                except (AttributeError, TypeError):
                    pass
        cons = list(getattr(n, "constraints", ()))
        if getattr(n, "pose", None) is not None:
            for pb in n.pose.bones:             # IK targets, pole targets, Armature constraints
                cons.extend(pb.constraints)
        for con in cons:
            for attr in ("target", "pole_target"):
                try:
                    tgt = getattr(con, attr, None)
                    if tgt is not None and tgt in mapping:
                        setattr(con, attr, mapping[tgt])
                except (AttributeError, TypeError):
                    pass
            for t in getattr(con, "targets", ()):
                try:
                    if t.target is not None and t.target in mapping:
                        t.target = mapping[t.target]
                except (AttributeError, TypeError):
                    pass
        ad = getattr(n, "animation_data", None)
        for drv in (ad.drivers if ad else ()):
            for var in drv.driver.variables:
                for t in var.targets:
                    try:
                        if t.id is not None and t.id in mapping:
                            t.id = mapping[t.id]
                    except (AttributeError, TypeError):
                        pass
    return mapping[root]


def _import_asset(asset, ground_y, index, roots_by_id=None):
    """Import one model file, keep the asset's object of a multi-object
    file, position it, optionally rebuild its material."""
    fbx = asset["fbx"]
    aid = asset.get("id", f"asset_{index}")
    obj_name = asset.get("object")
    print(f"  [{index + 1}] {aid}: {Path(fbx).name}"
          + (f" -> {obj_name}" if obj_name else ""))

    parked = []
    if obj_name:
        # an earlier asset may hold this name. Blender would rename the
        # incoming object ('X.001' arrives as 'X.002') and the pick below
        # would miss it: park the old holders until the pick is done
        for o in list(bpy.data.objects):
            if o.name == obj_name:
                o.name = f"__pharos_parked_{index}_{len(parked)}"
                parked.append(o)
    # the same file and object again, with the same material block: a
    # linked duplicate of the first import (one mesh, one set of materials;
    # a building used four times was imported four times)
    key = (str(fbx).replace("\\", "/").lower(), (obj_name or "").lower(),
           json.dumps(asset.get("materials"), sort_keys=True, default=str))
    cached = _IMPORTED.get(key)
    if cached is not None and cached.name in bpy.data.objects:
        for o in parked:
            o.name = obj_name
        try:
            root = _duplicate_hierarchy(cached)
        except Exception as exc:                          # noqa: BLE001
            print(f"    ERROR: {aid} skipped: could not duplicate {cached.name}: {exc}")
            _SKIPPED.append(aid)
            return
        root.name = f"{aid}_root"
        root.location = (0.0, 0.0, 0.0)
        root.rotation_euler = (0.0, 0.0, 0.0)
        root.scale = (1.0, 1.0, 1.0)
        print(f"    linked duplicate of {cached.name[:-5]} (same file, object and materials)")
        _place_root(root, [root] + list(root.children_recursive), asset, ground_y)
        if roots_by_id is not None:
            roots_by_id[aid] = root
        bpy.ops.object.select_all(action='DESELECT')
        return
    imported = _import_file(fbx, obj_name)
    target = _pick_object(imported, obj_name) if obj_name else None
    for o in parked:
        o.name = obj_name       # Blender gives it the next free '.NNN'
    if not imported or (obj_name and target is None):
        print(f"    ERROR: {aid} skipped: "
              + (f"object {obj_name!r} not found in {Path(fbx).name}"
                 + (f" (it holds e.g. {sorted(o.name for o in imported)[:8]})"
                    if imported else "")
                 if obj_name else f"no objects imported from {fbx}"))
        _SKIPPED.append(aid)
        for o in imported:
            bpy.data.objects.remove(o, do_unlink=True)
        return

    if obj_name:
        # a multi-object file: the asset is ONE object (and its children).
        # Importing the whole file and moving its first object once put a
        # whole office into the scene for one chair.
        keep = {target, *target.children_recursive}
        if target.parent is not None:
            bpy.context.view_layer.update()
            world = target.matrix_world.copy()
            target.parent = None
            target.matrix_world = world
        drop = [o for o in imported if o not in keep]
        imported = [o for o in imported if o in keep]
        for o in drop:          # a removed object must not be touched again
            bpy.data.objects.remove(o, do_unlink=True)

    # every asset gets its own empty as root; position, rotation and scale
    # go on that empty. Written onto the imported object itself they
    # REPLACED what the importer had put there: a file whose up-axis
    # conversion is an object rotation was built lying on its side, and a
    # glTF object (quaternion rotation mode) ignored its rotation.
    loose = [o for o in imported
             if o.parent is None or o.parent not in imported]
    if len(loose) == 1:
        # one model: its pivot goes to the asset position (a part picked
        # from a kit file does not keep its place in the kit's layout).
        # Several loose parts keep their distances and move as one.
        loose[0].location = (0.0, 0.0, 0.0)
    root = bpy.data.objects.new(f"{aid}_root", None)
    bpy.context.scene.collection.objects.link(root)
    for o in loose:
        o.parent = root            # root sits at the origin: no jump
    imported.append(root)
    bpy.context.view_layer.update()
    _IMPORTED[key] = root
    _place_root(root, imported, asset, ground_y)

    # rebuild material if recipe provided
    mats = asset.get("materials")
    if not isinstance(mats, dict):
        mats = None             # "recipe" is resolved before the build
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
        print(f"    (no material recipe -- placeholder materials remain)")

    if roots_by_id is not None:
        roots_by_id[aid] = root

    # deselect for next import
    bpy.ops.object.select_all(action='DESELECT')


def _place_root(root, imported, asset, ground_y):
    """Position, rotation, scale and ground snap of an asset's root empty
    (the imported objects hang below it)."""
    # position: the manifest is Y-up ([x, y=height, z]); Blender is Z-up.
    # Same frame conversion the rotations get (Rx90 mapping y->z, z->-y):
    # applied VERBATIM, a manifest height of y=2 once floated the asset
    # 2 m sideways, and manifest z moved it INTO the air.
    pos = asset.get("position", [0, 0, 0])
    root.location = Vector((pos[0], -pos[2], pos[1]))

    # rotation: the manifest declares Y-up euler [pitch, yaw, roll]
    # (pharos.scene/v1 contract); Blender is Z-up. Convert the rotation
    # into Blender's frame exactly: R_blender = Rx(+90°) · R_manifest.
    # Single-axis check: pitch→X, yaw→Z, roll→−Y, exactly as a Y-up
    # author expects (V5 cross-validation D3: applying the raw euler
    # turned yaw into roll and buried assets in the ground).
    rot = asset.get("rotation", [0, 0, 0])
    # The manifest declares Y-up euler [pitch, yaw, roll] (scene frame);
    # Blender is Z-up. Converting a PLACEMENT rotation between frames is
    # a CONJUGATION (C · R · C⁻¹), not a left-multiply: the FBX importer
    # already converts the geometry itself, and left-multiplying added a
    # constant 90° X-tilt to every asset (identity rotation included) --
    # real-world finding: all buildings on their sides. Conjugation maps
    # yaw→Z, roll→−Y, pitch→X, and identity stays identity.
    r_mat = mathutils.Euler((rot[0], rot[1], rot[2]), 'XYZ') \
        .to_matrix().to_4x4()
    conv = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
    root.rotation_euler = (conv @ r_mat @ conv.inverted()).to_euler('XYZ')

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


def _make_material(name, recipe):
    """Build a Principled BSDF material from a recipe dict
    (hero_textures shape or a per-slot maps dict). Returns the material."""
    mat = bpy.data.materials.new(f"M_{name}")
    mat.use_nodes = True
    mat["pharos_recipe"] = True      # built from a recipe: never re-read by name
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
        from scene_manifest import packed_channel_sockets  # on sys.path via build()
        sep = nodes.new("ShaderNodeSeparateColor")
        links.new(packed.outputs["Color"], sep.inputs["Color"])

        # map channel letters to BSDF inputs
        # Blender 5.x SeparateColor outputs: "Red", "Green", "Blue"
        # null channels -> default ORM order; real *_RGB ORM maps rely on it
        ch_map = channels or {"r": "ao", "g": "roughness", "b": "metallic"}
        if "roughness" in ch_map.values():
            # `a` reads the image's own Alpha output; unknown letters skip
            for out_name, role in packed_channel_sockets(ch_map):
                out = (packed.outputs["Alpha"] if out_name == "Alpha"
                       else sep.outputs[out_name])
                if role == "roughness" and not rgh:
                    links.new(out, bsdf.inputs["Roughness"])
                elif role == "metallic" and not met:
                    links.new(out, bsdf.inputs["Metallic"])
        print(f"    material: packed map with channels {ch_map}")

    # emissive -> additive Emission shader (V5 D4: neon/signage/screens
    # rendered dark and dead without it)
    emi = load_tex(recipe.get("emissive"))
    if emi:
        emish = nodes.new("ShaderNodeEmission")
        # the map's own strength (3x once clipped half of a hologram's pixels)
        emish.inputs["Strength"].default_value = float(
            recipe.get("emissive_strength", 1.0))
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
    # refraction / transmission map -> Principled Transmission (the role
    # was in the recipes and never read: a pack's glass rendered opaque)
    ref = load_tex(recipe.get("refraction") or recipe.get("transmission"), non_color=True)
    tw = bsdf.inputs.get("Transmission Weight") or bsdf.inputs.get("Transmission")
    if ref and tw is not None:
        links.new(ref.outputs["Color"], tw)
        _refractive(mat)
    return mat


def _refractive(mat) -> None:
    """Let EEVEE refract through the material (attribute names moved
    between Blender versions; whichever exists is set)."""
    for attr in ("use_screen_refraction", "use_raytrace_refraction"):
        try:
            setattr(mat, attr, True)
        except Exception:                                 # noqa: BLE001
            pass


_GLASS_NOT = {"frame", "frames", "trim", "metal", "plastic", "wood", "holder", "rack", "fiberglass",
              "fibreglass", "glassfiber", "glassfibre", "sunglasses", "glasses", "hourglass"}


def _named_glass(name: str) -> bool:
    """'Window_Glass', 'glass', 'GlassPane' are glass; 'Fiberglass_Hull',
    'Glass_Frame_Metal', 'Sunglasses_Plastic' are not (the word is a part
    of another, or the thing is the frame around the glass)."""
    words = [w for w in re.split(r"[^a-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name).lower()) if w]
    if not words or any(w in _GLASS_NOT for w in words):
        return False
    return any(w == "glass" or (w.endswith("glass") and w[:-5] in ("window", "pane", "clear", "tinted", "frosted"))
               or w.startswith("glass") and w[5:] in ("", "pane", "panel", "window", "clear", "tinted", "frosted")
               for w in words)


def _glass_by_name() -> int:
    """Materials the file names glass but that carry no transparency after
    the import (FBX has none to carry; KitBash kits, most vendors): they
    get a Principled transmission, so windows and a vending machine's
    front stop rendering opaque. Only a material with alpha 1 and no
    transmission yet is touched, and the log says how many."""
    n = 0
    for mat in bpy.data.materials:
        if not mat.users or not _named_glass(mat.name) or mat.get("pharos_recipe") \
                or not mat.use_nodes or not mat.node_tree:
            continue
        bsdf = next((x for x in mat.node_tree.nodes if x.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        alpha = bsdf.inputs.get("Alpha")
        tw = bsdf.inputs.get("Transmission Weight") or bsdf.inputs.get("Transmission")
        if alpha is None or tw is None or alpha.is_linked or alpha.default_value < 0.999 \
                or tw.is_linked or tw.default_value > 0.0:
            continue
        tw.default_value = 1.0
        rough = bsdf.inputs.get("Roughness")
        if rough is not None and not rough.is_linked:
            rough.default_value = min(float(rough.default_value), 0.1)
        _refractive(mat)
        n += 1
    if n:
        print(f"  {n} material(s) named glass made transmissive (the file carried no transparency)")
    return n


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
    from scene_manifest import collapse_slot_maps  # on sys.path via build()
    built = {}
    for s in slots:
        # one map per role, the same pick as the registry's `primary`; a
        # `resolved: false` slot collapses to {} and keeps the FBX material
        maps = collapse_slot_maps(s)
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


def _remap_dead_kb3d_images():
    """KitBash FBX files reference an EMPTY
    'kb3d_<kit>.blender.native/KB3DTextures/4k' folder; the shipped
    textures live in the sibling 'kb3d_<kit>.png.2k' folder. Repoint any
    missing image whose path matches the dead pattern (V5 D8).

    Runs ONCE after all imports with a pre-indexed kit-folder cache:
    called per-asset with per-image recursive globs it was O(assets x
    images x drive) and cost 45+ minutes on a 77-asset build
    (real-world finding); the cached pass takes seconds.
    """
    # one-time: index every kb3d_*.png.2k folder under the configured
    # roots into {kit-key: [folder, ...]} -- keys come from the SAME
    # helper the lookup uses, so the two sides cannot drift apart again
    # (they once did: cache stripped 'kb3d_', lookup kept it, and the
    # cache could never hit)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from asset_service.kb3d_paths import (kit_key_from_texture_dir,
                                              dead_texture_kit)
    except Exception:
        return    # cannot remap without the helpers; imports stay as-is

    kit_folders = {}
    try:
        from asset_service import config as _cfg
        _c = _cfg.load()
        for key in ("kitbash_root", "library_root"):
            root = _c.get(key)
            if not root:
                continue
            for found in Path(root).glob("**/kb3d_*.png.2k"):
                kit_folders.setdefault(
                    kit_key_from_texture_dir(found.name), []).append(found)
    except Exception:
        pass

    fixed = 0
    for img in bpy.data.images:
        src = bpy.path.abspath(img.filepath)
        if not src or Path(src).is_file():
            continue
        p = Path(src.replace("\\", "/"))
        kit_dir, kit = dead_texture_kit(p)
        if kit_dir is None:
            continue
        kit_dir = Path(kit_dir)
        # the FBX records the library-of-origin path; when the library was
        # copied elsewhere that dir does not exist -> resolve from the cache
        candidates = []
        if kit_dir.is_dir():
            candidates += [f / p.name for f in sorted(kit_dir.glob("kb3d_*.png.2k"))
                           if not kit or kit_key_from_texture_dir(f.name) == kit]
        for folder in (kit_folders.get(kit, []) if kit
                       else [f for fs in kit_folders.values() for f in fs]):
            candidates.append(folder / p.name)
        hit = next((c for c in candidates if c.is_file()), None)
        if hit is not None:
            img.filepath = str(hit)
            fixed += 1
    if fixed:
        print(f"  remapped {fixed} dead KitBash texture reference(s) "
              f"to the shipped .png.2k folder")


def _apply_texture_sets(manifest, ground_y, roots_by_id):
    """manifest.textures[] — apply each set as a ground plane (V5 D5: the
    block was validated but never did anything). Entry: {folder, set,
    apply_to ('ground'|'ground_plane'|omit), size (m, default 20)}."""
    for tex in manifest.get("textures", []) or []:
        # apply_to: "ground" (default) | an asset id | a LIST mixing both.
        # This is how the gigabytes of library texture sets reach assets
        # that carry no recipe of their own (scanner-indexed, modelled).
        raw_at = tex.get("apply_to") or "ground"
        targets = ([raw_at] if isinstance(raw_at, str) else
                   [t for t in raw_at if isinstance(t, str)])
        wants_ground = any(t.lower() in
                           ("ground", "ground_plane", "floor")
                           for t in targets)
        asset_targets = [t for t in targets
                         if t.lower() not in ("ground", "ground_plane",
                                              "floor")]
        folder = Path(tex.get("folder") or "")
        setname = tex.get("set") or ""
        if not folder.is_dir():
            print(f"    WARNING: texture folder missing: {folder}")
            continue
        # which file is which map: the registry's answer for an indexed
        # set, else read from the file names (scene_manifest.texture_maps;
        # the substring guess that lived here missed '<name>_Color' and
        # took the DirectX normal map)
        from scene_manifest import texture_maps
        recipe, source = texture_maps(tex)
        if source == "folder":
            print(f"    note: set name {setname!r} matches no "
                  f"filename in {folder} -- using all maps in the "
                  f"folder")
        if not recipe:
            print(f"    WARNING: no map channels recognised in {folder} "
                  f"(set {setname!r})")
            continue
        if "albedo" not in recipe:
            print(f"    WARNING: set {setname!r} has no colour map "
                  f"(found: {sorted(recipe)}): the surface stays grey")
        applied = []
        if wants_ground:
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
            applied.append(f"ground plane {size}x{size}m")

        for aid in asset_targets:
            root = roots_by_id.get(aid)
            if root is None:
                print(f"    WARNING: textures[] apply_to unknown asset "
                      f"id '{aid}' (known: {sorted(roots_by_id)})")
                continue
            mat = _make_material(f"{aid}_{setname or 'tex'}"[:60], recipe)
            n = 0
            for child in _all_meshes(root):
                if child.data.users > 1:
                    # a linked duplicate shares its mesh: this copy gets its
                    # own before its slots change, or every copy of the key
                    # took the set (and the last set won)
                    child.data = child.data.copy()
                if child.data.materials:
                    child.data.materials.clear()
                child.data.materials.append(mat)
                n += 1
            applied.append(f"{n} mesh(es) of '{aid}'")

        if applied:
            print(f"    texture set '{setname or folder.name}' "
                  f"[{sorted(recipe.keys())}] -> {'; '.join(applied)}")


def _is_identity(m, tol=1e-4) -> bool:
    return all(abs(m[r][c] - (1.0 if r == c else 0.0)) <= tol for r in range(4) for c in range(4))


def _bbox(points):
    lo = [min(p[k] for p in points) for k in range(3)]
    hi = [max(p[k] for p in points) for k in range(3)]
    return lo, hi


def _skin_correction(arm, meshes):
    """The one rotation (or None) that brings the skin of `arm` onto its
    bones, judged from ALL its skinned vertices at once: the mesh box and
    the bones' box are compared on every axis and centre for identity,
    the quarter turns and the half turns, and the closest wins (identity
    on a tie). A rule per mesh and per longest axis once turned correctly
    placed eyes and shoes off the body and lay a T-posed body flat because
    its arm span beat its height."""
    from mathutils import Matrix, Vector
    bones = arm.data.bones
    if not bones:
        return None
    b_lo, b_hi = _bbox([b.head_local for b in bones] + [b.tail_local for b in bones])
    pts = [v.co for me in meshes for v in me.data.vertices]
    if not pts:
        return None
    m_lo, m_hi = _bbox(pts)
    corners = [Vector((x, y, z)) for x in (m_lo[0], m_hi[0]) for y in (m_lo[1], m_hi[1]) for z in (m_lo[2], m_hi[2])]
    b_ext = [b_hi[k] - b_lo[k] for k in range(3)]
    b_mid = [(b_hi[k] + b_lo[k]) / 2 for k in range(3)]
    cands = [None] + [Matrix.Rotation(math.radians(a), 4, ax) for ax in "XYZ" for a in (90.0, -90.0, 180.0)]
    best, best_score = None, None
    for rot in cands:
        pts_r = [rot @ c for c in corners] if rot is not None else corners
        lo, hi = _bbox(pts_r)
        score = sum(abs((hi[k] - lo[k]) - b_ext[k]) for k in range(3)) \
            + sum(abs((hi[k] + lo[k]) / 2 - b_mid[k]) for k in range(3))
        if best_score is None or score < best_score - 1e-9:
            best, best_score = rot, score
    return best


def _rebind_skin(arm, meshes) -> int:
    """A skinned body whose meshes sit in another frame than their armature
    (Character Creator exports: the skin in a Y-up metre frame, the
    armature and every clip Z-up in centimetres) tears apart in any pose.
    Bake each mesh skinned to `arm` into armature space (shape keys with
    it: a body with morphs once shrank to a dot) and parent it there; then
    turn the whole skin onto the bones with one rotation decided for all
    its meshes (see _skin_correction). Meshes skinned to another armature
    are left alone. Returns how many were re-bound."""
    from mathutils import Matrix
    bpy.context.view_layer.update()
    skinned = [me for me in meshes if me.type == 'MESH'
               and any(m.type == 'ARMATURE' and m.object is arm for m in me.modifiers)]
    fixed = 0
    for me in skinned:
        rel = arm.matrix_world.inverted() @ me.matrix_world
        if _is_identity(rel):
            continue
        if me.data.users > 1:
            me.data = me.data.copy()       # shared data would be baked once per object
        me.data.transform(rel, shape_keys=True)
        me.parent = arm
        me.matrix_parent_inverse = Matrix.Identity(4)
        me.matrix_basis = Matrix.Identity(4)
        fixed += 1
    if fixed:
        rot = _skin_correction(arm, skinned)
        if rot is not None:
            for me in skinned:
                if me.data.users > 1:
                    me.data = me.data.copy()
                me.data.transform(rot, shape_keys=True)
            print(f"    re-bound {fixed} skinned mesh(es) into armature space and turned the skin onto the bones")
        else:
            print(f"    re-bound {fixed} skinned mesh(es) into armature space")
    return fixed


def _fcurves_of(act):
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


def _settle_body(body_objs):
    """One imported body: its root, its loose parts under it, its skin
    re-bound. The hierarchy root is the armature that deforms the body
    (the one with the most skinned vertices: an accessory rig once became
    the root and the body stayed at the origin), else the parentless object
    with the most below it (the first parentless object once was a stray
    empty, so no instance was placed)."""
    arms = [o for o in body_objs if o.type == 'ARMATURE']

    def _skinned(a):
        return sum(len(m.data.vertices) for m in body_objs if m.type == 'MESH'
                   and any(md.type == 'ARMATURE' and md.object is a for md in m.modifiers))
    body_root = max(arms, key=lambda a: (_skinned(a), len(a.children_recursive))) if arms else \
        max((o for o in body_objs if o.parent is None),
            key=lambda o: len(o.children_recursive), default=body_objs[0])
    while body_root.parent is not None and body_root.parent in body_objs:
        body_root = body_root.parent
    family = [body_root] + list(body_root.children_recursive)
    adopted = 0
    for o in body_objs:
        if o.parent is None and o is not body_root and o not in family:
            if o.type == 'EMPTY' and not o.children:
                bpy.data.objects.remove(o, do_unlink=True)   # a stray empty of the import
            else:
                # a second family of the body file (an accessory rig, a loose
                # prop): it moves, copies and animates with the body
                o.parent = body_root
                o.matrix_parent_inverse = body_root.matrix_world.inverted()
                adopted += 1
    if adopted:
        print(f"    {adopted} loose object(s) of the body file parented under the body")
    family = [body_root] + list(body_root.children_recursive)
    body_arm = body_root if body_root.type == 'ARMATURE' else next(
        (o for o in family if o.type == 'ARMATURE'), None)
    if body_arm is not None:
        _rebind_skin(body_arm, [o for o in family if o.type == 'MESH'])
    return body_root


def _build_crowd(group, ground_y):
    """Import a body + animation; place instances (a ring around the
    position, or the given `instances` with position and heading); every
    instance plays the clip's own take, staggered per instance."""
    body_fbx = group["body_fbx"]
    anim_fbx = group["animation_fbx"]
    instances = group.get("instances") or []
    count = len(instances) if instances else group.get("count", 1)
    spacing = group.get("spacing", 2.0)
    base_pos = group.get("position", [0, 0, 0])
    anim_offset = group.get("animation_offset", 0.0)
    phase_spread = group.get("phase_spread")

    print(f"  [crowd] {count} instances" + (f", spacing {spacing}m" if not instances else ", placed one by one"))
    sys.stdout.flush()

    before_objs = _scene_objects()
    key = str(body_fbx).replace("\\", "/").lower()
    cached = _BODIES.get(key)
    if cached is not None and cached[0].name in bpy.data.objects:
        # the same body in another clip group: a linked duplicate of the
        # imported, re-bound body (six groups once imported it six times,
        # 27 s to 115 s), with the import's own rotation and no clip yet
        # (it once carried the first group's heading and take)
        body_root = _duplicate_hierarchy(cached[0])
        body_root.rotation_euler = cached[1].copy()
        body_root.location = (0.0, 0.0, 0.0)
        body_objs = [body_root] + list(body_root.children_recursive)
        for o in body_objs:
            if o.animation_data:
                o.animation_data_clear()
        print("    body: linked duplicate of the earlier import")
    else:
        bpy.ops.import_scene.fbx(filepath=body_fbx, global_scale=1.0)
        body_objs = [o for o in bpy.context.scene.objects if o not in before_objs]
        if not body_objs:
            print(f"    WARNING: body import failed: {body_fbx}")
            return
        body_root = _settle_body(body_objs)
        _BODIES[key] = (body_root, body_root.rotation_euler.copy())

    # the animation: the take the importer gives the clip's own armature
    # (the body file's takes are never candidates -- a body with a 60-frame
    # idle once won over the 30-frame clip; nor another object's action --
    # a keyed CC_Game_Tongue empty of the same length once drove the
    # armature's location); then DELETE the animation import's own objects
    # -- otherwise a ghost armature stays parked at the origin (V5 D6:
    # 5-instance crowd produced 6 armatures)
    before_actions = set(bpy.data.actions)
    bpy.ops.import_scene.fbx(filepath=anim_fbx, global_scale=1.0)
    anim_objs = [o for o in bpy.context.scene.objects
                 if o not in before_objs and o not in body_objs]

    def _frames(a):
        return a.frame_range[1] - a.frame_range[0]
    new_actions = [a for a in bpy.data.actions if a not in before_actions]
    anim_arm = next((o for o in anim_objs if o.type == 'ARMATURE'), None)
    action = anim_arm.animation_data.action if (anim_arm is not None and anim_arm.animation_data) else None
    if action is None or _frames(action) <= 1:
        bone_acts = [a for a in new_actions if _frames(a) > 1
                     and any(fc.data_path.startswith("pose.bones") for fc in _fcurves_of(a))]
        action = max(bone_acts or [a for a in new_actions if _frames(a) > 1], key=_frames, default=None)
    if action is None:
        print(f"    WARNING: no animation take longer than one frame in {anim_fbx}")
    else:
        # every body loops its clip, the unshifted ones too (instance 0 once
        # froze on the last key while its neighbours kept walking)
        for fc in _fcurves_of(action):
            if not any(m.type == 'CYCLES' for m in fc.modifiers):
                fc.modifiers.new('CYCLES')
    bpy.ops.object.select_all(action='DESELECT')
    for o in anim_objs:
        if o.name in bpy.data.objects:
            o.select_set(True)
    if bpy.context.selected_objects:
        bpy.ops.object.delete()

    def _offset_action(src, frames):
        """Copy an action with keyframes shifted -- linked duplicates share
        one action, so per-instance stagger needs per-instance copies
        (V5 D6: documented stagger resolved to 0 for every instance)."""
        new = src.copy()
        for fc in _fcurves_of(new):
            for kp in fc.keyframe_points:
                kp.co.x += frames
                kp.handle_left.x += frames
                kp.handle_right.x += frames
        new.name = f"{src.name}_{frames:+d}f"
        # the clip loops, so any phase into it is a pose of the cycle
        for fc in _fcurves_of(new):
            if not any(m.type == 'CYCLES' for m in fc.modifiers):
                fc.modifiers.new('CYCLES')
        return new

    # placement. Manifest positions are Y-up (y = height): the same Rx90
    # mapping assets get; a ring lies on Blender's ground plane (X/Y)
    bx, by, bz = (base_pos[0], -base_pos[2], base_pos[1])
    base_rot = body_root.rotation_euler.copy()       # the import's own rotation (a Y-up file: X 90)
    for i in range(count):
        if i == 0:
            inst = body_root
        else:
            # linked duplicate of the WHOLE hierarchy (shares mesh data):
            # duplicating only the armature root once left every extra
            # instance an invisible empty skeleton
            inst = _duplicate_hierarchy(body_root)

        if instances:
            spec = instances[i] or {}
            pos = spec.get("position", base_pos)
            inst.location = Vector((pos[0], -pos[2], pos[1]))
            # from the import's rotation, not from what the copied object
            # carries: instance 0's heading once added up into every copy
            inst.rotation_euler = base_rot.copy()
            heading = spec.get("heading")
            if heading is not None:
                inst.rotation_euler.z += math.radians(float(heading))   # yaw about the vertical
        else:
            angle = (i / max(count, 1)) * 2 * math.pi
            radius = spacing * max(1, count // 6)
            inst.location = Vector((
                bx + math.cos(angle) * radius,
                by + math.sin(angle) * radius,
                bz,
            ))

        # assign animation to the ARMATURE with a real per-instance offset
        if action:
            arm = inst if inst.type == 'ARMATURE' else next(
                (o for o in inst.children_recursive
                 if o.type == 'ARMATURE'), None)
            if arm:
                if not arm.animation_data:
                    arm.animation_data_create()
                ad = arm.animation_data
                # where in the clip this body stands: its own `phase`
                # (seconds), the group's `phase_spread`, the start delay
                # `animation_offset`, else spread over the cycle -- a
                # saved frame once showed one pose on 72 people
                fps = float(bpy.context.scene.render.fps or 24)
                spec = (instances[i] or {}) if instances else {}
                if spec.get("phase") is not None:
                    shift = -int(round(float(spec["phase"]) * fps))
                elif phase_spread is not None:
                    shift = -int(round(float(phase_spread) * fps * i / max(count, 1)))
                elif anim_offset:
                    shift = int(round(anim_offset * i * fps))
                else:
                    shift = -int(round(max(_frames(action), 1.0) * i / max(count, 1)))
                ad.action = action if shift == 0 else _offset_action(action, shift)
                # Blender 4.4+: an action only plays through a slot. With
                # none assigned the clip was attached and the body stood
                # still in every frame.
                if (getattr(ad, "action_slot", 0) is None
                        and getattr(ad, "action_suitable_slots", None)):
                    ad.action_slot = ad.action_suitable_slots[0]

    bpy.ops.object.select_all(action='DESELECT')


# --- CLI entry (when run via blender --python) ---
if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args:
        manifest = args[0]
        save = None
        if "--save" in args:
            save = args[args.index("--save") + 1]
        try:
            build(manifest, save, pack_textures="--pack" in args)
        except SystemExit:
            raise
        except Exception as exc:                          # noqa: BLE001
            # an importer that refuses a file, a save that cannot write:
            # Blender exits 0 after a traceback unless told otherwise, and
            # the agent read 'built' with no .blend anywhere
            import traceback
            traceback.print_exc()
            print(f"BUILD FAILED: {type(exc).__name__}: {exc} -- nothing was saved")
            sys.stdout.flush()
            sys.exit(4)
    else:
        print("Usage: blender --background --python scene_builder.py -- "
              "manifest.json [--save output.blend] [--pack]")
