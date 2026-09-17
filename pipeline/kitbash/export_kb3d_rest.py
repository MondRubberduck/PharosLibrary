"""Second pass: the three KitBash3D kits that use the OLDER packaging, where the
.blend sits directly in the kit folder rather than in a .blender.native subfolder.
Re-runnable; skips kits that already have Exports/kit_manifest.json."""
import glob, os, subprocess, sys
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import section_root
BLENDER = os.environ.get("BLENDER_EXE", r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe")
SCRIPT = os.path.join(T, "export_kb3d.py")
ROOT = section_root("kitbash", "PHAROS_KB3D_ROOT")
if not ROOT or not os.path.isdir(ROOT):
    raise SystemExit("FATAL: kits root not found: %r -- set `kitbash_root` in "
                     "pharos_config.json or PHAROS_KB3D_ROOT "
                     "(see pipeline/README.md)" % ROOT)

# every .blend under the kits root, at any depth
allb = sorted(glob.glob(os.path.join(ROOT, "**", "*.blend"), recursive=True))
todo = []
for blend in allb:
    # strip a trailing .blender.native segment if present
    kit_dir = os.path.dirname(blend)
    if os.path.basename(kit_dir).endswith(".blender.native"):
        kit_dir = os.path.dirname(kit_dir)
    man = os.path.join(kit_dir, "Exports", "kit_manifest.json")
    if os.path.isfile(man):
        continue
    todo.append((os.path.basename(kit_dir), blend))

print("remaining kit .blend files:", len(todo), flush=True)
for name, blend in todo:
    print("  ", name, "->", os.path.basename(blend), flush=True)

ok = failed = 0
for i, (name, blend) in enumerate(todo, 1):
    print("[%d/%d] EXPORT %-22s %6.0f MB" % (i, len(todo), name, os.path.getsize(blend) / 1e6), flush=True)
    try:
        r = subprocess.run([BLENDER, "--background", "--factory-startup", blend, "--python", SCRIPT],
                           capture_output=True, text=True, timeout=3600)
        out = (r.stdout or "") + (r.stderr or "")
        line = [l for l in out.splitlines() if "KB3D_EXPORT" in l]
        if line:
            print("         " + line[-1].strip(), flush=True)
            ok += 1
        else:
            failed += 1
            print("         FAILED rc=%s" % r.returncode, flush=True)
            for l in out.splitlines():
                if "Error" in l or "Traceback" in l:
                    print("         " + l.strip()[:150], flush=True)
    except subprocess.TimeoutExpired:
        failed += 1
        print("         TIMEOUT", flush=True)

print("\n==== second pass done: exported=%d failed=%d ====" % (ok, failed), flush=True)
