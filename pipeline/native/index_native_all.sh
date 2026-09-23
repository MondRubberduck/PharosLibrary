#!/usr/bin/env bash
# index_native_all.sh -- count/index the geometry that is NOT in the converted
# packs: the kit .blend files (LOADED, one session each; list from
# native_blends.txt) plus the importable FBX/OBJ/USD files (one session for
# all of them; list from native_manifest.json). Both lists are derived by
# make_native_manifest2.py -- no counts or folder names are hardcoded here.
# Writes a timestamped jsonl so nothing has to be deleted first.
#
# Exit codes: 0 ok · 2 no worklist (run make_native_manifest2.py first) ·
#             3 no Blender found (set BLENDER_EXE) ·
#             9 one or more Blender runs failed · 8 no output file at all.
#             A crashed Blender must never again read as success: the old
#             pipeline-to-grep swallowing lost every exit code and the
#             driver always exited 0.
set -u

T="$(cd "$(dirname "$0")" && pwd)"
# BLENDER_EXE is the repo-wide convention; BLENDER kept for compatibility;
# neither set -> the Python drivers' finder (pipeline/_config.py blender_exe)
BLENDER="${BLENDER_EXE:-${BLENDER:-}}"
if [[ -z "$BLENDER" ]]; then
  FIND='import _config; print(_config.blender_exe())'
  BLENDER="$(cd "$T/.." && { python -c "$FIND" 2>/dev/null || python3 -c "$FIND" 2>/dev/null; })"
  BLENDER="${BLENDER//$'\r'/}"
fi
LIST="$T/native_manifest.json"
BLENDS="$T/native_blends.txt"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$T/native_index_${STAMP}.jsonl"
echo "OUT=$OUT"

if [[ ! -f "$LIST" ]]; then
  echo "ERROR: $LIST missing -- run make_native_manifest2.py first" >&2
  exit 2
fi
if [[ -z "$BLENDER" ]]; then
  echo "ERROR: Blender not found -- set BLENDER_EXE to your Blender executable" >&2
  exit 3
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

echo "--- importable model files (single Blender session) ---"
run_one env AMNATIVE_LIST="$LIST" AMNATIVE_OUT="$OUT" "$BLENDER" \
  --background --factory-startup --python "$T/index_native.py"
if [[ $? -ne 0 ]]; then
  FAILS=$((FAILS+1))
  echo "PASS FAILED (Blender exit nonzero)"
fi

# .blend OBJECTS are enumerated by run_blends.py into native_index_blends.jsonl
# (-> promote_blends.py, which skips already-exported kits). They must never
# land in $OUT: promote_native.py publishes this file wholesale, and a
# second pass here listed every kit assembly twice.

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
