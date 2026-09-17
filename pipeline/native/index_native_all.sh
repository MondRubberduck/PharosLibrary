#!/usr/bin/env bash
# index_native_all.sh -- count/index the geometry that is NOT in the converted
# Leartes packs: the 11 KitBash3D .blend kits (LOADED, one session each) plus
# the importable FBX/OBJ/USD files (one session for all of them).
# Writes a timestamped jsonl so nothing has to be deleted first.
set -u

T="$(cd "$(dirname "$0")" && pwd)"
BLENDER="${BLENDER:-C:/Program Files/Blender Foundation/Blender 5.1/blender.exe}"
LIST="$T/native_manifest.json"
BLENDS="$T/native_blends.txt"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$T/native_index_${STAMP}.jsonl"
echo "OUT=$OUT"

echo "--- [1/2] importable model files (single Blender session) ---"
AMNATIVE_LIST="$LIST" AMNATIVE_OUT="$OUT" "$BLENDER" --background --factory-startup \
  --python "$T/index_native.py" 2>&1 | grep -E 'NATIVE_DONE|Error:' | tail -3

echo "--- [2/2] .blend kits (one session each) ---"
while IFS= read -r path; do
  IFS= read -r section
  [ -z "$path" ] && continue
  printf '  %-58s ' "$(basename "$path")"
  AMNATIVE_OUT="$OUT" AMNATIVE_SECTION="$section" "$BLENDER" --background --factory-startup \
    "$path" --python "$T/index_native.py" 2>&1 | grep -E 'NATIVE_DONE' | tail -1
done < "$BLENDS"

echo
echo "records written: $(wc -l < "$OUT")"
echo "OUT=$OUT"
