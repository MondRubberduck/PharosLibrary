"""build_agent_index.py -- the agent-facing layer for the asset library.

Produces, under <library_root>/_Agent_Files :
  models.jsonl          one JSON per exported mesh: absolute fbx path, tri/vert
                        counts, real-world bbox in metres, materials, and the
                        texture files each material needs

Re-runnable at any time; safe to run while a conversion batch is in progress
(reads only).  Never writes outside _Agent_Files.
"""
import io, os, json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service"))
from _config import agent_files, library_root, refuse_near_empty
from asset_service.fbx_dims import fbx_file_info
LIB = library_root() or "."
AGENT = agent_files() or os.path.join(LIB, "_Agent_Files")
os.makedirs(AGENT, exist_ok=True)

def read_manifest(exports_dir):
    p = os.path.join(exports_dir, "manifest.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None


FBX_PARSE_MAX_BYTES = 512 * 1024 * 1024


def fbx_counts(fb):
    """(triangles, vertices) read from the exported FBX, or None.

    The manifest's engine count is render LOD0 (the reduced fallback for
    Nanite meshes) and null for every SkeletalMesh; the exported file is
    the authority. Files over 512 MB are not parsed."""
    try:
        if os.path.getsize(fb) > FBX_PARSE_MAX_BYTES:
            return None
    except OSError:
        return None
    info = fbx_file_info(fb)
    if not info:
        return None
    return info.get("triangles"), info.get("vertices")


# --------------------------------------------------------------------------- #
# 2. models.jsonl
# --------------------------------------------------------------------------- #
rows = []
manifest_dirs = []
# every Exports/manifest.json in the library, at any depth (converted packs
# can sit under category folders): one walk, no per-folder census
for dp, dn, fn in os.walk(LIB):
    dn[:] = [x for x in dn if not x.startswith((".", "_")) and x != "Exports"]
    ex = os.path.join(dp, "Exports")
    if os.path.isfile(os.path.join(ex, "manifest.json")):
        manifest_dirs.append((os.path.basename(dp), ex))

for pack, ex in manifest_dirs:
    man = read_manifest(ex)
    if not man:
        continue
    for mesh in man.get("meshes") or []:
        fb = os.path.join(ex, (mesh.get("fbx") or "").replace("/", os.sep))
        tex_files = []
        for mat in mesh.get("materials") or []:
            for t in mat.get("textures") or []:
                f = t.get("file")
                if f:
                    tex_files.append(os.path.join(ex, f.replace("/", os.sep)))
        # parse failed / too big -> keep the engine value
        counts = fbx_counts(fb) if os.path.isfile(fb) else None
        rows.append({
            "pack": pack,
            "name": mesh.get("name"),
            "asset_path": mesh.get("asset_path"),
            "kind": mesh.get("kind"),
            "fbx": fb,
            "exists": os.path.isfile(fb),
            "bytes": mesh.get("bytes"),
            "triangles": counts[0] if counts else mesh.get("triangles"),
            "triangles_engine": mesh.get("triangles"),
            "vertices": counts[1] if counts else mesh.get("vertices"),
            "bbox_m": mesh.get("bbox_m"),
            "max_dim_m": (max(mesh["bbox_m"]) if mesh.get("bbox_m") else None),
            "materials": [m.get("name") for m in (mesh.get("materials") or [])],
            "texture_files": sorted(set(tex_files)),
        })

rows.sort(key=lambda r: (r["pack"], r["name"] or ""))
refuse_near_empty(len(rows), os.path.join(AGENT, "models.jsonl"))
with io.open(os.path.join(AGENT, "models.jsonl"), "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

print("models      :", len(rows))
print("manifests   :", len(manifest_dirs))
print("wrote models.jsonl")
