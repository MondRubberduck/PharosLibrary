#!/usr/bin/env bash
# index_native_all.sh -- count/index the geometry that is NOT in the converted
# packs: the kit .blend files (LOADED, one session each; list from
# native_blends.txt) plus the importable FBX/OBJ/USD files (one session for
# all of them; list from native_manifest.json). Both lists are derived by
# make_native_manifest2.py -- no counts or folder names are hardcoded here.
# Writes a timestamped jsonl so nothing has to be deleted first.
#
# Exit codes: 0 ok · 2 no worklist (run make_native_manifest2.py first) ·
#             9 one or more Blender runs failed · 8 no output file at all.
#             A crashed Blender must never again read as success: the old
#             pipeline-to-grep swallowing lost every exit code and the
#             driver always exited 0.
set -u

T="$(cd "$(dirname "$0")" && pwd)"
# BLENDER_EXE is the repo-wide convention; BLENDER kept for compatibility
BLENDER="${BLENDER_EXE:-${BLENDER:-C:/Program Files/Blender Foundation/Blender 5.1/blender.exe}}"
LIST="$T/native_manifest.json"
BLENDS="$T/native_blends.txt"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$T/native_index_${STAMP}.jsonl"
echo "OUT=$OUT"

if [[ ! -f "$LIST" ]]; then
  echo "ERROR: $LIST missing -- run make_native_manifest2.py first" >&2
  exit 2
fi

FAILS=0

run_one() {
  # runs Blender, shows the marker/error lines, RETURNS Blender's exit code
  local log; log="$(mktemp "$T/native_blender_XXXX.log")"
  "$@" > "$log" 2>&1
  local rc=$?
  grep -E 'NATIVE_DONE|Error:' "$log" | tail -3
  rm -f "$log"
  return $rc
}

echo "--- [1/2] importable model files (single Blender session) ---"
run_one env AMNATIVE_LIST="$LIST" AMNATIVE_OUT="$OUT" "$BLENDER" \
  --background --factory-startup --python "$T/index_native.py"
if [[ $? -ne 0 ]]; then
  FAILS=$((FAILS+1))
  echo "PASS 1/2 FAILED (Blender exit nonzero)"
fi

echo "--- [2/2] .blend kits (one session each) ---"
if [[ -f "$BLENDS" ]]; then
  while IFS= read -r path; do
    IFS= read -r section
    [ -z "$path" ] && continue
    printf '  %-58s ' "$(basename "$path")"
    run_one env AMNATIVE_OUT="$OUT" AMNATIVE_SECTION="$section" "$BLENDER" \
      --background --factory-startup "$path" --python "$T/index_native.py"
    rc=$?
    if [[ $rc -ne 0 ]]; then
      FAILS=$((FAILS+1))
      echo "FAILED (Blender exit $rc)"
    fi
  done < "$BLENDS"
else
  echo "(no $BLENDS -- skipping the per-blend pass)"
fi

echo
if [[ ! -f "$OUT" ]]; then
  echo "ERROR: no output file was written ($OUT)" >&2
  exit 8
fi
echo "records written: $(wc -l < "$OUT")"
echo "OUT=$OUT"
if [[ $FAILS -gt 0 ]]; then
  echo "RESULT: FAIL ($FAILS Blender run(s) failed -- see above)" >&2
  exit 9
fi
echo "RESULT: OK"
