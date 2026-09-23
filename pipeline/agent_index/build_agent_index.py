"""build_agent_index.py -- the agent-facing layer for the asset library.

Produces, under <library_root>/_Agent_Files :
  packs.json            one record per library section / pack, with a
                        `delivery_state` that says whether an agent can use it
                        RIGHT NOW and where the usable geometry lives
  models.jsonl          one JSON per exported mesh: absolute fbx path, tri/vert
                        counts, real-world bbox in metres, materials, and the
                        texture files each material needs
  MODELS_AGENT_INDEX.md readable guide + how to use

Re-runnable at any time; safe to run while a conversion batch is in progress
(reads only).  Never writes outside _Agent_Files.
"""
import io, os, json, datetime, collections
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

MODEL_EXT = {".fbx", ".obj", ".blend", ".usd", ".usda", ".usdc", ".abc",
             ".glb", ".gltf", ".dae", ".max", ".ma", ".mb", ".stl", ".ply"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".tga", ".exr", ".hdr",
           ".bmp", ".psd", ".webp", ".gif", ".svg"}

now = datetime.datetime.now().isoformat(timespec="seconds")


def walk_stats(root):
    n_model = collections.Counter()
    n_img = uasset = umap = 0
    total = 0
    for dp, dn, fn in os.walk(root):
        for f in fn:
            e = os.path.splitext(f)[1].lower()
            total += 1
            if e in MODEL_EXT:
                n_model[e] += 1
            elif e in IMG_EXT:
                n_img += 1
            elif e == ".uasset":
                uasset += 1
            elif e == ".umap":
                umap += 1
    return n_model, n_img, uasset, umap, total


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
# 1. packs.json
# --------------------------------------------------------------------------- #
packs = []
for name in sorted(os.listdir(LIB)):
    d = os.path.join(LIB, name)
    if not os.path.isdir(d) or name.startswith((".", "_")):
        continue
    # generic one-level expansion: if this folder's children carry Exports
    # dirs, each child is a pack (nested category layouts); otherwise the
    # folder itself is the pack candidate
    try:
        kids = [s for s in sorted(os.listdir(d))
                if os.path.isdir(os.path.join(d, s)) and not s.startswith(".")]
    except OSError:
        kids = []
    kid_packs = [s for s in kids
                 if os.path.isdir(os.path.join(d, s, "Exports"))]
    if kid_packs:
        candidates = [(s, os.path.join(d, s)) for s in kid_packs]
    else:
        candidates = [(name, d)]

    for sub, root in candidates:
        m, nimg, uasset, umap, total = walk_stats(root)
        if total == 0:
            continue
        exports = os.path.join(root, "Exports")
        man = read_manifest(exports)
        has_models = sum(m.values()) > 0

        if man and (man.get("counts") or {}).get("fbx_written", 0) > 0:
            state = "converted-fbx"
            note = "converted; use the FBX under Exports/FBX"
        elif uasset and not has_models:
            state = "unreal-only"
            note = "Unreal assets only - needs conversion before Blender can open it"
        elif uasset and has_models:
            state = "mixed"
            note = "both native model files and Unreal assets present"
        elif has_models:
            state = "native-models"
            note = "already openable by Blender/Unreal"
        elif nimg and not has_models:
            state = "image-only"
            note = "gallery/reference images only - NOT downloaded as assets"
        else:
            state = "other"
            note = ""

        rec = {
            "section": name,
            "pack": sub,
            "path": root,
            "delivery_state": state,
            "note": note,
            "counts": {
                "files": total,
                "fbx": m.get(".fbx", 0), "obj": m.get(".obj", 0),
                "blend": m.get(".blend", 0),
                "usd": m.get(".usd", 0) + m.get(".usda", 0) + m.get(".usdc", 0),
                "other_models": sum(v for k, v in m.items()
                                    if k not in (".fbx", ".obj", ".blend", ".usd", ".usda", ".usdc")),
                "uasset": uasset, "umap": umap, "images": nimg,
            },
        }
        if man:
            c = man.get("counts") or {}
            rec["export"] = {
                "dir": exports,
                "fbx_dir": os.path.join(exports, "FBX"),
                "textures_dir": os.path.join(exports, "Textures"),
                "manifest": os.path.join(exports, "manifest.json"),
                "meshes_exported": c.get("fbx_written", 0),
                "textures_exported": c.get("texture_files_written", 0),
                "failures": c.get("failures", 0),
                "exported_at": man.get("exported_at"),
            }
        packs.append(rec)

# packs.json is a LIVE index too: refuse BEFORE writing it, so a wrong root
# cannot leave an empty pack list behind for the app to read
refuse_near_empty(len(packs), os.path.join(AGENT, "packs.json"), "packs")

io.open(os.path.join(AGENT, "packs.json"), "w", encoding="utf-8").write(
    json.dumps({"schema": "pharos.agent.packs/v1", "generated": now,
                "root": LIB, "packs": packs}, indent=1, ensure_ascii=False))

# --------------------------------------------------------------------------- #
# 2. models.jsonl
# --------------------------------------------------------------------------- #
rows = []
manifest_dirs = []
for rec in packs:
    ex = (rec.get("export") or {}).get("dir")
    if ex:
        manifest_dirs.append((rec.get("pack") or rec["section"], ex))
# packs.json looks two levels deep; converted packs can sit deeper
# (<top>/<category>/<pack>/Exports) and ingest imports their recipes, so
# every Exports/manifest.json in the library must also get its mesh rows
_seen = {os.path.normcase(os.path.abspath(ex)) for _, ex in manifest_dirs}
for dp, dn, fn in os.walk(LIB):
    dn[:] = [x for x in dn if not x.startswith((".", "_")) and x != "Exports"]
    ex = os.path.join(dp, "Exports")
    if (os.path.isfile(os.path.join(ex, "manifest.json"))
            and os.path.normcase(os.path.abspath(ex)) not in _seen):
        _seen.add(os.path.normcase(os.path.abspath(ex)))
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

# --------------------------------------------------------------------------- #
# 3. MODELS_AGENT_INDEX.md
# --------------------------------------------------------------------------- #
by_pack = collections.defaultdict(list)
for r in rows:
    by_pack[r["pack"]].append(r)

state_counts = collections.Counter(p["delivery_state"] for p in packs)

L = []
A = L.append
A("# Converted Model Library - Agent Index")
A("")
A("> Machine-readable companions in this folder: **packs.json** (every section/pack")
A("> with a `delivery_state`) and **models.jsonl** (one JSON per exported mesh, with")
A("> dimensions, materials and the texture files each one needs).")
A("> Generated %s | Root: `%s`" % (now, LIB))
A("")
A("## Why this file exists")
A("An agent asked to build a scene must answer three things WITHOUT opening any asset")
A("file: (1) which packs are usable right now, (2) what a mesh's real size is, and")
A("(3) which texture files belong to it. `packs.json` + `models.jsonl` answer all three.")
A("")
A("## delivery_state - read this before planning anything")
A("")
A("| delivery_state | Meaning | What to do |")
A("|---|---|---|")
A("| `converted-fbx` | Exported to FBX; ready to import | Use `Exports/FBX/...` |")
A("| `native-models` | Ships FBX/OBJ/USD/Blend already | Use the files directly |")
A("| `unreal-only` | `.uasset` only - Blender CANNOT open it | Convert first, or ask the human |")
A("| `image-only` | Marketing images only, no assets downloaded | Ask the human to download it |")
A("| `mixed` | Both native models and Unreal assets | Prefer the native files |")
A("")
A("Current state of the library:")
A("")
A("| delivery_state | Sections/packs |")
A("|---|---:|")
for k, v in state_counts.most_common():
    A("| `%s` | %d |" % (k, v))
A("")
A("## Converted packs (usable today)")
A("")
if by_pack:
    A("| Pack | Meshes | Tris | Largest mesh (m) | Export folder |")
    A("|---|---:|---:|---:|---|")
    for pack, rs in sorted(by_pack.items()):
        tris = sum(r["triangles"] or 0 for r in rs)
        big = max((r["max_dim_m"] or 0) for r in rs)
        pack_root = next((p["path"] for p in packs if p["pack"] == pack), "")
        ex = os.path.join(pack_root, "Exports") if pack_root else "Exports"
        A("| **%s** | %d | %s | %.2f | `%s` |" % (pack, len(rs), "{:,}".format(tris), big, ex))
else:
    A("_Nothing converted yet._")
A("")
A("## How to use it")
A("")
A("Search `models.jsonl` by pack, size or name - it is a flat file, so grep is enough:")
A("")
A("```")
A("grep '\"pack\": \"<a-pack-name>\"' models.jsonl")
A("```")
A("")
A("Each line gives you the absolute FBX path, triangle/vertex counts, the bounding box")
A("in metres, the material slot names, and the absolute paths of the texture files.")
A("")
A("To see which packs are usable right now, read the `delivery_state` field in `packs.json`.")
A("")
A("## Rules an agent must respect")
A("")
A("1. **FBX are in metres.** Unreal stores centimetres; the exporter already converted.")
A("   Import at scale 1.0 - do NOT apply an extra 0.01.")
A("2. **Materials are placeholder.** FBX material slots exist but are not wired up.")
A("   Rebuild materials, using the `texture_files` list on each mesh.")
A("3. **`Exports/` is derived data.** It can be regenerated; never edit it by hand.")
A("4. **Never write outside `Exports/`.** The rest of the library is source data.")
A("5. If a mesh you need has `exists: false`, or its pack is `unreal-only`, stop and say")
A("   so rather than substituting a worse asset.")
A("")
A("## Mesh inventory by pack")
A("")
for pack, rs in sorted(by_pack.items()):
    A("### %s - %d meshes" % (pack, len(rs)))
    A("")
    A("| Mesh | Tris | Verts | BBox (m) | Materials | Textures |")
    A("|---|---:|---:|---|---|---:|")
    for r in sorted(rs, key=lambda x: -(x["triangles"] or 0))[:40]:
        b = r["bbox_m"] or [None, None, None]
        bs = " x ".join(("%.2f" % v) if isinstance(v, (int, float)) else "-" for v in b)
        A("| `%s` | %s | %s | %s | %d | %d |" % (
            r["name"],
            "{:,}".format(r["triangles"]) if r["triangles"] else "-",
            "{:,}".format(r["vertices"]) if r["vertices"] else "-",
            bs, len(r["materials"]), len(r["texture_files"])))
    if len(rs) > 40:
        A("")
        A("_(showing 40 of %d - see `models.jsonl` for the rest)_" % len(rs))
    A("")

io.open(os.path.join(AGENT, "MODELS_AGENT_INDEX.md"), "w", encoding="utf-8").write("\n".join(L))

print("packs       :", len(packs))
print("models      :", len(rows))
print("manifests   :", len(manifest_dirs))
print("states      :", dict(state_counts))
print("wrote packs.json, models.jsonl, MODELS_AGENT_INDEX.md")
