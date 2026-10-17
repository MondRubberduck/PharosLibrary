"""Pharos Ingestion CLI — scan any folder of assets into the registry.

    python -m asset_service.scanner <folder> [--dry-run] [--verbose]

Discovers:
  - Mesh files (.fbx, .obj, .glb, .gltf, .stl, .usd, .blend) → mesh records
  - Texture folders (albedo/normal/roughness/… files grouped by stem) → texture sets
  - Audio files (.wav, .ogg, .mp3, .flac) → audio records with duration
  - Converted packs (a folder holding Exports/manifest.json or
    Exports/kit_manifest.json) are SKIPPED: they are imported through their
    manifests (pharos.py ingest). Scanning an Exports folder directly still
    reads its manifest.json for GEOMETRY STATS (triangles/bbox)
  - A model file that already has a row from a richer source (native/blend
    measured in Blender, leartes/kitbash3d from a manifest) is SKIPPED: one
    file, one description. An earlier scan row of such a file is removed

The scanner is honest about its limits:
  - FBX bounding boxes are extracted when possible (binary FBX parse for
    geometry extents); OBJ/GLB/STL by measure.py (standard library)
  - Material wiring is NOT joined here — that lives in meshes_import.py
    via the crawler indexes + export manifests
  - Every fact comes from one read of the file (measure.py); unreadable
    files keep their name facts (LOD, collision) and no size
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
from asset_service import textmatch as tm


# ---------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------

from asset_service import kinds

MESH_EXTS = kinds.MODEL_SCAN
BLEND_EXTS = kinds.BLEND
AUDIO_EXTS = kinds.AUDIO
IMAGE_EXTS = kinds.IMAGE

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
        "meshes": 0, "parked": 0, "texture_sets": 0, "audio": 0,
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
        from asset_service.meshes_import import MESHES_DDL, _migrate
        from asset_service.textures_import import (TEXTURES_DDL,
                                                   ensure_scan_unique_index)
        from asset_service.audio_import import AUDIO_DDL, ensure_source_column
        # an OLDER registry's tables lack columns the new indexes filter on
        # (`source`): migrate first, then create (every scan crashed with
        # 'no such column: source' on a registry from an older Pharos)
        _migrate(conn)
        conn.executescript(MESHES_DDL + TEXTURES_DDL + AUDIO_DDL)
        from asset_service.meshes_import import ensure_parked
        ensure_parked(conn)
        # the audio source column and the textures scan-unique index
        # post-date some registries: make sure THIS writer can write
        # before any importer ever ran
        ensure_source_column(conn)
        ensure_scan_unique_index(conn)
        conn.commit()

    # converted packs (a folder holding Exports/manifest.json or
    # Exports/kit_manifest.json) are imported through their manifests by
    # meshes_import; scanning them too indexed every FBX a second time,
    # without its recipe. Scanning an Exports folder directly still works.
    converted = {m.parent.parent for pat in ("Exports/manifest.json",
                                             "Exports/kit_manifest.json")
                 for m in folder.rglob(pat)}
    # a pack scanned while still RAW keeps its old scan rows after it is
    # converted (scan rows survive every rebuild): drop them, or the mesh
    # shows up twice -- once from the manifest (with recipe), once here
    if converted and conn is not None:
        from asset_service.db import path_under
        stale = [(tbl, rid) for tbl, col in (("meshes", "fbx"),
                                             ("textures", "folder"))
                 for rid, val in conn.execute(
                     f"SELECT id, {col} FROM {tbl} WHERE source='scan'")
                 if val and any(path_under(val, c) for c in converted)]
        for tbl, rid in stale:
            conn.execute(f"DELETE FROM {tbl} WHERE id=?", (rid,))
        if stale:
            conn.commit()
            print(f"  removed {len(stale)} earlier scan row(s) inside "
                  f"converted pack(s)", flush=True)

    manifests = {}
    for mf in folder.rglob("manifest.json"):
        # skip .bak files — only exact filename (handover instruction)
        if mf.name != "manifest.json":
            continue
        if "Exports" not in str(mf.parent) or mf.parent.parent in converted:
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
        # macOS metadata noise (AppleDouble ._<name>.ext on exFAT/SMB/ZIP,
        # .DS_Store) is not an asset -- it once indexed as 0-geometry
        # meshes/audio and polluted every size filter
        if p.name.startswith("._") or p.name == ".DS_Store":
            continue
        if converted and any(a in converted for a in p.parents):
            summary["skipped"] += 1
            continue
        ext = p.suffix.lower()
        if ext in MESH_EXTS or ext in BLEND_EXTS:
            mesh_files.append(p)
        elif ext in AUDIO_EXTS:
            audio_files.append(p)
        elif ext in IMAGE_EXTS:
            image_files.append(p)
    if summary["skipped"]:
        print(f"  skipped {summary['skipped']} file(s) in {len(converted)} "
              f"converted pack folder(s) "
              f"({', '.join(sorted(c.name for c in converted))}): they hold "
              f"Exports/manifest.json or Exports/kit_manifest.json and are "
              f"imported through their manifests by 'pharos.py ingest', "
              f"not scanned", flush=True)

    # a model file that already has a row from a richer source (native/
    # blend: measured in Blender; leartes/kitbash3d: export manifest) gets
    # no second SERVED row: the file-level reading here contradicted the
    # measured one (one OBJ: 20,938 triangles there, 10,600 and a 2,484 m
    # bbox here). Its scan row is PARKED (meshes_parked) instead: the next
    # import serves it again if that richer row disappears. The sources
    # spell one path differently (D:/Lib\Pack\x.fbx, D:\Lib\Pack\x.fbx,
    # D:/Lib/Pack/x.fbx), so compare normalised text; ONE SELECT for the
    # run, not one per file. A dry run reads the registry too (SELECT
    # only): it must count what a real run writes.
    reg = conn
    if (reg is None and mesh_files
            and Path(db_path or config.DB_PATH).is_file()):
        reg = sqlite3.connect(db_path or config.DB_PATH)
    if reg is not None and mesh_files:
        from asset_service.db import _norm_path
        cand = {_norm_path(p) for p in mesh_files}
        try:
            hits = [(rid, src, key) for rid, src, fbx in reg.execute(
                        "SELECT id, source, fbx FROM meshes WHERE fbx != ''")
                    if (key := _norm_path(fbx)) in cand]
        except sqlite3.Error:
            if conn is not None:
                raise
            hits = []            # dry run: this registry has no meshes yet
        # a row WITHOUT a source name (pre-`source` registry; the next
        # rebuild drops it) covers nothing: it must not cost a scan row
        richer = {key: src for _, src, key in hits if src and src != "scan"}
        # a file scanned BEFORE the richer source covered it kept its scan
        # row (scan rows survive every rebuild): that row is the same
        # duplicate. Scan rows of files nobody else covers are not touched.
        stale = [rid for rid, src, key in hits
                 if src == "scan" and key in richer]
        if stale and conn is not None:
            for rid in stale:
                conn.execute("DELETE FROM meshes WHERE id=?", (rid,))
            conn.commit()
        if richer:
            n_cov = sum(1 for p in mesh_files if _norm_path(p) in richer)
            print(f"  parked {n_cov} model file(s) that already have a "
                  f"richer row for the same file "
                  f"({', '.join(sorted(set(richer.values())))}): no second, "
                  f"file-level row is served (kept aside in case that row "
                  f"goes away)", flush=True)
    else:
        richer = {}
    if reg is not None and reg is not conn:
        reg.close()

    # --- mesh records ---
    # if a manifest covers this folder, use its data (exact wiring)
    manifest_meshes = {}  # fbx_path -> record
    for parent, m in manifests.items():
        for me in m.get("meshes", []):
            fbx = me.get("fbx", "")
            if fbx:
                manifest_meshes[fbx] = me

    # every fact comes from ONE read of the file (measure.py), cached per
    # folder under _Agent_Files/index: a re-scan reads only changed files
    from asset_service import measure as _measure
    lib_root = Path(config.LIBRARY_ROOT)
    index_file = _measure.index_path(lib_root, folder)
    cache = _measure.load_index(index_file)
    seen_index: dict = {}

    def _rel_of(p: Path) -> str:
        try:
            return p.resolve().relative_to(lib_root.resolve()).as_posix()
        except (ValueError, OSError):
            return p.as_posix()

    try:
        for mf in mesh_files:
            rel = mf.relative_to(folder).as_posix()
            name = mf.stem

            try:
                if mf.suffix.lower() in BLEND_EXTS:
                    m = None            # .blend files are Blender's business
                else:
                    m = _measure.measure_cached(mf, _rel_of(mf), cache)
                    seen_index[m["rel"]] = m
                size = lo = hi = None
                facts: dict = {}
                if m is not None:
                    size, lo, hi = _measure.registry_bbox(m)
                    facts = _measure.facts_of(m)
                    if m.get("error") and not size and verbose:
                        print(f"  [warn] {mf.name}: {m['error']}", flush=True)
                kind = "SkeletalMesh" if m and m.get("kind") == "skeletal" else "mesh"
                # check manifest first
                me = manifest_meshes.get(mf.as_posix()) or manifest_meshes.get(rel)
                if me:
                    # counts from the file itself when it parses: the
                    # manifest's engine count is render LOD0 (the reduced
                    # Nanite fallback), not the exported source mesh
                    record = {
                        "name": name, "pack": folder.name, "source": "scan",
                        "kind": me.get("kind", kind),
                        "fbx": mf.as_posix(), "on_disk": 1,
                        "bytes": mf.stat().st_size,
                        "triangles": (m["triangles"] if m and m["triangles"] is not None
                                      else me.get("triangles")),
                        "vertices": (m["vertices"] if m and m["vertices"] is not None
                                     else me.get("vertices")),
                        "bbox": size or me.get("bbox_m") or [0, 0, 0],
                    }
                else:
                    record = {
                        "name": name, "pack": folder.name, "source": "scan",
                        "kind": kind, "fbx": mf.as_posix(), "on_disk": 1,
                        "bytes": mf.stat().st_size,
                        # unknown stays NULL: a 0 passed every max_tri
                        # filter for free (same rule as meshes_import)
                        "triangles": m["triangles"] if m else None,
                        "vertices": m["vertices"] if m else None,
                        "bbox": size or [0, 0, 0],
                    }
                record["facts"] = facts
                record["corners"] = (lo, hi)
                record["materials"] = (m or {}).get("materials") or []

                covered = bool(richer) and _norm_path(mf) in richer
                if dry_run:
                    if verbose:
                        print(f"  [mesh] {record['name']} tri={record['triangles']}")
                else:
                    _insert_mesh(conn, record, folder_str,
                                 "meshes_parked" if covered else "meshes")
                summary["parked" if covered else "meshes"] += 1
            except Exception as exc:                          # noqa: BLE001
                summary["errors"] += 1
                print(f"  [warn] skipped mesh {mf.name}: "
                      f"{type(exc).__name__}: {exc}", flush=True)

        if seen_index and not dry_run:
            _measure.save_index(index_file, seen_index)

        # --- texture sets (group images by PARENT FOLDER + stem) ---
        # keying on the stem alone merged same-named sets from different
        # packs and turned resolution subfolders (2K/, 4K/) into phantom
        # map channels; the parent folder keeps every real set separate
        tex_groups: dict[tuple, list[Path]] = {}
        for img in image_files:
            stem = _texture_stem(img.stem)
            tex_groups.setdefault((img.parent, stem), []).append(img)

        for (parent, stem), files in sorted(tex_groups.items()):
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
                _insert_texture_set(conn, stem, files, channels, parent)
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

        # durable per-section progress: a crash in a LATER section must
        # not roll back the rows an EARLIER section already wrote
        if conn:
            conn.commit()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:                             # noqa: BLE001
                pass

    return summary


def _texture_stem(stem: str) -> str:
    """Strip channel suffix from texture filename stem."""
    for role, pat in CHANNEL_PATTERNS:
        m = pat.search(stem + ".")
        if m:
            return stem[:m.start()] or stem
    return stem


def _word_meta(name: str, context: str, themes_table: dict,
               strict: bool, facets: dict, extra: dict = None) -> tuple:
    """(tags json, meta json) for a scanned row, cut by the SAME rules as
    the richer importers (textmatch). A scanned row used to carry its raw
    file name as its only tag and no theme at all, so a library indexed by
    the scanner alone -- every fresh install without the optional chains
    -- could not be filtered by theme and matched whole file names only."""
    words = tm.words(name, tm.STOPWORDS)
    themes = tm.match_themes(name, themes_table, strict=strict)
    stems = tm.stems(name)
    ctx = [w for w in tm.stems(context) if w not in stems and len(w) > 2]
    return (json.dumps(list(dict.fromkeys(words + themes)),
                       ensure_ascii=False),
            json.dumps({"stems": stems, "ctx": ctx, "themes": themes,
                        "facets": facets, **(extra or {})},
                       ensure_ascii=False))


def _insert_mesh(conn, record, root, table="meshes"):
    from asset_service.meshes_import import scan_meta
    tags, meta = scan_meta(record["name"], record["pack"], record["fbx"])
    # one place per file: served (meshes) or parked (meshes_parked)
    other = "meshes" if table == "meshes_parked" else "meshes_parked"
    conn.execute(f"DELETE FROM {other} WHERE fbx=? AND source='scan'",
                 (record["fbx"],))
    lo, hi = record.get("corners") or (None, None)
    facts = record.get("facts") or {}
    conn.execute(
        f"INSERT OR REPLACE INTO {table} (name,pack,source,kind,fbx,on_disk,"
        "bytes,triangles,vertices,submeshes,bbox_x,bbox_y,bbox_z,max_dim,"
        "materials,texture_count,texture_files,tags,meta,"
        "bbox_min_x,bbox_min_y,bbox_min_z,bbox_max_x,bbox_max_y,bbox_max_z,facts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record["name"], record["pack"], record["source"], record["kind"],
         record["fbx"], record["on_disk"], record["bytes"],
         record["triangles"], record["vertices"], 0,
         record["bbox"][0] if len(record["bbox"]) > 0 else 0,
         record["bbox"][1] if len(record["bbox"]) > 1 else 0,
         record["bbox"][2] if len(record["bbox"]) > 2 else 0,
         max(record["bbox"]) if record["bbox"] else 0,
         json.dumps(record.get("materials") or [], ensure_ascii=False), 0, "[]",
         tags, meta,
         *(lo if lo else (None, None, None)), *(hi if hi else (None, None, None)),
         json.dumps(facts, ensure_ascii=False) if facts else None))


def _tex_themes() -> dict:
    from asset_service.textures_import import TEX_THEMES
    return TEX_THEMES


def _audio_themes() -> dict:
    from asset_service.audio_import import AUDIO_THEMES
    return AUDIO_THEMES


def _insert_texture_set(conn, stem, files, channels, folder):
    first = sorted(files)[0]
    # the same file-derived facts the texture importer serves (measured
    # pixel size, which file is which map, a preview as thumb): a scanned
    # set used to carry none of them
    from asset_service import imgmeta, textures_import
    extra, thumb = {}, first.as_posix()
    try:
        shown = sorted((f for f in files if f.suffix.lower() in
                        {".jpg", ".jpeg", ".png", ".webp"}),
                       key=lambda p: p.name.lower())
        facts = textures_import._facts(
            folder, {f.name: f.stat().st_size for f in files
                     if f.parent == folder}, shown)
        px = next((list(s) for s in (imgmeta.image_size(r)
                                     for r in facts["ref"]) if s), None)
        extra = {"px": px, "maps": facts["maps"],
                 "res": textures_import._res_label(max(px)) if px else "",
                 "res_source": "measured" if px else "name"}
        thumb = facts["thumb"] or thumb
    except (OSError, ValueError, KeyError):
        pass        # facts are a bonus; the set itself is still indexed
    conn.execute(
        "INSERT OR REPLACE INTO textures (name,grp,sub,folder,files,images,"
        "file_count,bytes,first_image,thumb,tags,meta,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stem, folder.name, "Scanned", str(folder),
         json.dumps([f.as_posix() for f in files]),
         json.dumps([f.as_posix() for f in sorted(files)
                     if f.suffix.lower() in {".jpg", ".png", ".webp"}]),
         len(files), sum(f.stat().st_size for f in files),
         first.as_posix(), thumb,
         *_word_meta(stem, folder.name, _tex_themes(), True,
                     {"grp": folder.name.lower()}, extra),
         "scan"))


def _insert_audio(conn, af, duration, folder):
    # rel is section-relative when the file lives under the configured
    # AUDIO section (matching the crawl jsonl format), else ABSOLUTE:
    # UNIQUE(rel) once made two separately-scanned folders silently
    # overwrite each other's same-named files
    try:
        rel = af.resolve().relative_to(config.AUDIO_ROOT).as_posix()
    except ValueError:
        rel = af.resolve().as_posix()
    # folder taxonomy beats a generic label: Impacts/Metal/x.wav ->
    # cat "Impacts", sub "Metal"; loose root files fall back to SFX. For a
    # file OUTSIDE the audio section the folders come from the path below
    # the scanned folder (from the absolute path they read 'C:\' > 'Users')
    if Path(rel).is_absolute():
        try:
            parts = af.resolve().relative_to(folder.resolve()).parts
        except ValueError:
            parts = (af.name,)
    else:
        parts = Path(rel).parts
    conn.execute(
        "INSERT OR REPLACE INTO audio (name,cat,sub,rel,ext,bytes,dur,sr,ch,"
        "playable,desc,tags,meta,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (af.stem, parts[0] if len(parts) >= 2 else "SFX",
     parts[1] if len(parts) >= 3 else "", rel, af.suffix.lower(),
         af.stat().st_size, duration, 0, 0,
         1 if af.suffix.lower() != ".aif" else 0, "",
         # the category folders are part of what a sound is called
         # ('Impacts/Metal'); for a file outside the audio section `rel`
         # is absolute, and its path must not turn into tags
         *_word_meta(af.stem + " " + " ".join(parts[:-1][:2]), "",
                     _audio_themes(), True,
                     {"cat": folder.name.lower()}),
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

    # a registry left behind by ANOTHER library: refuse before writing
    dbp = args.db or config.DB_PATH
    check = not args.dry_run and config.is_configured()
    if check:
        from asset_service import db as _db
        owner = _db.registry_foreign(dbp, config.LIBRARY_ROOT)
        if owner:
            print(_db.foreign_registry_message(dbp, owner,
                                               config.LIBRARY_ROOT))
            return 2
    print(f"scanning {folder} ...")
    summary = scan_folder(folder, dry_run=args.dry_run,
                          verbose=args.verbose, db_path=args.db)
    if check:
        # rows from a folder OUTSIDE the library root must not make this
        # registry read as foreign later: stamp it for this library now
        _db.stamp_registry(dbp, config.LIBRARY_ROOT)
        _db.touch_sources(dbp)
    mode = "DRY RUN — nothing written" if args.dry_run else "written to registry"
    print(f"\n{mode}")
    print(f"  meshes:        {summary['meshes']}")
    print(f"  texture sets:  {summary['texture_sets']}")
    print(f"  audio:         {summary['audio']}")
    print(f"  manifests:     {summary['manifests_read']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
