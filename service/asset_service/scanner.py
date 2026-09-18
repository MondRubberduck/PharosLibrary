"""Pharos Ingestion CLI — scan any folder of assets into the registry.

    python -m asset_service.scanner <folder> [--dry-run] [--verbose]

Discovers:
  - Mesh files (.fbx, .obj, .glb, .gltf, .stl, .usd, .blend) → mesh records
  - Texture folders (albedo/normal/roughness/… files grouped by stem) → texture sets
  - Audio files (.wav, .ogg, .mp3, .flac) → audio records with duration
  - Any Exports/manifest.json in the tree → authoritative GEOMETRY STATS
    (triangles/bbox) for matching meshes

The scanner is honest about its limits:
  - FBX bounding boxes are extracted when possible (binary FBX parse for
    geometry extents); OBJ/GLB via trimesh if available
  - Material wiring is NOT joined here — that lives in meshes_import.py
    via the crawler indexes + export manifests
  - Without trimesh, OBJ/GLB/STL get no dimensions (recorded, not guessed)

Writes into the same SQLite tables the HTTP API and MCP server read.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config

# ---------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------

MESH_EXTS = {".fbx", ".obj", ".glb", ".gltf", ".stl"}
BLEND_EXTS = {".blend"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tga", ".tif", ".tiff",
              ".exr", ".bmp"}

# texture channel detection from filename
CHANNEL_PATTERNS = [
    ("albedo", re.compile(r"(_alb|_albedo|_basecolor|_base_color|_diffuse|_col|_d)\.", re.I)),
    ("normal", re.compile(r"(_nrm|_normal|_norm|_n)\.", re.I)),
    ("roughness", re.compile(r"(_rough|_roughness|_rgh)\.", re.I)),
    ("metallic", re.compile(r"(_metal|_metallic|_met|_m)\.", re.I)),
    ("ao", re.compile(r"(_ao|_occ|_occlusion|_o)\.", re.I)),
    ("height", re.compile(r"(_height|_disp|_displacement|_h)\.", re.I)),
    ("emissive", re.compile(r"(_emissive|_emit|_e)\.", re.I)),
    ("opacity", re.compile(r"(_opacity|_alpha|_mask|_op)\.", re.I)),
    ("packed", re.compile(r"(_orm|_arm|_rma|_aorm)\.", re.I)),
    ("specular", re.compile(r"(_spec|_specular|_s)\.", re.I)),
]


def detect_channel(filename: str) -> Optional[str]:
    for role, pat in CHANNEL_PATTERNS:
        if pat.search(filename):
            return role
    return None


# ---------------------------------------------------------------------------
# FBX bounding box extraction (binary, no dependencies)
# ---------------------------------------------------------------------------

def obj_bbox(path: Path) -> Optional[list[float]]:
    """Parse OBJ vertex lines for min/max."""
    try:
        min_v = [float("inf")] * 3
        max_v = [float("-inf")] * 3
        count = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("v "):
                    parts = line.split()
                    if len(parts) >= 4:
                        for i in range(3):
                            v = float(parts[i + 1])
                            min_v[i] = min(min_v[i], v)
                            max_v[i] = max(max_v[i], v)
                        count += 1
                        if count > 500000:
                            break
        if count == 0:
            return None
        return [max_v[i] - min_v[i] for i in range(3)]
    except (OSError, ValueError):
        return None


def obj_triangles(path: Path) -> int:
    """Count faces in OBJ."""
    try:
        n = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("f "):
                    n += 1
        return n
    except OSError:
        return 0


def try_trimesh(path: Path) -> Optional[dict]:
    """Use trimesh if available for GLB/GLTF/STL."""
    try:
        import trimesh
        scene = trimesh.load(str(path), force="mesh", process=False)
        bounds = scene.bounds
        extents = bounds[1] - bounds[0]
        triangles = len(scene.faces)
        vertices = len(scene.vertices)
        return {
            "bbox": [float(x) for x in extents],
            "triangles": int(triangles),
            "vertices": int(vertices),
        }
    except ImportError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# audio duration (WAV header)
# ---------------------------------------------------------------------------

def wav_duration(path: Path) -> Optional[float]:
    """Duration via a proper RIFF chunk walk. A fixed 44-byte header
    breaks on BEXT/LIST/cue metadata chunks (offset 40 then holds chunk
    headers, not the data size -- laptop-run found 8628s for a 2s hit)."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                return None
            rate = channels = bits = 0
            data_size = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid = ch[:4]
                (csz,) = struct.unpack("<I", ch[4:8])
                if cid == b"fmt ":
                    body = f.read(min(csz, 16))
                    if len(body) >= 14:
                        channels = struct.unpack_from("<H", body, 2)[0]
                        rate = struct.unpack_from("<I", body, 4)[0]
                        bits = struct.unpack_from("<H", body, 14)[0] or 16
                    f.seek(csz - min(csz, 16), 1)
                elif cid == b"data":
                    data_size = csz
                    break                       # enough for duration
                else:
                    f.seek(csz + (csz & 1), 1)   # pad byte + LIST/BEXT skip
            if data_size is None:
                return None
            if rate == 0 or channels == 0:
                return None
            bytes_per_frame = channels * (bits // 8)
            if bytes_per_frame == 0:
                return None
            return data_size / bytes_per_frame / rate
    except (OSError, struct.error):
        return None


# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------

def scan_folder(folder: Path, dry_run: bool = False, verbose: bool = False,
                db_path: Optional[str] = None) -> dict:
    """Walk a folder, discover assets, insert into the registry.
    Returns a summary dict."""
    if not folder.is_dir():
        raise NotADirectoryError(f"not a directory: {folder}")

    folder_str = folder.as_posix()
    summary = {
        "meshes": 0, "texture_sets": 0, "audio": 0,
        "manifests_read": 0, "skipped": 0, "errors": 0,
    }

    conn = None
    if not dry_run:
        Path(db_path or config.DB_PATH).parent.mkdir(parents=True,
                                                    exist_ok=True)
        conn = sqlite3.connect(db_path or config.DB_PATH)
        conn.row_factory = sqlite3.Row
        # a fresh install may run the scanner BEFORE the server ever
        # started -- create every table this scanner writes (all DDLs are
        # IF NOT EXISTS, so this is a no-op against an existing registry)
        from asset_service.meshes_import import MESHES_DDL
        from asset_service.textures_import import TEXTURES_DDL
        from asset_service.audio_import import AUDIO_DDL, ensure_source_column
        conn.executescript(MESHES_DDL + TEXTURES_DDL + AUDIO_DDL)
        # the audio source column post-dates some registries: make sure
        # THIS writer can write before any importer ever ran
        ensure_source_column(conn)
        conn.commit()

    # --- pass 1: find manifests (authoritative mesh data) ---
    manifests = {}
    for mf in folder.rglob("manifest.json"):
        # skip .bak files — only exact filename (handover instruction)
        if mf.name != "manifest.json":
            continue
        if "Exports" not in str(mf.parent):
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            if "meshes" in m:
                manifests[mf.parent.parent] = m
                summary["manifests_read"] += 1
        except (ValueError, OSError):
            pass

    # --- pass 2: walk files ---
    mesh_files = []
    audio_files = []
    image_files = []

    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in MESH_EXTS or ext in BLEND_EXTS:
            mesh_files.append(p)
        elif ext in AUDIO_EXTS:
            audio_files.append(p)
        elif ext in IMAGE_EXTS:
            image_files.append(p)

    # --- mesh records ---
    # if a manifest covers this folder, use its data (exact wiring)
    manifest_meshes = {}  # fbx_path -> record
    for parent, m in manifests.items():
        for me in m.get("meshes", []):
            fbx = me.get("fbx", "")
            if fbx:
                manifest_meshes[fbx] = me

    for mf in mesh_files:
        rel = mf.relative_to(folder).as_posix()
        name = mf.stem

        # check manifest first
        me = manifest_meshes.get(mf.as_posix()) or manifest_meshes.get(rel)
        if me:
            bbox = me.get("bbox_m") or [0, 0, 0]
            record = {
                "name": name, "pack": folder.name, "source": "scan",
                "kind": me.get("kind", "mesh"),
                "fbx": mf.as_posix(), "on_disk": 1,
                "bytes": mf.stat().st_size,
                "triangles": me.get("triangles", 0),
                "vertices": me.get("vertices", 0),
                "bbox": bbox,
            }
        else:
            # try to extract geometry
            geo = None
            if mf.suffix.lower() == ".fbx":
                from asset_service.fbx_dims import fbx_bbox_m
                g = fbx_bbox_m(mf)
                if g:
                    geo = {"bbox": g["bbox_m"],
                           "triangles": g["triangles"],
                           "vertices": g["vertices"]}
            elif mf.suffix.lower() == ".obj":
                bbox = obj_bbox(mf)
                tri = obj_triangles(mf)
                if bbox:
                    geo = {"bbox": bbox, "triangles": tri, "vertices": 0}
            elif mf.suffix.lower() in (".glb", ".gltf", ".stl"):
                geo = try_trimesh(mf)
            record = {
                "name": name, "pack": folder.name, "source": "scan",
                "kind": "mesh", "fbx": mf.as_posix(), "on_disk": 1,
                "bytes": mf.stat().st_size,
                "triangles": geo["triangles"] if geo else 0,
                "vertices": geo["vertices"] if geo else 0,
                "bbox": geo["bbox"] if geo else [0, 0, 0],
            }

        if dry_run:
            if verbose:
                print(f"  [mesh] {record['name']} tri={record['triangles']}")
            summary["meshes"] += 1
        else:
            _insert_mesh(conn, record, folder_str)
            summary["meshes"] += 1

    # --- texture sets (group images by stem) ---
    tex_groups: dict[str, list[Path]] = {}
    for img in image_files:
        stem = _texture_stem(img.stem)
        tex_groups.setdefault(stem, []).append(img)

    for stem, files in sorted(tex_groups.items()):
        channels = {}
        for f in files:
            ch = detect_channel(f.name)
            if ch:
                channels.setdefault(ch, f.as_posix())
        if not channels and len(files) < 2:
            continue  # single unidentifiable image — not a set
        if dry_run:
            summary["texture_sets"] += 1
            if verbose:
                print(f"  [tex]  {stem} -> {list(channels.keys())}")
        else:
            _insert_texture_set(conn, stem, files, channels, folder)
            summary["texture_sets"] += 1

    # --- audio ---
    for af in audio_files:
        dur = wav_duration(af) if af.suffix.lower() == ".wav" else None
        if dry_run:
            summary["audio"] += 1
            if verbose:
                print(f"  [aud]  {af.name} dur={dur}")
        else:
            _insert_audio(conn, af, dur, folder)
            summary["audio"] += 1

    if conn:
        conn.commit()
        conn.close()

    return summary


def _texture_stem(stem: str) -> str:
    """Strip channel suffix from texture filename stem."""
    for role, pat in CHANNEL_PATTERNS:
        m = pat.search(stem + ".")
        if m:
            return stem[:m.start()] or stem
    return stem


def _insert_mesh(conn, record, root):
    tags = json.dumps([record["name"].lower(), record["pack"].lower()])
    meta = json.dumps({"stems": [record["name"].lower()],
                       "themes": [], "facets": {"pack": record["pack"].lower()}})
    conn.execute(
        "INSERT OR REPLACE INTO meshes (name,pack,source,kind,fbx,on_disk,"
        "bytes,triangles,vertices,submeshes,bbox_x,bbox_y,bbox_z,max_dim,"
        "materials,texture_count,texture_files,tags,meta) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record["name"], record["pack"], record["source"], record["kind"],
         record["fbx"], record["on_disk"], record["bytes"],
         record["triangles"], record["vertices"], 0,
         record["bbox"][0] if len(record["bbox"]) > 0 else 0,
         record["bbox"][1] if len(record["bbox"]) > 1 else 0,
         record["bbox"][2] if len(record["bbox"]) > 2 else 0,
         max(record["bbox"]) if record["bbox"] else 0,
         "[]", 0, "[]", tags, meta))


def _insert_texture_set(conn, stem, files, channels, folder):
    first = sorted(files)[0]
    conn.execute(
        "INSERT OR REPLACE INTO textures (name,grp,sub,folder,files,images,"
        "file_count,bytes,first_image,thumb,tags,meta,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stem, folder.name, "Scanned", str(folder),
         json.dumps([f.as_posix() for f in files]),
         json.dumps([f.as_posix() for f in sorted(files)
                     if f.suffix.lower() in {".jpg", ".png", ".webp"}]),
         len(files), sum(f.stat().st_size for f in files),
         first.as_posix(), first.as_posix(),
         json.dumps([stem.lower()]),
         json.dumps({"stems": [stem.lower()], "themes": [],
                     "facets": {"grp": folder.name.lower()}}),
         "scan"))


def _insert_audio(conn, af, duration, folder):
    rel = af.relative_to(folder).as_posix()
    # folder taxonomy beats a generic label: Impacts/Metal/x.wav ->
    # cat "Impacts", sub "Metal"; loose root files fall back to SFX
    parts = Path(rel).parts
    conn.execute(
        "INSERT OR REPLACE INTO audio (name,cat,sub,rel,ext,bytes,dur,sr,ch,"
        "playable,desc,tags,meta,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (af.stem, parts[0] if len(parts) >= 2 else "SFX",
     parts[1] if len(parts) >= 3 else "", rel, af.suffix.lower(),
         af.stat().st_size, duration, 0, 0,
         1 if af.suffix.lower() != ".aif" else 0, "",
         json.dumps([af.stem.lower()]),
         json.dumps({"stems": [af.stem.lower()], "themes": [],
                     "facets": {"cat": folder.name.lower()}}),
         "scan"))


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Pharos scanner — index any folder of assets")
    parser.add_argument("folder", help="folder to scan")
    parser.add_argument("--dry-run", action="store_true",
                        help="discover but don't write to DB")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--db", default=None,
                        help="registry DB path (default: from config)")
    args = parser.parse_args(argv)

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"error: {folder} is not a directory", file=sys.stderr)
        return 1

    print(f"scanning {folder} ...")
    summary = scan_folder(folder, dry_run=args.dry_run,
                          verbose=args.verbose, db_path=args.db)
    mode = "DRY RUN — nothing written" if args.dry_run else "written to registry"
    print(f"\n{mode}")
    print(f"  meshes:        {summary['meshes']}")
    print(f"  texture sets:  {summary['texture_sets']}")
    print(f"  audio:         {summary['audio']}")
    print(f"  manifests:     {summary['manifests_read']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
