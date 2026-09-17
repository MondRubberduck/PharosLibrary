import glob, io, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import library_root, section_root
LIB = library_root() or "."
MODEL_EXT = {".fbx", ".obj", ".usd", ".usda", ".usdc", ".blend", ".max"}

if not os.path.isdir(LIB):
    print("library root not found: %s" % LIB)
    sys.exit(0)


def converted_section() -> str:
    """The section that holds CONVERTED Unreal packs -- its children carry
    Exports/manifest.json (as opposed to the kit layout's kit_manifest.json).
    Derived from the library instead of a hardcoded folder name.
    """
    for top in sorted(os.listdir(LIB)):
        if glob.glob(os.path.join(LIB, top, "*", "Exports", "manifest.json")):
            return os.path.join(LIB, top)
    return ""


def kit_section() -> str:
    """The section that holds the KIT layout -- its children carry
    Exports/kit_manifest.json (converted packs carry Exports/manifest.json).
    Derived from the library instead of a hardcoded folder name.
    """
    for top in sorted(os.listdir(LIB)):
        if glob.glob(os.path.join(LIB, top, "*", "Exports", "kit_manifest.json")):
            return os.path.join(LIB, top)
    return ""


# PHAROS_CONVERTED_ROOT overrides the derived section (needed only when a
# library holds more than one converted section).
CONV = os.environ.get("PHAROS_CONVERTED_ROOT") or converted_section()

print("=== CONVERTED packs: uassets vs existing Exports ===")
tot_new = 0
new_packs = []
pack_dirs = sorted(os.listdir(CONV)) if os.path.isdir(CONV) else []
for d in pack_dirs:
    p = os.path.join(CONV, d)
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

print("\n=== .blend kits now on disk ===")
kb = section_root("kitbash", "PHAROS_KB3D_ROOT") or kit_section()
if not os.path.isdir(kb):
    print("   kit section not found -- set `kitbash_root` in pharos_config.json "
          "or PHAROS_KB3D_ROOT (see pipeline/README.md)")
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

# Leftover check: is a pack that was supposed to have been removed from the
# converted section still on disk? Name it explicitly rather than hardcoding
# one library's product: PHAROS_RECON_LEFTOVER=<folder name>. Skipped when unset.
LEFTOVER = os.environ.get("PHAROS_RECON_LEFTOVER", "").strip()
if LEFTOVER:
    print("\n=== leftover check: %s ===" % LEFTOVER)
    print("   still present:", os.path.isdir(os.path.join(CONV, LEFTOVER)))

print("\n=== converted section summary ===")
allf = sum(len(fn) for _, _, fn in os.walk(CONV)) if os.path.isdir(CONV) else 0
print("   total files:", allf)
