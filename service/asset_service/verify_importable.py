"""Probe FBX importability in headless Blender — one file per process.

Blender 5.1's threaded FBX importer can hard-crash (EXCEPTION_ACCESS_-
VIOLATION, exit 0xC0000005) on individual files, with a 0-byte log
because stdout is buffered in --background. This probe makes that a
five-second answer instead of a debugging session (V5 cross-validation
D2: an API-recommended mesh crashed the builder with no advance signal).

Usage:
    blender --background --factory-startup --python verify_importable.py -- <file.fbx>

Exit codes: 0 = imports fine (object count printed), 1 = import raised,
0xC0000005 (or any non-zero) = Blender itself crashed on this file.
Batch from bash/powershell: one process per file, never batch in one
session (the crash is in Blender's threaded importer).
"""

import sys

import bpy

print("PROBE: importing...", flush=True)
args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if not args:
    print("PROBE: no file given", flush=True)
    sys.exit(2)
path = args[0]
try:
    bpy.ops.import_scene.fbx(filepath=path, global_scale=1.0)
except Exception as exc:                       # noqa: BLE001
    print(f"PROBE-FAIL: {type(exc).__name__}: {exc}", flush=True)
    sys.exit(1)
n = len(bpy.context.selected_objects)
meshes = sum(1 for o in bpy.context.selected_objects if o.type == 'MESH')
print(f"PROBE-OK: {n} objects ({meshes} meshes)", flush=True)
sys.stdout.flush()
sys.exit(0)
