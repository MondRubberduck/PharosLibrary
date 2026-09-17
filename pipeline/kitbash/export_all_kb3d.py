"""Export FBX from every KitBash3D .blend kit, one FBX per group assembly.
Skips kits that already have Exports/kit_manifest.json (so it is re-runnable)."""
import glob, io, json, os, subprocess, sys
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

kits = sorted(glob.glob(os.path.join(ROOT, "*", "*.blender.native", "*.blend")))
print("kits found:", len(kits), flush=True)

ok = skipped = failed = 0
for i, blend in enumerate(kits, 1):
    kit_dir = os.path.dirname(os.path.dirname(blend))          # <Kit>
    man = os.path.join(kit_dir, "Exports", "kit_manifest.json")
    name = os.path.basename(kit_dir)
    if os.path.isfile(man):
        try:
            d = json.load(io.open(man, encoding="utf-8"))
            n = (d.get("counts") or {}).get("groups_exported", 0)
            print("[%d/%d] SKIP %-18s (already exported: %s groups)" % (i, len(kits), name, n), flush=True)
        except Exception:
            print("[%d/%d] SKIP %-18s (manifest present)" % (i, len(kits), name), flush=True)
        skipped += 1
        continue
    print("[%d/%d] EXPORT %-18s %6.0f MB" % (i, len(kits), name, os.path.getsize(blend) / 1e6), flush=True)
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

print("\n==== done: exported=%d skipped=%d failed=%d ====" % (ok, skipped, failed), flush=True)
