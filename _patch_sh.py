"""One-shot shell-driver patcher (deleted after use): env-overridable paths."""
from pathlib import Path


def patch(path, pairs):
    p = Path(path)
    s = p.read_text(encoding="utf-8", errors="replace")
    for old, new in pairs:
        assert old in s, f"{path}: missing {old[:70]!r}"
        s = s.replace(old, new)
    p.write_text(s, encoding="utf-8")
    print("patched", path)


SELF = 'CONV_ROOT="$(cd "$(dirname "$0")" && pwd)"'
UE = ('UE_EXE="${UE_EXE:-}"   # e.g. /e/Epic/Games/UE_5.7/Engine/'
      'Binaries/Win64/UnrealEditor-Cmd.exe')
BL = ('BLENDER_EXE="${BLENDER_EXE:-}"   # e.g. "/c/Program Files/'
      'Blender Foundation/Blender 5.1/blender.exe"')

for sh in ("pipeline/conversion/convert_packs.sh",
           "pipeline/conversion/relink_pack.sh"):
    patch(sh, [
        ('CONV_ROOT="/d/Pipeline/kiosk/conversion"', SELF),
        ('ASSETS_ROOT="D:/3D_Assets/Leartes Env_ gumroad"',
         'ASSETS_ROOT="${ASSETS_ROOT:-}"   # your converted-pack source root'),
        ('UE_EXE="/e/Epic/Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"',
         UE),
        ('BLENDER_EXE="/c/Program Files/Blender Foundation/Blender 5.1/blender.exe"',
         BL),
    ])

patch("pipeline/conversion/convert_all.sh", [
    ('cd /d/Pipeline/kiosk/conversion/tools || exit 2',
     'cd "$(dirname "$0")" || exit 2'),
    ('ASSETS_ROOT="${ASSETS_ROOT:-/d/3D_Assets/Leartes Env_ gumroad}"',
     'ASSETS_ROOT="${ASSETS_ROOT:?set ASSETS_ROOT to your pack source root}"'),
])
patch("pipeline/conversion/batch_packs.sh", [
    ('CONV_ROOT="/d/Pipeline/kiosk/conversion"',
     'CONV_ROOT="$(cd "$(dirname "$0")" && pwd)"'),
    ('ASSETS_ROOT="D:/3D_Assets/Leartes Env_ gumroad"',
     'ASSETS_ROOT="${ASSETS_ROOT:?set ASSETS_ROOT to your pack source root}"'),
])
print("SHELL PATCHED")
