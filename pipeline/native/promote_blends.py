"""FIX: promote the .blend object enumeration into the agent-facing native index.

Background: an earlier enumeration (native_index_blends.jsonl) walked every .blend
  under the library's top-level sections, read-only
It was NOT promoted, on a mis-reading of the owner's instruction "do not export mesh
from the Blendfiles, leave them be" - which was about EXPORTING/CONVERTING, not about
knowing what exists. A .blend is the MOST Blender-native format there is, so excluding
it was backwards.

The kit section is deliberately EXCLUDED here (see EXCLUDE_SECTIONS, resolved from the
library at run time): its kits are already represented assembly-by-assembly in
kb3d_models.jsonl, so promoting their loose objects would double-count and confuse.

Writes the extra records into native_models.jsonl (one native index, not two).
READ-ONLY on every .blend - nothing is opened or modified, this only re-files
records an earlier read-only pass already produced.
"""
import collections, glob, io, json, os, sys
from pathlib import Path

T = str(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import agent_files, library_root, section_root
AGENT = agent_files()
BLENDS = os.path.join(T, "native_index_blends.jsonl")
NATIVE = os.path.join(AGENT, "native_models.jsonl")
LIB = library_root() or "."


def _covered_section() -> str:
    """The section already covered assembly-by-assembly by kb3d_models.jsonl,
    i.e. the one holding kit exports. That is exactly the condition
    build_kb3d_index.py uses to fill kb3d_models.jsonl, so the exclusion cannot
    drift from the coverage it exists to avoid double-counting. Taken from the
    configured kitbash root, else derived from the library -- never hardcoded.
    """
    root = section_root("kitbash", "PHAROS_KB3D_ROOT")
    if not root and os.path.isdir(LIB):
        for top in sorted(os.listdir(LIB)):
            if glob.glob(os.path.join(LIB, top, "*", "Exports", "kit_manifest.json")):
                root = os.path.join(LIB, top)
                break
    return os.path.basename(root.rstrip("/\\")) if root else ""


EXCLUDE_SECTIONS = {s for s in (_covered_section(),) if s}

if not os.path.isfile(BLENDS):
    print("no blend index found to promote: %s (run run_blends.py first)" % BLENDS)
    sys.exit(0)

# 1. read the existing native records
rows = [json.loads(l) for l in io.open(NATIVE, encoding="utf-8") if l.strip()] if os.path.isfile(NATIVE) else []
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
if len(rows) < 10:
    raise SystemExit("FATAL: only %d records -- refusing to overwrite a "
                     "live index with a near-empty scan"
                     % len(rows))
with io.open(NATIVE, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

print("native_models.jsonl: %d -> %d  (+%d blend objects, %d dupes skipped)"
      % (before, len(rows), added, skipped_dup))
print("excluded sections:", sorted(EXCLUDE_SECTIONS) or "(none -- no kit exports found)")
print("added by section:", dict(by_section))
print("by kind now:", dict(collections.Counter(r.get("kind") for r in rows).most_common()))

# 3. .max files are a separate, real gap - flag them
maxf = []
if os.path.isdir(LIB):
    for dp, dn, fn in os.walk(LIB):
        for f in fn:
            if f.lower().endswith(".max"):
                maxf.append(os.path.join(dp, f))
print("\n.max files on disk (NOT readable by Blender or this pipeline): %d" % len(maxf))
total = sum(os.path.getsize(p) for p in maxf)
print("   total size: %.1f GB" % (total / 2**30))
for p in sorted(maxf)[:6]:
    print("   ", os.path.relpath(p, LIB))
