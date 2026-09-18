#!/usr/bin/env bash
# convert_packs.sh -- convert ONE Unreal pack under the assets root into
# <pack>\Exports\{FBX,Textures}\... plus <pack>\Exports\manifest.json.
#
# Writes only inside the pack's new `Exports` folder.  The .uasset sources are
# never opened for writing: the engine works on a copy in ../sandbox/Content.
#
# Usage:
#   convert_packs.sh --pack "MyPack" [options]
#
# Options:
#   --assets-root DIR   default: $ASSETS_ROOT env var
#   --label NAME        tags logs (default: full)
#   --limit-meshes N    smoke-test only
#   --limit-textures N
#   --limit-skel N
#   --skip-meshes LIST  comma-separated asset names that are NEVER exported, e.g.
#                       --skip-meshes "sm_Pipe_Straight_4m_03_11,sm_Foo_01"
#                       (the asset is dropped from the mesh list before the
#                        export loop, so the engine never loads it)
#   --skip-pattern LIST comma-separated substrings matched against asset name and
#                       package path, e.g. --skip-pattern "sm_Pipe_Straight_4m_03_"
#                       (a prefix is a substring, so a whole family can go)
#   --no-sync           reuse whatever is already in the sandbox copy
#   --skip-blender      skip the Blender round-trip step
#   --no-replace        refuse to reuse an existing Exports folder
#
#   --skip-meshes / --skip-pattern exist because Unreal 5.7's FBX exporter can die
#   on a bad asset with a C++ assertion (check() abort) that Python cannot catch.
#   Skipping such assets is the only way to get the rest of the pack out.
#
# Exit codes: 0 pass, 3 discovery failed, 4 sync failed, 5 no manifest,
#             6 verification failed
set -uo pipefail

UE_EXE="${UE_EXE:-}"   # e.g. "/c/Program Files/Epic Games/UE_5.7/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
BLENDER_EXE="${BLENDER_EXE:-}"   # e.g. "/c/Program Files/Blender Foundation/Blender 5.1/blender.exe"
CONV_ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$CONV_ROOT"    # the helper scripts live beside this driver
SANDBOX="$CONV_ROOT/sandbox"
SANDBOX_CONTENT="$SANDBOX/Content"
PROJECT="$SANDBOX/Sandbox.uproject"
LOGDIR="$CONV_ROOT/logs"
STATUSDIR="$CONV_ROOT/status"
ASSETS_ROOT="${ASSETS_ROOT:-}"   # your converted-pack source root
OUT_ROOT=""          # empty => <pack>/Exports (the real target)

PACK=""
LABEL="full"
LIMIT_MESHES=0
LIMIT_SKEL=0
LIMIT_TEXTURES=0
SKIP_MESHES=""
SKIP_PATTERNS=""
DO_SYNC=1
DO_BLENDER=1
NO_REPLACE=0

usage() {
  # print the Options/Usage block straight out of this file's header so the
  # printed help can never drift from the documented one
  sed -n '/^# Usage:/,/^# Exit codes:/p' "$0" | sed -e 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)            PACK="$2"; shift 2 ;;
    --assets-root)     ASSETS_ROOT="$2"; shift 2 ;;
    --out-root)        OUT_ROOT="$2"; shift 2 ;;
    --label)           LABEL="$2"; shift 2 ;;
    --limit-meshes)    LIMIT_MESHES="$2"; shift 2 ;;
    --limit-skel)      LIMIT_SKEL="$2"; shift 2 ;;
    --limit-textures)  LIMIT_TEXTURES="$2"; shift 2 ;;
    --skip-meshes)     SKIP_MESHES="$2"; shift 2 ;;
    --skip-pattern)    SKIP_PATTERNS="$2"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    --no-sync)         DO_SYNC=0; shift ;;
    --skip-blender)    DO_BLENDER=0; shift ;;
    --no-replace)      NO_REPLACE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$PACK" ]] || { echo "ERROR: --pack required" >&2; exit 2; }
# --no-replace is a safety gate: refuse to silently reuse an Exports folder
# a previous run (or a previous pack version) left behind
if [[ "$NO_REPLACE" == "1" ]]; then
  EXISTING_EXPORTS="$OUT_ROOT/$PACK/Exports"
  [[ -z "$OUT_ROOT" ]] && EXISTING_EXPORTS="$ASSETS_ROOT/$PACK/Exports"
  if [[ -e "$EXISTING_EXPORTS" ]]; then
    echo "ERROR: --no-replace: $EXISTING_EXPORTS already exists" >&2
    echo "       (delete it first, or drop the flag to merge into it)" >&2
    exit 5
  fi
fi
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
DISC_JSON="$LOGDIR/discovery_${SLUG}.json"
JOB_JSON="$LOGDIR/job_${SLUG}_${LABEL}_${STAMP}.json"
UELOG="$LOGDIR/ue_${SLUG}_${LABEL}_${STAMP}.log"
REPORT="$LOGDIR/report_${SLUG}_${LABEL}_${STAMP}.json"
VERIFY_JSON="$LOGDIR/verify_${SLUG}.json"
BLENDER_JSON="$LOGDIR/blender_${SLUG}.json"
STATUS_JSON="$STATUSDIR/${SLUG}.json"
STATUS_LOG="$STATUSDIR/packs.log"

echo "================================================================"
echo "convert_packs.sh  pack=$PACK  label=$LABEL"
echo "================================================================"

# ---------- 1. discovery ----------
echo
echo "--- [1/7] discovery ---"
DISC_LOG="$LOGDIR/discovery_${SLUG}.txt"
python "$TOOLS/pack_discovery.py" --pack "$PACK_DIR" --verbose > "$DISC_JSON" 2> "$DISC_LOG"
DISC_RC=$?
sed 's/^/  /' "$DISC_LOG" 2>/dev/null
if [[ $DISC_RC -ne 0 ]]; then
  echo "DISCOVERY FAILED for '$PACK' (zero assets, or no Content root):"
  cat "$DISC_JSON"
  exit 3
fi

# pull fields out of the discovery JSON with python (no jq dependency)
eval "$(python - "$DISC_JSON" <<'PY'
import json, shlex, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print("CONTENT_ROOT=%s" % shlex.quote(d["content_root"]))
print("PRIMARY_GAME_ROOT=%s" % shlex.quote(d["primary_game_root"] or ""))
print("GAME_ROOTS=%s" % shlex.quote("|".join(d["game_roots"])))
print("PROJECT_NAME=%s" % shlex.quote(d.get("project_name") or ""))
print("UASSET_TOTAL=%s" % int(d["uasset_total"]))
PY
)"
: "${CONTENT_ROOT:?discovery produced no content_root}"
: "${PRIMARY_GAME_ROOT:?discovery produced no game root}"
: "${GAME_ROOTS:?discovery produced no game roots}"
echo "content_root     : $CONTENT_ROOT"
echo "primary game root: $PRIMARY_GAME_ROOT"
echo "game roots       : $GAME_ROOTS"
echo "project name     : $PROJECT_NAME"
echo "uassets (source) : $UASSET_TOTAL"

# ---------- 2. sandbox sync (source is only ever READ) ----------
echo
echo "--- [2/7] sandbox sync ---"
if [[ "$DO_SYNC" == "1" ]]; then
  echo "source : $CONTENT_ROOT   (read-only)"
  echo "target : $SANDBOX_CONTENT"
  mkdir -p "$SANDBOX_CONTENT"
  MSYS_NO_PATHCONV=1 robocopy "$(cygpath -w "$CONTENT_ROOT")" "$(cygpath -w "$SANDBOX_CONTENT")" \
    /E /NFL /NDL /NJH /NJS /R:1 /W:1 /XD "__ExternalActors__" "__ExternalObjects__"
  RC=$?
  echo "robocopy exit=$RC (0-7 = success)"
  if [[ $RC -ge 8 ]]; then echo "SYNC FAILED" >&2; exit 4; fi
else
  echo "sync skipped (--no-sync)"
fi

# every discovered game root must exist in the sandbox, else the engine will
# silently scan nothing and we'd write an empty manifest. Split on '|' ONLY
# (quoted + read -ra): a /Game top folder may contain spaces, and word-
# splitting on spaces would fragment it into a bogus SYNC FAILED.
MISSING=""
IFS='|' read -r -a _GAME_ROOT_LIST <<< "$GAME_ROOTS"
for gr in "${_GAME_ROOT_LIST[@]}"; do
  d="$SANDBOX_CONTENT/${gr#/Game/}"
  [[ -d "$d" ]] || MISSING="$MISSING $d"
done
if [[ -n "$MISSING" ]]; then
  echo "SYNC FAILED: sandbox is missing:$MISSING" >&2
  exit 4
fi

# ---------- 3. job json ----------
EXPORTS_DIR="$ASSETS_ROOT/$PACK/Exports"
[[ -n "$OUT_ROOT" ]] && EXPORTS_DIR="$OUT_ROOT/$PACK"
FBX_OUT="$EXPORTS_DIR/FBX"
TEX_OUT="$EXPORTS_DIR/Textures"
MANIFEST="$EXPORTS_DIR/manifest.json"
mkdir -p "$FBX_OUT" "$TEX_OUT"

# NOTE: bare "/Game/..." arguments get rewritten by Git Bash into
# "C:/Program Files/Git/Game/..." and the engine then scans nothing.  Every
# /Game path is therefore passed with a leading '@' and stripped in Python.
GAME_ROOTS_AT="$(echo "$GAME_ROOTS" | sed 's/^/@/; s/|/|@/g')"
# --- MSYS safety -------------------------------------------------------------
# Two separate problems when running under Git Bash:
#   1. a bare "/Game/X" argument is rewritten to "C:/Program Files/Git/Game/X"
#      (the engine then scans nothing and exports zero files)
#   2. every /d/... argument is rewritten inconsistently
# Fix: hand python only Windows-form paths (cygpath -m) and disable MSYS
# argument conversion for this one call.  The "@" prefix stays as a second
# line of defence and is stripped by ungame() inside the heredoc.
JOB_JSON_M="$(cygpath -m "$JOB_JSON")"
REPORT_M="$(cygpath -m "$REPORT")"
FBX_OUT_M="$(cygpath -m "$FBX_OUT")"
TEX_OUT_M="$(cygpath -m "$TEX_OUT")"
MANIFEST_M="$(cygpath -m "$MANIFEST")"
MSYS2_ARG_CONV_EXCL='*' python - "$JOB_JSON_M" "$PACK" "$PACK_DIR" "$CONTENT_ROOT" "$PROJECT_NAME" \
        "@$PRIMARY_GAME_ROOT" "$GAME_ROOTS_AT" "$FBX_OUT_M" "$TEX_OUT_M" "$MANIFEST_M" "$REPORT_M" \
        "$LIMIT_MESHES" "$LIMIT_SKEL" "$LIMIT_TEXTURES" "$SKIP_MESHES" "$SKIP_PATTERNS" <<'PY'
import json, os, sys
(a, pack, pack_dir, content_root, project, primary, roots, fbx_out, tex_out,
 manifest, report, lm, ls, lt, skip_m, skip_p) = sys.argv[1:]


def ungame(s):
    return s[1:] if s.startswith("@") else s


def split_list(raw):
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


job = {
    "pack_name": pack,
    "pack_dir": pack_dir.replace("\\", "/"),
    "content_root": content_root.replace("\\", "/"),
    "project_name": project or None,
    "game_root": ungame(primary),
    "game_roots": [ungame(g) for g in roots.split("|") if g],
    "fbx_out": fbx_out.replace("\\", "/"),
    "tex_out": tex_out.replace("\\", "/"),
    "manifest": manifest.replace("\\", "/"),
    "report": report.replace("\\", "/"),
    "exclude_dirs": ["EpicContent"],
    "limit_meshes": int(lm), "limit_skel": int(ls), "limit_textures": int(lt),
    "export_meshes": True, "export_textures": True,
    "skip_meshes": split_list(skip_m),
    "skip_patterns": split_list(skip_p),
}
assert all(g.startswith("/Game/") for g in job["game_roots"]), job["game_roots"]
json.dump(job, open(a, "w", encoding="utf-8"), indent=1)
print("job json : %s" % a)
print("game roots: %s" % job["game_roots"])
print("fbx out  : %s" % job["fbx_out"])
print("tex out  : %s" % job["tex_out"])
print("manifest : %s" % job["manifest"])
print("skip meshes  : %s" % (job["skip_meshes"] or "(none)"))
print("skip patterns: %s" % (job["skip_patterns"] or "(none)"))
PY
[[ -f "$JOB_JSON" ]] || { echo "job json write failed" >&2; exit 2; }
export AMCONV_JOB="$(cat "$JOB_JSON")"

# ---------- 4. engine ----------
echo
echo "--- [3/7] UnrealEditor headless ---"
START=$(date +%s)
"$UE_EXE" "$(cygpath -w "$PROJECT")" \
  -unattended -nopause -nosplash -NoSound -NoLoadingScreen \
  -stdout -FullStdOutLogOutput -UTF8Output \
  -EnablePlugins=PythonScriptPlugin \
  -ExecutePythonScript="$(cygpath -w "$TOOLS/export_pack.py")" \
  > "$UELOG" 2>&1
UE_RC=$?
END=$(date +%s)
UE_ELAPSED=$((END-START))
echo "UE exit code : $UE_RC  (UE 5.7 often returns non-zero from a CEF teardown"
echo "                        crash AFTER the manifest is written; the manifest,"
echo "                        not this code, is the pass/fail authority)"
echo "UE elapsed   : ${UE_ELAPSED}s ($(printf '%d:%02d' $((UE_ELAPSED/60)) $((UE_ELAPSED%60))))"
echo "UE log       : $UELOG"
if [[ ! -f "$MANIFEST" ]]; then
  echo
  echo "FAIL: no manifest written -> engine died before finishing"
  grep -a "\[amconv\]" "$UELOG" | tail -20
  exit 5
fi
grep -a "\[amconv\] " "$UELOG" | grep -av "WARN" | tail -6

# ---------- 5. manifest link integrity ----------
echo
echo "--- [4/7] manifest link integrity ---"
python "$TOOLS/verify_pack_export.py" "$(cygpath -w "$EXPORTS_DIR")" --json "$VERIFY_JSON"
VERIFY_RC=$?

# ---------- 6. independent FBX header + Blender round-trip ----------
echo
echo "--- [5/7] FBX headers (independent, no engine involved) ---"
python - "$MANIFEST" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
ex = os.path.dirname(os.path.abspath(sys.argv[1]))
bad = []
n = 0
for r in m.get("meshes") or []:
    p = os.path.join(ex, r["fbx"].replace("/", os.sep))
    n += 1
    try:
        with open(p, "rb") as fh:
            head = fh.read(32)
        if not (head[:7] == b"Kaydara" and b"FBX" in head[:24]):
            bad.append((r["name"], head[:16]))
    except Exception as exc:
        bad.append((r["name"], str(exc)))
print("FBX files checked : %d" % n)
print("binary 'Kaydara FBX Binary' : %d" % (n - len(bad)))
print("bad headers       : %d %s" % (len(bad), bad[:5] if bad else ""))
PY

echo
echo "--- [6/7] Blender round-trip ---"
if [[ "$DO_BLENDER" == "1" && -f "$BLENDER_EXE" ]]; then
  SAMPLE="$(python - "$VERIFY_JSON" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print("\n".join(d.get("blender_sample", [])))
except Exception:
    pass
PY
)"
  if [[ -n "$SAMPLE" ]]; then
    # pack names can contain spaces -- an unquoted $SAMPLE split on IFS and
    # Blender was handed path fragments.
    SAMPLE="${SAMPLE//$'\r'/}"   # python heredoc output is CRLF; a trailing \r broke Blender
    mapfile -t SAMPLE_ARR <<< "$SAMPLE"
    echo "files: ${#SAMPLE_ARR[@]}"
    "$BLENDER_EXE" --background --factory-startup --python "$TOOLS/blender_inspect_fbx.py" -- "${SAMPLE_ARR[@]}" \
      > "$LOGDIR/blender_${SLUG}.log" 2>&1
    echo "blender exit=$?"
    python - "$LOGDIR/blender_${SLUG}.log" "$BLENDER_JSON" "$MANIFEST" <<'PY'
import datetime, json, os, sys
log, out, man_p = sys.argv[1:4]
raw = open(log, "r", encoding="utf-8", errors="replace").read()
try:
    doc = json.loads(raw.split("AMCONV_JSON_BEGIN", 1)[1].split("AMCONV_JSON_END", 1)[0])
except Exception as exc:
    print("could not parse Blender JSON: %s" % exc)
    sys.exit(0)

m = json.load(open(man_p, encoding="utf-8"))
ex = os.path.dirname(os.path.abspath(man_p))
by_rel = {}
for r in m.get("meshes") or []:
    by_rel[os.path.normcase(os.path.join(ex, r["fbx"].replace("/", os.sep)))] = r

print("blender %s, %d file(s) round-tripped" % (doc.get("blender_version"), doc.get("count")))
deltas = []
for r in doc["results"]:
    b = r.get("bbox") or {}
    d = b.get("dims_m")
    key = os.path.normcase(os.path.abspath(r["file"]))
    eng = by_rel.get(key)
    print("  %-44s verts=%-7s tris=%-7s dims_m=%s mats=%s%s" % (
        os.path.basename(r["file"])[:44], r.get("vertices"), r.get("triangles"),
        [round(x, 3) for x in d] if d else None, r.get("materials"),
        "  ERROR=%s" % r["error"] if r.get("error") else ""))
    if d and (d[0] == 0 or d[1] == 0 or d[2] == 0):
        print("    !! zero-volume bounding box")
    if r.get("triangles") == 0:
        print("    !! zero faces")
    # cross-check the engine-reported bbox against the measured FBX
    if eng and d and eng.get("bbox_m"):
        e = eng["bbox_m"]
        dev = [round(abs(e[i] - d[i]) / max(e[i], d[i], 1e-6) * 100.0, 2) for i in range(3)]
        r["engine_bbox_m"] = e
        r["bbox_deviation_pct"] = dev
        r["bbox_agrees_5pct"] = all(x <= 5.0 for x in dev)
        deltas.append((os.path.basename(r["file"]), e, [round(x, 4) for x in d], dev,
                       r["bbox_agrees_5pct"]))
        print("    engine bbox_m=%s   deviation=%s%%   agrees(<=5%%)=%s"
              % (e, dev, r["bbox_agrees_5pct"]))
    # fill in what the engine could not give us (SkeletalMesh triangles/vertices)
    if eng is not None and (eng.get("triangles") is None or eng.get("vertices") is None):
        r["filled_metric_for"] = eng.get("name")

if deltas:
    doc["bbox_cross_check"] = [
        {"file": n, "engine_bbox_m": e, "blender_bbox_m": b, "deviation_pct": dv, "agrees": ok}
        for n, e, b, dv, ok in deltas]
    doc["bbox_all_agree_5pct"] = all(x[4] for x in deltas)

doc["verified_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
json.dump(doc, open(out, "w", encoding="utf-8"), indent=1)
print("wrote %s" % out)
PY
  else
    echo "no FBX to sample"
  fi
else
  echo "skipped"
fi

# ---------- 7. status ----------
echo
echo "--- [7/7] status ---"
python - "$MANIFEST" "$VERIFY_JSON" "$STATUS_JSON" "$STATUS_LOG" "$PACK" "$LABEL" "$UE_RC" "$UE_ELAPSED" "$VERIFY_RC" <<'PY'
import datetime, json, os, sys
man_p, ver_p, st_p, log_p, pack, label, ue_rc, ue_el, ver_rc = sys.argv[1:11]
m = json.load(open(man_p, encoding="utf-8"))
try:
    v = json.load(open(ver_p, encoding="utf-8"))
except Exception:
    v = {}
c = m.get("counts") or {}
verdict = "PASS" if (v.get("result") == "PASS" and c.get("failures", 0) == 0) else "FAIL"
st = {
    "pack": pack, "label": label,
    "status": verdict,
    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "exports_dir": os.path.dirname(os.path.abspath(man_p)),
    "manifest": os.path.abspath(man_p),
    "game_root": m.get("game_root"), "game_roots": m.get("game_roots"),
    "content_root": m.get("content_root"),
    "counts": c,
    "broken_links": v.get("broken_links"),
    "mesh_stats": v.get("meshes"),
    "texture_stats": v.get("textures"),
    "ue_exit_code": int(ue_rc), "ue_elapsed_s": int(ue_el),
    "verify_exit_code": int(ver_rc),
    "engine_elapsed_s": m.get("elapsed_seconds"),
    "not_exported_texturecube": len((m.get("not_exported") or {}).get("texturecube") or []),
    "failures": m.get("failures") or [],
}
json.dump(st, open(st_p, "w", encoding="utf-8"), indent=1)
line = ("%s  %-52s %-4s  sm=%-5s skm=%-3s tex=%-5s cube=%-3s fbx=%-5s tfiles=%-5s "
        "fail=%-3s broken=%-3s eng=%ss  ue_rc=%s") % (
    st["at"], pack[:52], verdict,
    c.get("static_mesh"), c.get("skeletal_mesh"), c.get("texture2d"),
    c.get("texturecube_skipped"), c.get("fbx_written"),
    c.get("texture_files_written"), c.get("failures"), v.get("broken_links"),
    m.get("elapsed_seconds"), ue_rc)
with open(log_p, "a", encoding="utf-8") as fh:
    fh.write(line + "\n")
print(line)
PY

if [[ "$(python -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['status'])" "$STATUS_JSON")" == "PASS" ]]; then
  echo
  echo "PACK RESULT: PASS  ->  $EXPORTS_DIR"
  exit 0
else
  echo
  echo "PACK RESULT: FAIL  ->  see $STATUS_JSON"
  exit 6
fi
