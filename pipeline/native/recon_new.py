import io, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import library_root, section_root
LIB = library_root() or "."
LEA = os.path.join(LIB, "Leartes Env_ gumroad")
MODEL_EXT = {".fbx", ".obj", ".usd", ".usda", ".usdc", ".blend", ".max"}

if not os.path.isdir(LIB):
    print("library root not found: %s" % LIB)
    sys.exit(0)

print("=== LEARTES packs: uassets vs existing Exports ===")
tot_new = 0
new_packs = []
lea_dirs = sorted(os.listdir(LEA)) if os.path.isdir(LEA) else []
for d in lea_dirs:
    p = os.path.join(LEA, d)
    if not os.path.isdir(p) or d.startswith("."):
        continue
    ua = um = 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            e = os.path.splitext(f)[1].lower()
            if e == ".uasset": ua += 1
            elif e == ".umap": um += 1
    man = os.path.join(p, "Exports", "manifest.json")
    has = os.path.isfile(man)
    n_fbx = 0
    if has:
        try:
            n_fbx = (json.load(io.open(man, encoding="utf-8")).get("counts") or {}).get("fbx_written", 0)
        except Exception:
            n_fbx = -1
    flag = "DONE" if (has and n_fbx > 0) else ("PARTIAL" if has else "NEW")
    if flag == "NEW":
        tot_new += 1
        new_packs.append(d)
    print("   %-52s uasset=%-5d umap=%-4d exports=%-8s fbx=%s" % (d[:52], ua, um, flag, n_fbx))

print("\n=== NEW (no export yet): %d ===" % tot_new)
for n in new_packs:
    print("   " + n)

print("\n=== KITBASHORDNER kits now on disk ===")
kb = section_root("kitbash", "PHAROS_KB3D_ROOT") or os.path.join(LIB, "KitbashOrdner")
kb_dirs = sorted(os.listdir(kb)) if os.path.isdir(kb) else []
for d in kb_dirs:
    p = os.path.join(kb, d)
    if not os.path.isdir(p):
        continue
    n = 0; sz = 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            if os.path.splitext(f)[1].lower() in MODEL_EXT:
                n += 1
                try: sz += os.path.getsize(os.path.join(dp, f))
                except OSError: pass
    print("   %-34s model files=%-6d %.2f GB" % (d[:34], n, sz / 2**30))

print("\n=== Dark Medieval Environment Megapack Unity ===")
u = os.path.join(LEA, "Dark Medieval Environment Megapack Unity")
print("   still present:", os.path.isdir(u))

print("\n=== Leartes folder summary ===")
allf = sum(len(fn) for _, _, fn in os.walk(LEA)) if os.path.isdir(LEA) else 0
print("   total files:", allf)
