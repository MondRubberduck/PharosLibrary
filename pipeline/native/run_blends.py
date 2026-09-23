"""Drive Blender directly (no bash/MSYS) to index each .blend kit.
Windows paths are passed as-is; nothing can mangle them."""
import io, json, os, subprocess, sys
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import blender_exe
BLENDER = blender_exe()
if not BLENDER:
    raise SystemExit("FATAL: Blender not found -- set BLENDER_EXE to your "
                     "Blender executable (see pipeline/README.md)")
SCRIPT = os.path.join(T, "index_native.py")
MAN = os.path.join(T, "native_manifest.json")
OUT = os.path.join(T, "native_index_blends.jsonl")

if not os.path.isfile(MAN):
    print("manifest not found: %s (run make_native_manifest2.py first)" % MAN)
    sys.exit(0)

man = json.load(io.open(MAN, encoding="utf-8"))
kits = man.get("blend_files", [])
print("kits to index:", len(kits), flush=True)

ok = fail = 0
for i, b in enumerate(kits, 1):
    env = dict(os.environ)
    env["AMNATIVE_OUT"] = OUT
    env["AMNATIVE_SECTION"] = b["section"]
    print("[%d/%d] %s" % (i, len(kits), os.path.basename(b["path"])), flush=True)
    try:
        r = subprocess.run([BLENDER, "--background", "--factory-startup", b["path"],
                            "--python", SCRIPT],
                           capture_output=True, text=True, env=env, timeout=1800)
        out = (r.stdout or "") + (r.stderr or "")
        done = [l for l in out.splitlines() if "NATIVE_DONE" in l]
        if done:
            print("      " + done[-1].strip(), flush=True)
            ok += 1
        else:
            fail += 1
            print("      FAILED rc=%s" % r.returncode, flush=True)
            for l in out.splitlines():
                if "Error" in l or "Traceback" in l or "not found" in l:
                    print("      " + l.strip()[:160], flush=True)
    except subprocess.TimeoutExpired:
        fail += 1
        print("      TIMEOUT", flush=True)

n = sum(1 for _ in io.open(OUT, encoding="utf-8")) if os.path.exists(OUT) else 0
print("\nok=%d failed=%d  records=%d" % (ok, fail, n), flush=True)
