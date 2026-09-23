"""Fold the KitBash3D kit exports into the agent-facing model index.

One record per group assembly, matching the models.jsonl schema so an agent can
query kits and converted packs the same way.

Writes <library_root>/_Agent_Files/kb3d_models.jsonl
"""
import glob, io, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import agent_files, refuse_near_empty
AGENT = agent_files()
from _config import load, section_root
ROOT = section_root("kitbash", "PHAROS_KB3D_ROOT")
# kits may sit under more than one parent folder: init keeps ONE
# kitbash_root, but ingest records every kit parent in manifest_roots
_ROOTS = ([ROOT] if os.environ.get("PHAROS_KB3D_ROOT") else
          [ROOT] + [r for r in (load().get("manifest_roots") or [])
                    if isinstance(r, str)])
_MANS = sorted({os.path.normcase(os.path.abspath(m)): m for r in _ROOTS if r
                for m in glob.glob(os.path.join(r, "*", "Exports",
                                                "kit_manifest.json"))}.values())
os.makedirs(AGENT, exist_ok=True)
OUT = os.path.join(AGENT, "kb3d_models.jsonl")

rows = []
kits = 0
for man_path in _MANS:
    kit_dir = os.path.dirname(os.path.dirname(man_path))
    kit = os.path.basename(kit_dir)
    exports = os.path.dirname(man_path)
    d = json.load(io.open(man_path, encoding="utf-8"))
    kits += 1
    def _portable(path, base):
        """kit_manifests written by kb3d_metadata record ABSOLUTE paths
        from the machine that exported them; when the library was copied
        elsewhere those are dead -- re-anchor via the manifest's own dir.
        export_kb3d records group FBX paths RELATIVE to the Exports dir
        ("FBX/<grp>/<grp>.fbx"); those are absolutized against `base`.
        Anything the index emits is absolute so downstream consumers
        (registry `fbx` column, viewer) can open it."""
        if not path:
            return os.path.join(base, "")
        if not os.path.isabs(path):
            return os.path.normpath(os.path.join(base,
                                                 path.replace("/", os.sep)))
        if not os.path.isfile(path):
            rel = path.replace("/", os.sep)
            # try the tail segments (up to 4) relative to exports/kit root
            parts = rel.split(os.sep)
            for keep in range(1, 5):
                cand = os.path.join(base, *parts[-keep:])
                if os.path.isfile(cand):
                    return cand
            return os.path.join(base, os.path.basename(path))
        return path

    for g in d.get("groups") or []:
        fbx_abs = _portable(g.get("fbx") or "", exports)
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
            "texture_files": [_portable(t, kit_dir)
                             for t in (g.get("texture_files") or [])],
            "texture_count": g.get("texture_count", 0),
            "manifest": man_path,
        })

rows.sort(key=lambda r: (r["pack"], r["name"] or ""))
refuse_near_empty(len(rows), OUT)
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
