import io, os, json

LIB = r"D:\3D_Assets"
T = str(Path(__file__).resolve().parent)
OUT = os.path.join(T, "native_manifest.json")
BLENDS = os.path.join(T, "native_blends.txt")

SKIP_TOP = {"Leartes Env_ gumroad", "Animation", "_Agent_Files", "kiosk_data", "Kiosk_zCode"}
MODEL_EXT = {".fbx", ".obj", ".usd", ".usda", ".usdc", ".blend", ".max"}

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

from collections import Counter
print("importable:", len(others), " blend kits:", len(blends))
print("blend kits by section:", dict(Counter(b["section"] for b in blends)))
print("importable by section:", dict(Counter(i["section"] for i in others)))
print("importable by ext:", dict(Counter(i["ext"] for i in others)))
print("wrote:", OUT, "and", BLENDS)
