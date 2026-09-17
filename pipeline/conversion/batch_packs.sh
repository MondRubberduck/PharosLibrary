#!/usr/bin/env bash
# batch_packs.sh -- run convert_packs.sh over every Unreal pack in the assets
# root, smallest first, isolating failures so one bad pack cannot abort the run.
#
# Usage:
#   batch_packs.sh [--assets-root DIR] [--max-packs N] [--start-after PACK]
#                  [--only PACK ...] [--force] [--dry-run]
#
#   --max-packs N     stop after N packs were attempted successfully
#   --start-after P   resume: skip everything up to and including pack P
#   --only P          run just this pack (repeatable)
#   --force           re-run packs that already have a manifest
#   --dry-run         print the plan and exit
#
# Every pack writes status/<slug>.json and one line to status/packs.log.
set -uo pipefail

CONV_ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$CONV_ROOT"          # the helper scripts live beside this driver
STATUSDIR="$CONV_ROOT/status"
ASSETS_ROOT="${ASSETS_ROOT:?set ASSETS_ROOT to your pack source root}"
MAX_PACKS=0
START_AFTER=""
FORCE=0
DRY=0
ONLY=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --assets-root) ASSETS_ROOT="$2"; shift 2 ;;
    --max-packs)   MAX_PACKS="$2"; shift 2 ;;
    --start-after) START_AFTER="$2"; shift 2 ;;
    --only)        ONLY+=("$2"); shift 2 ;;
    --force)       FORCE=1; shift ;;
    --dry-run)     DRY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$STATUSDIR" "$CONV_ROOT/logs"

# ---- build the plan: every pack with >0 assets, ascending by asset count ----
PLAN="$(python - "$TOOLS/pack_discovery.py" "$ASSETS_ROOT" <<'PY'
import json, subprocess, sys
disc, root = sys.argv[1], sys.argv[2]
out = subprocess.run([sys.executable, disc, "--scan-all", root],
                     capture_output=True, text=True, encoding="utf-8")
if out.returncode not in (0, 1) or not out.stdout.strip():
    sys.stderr.write(out.stderr or "discovery produced no output\n")
    sys.exit(2)
d = json.loads(out.stdout)
rows = [r for r in d if r.get("ok") and r.get("uasset_total", 0) > 0]
rows.sort(key=lambda r: (r["uasset_total"], r["pack"]))
for r in rows:
    print("%s\t%s\t%s" % (r["uasset_total"], r["pack"], r["primary_game_root"]))
skipped = [r["pack"] for r in d if not (r.get("ok") and r.get("uasset_total", 0) > 0)]
if skipped:
    sys.stderr.write("[batch] skipped (no .uasset): %s\n" % ", ".join(skipped))
PY
)"
if [[ -z "$PLAN" ]]; then echo "no packs discovered" >&2; exit 3; fi

echo "=== batch plan (ascending asset count) ==="
echo "$PLAN" | nl -ba

if [[ "$DRY" == "1" ]]; then exit 0; fi

TOTAL_RUN=0
TOTAL_OK=0
TOTAL_FAIL=0
SKIPPED=0
SEEN_START=$([[ -z "$START_AFTER" ]] && echo 1 || echo 0)
FAILED_PACKS=()

while IFS=$'\t' read -r ASSETS PACK GROOT; do
  [[ -z "$PACK" ]] && continue

  if [[ ${#ONLY[@]} -gt 0 ]]; then
    keep=0
    for o in "${ONLY[@]}"; do [[ "$o" == "$PACK" ]] && keep=1; done
    [[ "$keep" == "1" ]] || continue
  fi

  # resume support: skip everything up to and including START_AFTER
  if [[ "$SEEN_START" == "0" ]]; then
    if [[ "$PACK" == "$START_AFTER" ]]; then SEEN_START=1; fi
    echo "[batch] skip (resume): $PACK"
    SKIPPED=$((SKIPPED+1))
    continue
  fi

  SLUG="$(echo "$PACK" | tr ' /\\' '___')"
  if [[ "$FORCE" == "0" && -f "$ASSETS_ROOT/$PACK/Exports/manifest.json" ]]; then
    echo "[batch] skip (already exported): $PACK"
    SKIPPED=$((SKIPPED+1))
    continue
  fi

  if [[ "$MAX_PACKS" -gt 0 && "$TOTAL_RUN" -ge "$MAX_PACKS" ]]; then
    echo "[batch] stop: reached --max-packs $MAX_PACKS"
    break
  fi

  echo
  echo "############################################################"
  echo "# [$((TOTAL_RUN+1))] $PACK   ($ASSETS assets, $GROOT)"
  echo "############################################################"
  TOTAL_RUN=$((TOTAL_RUN+1))

  S=$(date +%s)
  bash "$TOOLS/convert_packs.sh" --pack "$PACK" --assets-root "$ASSETS_ROOT" --label full \
    2>&1 | tee "$CONV_ROOT/logs/driver_${SLUG}.log"
  RC=${PIPESTATUS[0]}
  E=$(( $(date +%s) - S ))

  if [[ $RC -eq 0 ]]; then
    TOTAL_OK=$((TOTAL_OK+1))
    echo ">>> [$PACK] OK  (mkdir $E s) exit=0"
  else
    TOTAL_FAIL=$((TOTAL_FAIL+1))
    FAILED_PACKS+=("$PACK(rc=$RC)")
    echo ">>> [$PACK] FAILED exit=$RC after ${E}s -- run isolated, continuing"
  fi
done <<< "$PLAN"

echo
echo "=================== batch summary ==================="
echo "packs run    : $TOTAL_RUN"
echo "packs OK     : $TOTAL_OK"
echo "packs FAILED : $TOTAL_FAIL  ${FAILED_PACKS[*]:-}"
echo "packs skipped: $SKIPPED"
echo "status lines : $STATUSDIR/packs.log"
[[ $TOTAL_FAIL -eq 0 ]]
