"""What a file is, by extension: the ONE table every module reads.

Six writers used to carry their own list of what counts as a 3D file,
and they disagreed: a folder detected as models because of .usd files was
then scanned for nothing, the scanner's docstring promised .usd it never
read, and .uasset counted as a mesh in one place and as Unreal content
in another. Stdlib only; sets, lower-case, with the dot.
"""

from __future__ import annotations

# 3D model files, whatever tool wrote them
MODEL = frozenset((
    ".fbx", ".obj", ".glb", ".gltf", ".stl", ".ply", ".dae", ".abc", ".3ds",
    ".usd", ".usda", ".usdc", ".usdz", ".max", ".ma", ".mb"))
# the model files the stdlib scanner lists (and measures where it can)
MODEL_SCAN = frozenset((".fbx", ".obj", ".glb", ".gltf", ".stl"))
BLEND = frozenset((".blend",))
# animation clips (.fbx is both: the file's content decides)
CLIP = frozenset((".fbx", ".bvh", ".anim", ".c3d", ".trc"))
AUDIO = frozenset((
    ".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aif", ".aiff", ".aac", ".wma"))
IMAGE = frozenset((
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".exr",
    ".hdr", ".dds", ".tga", ".psd", ".gif", ".tx", ".sbsar"))
HDRI = frozenset((".hdr", ".exr"))
UNREAL = frozenset((".uasset", ".umap", ".uexp", ".ubulk", ".upk"))
UNITY = frozenset((".unity", ".prefab", ".unitypackage", ".mat"))
ARCHIVE = frozenset((".zip", ".rar", ".7z"))
