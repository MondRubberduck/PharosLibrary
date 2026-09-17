#!/usr/bin/env bash
# index_blends.sh -- stage 2 only: LOAD each .blend kit in its own Blender
# session and index its mesh objects.  Appends to the existing native index so
# no file has to be deleted first.
set -u

T="$(cd "$(dirname "$0")" && pwd)"
BLENDER="${BLENDER:-C:/Program Files/Blender Foundation/Blender 5.1/blender.exe}"
BLENDS="$T/native_blends.txt"
OUT="$T/native_index_blends.jsonl"

echo "OUT=$OUT"
echo "--- .blend kits (one Blender session each) ---"
while IFS= read -r path; do
  IFS= read -r section
  [ -z "$path" ] && continue
  printf '  %-56s ' "$(basename "$path" | cut -c1-56)"
  AMNATIVE_OUT="$OUT" AMNATIVE_SECTION="$section" "$BLENDER" --background --factory-startup \
    "$path" --python "$T/index_native.py" 2>&1 | grep -E 'NATIVE_DONE' | tail -1
done < "$BLENDS"

echo
echo "records: $(wc -l < "$OUT" 2>/dev/null || echo 0)"
echo "DONE_OUT=$OUT"
