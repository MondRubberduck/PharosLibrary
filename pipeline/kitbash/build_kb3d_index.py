import sys
from pathlib import Path
"""Fold the KitBash3D kit exports into the agent-facing model index.

One record per group assembly, matching the models.jsonl schema so an agent can
query kits and Leartes packs the same way.

Writes <library_root>/_Agent_Files/kb3d_models.jsonl
"""
import glob, io, json, os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import agent_files
AGENT = agent_files()
from _config import section_root
ROOT = section_root("kitbash", "PHAROS_KB3D_ROOT")
os.makedirs(AGENT, exist_ok=True)
OUT = os.path.join(AGENT, "kb3d_models.jsonl")

rows = []
kits = 0
for man_path in sorted(glob.glob(os.path.join(ROOT, "*", "Exports", "kit_manifest.json"))):
    kit_dir = os.path.dirname(os.path.dirname(man_path))
    kit = os.path.basename(kit_dir)
    exports = os.path.dirname(man_path)
    d = json.load(io.open(man_path, encoding="utf-8"))
    kits += 1
    for g in d.get("groups") or []:
        fbx_abs = os.path.join(exports, (g["fbx"] or "").replace("/", os.sep))
        bbox = g.get("bbox_m")
        rows.append({
            "pack": kit,
            "source": "kitbash3d",
            "name": g.get("group"),
            "asset_path": None,
            "kind": "group-assembly",
            "fbx": fbx_abs,
            "exists": os.path.isfile(fbx_abs),
            "bytes": g.get("bytes"),
            "triangles": g.get("triangles"),
            "vertices": g.get("vertices"),
            "submeshes": g.get("submeshes"),
            "bbox_m": bbox,
            "max_dim_m": (max(bbox) if bbox else None),
            "materials": g.get("materials") or [],
            "material_count": g.get("material_count", 0),
            "texture_files": g.get("texture_files") or [],
            "texture_count": g.get("texture_count", 0),
            "manifest": man_path,
        })

rows.sort(key=lambda r: (r["pack"], r["name"] or ""))
if len(rows) < 10:
    raise SystemExit("FATAL: only %d records -- refusing to overwrite a "
                     "live index with a near-empty scan (wrong root?)"
                     % len(rows))
with io.open(OUT, "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

present = sum(1 for r in rows if r["exists"])
with_tex = sum(1 for r in rows if r["texture_files"])
tris = sum(r["triangles"] or 0 for r in rows)
subs = sum(r["submeshes"] or 0 for r in rows)
print("kits: %d   assemblies: %d   present on disk: %d" % (kits, len(rows), present))
print("submeshes: %d   triangles: %s" % (subs, format(tris, ",")))
print("assemblies with textures: %d / %d" % (with_tex, len(rows)))
print("wrote:", OUT, "(%d bytes)" % os.path.getsize(OUT))
