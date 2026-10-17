import glob, io, json, os, sys
from collections import Counter
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service"))
from _config import library_root, load, map_folders
from asset_service import kinds
LIB = library_root() or "."
OUT = os.path.join(T, "native_manifest.json")

_cfg = load()
MODEL_EXT = kinds.MODEL | kinds.BLEND

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
SKIP_TOP = ({(_cfg.get("sections") or {}).get("animation") or "Animation",
             _cfg.get("agent_files") or "_Agent_Files"}
            | set(_cfg.get("indexer_skip_dirs") or [])
            # folders `pharos.py ingest` already scans (scanner rows)
            | set(_cfg.get("scan_folders") or [])
            | {top for top in os.listdir(LIB) if already_converted(top)})

# the folders `pharos.py extract` approved in the library map: with them
# set, exactly those are walked (at any depth); without, every top-level
# folder not skipped
ALLOW = map_folders()
if ALLOW:
    walk_roots = [(a.split("/", 1)[0], os.path.join(LIB, *a.split("/")))
                  for a in sorted(ALLOW)]
else:
    walk_roots = [(top, os.path.join(LIB, top)) for top in sorted(os.listdir(LIB))
                  if top not in SKIP_TOP and not top.startswith(".")]
items = []
for top, root in walk_roots:
    if not os.path.isdir(root):
        continue
    for dp, dn, fn in os.walk(root):
        # converted packs / exported kits at ANY depth come in through
        # their manifests (models.jsonl / kb3d_models.jsonl): never again
        dn[:] = [d for d in dn if not (d == "Exports" and any(
            os.path.isfile(os.path.join(dp, d, m))
            for m in ("manifest.json", "kit_manifest.json")))]
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
          io.open(OUT, "w", encoding="utf-8", newline="\n"), indent=1)

print("importable:", len(others), " blend kits:", len(blends))
print("blend kits by section:", dict(Counter(b["section"] for b in blends)))
print("importable by section:", dict(Counter(i["section"] for i in others)))
print("importable by ext:", dict(Counter(i["ext"] for i in others)))
print("wrote:", OUT)
