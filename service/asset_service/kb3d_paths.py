"""Pure KitBash3D path/key helpers -- importable by plain Python tests
AND by scene_builder inside Blender (it already imports asset_service
modules for config).

The remap lookup once stripped the ``kb3d_`` prefix on the cache side but
kept it on the lookup side, so the cache could never hit. Both sides now
reduce through these functions, making the symmetry structural.
"""


def kit_key_from_native_dir(name: str) -> str:
    """'kb3d_Apocalypse.blender.native' -> 'apocalypse'.

    The ancestor folder name a dead FBX texture path carries."""
    base = (name or "").split(".blender.native")[0]
    if base.lower().startswith("kb3d_"):
        base = base[len("kb3d_"):]
    return base.lower()


def kit_key_from_texture_dir(name: str) -> str:
    """'kb3d_Apocalypse.png.2k' -> 'apocalypse'.

    The shipped-texture folder name the cache is built from."""
    base = name or ""
    if base.lower().endswith(".png.2k"):
        base = base[: -len(".png.2k")]
    if base.lower().startswith("kb3d_"):
        base = base[len("kb3d_"):]
    return base.lower()


def dead_texture_kit(path) -> tuple:
    """(kit folder, kit key or '') for a dead KitBash FBX texture path, or
    (None, '') when the path is not one.

    Two kit layouts ship: 'kb3d_<kit>.blender.native/KB3DTextures/4k/X.png'
    (key from the native folder) and the .blend in the kit root with
    'KB3DTextures/4k/X.png' beside it (no key: the kit folder's own
    'kb3d_*.png.2k' holds the maps). The second once matched nothing and
    every texture of such a kit stayed dead."""
    from pathlib import PurePath
    p = PurePath(str(path).replace("\\", "/"))
    for anc in p.parents:
        low = anc.name.lower()
        if low.startswith("kb3d_") and ".blender.native" in low:
            return anc.parent, kit_key_from_native_dir(anc.name)
    for anc in p.parents:
        if anc.name.lower() == "kb3dtextures":
            return anc.parent, ""
    return None, ""
