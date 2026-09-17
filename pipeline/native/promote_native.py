import sys
from pathlib import Path
"""
TASK 2 -- promote the native geometry records into the agent index.

Source : the 1,461 records produced by the native import pass
         (CGTrader / TurboSquid / Mens_V1 / uploads / KitbashOrdner FBX+USD)
Output: <library_root>/_Agent_Files/native_models.jsonl

Schema is models.jsonl-compatible so an agent queries all four indexes the same way.
Deliberately EXCLUDED: the .blend object enumeration (native_index_blends.jsonl) --
the owner's standing instruction is to leave .blend files alone, and the KitBash3D
kits are already covered assembly-by-assembly by kb3d_models.jsonl.
"""
import collections, io, json, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import agent_files, library_root
AGENT = agent_files()
LIB = library_root() or "."
T = str(Path(__file__).resolve().parent)
SRC = os.path.join(T, "native_index_20260915-172648.jsonl")
if not os.path.isfile(SRC):
    # look for any recent native_index_*.jsonl
    import glob
    candidates = sorted(glob.glob(os.path.join(T, "native_index_*.jsonl")))
    SRC = candidates[-1] if candidates else ""

if not SRC or not os.path.isfile(SRC):
    print("no native_index JSONL found to promote (run index_native_all.sh first)")
    sys.exit(0)

os.makedirs(AGENT, exist_ok=True)
OUT = os.path.join(AGENT, "native_models.jsonl")

rows = []
skipped = 0
for line in io.open(SRC, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    if r.get("error"):
        skipped += 1
        continue
    src = r.get("source") or ""
    abs_src = src if os.path.isabs(src) else os.path.join(LIB, src.replace("/", os.sep))
    bbox = r.get("bbox_m")
    rows.append({
        "pack": r.get("section"),
        "source": "native",
        "name": r.get("object"),
        "asset_path": None,
        "kind": r.get("kind"),
        "container": src,
        "fbx": abs_src,
        "exists": os.path.isfile(abs_src),
        "bytes": (os.path.getsize(abs_src) if os.path.isfile(abs_src) else None),
        "triangles": r.get("triangles"),
        "vertices": r.get("vertices"),
        "bbox_m": bbox,
        "max_dim_m": (max(bbox) if bbox else None),
        "materials": r.get("materials") or [],
        "material_count": len(r.get("materials") or []),
        "texture_files": [],
        "texture_count": 0,
        "collection": r.get("collection"),
    })

rows.sort(key=lambda x: (x["pack"] or "", x["name"] or ""))
if len(rows) < 10:
    raise SystemExit("FATAL: only %d records -- refusing to overwrite a "
                     "live index with a near-empty scan"
                     % len(rows))
with io.open(OUT, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

print("promoted      : %d records" % len(rows))
print("skipped/errors: %d" % skipped)
print("by section    :", dict(collections.Counter(r["pack"] for r in rows).most_common()))
print("by kind       :", dict(collections.Counter(r["kind"] for r in rows).most_common()))
print("with bbox     : %d / %d" % (sum(1 for r in rows if r["bbox_m"]), len(rows)))
print("with materials: %d / %d" % (sum(1 for r in rows if r["materials"]), len(rows)))
print("source exists : %d / %d" % (sum(1 for r in rows if r["exists"]), len(rows)))
print("wrote         : %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
