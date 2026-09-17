import glob, io, json, os, sys
from collections import Counter
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import library_root, load
LIB = library_root() or "."
OUT = os.path.join(T, "native_manifest.json")
BLENDS = os.path.join(T, "native_blends.txt")

_cfg = load()
MODEL_EXT = {".fbx", ".obj", ".usd", ".usda", ".usdc", ".blend", ".max"}

if not os.path.isdir(LIB):
    print("library root not found: %s" % LIB)
    sys.exit(0)


def already_converted(section: str) -> bool:
    """True for a section the pack-conversion chain already serves.

    Those models arrive as thin per-mesh FBX under <pack>/Exports/ beside a
    manifest.json, so enumerating them here again would double-count them
    against models.jsonl and make the native pass spawn one Blender process
    per export mesh.  KitBash3D kits write Exports/kit_manifest.json instead,
    so they stay in scope.
    """
    return bool(glob.glob(os.path.join(LIB, section, "*", "Exports",
                                       "manifest.json")))


# never enumerate: the app's own output, the sections the app indexes itself,
# whatever the owner's config excludes, and anything already converted
SKIP_TOP = ({"Animation", "_Agent_Files"}
            | set(_cfg.get("indexer_skip_dirs") or [])
            | {top for top in os.listdir(LIB) if already_converted(top)})

items = []
for top in sorted(os.listdir(LIB)):
    if top in SKIP_TOP or top.startswith("."):
        continue
    root = os.path.join(LIB, top)
    if not os.path.isdir(root):
        continue
    for dp, dn, fn in os.walk(root):
        for f in fn:
            e = os.path.splitext(f)[1].lower()
            if e not in MODEL_EXT:
                continue
            p = os.path.join(dp, f)
            items.append({"section": top, "rel": os.path.relpath(p, LIB),
                          "path": p, "ext": e, "bytes": os.path.getsize(p)})

blends = [i for i in items if i["ext"] == ".blend"]
others = [i for i in items if i["ext"] not in (".blend", ".max")]

json.dump({"importable": others, "blend_files": blends},
          io.open(OUT, "w", encoding="utf-8"), indent=1)

# plain list so the bash launcher does not need to parse JSON
with io.open(BLENDS, "w", encoding="utf-8") as fh:
    for b in blends:
        fh.write(b["path"] + "\n")
        fh.write(b["section"] + "\n")

print("importable:", len(others), " blend kits:", len(blends))
print("blend kits by section:", dict(Counter(b["section"] for b in blends)))
print("importable by section:", dict(Counter(i["section"] for i in others)))
print("importable by ext:", dict(Counter(i["ext"] for i in others)))
print("wrote:", OUT, "and", BLENDS)
