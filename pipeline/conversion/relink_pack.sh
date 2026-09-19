#!/usr/bin/env bash
# relink_pack.sh -- rebuild ONE pack's Exports\manifest.json material->texture
# wiring from the engine's own material parameter values.
#
# This is the manifest-level sibling of convert_packs.sh: same discovery step,
# same sandbox guard, same job-json plumbing, same verifier.  It writes exactly
# two things:
#   <pack>\Exports\manifest.json     rewritten in place (schema v2)
#   <pack>\Exports\manifest.v1.bak   pristine copy of what was there before
# Nothing else under the assets root is touched: no FBX, no texture, no .uasset.
#
# Usage:
#   relink_pack.sh --pack "MyPack" [options]
#
# Options:
#   --assets-root DIR   default: $ASSETS_ROOT env var
#   --label NAME        tags logs (default: relink)
#   --dry-run           resolve everything, rewrite nothing
#   --preview           dry run that ALSO writes the patched manifest to
#                       logs/preview_<slug>.json and verifies THAT against the real
#                       Exports tree -- hard evidence before the live file changes
#   --sync              robocopy the pack's Content into the sandbox first
#                       (off by default: relink only READS, and the sandbox that
#                        produced the manifest is normally still in place)
#   --limit-meshes N    smoke-test only
#   --verify-only       skip the engine step; just re-verify the current manifest
#
# Exit codes: 0 pass, 2 bad usage, 3 discovery failed, 4 sandbox guard failed,
#             5 engine run produced no result, 6 verification failed
set -uo pipefail

UE_EXE="${UE_EXE:-}"   # e.g. "/c/Program Files/Epic Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
CONV_ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$CONV_ROOT"    # the helper scripts live beside this driver
SANDBOX="$CONV_ROOT/sandbox"
SANDBOX_CONTENT="$SANDBOX/Content"
PROJECT="$SANDBOX/Sandbox.uproject"
LOGDIR="$CONV_ROOT/logs"
STATUSDIR="$CONV_ROOT/status"
ASSETS_ROOT="${ASSETS_ROOT:-}"   # your converted-pack source root

PACK=""
LABEL="relink"
LIMIT_MESHES=0
DO_SYNC=0
DO_APPLY=1
VERIFY_ONLY=0
PREVIEW=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)            PACK="$2"; shift 2 ;;
    --assets-root)     ASSETS_ROOT="$2"; shift 2 ;;
    --label)           LABEL="$2"; shift 2 ;;
    --limit-meshes)    LIMIT_MESHES="$2"; shift 2 ;;
    --dry-run)         DO_APPLY=0; shift ;;
    --preview)         DO_APPLY=0; PREVIEW=1; shift ;;
    --sync)            DO_SYNC=1; shift ;;
    --verify-only)     VERIFY_ONLY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$PACK" ]] || { echo "ERROR: --pack required" >&2; exit 2; }
[[ -f "$UE_EXE" ]] || { echo "ERROR: UE not found: $UE_EXE" >&2; exit 2; }
# the sandbox is deliberately NOT shipped (its EngineAssociation must match
# THIS machine's UE) -- generate it locally, once, then re-run this script
[[ -f "$PROJECT" ]] || {
  echo "ERROR: sandbox project missing: $PROJECT" >&2
  echo "       create it once with:" >&2
  echo "         python \"$(cygpath -m "$CONV_ROOT" 2>/dev/null || echo "$CONV_ROOT")/make_sandbox.py\"" >&2
  echo "       (empty throwaway UE project, Python plugin enabled;" >&2
  echo "        your sources are copied in and out, never modified)" >&2
  exit 2
}
mkdir -p "$LOGDIR" "$STATUSDIR"

SLUG="$(echo "$PACK" | tr ' /\\' '___')"
STAMP="$(date +%Y%m%d-%H%M%S)"
PACK_DIR="$(cygpath -m "$ASSETS_ROOT")/$PACK"
EXPORTS_DIR="$ASSETS_ROOT/$PACK/Exports"
MANIFEST="$EXPORTS_DIR/manifest.json"
DISC_JSON="$LOGDIR/discovery_${SLUG}.json"
JOB_JSON="$LOGDIR/relinkjob_${SLUG}_${LABEL}_${STAMP}.json"
UELOG="$LOGDIR/relink_ue_${SLUG}_${LABEL}_${STAMP}.log"
REPORT="$LOGDIR/relink_${SLUG}_${LABEL}_${STAMP}.json"
WIRING="$LOGDIR/wiring_${SLUG}.json"
VERIFY_JSON="$LOGDIR/verify_${SLUG}.json"
PREVIEW_MANIFEST="$LOGDIR/preview_${SLUG}.json"
STATUS_JSON="$STATUSDIR/relink_${SLUG}.json"

# preview mode: the patched manifest is written to the logs folder and THAT is
# verified against the real Exports tree, so the live file is never touched first
PREVIEW_M=""
VERIFY_ARGS=()
if [[ "$PREVIEW" == "1" ]]; then
  PREVIEW_M="$(cygpath -m "$PREVIEW_MANIFEST")"
  VERIFY_ARGS=(--manifest "$PREVIEW_MANIFEST")
elif [[ "$VERIFY_ONLY" == "1" ]]; then
  VERIFY_ARGS=()
fi

echo "================================================================"
echo "relink_pack.sh  pack=$PACK  label=$LABEL  apply=$DO_APPLY  verify_only=$VERIFY_ONLY"
echo "================================================================"

if [[ ! -f "$MANIFEST" ]]; then
  echo "FAIL: no manifest at $MANIFEST" >&2
  echo "      relink is a manifest PATCH-UP; run convert_packs.sh --pack \"$PACK\" first." >&2
  exit 3
fi

# ---------- 1. discovery (same step convert_packs.sh runs) ----------
echo
echo "--- [1/5] discovery ---"
DISC_LOG="$LOGDIR/discovery_${SLUG}.txt"
python "$TOOLS/pack_discovery.py" --pack "$PACK_DIR" --verbose > "$DISC_JSON" 2> "$DISC_LOG"
DISC_RC=$?
sed 's/^/  /' "$DISC_LOG" 2>/dev/null
if [[ $DISC_RC -ne 0 ]]; then
  echo "DISCOVERY FAILED for '$PACK' (zero assets, or no Content root):"
  cat "$DISC_JSON"
  exit 3
fi

eval "$(python - "$DISC_JSON" <<'PY'
import json, shlex, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print("CONTENT_ROOT=%s" % shlex.quote(d["content_root"]))
print("GAME_ROOTS=%s" % shlex.quote("|".join(d["game_roots"])))
print("UASSET_TOTAL=%s" % int(d["uasset_total"]))
PY
)"
: "${CONTENT_ROOT:?discovery produced no content_root}"
: "${GAME_ROOTS:?discovery produced no game roots}"
echo "content_root : $CONTENT_ROOT"
echo "game roots   : $GAME_ROOTS"
echo "uassets      : $UASSET_TOTAL"

# ---------- 2. sandbox guard: the assets we will read must be the ones the
#              manifest was exported from ----------
echo
echo "--- [2/5] sandbox guard ---"
if [[ "$DO_SYNC" == "1" ]]; then
  echo "syncing $CONTENT_ROOT -> $SANDBOX_CONTENT"
  MSYS_NO_PATHCONV=1 robocopy "$(cygpath -w "$CONTENT_ROOT")" "$(cygpath -w "$SANDBOX_CONTENT")" \
    /E /NFL /NDL /NJH /NJS /R:1 /W:1 /XD "__ExternalActors__" "__ExternalObjects__"
  RC=$?
  echo "robocopy exit=$RC (0-7 = success)"
  if [[ $RC -ge 8 ]]; then echo "SYNC FAILED" >&2; exit 4; fi
fi

MSYS2_ARG_CONV_EXCL='*' python - "$(cygpath -w "$MANIFEST")" "$(cygpath -w "$SANDBOX_CONTENT")" <<'PY'
import json, os, sys
man_p, sandbox = sys.argv[1], sys.argv[2]
m = json.load(open(man_p, encoding="utf-8"))
missing, checked = [], 0
for rec in m.get("meshes") or []:
    ap = rec.get("asset_path")
    if not ap:
        continue
    rel = ap[len("/Game/"):] if ap.startswith("/Game/") else ap.lstrip("/")
    p = os.path.join(sandbox, rel.replace("/", os.sep) + ".uasset")
    checked += 1
    if not os.path.isfile(p):
        missing.append(p)
print("meshes in manifest      : %d" % checked)
print("present in sandbox copy : %d" % (checked - len(missing)))
if missing:
    print("MISSING IN SANDBOX      : %d, first 5:" % len(missing))
    for p in missing[:5]:
        print("   %s" % p)
    sys.exit(9)
PY
GUARD_RC=$?
if [[ $GUARD_RC -ne 0 ]]; then
  echo "SANDBOX GUARD FAILED: the engine would load a DIFFERENT asset set than the" >&2
  echo "manifest describes.  Re-run with --sync (the pack is read-only; the sync" >&2
  echo "target is the disposable sandbox copy)." >&2
  exit 4
fi

# ---------- 3. engine: resolve wiring, rewrite the manifest ----------
echo
echo "--- [3/5] UnrealEditor: relink_materials.py ---"
# UE_RC/UE_RAN must be defined on EVERY path because step 5 reads them under
# `set -u`.  --verify-only never runs the engine, so they keep these defaults
# instead of dying with "UE_RC: unbound variable" after the verifier has passed.
UE_RC=0
UE_RAN=0
if [[ "$VERIFY_ONLY" == "0" ]]; then
  GAME_ROOTS_AT="$(echo "$GAME_ROOTS" | sed 's/^/@/; s/|/|@/g')"
  JOB_JSON_M="$(cygpath -m "$JOB_JSON")"
  REPORT_M="$(cygpath -m "$REPORT")"
  WIRING_M="$(cygpath -m "$WIRING")"
  MANIFEST_M="$(cygpath -m "$MANIFEST")"
  PACK_DIR_M="$(cygpath -m "$PACK_DIR")"
  MSYS2_ARG_CONV_EXCL='*' python - "$JOB_JSON_M" "$PACK" "$PACK_DIR_M" "$MANIFEST_M" \
        "$REPORT_M" "$WIRING_M" "$GAME_ROOTS_AT" "$LIMIT_MESHES" "$DO_APPLY" "$PREVIEW_M" <<'PY'
import json, sys
(a, pack, pack_dir, manifest, report, wiring, roots, lm, apply_it, preview) = sys.argv[1:]


def ungame(s):
    return s[1:] if s.startswith("@") else s


job = {
    "pack_name": pack,
    "pack_dir": pack_dir.replace("\\", "/"),
    "manifest": manifest.replace("\\", "/"),
    "report": report.replace("\\", "/"),
    "wiring_report": wiring.replace("\\", "/"),
    "game_roots": [ungame(g) for g in roots.split("|") if g],
    "exclude_dirs": ["EpicContent"],
    "apply": apply_it == "1",
    # .bak, never .json: the backup lives inside <pack>\Exports, so a *.json /
    # manifest*.json crawler there must not find a second, stale manifest.
    "backup_name": "manifest.v1.bak",
    "limit_meshes": int(lm),
}
if preview:
    job["manifest_out"] = preview.replace("\\", "/")
assert all(g.startswith("/Game/") for g in job["game_roots"]), job["game_roots"]
json.dump(job, open(a, "w", encoding="utf-8"), indent=1)
print("job json : %s" % a)
print("manifest : %s" % job["manifest"])
print("apply    : %s" % job["apply"])
PY
  [[ -f "$JOB_JSON" ]] || { echo "job json write failed" >&2; exit 2; }
  export AMCONV_JOB="$(cat "$JOB_JSON")"

  START=$(date +%s)
  "$UE_EXE" "$(cygpath -w "$PROJECT")" \
    -unattended -nopause -nosplash -NoSound -NoLoadingScreen \
    -stdout -FullStdOutLogOutput -UTF8Output \
    -EnablePlugins=PythonScriptPlugin \
    -ExecutePythonScript="$(cygpath -w "$TOOLS/relink_materials.py")" \
    > "$UELOG" 2>&1
  UE_RC=$?
  UE_RAN=1
  END=$(date +%s)
  echo "UE exit code : $UE_RC  (UE 5.7 can return non-zero from a CEF teardown crash"
  echo "                        AFTER the work is done; the report/manifest is the"
  echo "                        pass/fail authority, not this code)"
  echo "UE elapsed   : $((END-START))s"
  echo "UE log       : $UELOG"
  if [[ ! -f "$REPORT" ]]; then
    echo
    echo "FAIL: no relink report written -> engine died before finishing"
    grep -a "\[relink\]" "$UELOG" | tail -20
    exit 5
  fi
  grep -a "\[relink\]" "$UELOG" | grep -av "WARN" | tail -8
  grep -a "\[relink\]" "$UELOG" | grep -a "WARN" | tail -5
else
  echo "verify-only: engine step skipped"
fi

# ---------- 4. manifest link integrity (the standing verifier) ----------
echo
echo "--- [4/5] manifest link integrity + wiring ---"
if [[ "$PREVIEW" == "1" ]]; then
  echo "verifying the PREVIEW manifest (live manifest still untouched):"
  echo "  $PREVIEW_MANIFEST"
fi
python "$TOOLS/verify_pack_export.py" "$(cygpath -w "$EXPORTS_DIR")" "${VERIFY_ARGS[@]}" \
  --json "$(cygpath -w "$VERIFY_JSON")"
VERIFY_RC=$?

# ---------- 5. wiring numbers + status ----------
echo
echo "--- [5/5] wiring result ---"
STATUS_MANIFEST="$(cygpath -w "$MANIFEST")"
[[ "$PREVIEW" == "1" ]] && STATUS_MANIFEST="$(cygpath -w "$PREVIEW_MANIFEST")"
python - "$STATUS_MANIFEST" "$(cygpath -w "$REPORT")" "$(cygpath -w "$VERIFY_JSON")" \
        "$(cygpath -w "$STATUS_JSON")" "$PACK" "$LABEL" "$UE_RC" "$VERIFY_RC" "$DO_APPLY" "$PREVIEW" \
        "$UE_RAN" <<'PY'
import datetime, json, os, sys
(man_p, rep_p, ver_p, st_p, pack, label, ue_rc, ver_rc, applied, preview,
 ue_ran) = sys.argv[1:12]
ue_ran = ue_ran == "1"

m = json.load(open(man_p, encoding="utf-8"))
try:
    rep = json.load(open(rep_p, encoding="utf-8"))
except Exception:
    rep = {}
try:
    ver = json.load(open(ver_p, encoding="utf-8"))
except Exception:
    ver = {}

w = m.get("wiring") or {}
total, res = w.get("slots_total"), w.get("slots_resolved")
ratio = (float(res) / total) if total else 0.0

print("schema            : %s" % m.get("schema"))
print("engine run        : %s" % ("yes (exit %s)" % ue_rc if ue_ran else "no (verify-only)"))
print("slots_total       : %s" % total)
print("slots_resolved    : %s   (%.3f)" % (res, ratio))
print("  from instance   : %s" % w.get("slots_resolved_from_instance"))
print("  from defaults   : %s" % w.get("slots_resolved_from_defaults_only"))
print("method            : %s" % w.get("method"))
print("unresolved slots  : %s" % w.get("slots_unresolved"))
print("params resolved   : %s  (with an exported file: %s)" % (w.get("params_total"),
                                                               w.get("params_with_file")))
print("params_by_role    : %s" % json.dumps(w.get("params_by_role")))
print("params_by_source  : %s" % json.dumps(w.get("params_by_source")))
print("unresolved reasons:")
for k, v in list((w.get("unresolved_reasons") or {}).items())[:6]:
    print("   %5d  %s" % (v, k))
print("broken links      : %s (verifier: %s)" % (ver.get("broken_links"), ver.get("result")))

verdict = "PASS" if (ver.get("result") == "PASS" and not ver.get("problems")) else "FAIL"
# the relink report is the AUTHORITY on whether the relink ran: a FATAL or
# refused relink leaves the v1 manifest in place, the verifier then PASSes
# the (valid) v1 file, and the run used to exit 0 -- the tool's whole
# purpose silently no-opped
relink_ok = rep.get("ok")
if rep and relink_ok is False:
    verdict = "FAIL"
    print("RELINK REPORT SAYS ok=false -- the relink itself failed;")
    print("a verifier PASS on the untouched v1 manifest is NOT a pass")
st = {
    "pack": pack, "label": label, "status": verdict,
    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "manifest": os.path.abspath(man_p),
    "schema": m.get("schema"),
    "applied": applied == "1",
    "preview": preview == "1",
    "wiring": {k: v for k, v in w.items() if k != "unresolved"},
    "wiring_unresolved_count": len(w.get("unresolved") or []),
    "broken_links": ver.get("broken_links"),
    "verify_result": ver.get("result"),
    "verify_problems": (ver.get("problems") or [])[:20],
    "ue_exit_code": int(ue_rc) if ue_ran else None, "ue_ran": ue_ran,
    "verify_exit_code": int(ver_rc),
    "relink_report": rep,
    "relink_report_ok": relink_ok,
}
json.dump(st, open(st_p, "w", encoding="utf-8"), indent=1)
with open(os.path.join(os.path.dirname(st_p), "relink.log"), "a", encoding="utf-8") as fh:
    fh.write("%s  %-40s %-4s  slots=%s/%s (%.3f)  broken=%s  schema=%s\n"
             % (st["at"], pack[:40], verdict, res, total, ratio,
                ver.get("broken_links"), m.get("schema")))
print()
print("PACK RESULT: %s -> %s" % (verdict, os.path.abspath(man_p)))
sys.exit(0 if verdict == "PASS" else 1)
PY

# the status writer itself decides pass/fail now (verifier + relink
# report ok flag); its exit code is the run's verdict
PY_RC=$?
if [[ $PY_RC -ne 0 ]]; then
  echo "relink_pack: FAIL (details: $STATUS_JSON / $REPORT)" >&2
  exit 7
fi
if [[ "$VERIFY_RC" -ne 0 ]]; then
  exit 6
fi
exit 0
