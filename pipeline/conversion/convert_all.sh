#!/usr/bin/env bash
# convert_all.sh -- convert every Unreal pack under the assets root that does
# not already have a completed export.  Auto-discovers packs so newly
# downloaded folders are picked up without editing this file.
#
#   convert_all.sh                     # every pack not already done
#   convert_all.sh "Pack A" "Pack B"   # only the named packs
#   FORCE=1 convert_all.sh             # ignore the skip guard and redo
#
# Writes <pack>\Exports\{FBX,Textures}\ + manifest.json inside each pack.
# Never deletes anything.
set -u
cd "$(dirname "$0")" || exit 2

ASSETS_ROOT="${ASSETS_ROOT:?set ASSETS_ROOT to your pack source root}"

if [ "$#" -gt 0 ]; then
  PACKS=("$@")
else
  PACKS=()
  while IFS= read -r d; do
    [ -n "$d" ] && PACKS+=("$d")
  done < <(find "$ASSETS_ROOT" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null | sort)
fi

DONE=0; SKIP=0; BAD=0
for PACK in "${PACKS[@]}"; do
  SRC="$ASSETS_ROOT/$PACK"
  if [ ! -d "$SRC" ]; then echo "SKIP  $PACK  (folder missing)"; SKIP=$((SKIP+1)); continue; fi
  if [ -z "$(find "$SRC" -name '*.uasset' -print -quit 2>/dev/null)" ]; then
    echo "SKIP  $PACK  (contains no .uasset -- not an Unreal pack)"
    SKIP=$((SKIP+1)); continue
  fi
  MAN="$SRC/Exports/manifest.json"
  if [ -z "${FORCE:-}" ] && [ -f "$MAN" ]; then
    N=$(python -c "import json,sys;print((json.load(open(sys.argv[1],encoding='utf-8')).get('counts') or {}).get('fbx_written',0))" "$(cygpath -w "$MAN")" 2>/dev/null)
    if [ "${N:-0}" -gt 0 ] 2>/dev/null; then
      echo "SKIP  $PACK  (already converted: $N meshes)"
      SKIP=$((SKIP+1)); continue
    fi
  fi
  echo "================================================================"
  echo "PACK  $PACK     ($(date +%H:%M:%S))"
  echo "================================================================"
  ./convert_packs.sh --pack "$PACK" --label full
  RC=$?
  if [ $RC -eq 0 ]; then DONE=$((DONE+1)); else BAD=$((BAD+1)); echo "PACK FAILED rc=$RC: $PACK"; fi
done

echo
echo "==== convert_all summary: converted=$DONE skipped=$SKIP failed=$BAD at $(date +%H:%M:%S) ===="
