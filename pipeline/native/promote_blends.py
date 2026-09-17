import sys
from pathlib import Path
"""
FIX: promote the .blend object enumeration into the agent-facing native index.

Background: an earlier enumeration (native_index_blends.jsonl, 6,375 records) covered
  KitbashOrdner 5,731 / CGTrader 640 / BlendFiles 4
It was NOT promoted, on a mis-reading of the owner's instruction "do not export mesh
from the Blendfiles, leave them be" - which was about EXPORTING/CONVERTING, not about
knowing what exists. A .blend is the MOST Blender-native format there is, so excluding
it was backwards.

KitbashOrdner is deliberately EXCLUDED here: the 11 kits are already represented
assembly-by-assembly in kb3d_models.jsonl (1,041 assemblies / 5,847 submeshes), so
promoting 5,731 loose objects would double-count and confuse.

Writes the extra records into native_models.jsonl (one native index, not two).
READ-ONLY on every .blend - nothing is opened or modified, this only re-files
records an earlier read-only pass already produced.
"""
import io, os, json, collections

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import agent_files
AGENT = agent_files()
BLENDS = os.path.join(T, "native_index_blends.jsonl")
NATIVE = os.path.join(AGENT, "native_models.jsonl")
LIB = r"D:\3D_Assets"

EXCLUDE_SECTIONS = {"KitbashOrdner"}   # already covered by kb3d_models.jsonl

# 1. read the existing native records
rows = [json.loads(l) for l in io.open(NATIVE, encoding="utf-8") if l.strip()]
before = len(rows)
have = {(r.get("pack"), r.get("name")) for r in rows}

# 2. read the .blend enumeration and keep the sections not otherwise covered
added = 0
skipped_dup = 0
by_section = collections.Counter()
for line in io.open(BLENDS, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    sec = r.get("section")
    if sec in EXCLUDE_SECTIONS or r.get("error"):
        continue
    bbox = r.get("bbox_m")
    src = r.get("source") or ""
    rec = {
        "pack": sec,
        "source": "blend",
        "name": r.get("object"),
        "asset_path": None,
        "kind": "blend-object",
        "container": src,
        "container_kind": "blend",
        "fbx": src,                       # the .blend itself is the loadable container
        "exists": os.path.isfile(src) or os.path.isfile(os.path.join(LIB, src)),
        "bytes": None,
        "triangles": r.get("triangles"),
        "vertices": r.get("vertices"),
        "bbox_m": bbox,
        "max_dim_m": (max(bbox) if bbox else None),
        "materials": r.get("materials") or [],
        "material_count": len(r.get("materials") or []),
        "texture_files": [],
        "texture_count": 0,
        "collection": r.get("collection"),
        "note": "Blender-native; load the .blend directly. No export or conversion needed.",
    }
    if (rec["pack"], rec["name"]) in have:
        skipped_dup += 1
        continue
    have.add((rec["pack"], rec["name"]))
    rows.append(rec)
    added += 1
    by_section[sec] += 1

rows.sort(key=lambda x: (x["pack"] or "", x["name"] or ""))
with io.open(NATIVE, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

print("native_models.jsonl: %d -> %d  (+%d blend objects, %d dupes skipped)"
      % (before, len(rows), added, skipped_dup))
print("added by section:", dict(by_section))
print("by kind now:", dict(collections.Counter(r.get("kind") for r in rows).most_common()))

# 3. .max files are a separate, real gap - flag them
maxf = []
for dp, dn, fn in os.walk(LIB):
    for f in fn:
        if f.lower().endswith(".max"):
            maxf.append(os.path.join(dp, f))
print("\n.max files on disk (NOT readable by Blender or this pipeline): %d" % len(maxf))
total = sum(os.path.getsize(p) for p in maxf)
print("   total size: %.1f GB" % (total / 2**30))
for p in sorted(maxf)[:6]:
    print("   ", os.path.relpath(p, LIB))
