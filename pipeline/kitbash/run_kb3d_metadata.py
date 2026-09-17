"""Drive the KitBash3D material/texture metadata pass over all 11 kits."""
import glob, os, subprocess, sys
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import section_root
BLENDER = os.environ.get("BLENDER_EXE", r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe")
SCRIPT = os.path.join(T, "kb3d_metadata.py")
ROOT = section_root("kitbash", "PHAROS_KB3D_ROOT")
if not ROOT or not os.path.isdir(ROOT):
    raise SystemExit("FATAL: kits root not found: %r -- set `kitbash_root` in "
                     "pharos_config.json or PHAROS_KB3D_ROOT "
                     "(see pipeline/README.md)" % ROOT)

kits = sorted(glob.glob(os.path.join(ROOT, "*", "*", "*.blend"))) + \
       sorted(glob.glob(os.path.join(ROOT, "*", "*.blend")))
seen, todo = set(), []
for b in kits:
    kd = os.path.dirname(b)
    if os.path.basename(kd).endswith(".blender.native"):
        kd = os.path.dirname(kd)
    if kd in seen:
        continue
    seen.add(kd)
    if os.path.isfile(os.path.join(kd, "Exports", "kit_manifest.json")):
        todo.append((os.path.basename(kd), b))

print("kits to enrich:", len(todo), flush=True)
for i, (name, blend) in enumerate(todo, 1):
    print("[%d/%d] %s" % (i, len(todo), name), flush=True)
    try:
        r = subprocess.run([BLENDER, "--background", "--factory-startup", blend, "--python", SCRIPT],
                           capture_output=True, text=True, timeout=3600)
        out = (r.stdout or "") + (r.stderr or "")
        line = [l for l in out.splitlines() if "KB3D_META" in l]
        print("      " + (line[-1].strip() if line else "no output rc=%s" % r.returncode), flush=True)
    except subprocess.TimeoutExpired:
        print("      TIMEOUT", flush=True)
print("\ndone", flush=True)
