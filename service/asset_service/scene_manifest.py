"""Pharos Scene Manifest — schema, validation, and budget checking.

The manifest is the contract between an agent's scene plan and the
Blender builder. It's a JSON file the agent writes after querying
Pharos; the builder reads it and constructs the scene.

Schema (pharos.scene/v1; kiosk.scene/v1 accepted):

{
  "scene": "My Square",              # human-readable name
  "units": "meters",                  # enforced
  "up_axis": "Y",                     # enforced; rotations are Y-up euler
                                     # [pitch, yaw, roll] -- the builder
                                     # converts to Blender's Z-up space
  "ground_y": 0.0,                    # ground plane height for snapping
  "assets": [
    {
      "id": "hero_tower",             # unique within the scene
      "key": "Pack/Exports/FBX/SM_Tower.fbx",
                                      # the `key` from a Pharos search: the
                                      # file (and, after '#', the object
                                      # inside a multi-object file) is
                                      # looked up for you -- OR give:
      "fbx": "D:/path/to/tower.fbx",  # absolute path of the model file:
                                      # .fbx, .obj, .glb/.gltf or .blend
      "object": "Tower_A",            # optional: the ONE object to take
                                      # from a multi-object file (the
                                      # search item's object_name); the
                                      # rest of the file is dropped
      "position": [0.0, 0.0, 0.0],    # [x, y, z] in metres
      "rotation": [0.0, 0.0, 0.0],   # [rx, ry, rz] in radians
      "scale": 1.0,                   # uniform scale
      "snap_to_ground": true,         # place so bbox_min.y lands on ground_y
      "materials": "recipe",          # optional, with "key": the
                                      # model's own per-slot recipe from
                                      # the registry -- OR a recipe object
                                      # ({"slots": [...]} as served by
                                      # /api/meshes/item) -- OR hero maps:
      "materials": {
        "albedo": "D:/path/to/ALB.png",
        "normal": "D:/path/to/NRM.png",
        "roughness": "D:/path/to/rough.png",
        "metallic": "D:/path/to/metal.png",
        "packed": "D:/path/to/ORM.png",
        "packed_channels": {"r": "ao", "g": "roughness", "b": "metallic"}
      }
    }
  ],
  "textures": [
    {
      "set": "Pavement cobblestonemedieval12",
      "apply_to": "hero_building",     # asset id, "ground", or a list
                                      # mixing both (["ground","hero"])
      "folder": "D:/path/to/texture/set/"
    }
  ],
  "crowd": [
    {
      "body_fbx": "D:/path/to/Render_Dummy.fbx",
      "animation_fbx": "D:/path/to/walk_cycle.fbx",
      "position": [5.0, 0.0, 3.0],
      "count": 12,
      "spacing": 2.0,                # metres between instances
      "animation_offset": 0.0        # seconds (stagger start times)
    }
  ],
  "audio": [
    {"file": "D:/path/to/ambience.wav", "loop": true}
  ]
}
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

try:
    from . import imgmeta
except ImportError:         # run as a script, or imported inside Blender
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import imgmeta

# the model files the builder can import (scene_builder._import_file)
SUPPORTED = (".fbx", ".obj", ".glb", ".gltf", ".blend")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_manifest(data: dict) -> list[str]:
    """Validate a scene manifest. Returns a list of error strings
    (empty = valid). Does NOT check file existence (use check_paths)."""
    errors = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]

    # unknown top-level keys are INFORMATIONAL, not errors: a cosmetic
    # unknown key ("notes") once failed validation outright, and (worse)
    # suppressed the file-existence checks via the old early-return
    KNOWN_KEYS = {"schema", "scene", "units", "up_axis", "ground_y",
                  "assets", "textures", "crowd", "audio"}
    unknown = set(data.keys()) - KNOWN_KEYS
    if unknown:
        print(f"note: unknown top-level key(s) ignored: "
              f"{sorted(unknown)} (known: {sorted(KNOWN_KEYS)})")
    schema = data.get("schema")
    if schema is not None and schema not in (
            "pharos.scene/v1", "kiosk.scene/v1"):
        errors.append(f"unrecognized schema {schema!r} "
                      "(expected pharos.scene/v1)")

    if data.get("units", "meters") != "meters":
        errors.append("units must be 'meters' (Pharos exports are metre-based)")
    if data.get("up_axis", "Y") != "Y":
        errors.append("up_axis must be 'Y' (FBX exports are Y-up)")

    assets = data.get("assets", [])
    if not isinstance(assets, list):
        errors.append("assets must be a list")
        assets = []

    seen_ids = set()
    for i, a in enumerate(assets):
        label = f"assets[{i}]"
        if not a.get("fbx") and not a.get("key"):
            errors.append(f"{label}: needs 'key' (from a search) or 'fbx'")
            continue
        if a.get("fbx") and Path(a["fbx"]).suffix.lower() not in SUPPORTED:
            errors.append(f"{label}: cannot import "
                          f"'{Path(a['fbx']).suffix}' files (supported: "
                          f"{', '.join(SUPPORTED)})")
        mats = a.get("materials")
        if isinstance(mats, str) and (mats != "recipe" or not a.get("key")):
            errors.append(f"{label}.materials: the only text value is "
                          "\"recipe\", and it needs 'key'")
        aid = a.get("id") or f"unnamed_{i}"
        if aid in seen_ids:
            errors.append(f"{label}: duplicate id '{aid}'")
        seen_ids.add(aid)

        pos = a.get("position", [0, 0, 0])
        if not isinstance(pos, list) or len(pos) != 3:
            errors.append(f"{label}.position must be [x, y, z]")
        rot = a.get("rotation", [0, 0, 0])
        if not isinstance(rot, list) or len(rot) != 3:
            errors.append(f"{label}.rotation must be [rx, ry, rz]")
        sc = a.get("scale", 1.0)
        if not isinstance(sc, (int, float)) or sc <= 0:
            errors.append(f"{label}.scale must be a positive number")

    textures = data.get("textures", [])
    if not isinstance(textures, list):
        textures = []
    for i, t in enumerate(textures):
        if not t.get("folder"):
            errors.append(f"textures[{i}]: missing required field 'folder'")
        if not t.get("set"):
            errors.append(f"textures[{i}]: missing required field 'set'")

    crowd = data.get("crowd", [])
    if not isinstance(crowd, list):
        crowd = []
    for i, c in enumerate(crowd):
        if not c.get("body_fbx"):
            errors.append(f"crowd[{i}]: missing 'body_fbx'")
        if not c.get("animation_fbx"):
            errors.append(f"crowd[{i}]: missing 'animation_fbx'")
        if c.get("count", 1) < 1:
            errors.append(f"crowd[{i}]: count must be >= 1")

    return errors


def _registry_root() -> tuple:
    """(registry db path, library root) from the Pharos config."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from asset_service import config
    return (os.environ.get("PHAROS_DB") or str(config.DB_PATH),
            config.LIBRARY_ROOT)


def resolve_keys(data: dict, db_path: Optional[str] = None,
                 root=None) -> list[str]:
    """Turn every asset's search `key` into its file (`fbx`), the object
    inside a multi-object file (`object`) and, for materials "recipe",
    the registry's per-slot recipe. Changes `data` in place; returns
    errors. An agent copies the key from the search answer instead of
    building paths, and never pastes a recipe."""
    # every asset with a key: a record copied from the API carries key
    # AND fbx, and skipping those built the whole file for one object
    assets = [a for a in data.get("assets") or []
              if isinstance(a, dict) and a.get("key")]
    if not assets:
        return []
    if db_path is None or root is None:
        try:
            d, r = _registry_root()
        except Exception as exc:                         # noqa: BLE001
            return [f"cannot resolve keys: no Pharos config ({exc})"]
        db_path, root = db_path or d, root or r
    if not Path(db_path).is_file():
        return [f"cannot resolve keys: registry not found at {db_path}"]
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    errors = []
    base = Path(root)
    try:
        for i, a in enumerate(data.get("assets") or []):
            if a not in assets:
                continue
            key = a["key"].replace("\\", "/")
            # the whole key as a file first (a file name may hold '#'),
            # then '<file>#<object>' split at the LAST '#'; a bare file
            # key may name its object in the `object` field
            cands = [(key, a.get("object") or None)]
            if "#" in key:
                f, o = key.rsplit("#", 1)
                cands.append((f, o))
            in_file: list = []
            row = None
            for f, o in cands:
                path = Path(f) if Path(f).is_absolute() or ":" in f[:3] \
                    else base / f
                rows = conn.execute(
                    "SELECT id, name, object_name, recipe FROM meshes WHERE "
                    "lower(replace(fbx, '\\', '/')) = ?",
                    (path.as_posix().lower(),)).fetchall()
                if not rows:
                    continue
                in_file = [r["object_name"] or r["name"] for r in rows]
                if o is None and len(rows) == 1:
                    row = rows[0]
                elif o is not None:
                    row = next((r for r in rows if o in (
                        r["object_name"], r["name"])), None) or next(
                        (r for r in rows if o.lower() in (
                            (r["object_name"] or "").lower(),
                            (r["name"] or "").lower())), None)
                if row is not None:
                    a["fbx"] = path.as_posix()
                    if o is not None and not a.get("object"):
                        a["object"] = row["object_name"] or o
                    break
            if row is None:
                many = [len(conn.execute(
                    "SELECT 1 FROM meshes WHERE "
                    "lower(replace(fbx, '\\', '/')) = ?",
                    (((Path(f) if Path(f).is_absolute() or ":" in f[:3]
                       else base / f).as_posix().lower()),)).fetchall())
                    for f, _ in cands]
                wanted = a.get("object") or (
                    key.rsplit("#", 1)[1] if "#" in key else None)
                errors.append(
                    f"assets[{i}]: object {wanted!r} is not in that file; "
                    f"it holds: {', '.join(sorted(in_file)[:12])}"
                    + (" ..." if len(in_file) > 12 else "")
                    if wanted and in_file else
                    f"assets[{i}]: key names a file with {max(many)} objects: "
                    f"use the full key ('<file>#<object>') of the one you "
                    f"want: {a['key']}" if max(many) > 1 else
                    f"assets[{i}]: key not in the registry: {a['key']}")
                continue
            if a.get("materials") == "recipe":
                try:
                    rec = json.loads(row["recipe"] or "null") or {}
                except ValueError:
                    rec = {}
                if rec.get("slots"):
                    a["materials"] = {"slots": rec["slots"]}
                else:
                    a.pop("materials")       # no recipe: placeholder
                    print(f"note: assets[{i}] ({a.get('id', '?')}): the "
                          "registry has no material recipe for it; "
                          "placeholder materials")
    finally:
        conn.close()
    return errors


# ---------------------------------------------------------------------------
# texture sets and whole-file imports (pure; the check AND the builder use these)
# ---------------------------------------------------------------------------

_TEX_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")
_TEX_ROLES = ("albedo", "normal", "roughness", "metallic")


def texture_maps(tex: dict, db_path: Optional[str] = None) -> tuple:
    """({role: file}, source) for one textures[] entry.

    The builder used to guess roles from substrings of the file names: a
    set named '<name>_Color' / '<name>_NormalDX' / '<name>_NormalGL' got
    no colour map and the DirectX normal map. Now:
      "registry"  the set is indexed: the maps the texture search serves
                  (colour map found by the shared role reader, the OpenGL
                  normal of a DX/GL pair -- Blender reads GL). A set that
                  is one plain photo is that photo as the colour map. An
                  indexed set is NEVER guessed from other files: a photo
                  'Roof.jpg' once got its maps from three other sets
                  with 'roof' in their names.
      "names"     not indexed: the files whose name holds the set name,
                  read by the same role reader, all from ONE set (the
                  files that share a folder and a name prefix)
      "folder"    the set name matches no file: the largest such set in
                  the folder
    """
    folder = Path(tex.get("folder") or "")
    setname = tex.get("set") or ""
    try:
        if db_path is None:
            db_path = _registry_root()[0]
        if setname and Path(db_path).is_file():
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT meta, first_image FROM textures WHERE "
                    "lower(name) = ? AND "
                    "rtrim(lower(replace(folder, '\\', '/')), '/') = ?",
                    (setname.lower(),
                     folder.as_posix().lower().rstrip("/"))).fetchall()
            finally:
                conn.close()
            for meta, first in rows:
                maps = (json.loads(meta or "{}") or {}).get("maps") or {}
                found = {r: maps[r] for r in _TEX_ROLES
                         if maps.get(r) and Path(maps[r]).is_file()}
                if not maps and first and Path(first).is_file():
                    found = {"albedo": first}       # one plain photo
                return found, "registry"
    except Exception:                    # noqa: BLE001 -- no registry: names
        pass
    if not folder.is_dir():
        return {}, "names"
    files = [p for p in sorted(folder.rglob("*"))
             if p.is_file() and p.suffix.lower() in _TEX_EXT]
    source = "names"
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())       # noqa: E731
    if setname:
        # display names use spaces, file names underscores: compare on
        # letters and digits. A name that matches no file means the folder
        # IS the set (a silent skip would lose the ground plane).
        matched = [p for p in files if norm(setname) in norm(p.stem)]
        if matched:
            files = matched
        else:
            source = "folder"
    # one material = the files of one folder that share a name prefix.
    # Taking each role from whichever file came first mixed several sets.
    groups: dict = {}
    for p in files:
        role, _, prefix = imgmeta.role_parts(p.name)
        if role in _TEX_ROLES:
            groups.setdefault((str(p.parent), prefix), []).append((role, p))
    if not groups:
        return {}, source
    want = norm(setname)
    best = min(groups, key=lambda k: (
        norm(k[1]) != want,                 # the set's own name first
        "albedo" not in {r for r, _ in groups[k]},
        -len({r for r, _ in groups[k]}),    # then the most complete set
        len(k[1]), k))
    # shortest name first; of a DirectX/OpenGL normal pair the GL one
    is_dx = re.compile(r"normaldx$|(?:^|[ _.-])dx(?:[ _.-]|\d|$)")
    found = {}
    for role, p in sorted(groups[best], key=lambda rp: (
            bool(is_dx.search(rp[1].stem.lower())), len(rp[1].name),
            rp[1].name.lower())):
        found.setdefault(role, str(p))
    return found, source


def whole_file_notes(data: dict, db_path: Optional[str] = None) -> list[str]:
    """Warnings for assets that name a multi-object file WITHOUT an
    `object`: the builder then imports every object in it (one kit file
    holds hundreds, millions of triangles). Empty without a registry."""
    assets = [(i, a) for i, a in enumerate(data.get("assets") or [])
              if isinstance(a, dict) and a.get("fbx") and not a.get("object")]
    if not assets:
        return []
    try:
        if db_path is None:
            db_path = _registry_root()[0]
        if not Path(db_path).is_file():
            return []
        import sqlite3
        conn = sqlite3.connect(db_path)
    except Exception:                                    # noqa: BLE001
        return []
    notes = []
    try:
        for i, a in assets:
            n, tri = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(triangles), 0) FROM meshes "
                "WHERE lower(replace(fbx, '\\', '/')) = ?",
                (a["fbx"].replace("\\", "/").lower(),)).fetchone()
            if n > 1:
                notes.append(
                    f"assets[{i}] ('{a.get('id', '?')}'): "
                    f"{Path(a['fbx']).name} holds {n} objects "
                    f"({tri:,} triangles) and ALL of them will be imported, "
                    f"laid out as in the file (the file's origin goes to "
                    f"`position`) -- to place one, give its search key "
                    f"('<file>#<object>') or name it with \"object\"")
    except Exception:                                    # noqa: BLE001
        pass
    finally:
        conn.close()
    return notes


def check_paths(data: dict) -> list[str]:
    """Check that all referenced files exist on disk.
    Returns a list of missing-file errors."""
    missing = []
    for i, a in enumerate(data.get("assets", [])):
        fbx = a.get("fbx", "")
        if fbx and not Path(fbx).is_file():
            missing.append(f"assets[{i}] ('{a.get('id', '?')}'): file not found: {fbx}")
        mats = a.get("materials") or {}
        if isinstance(mats, dict) and isinstance(mats.get("slots"), list):
            # per-slot schema (recipe.slots[]): maps live under
            # slots[].maps[].file -- flat traversal crashed on the list
            for si, slot in enumerate(mats["slots"]):
                for mi, m in enumerate(slot.get("maps") or []):
                    mp = m.get("file")
                    if mp and not Path(mp).is_file():
                        missing.append(
                            f"assets[{i}].materials.slots[{si}].maps[{mi}]"
                            f" ({m.get('role', '?')}): not found: {mp}")
        elif isinstance(mats, dict):
            for role, path in mats.items():
                if role == "packed_channels":
                    continue
                if path and not Path(path).is_file():
                    missing.append(
                        f"assets[{i}].materials.{role}: not found: {path}")
    for i, t in enumerate(data.get("textures", [])):
        folder = t.get("folder", "")
        if folder and not Path(folder).is_dir():
            missing.append(f"textures[{i}] ('{t.get('set', '?')}'): folder not found: {folder}")

    for i, c in enumerate(data.get("crowd", [])):
        for field in ("body_fbx", "animation_fbx"):
            p = c.get(field, "")
            if p and not Path(p).is_file():
                missing.append(f"crowd[{i}].{field}: not found: {p}")
    for i, au in enumerate(data.get("audio", [])):
        p = au.get("file", "")
        if p and not Path(p).is_file():
            missing.append(f"audio[{i}]: not found: {p}")
    return missing


# ---------------------------------------------------------------------------
# material recipes (pure; the builder AND the registry importer use these)
# ---------------------------------------------------------------------------

def collapse_slot_maps(slot: dict) -> dict:
    """One map per role for a recipe slot, in the shape the builder's
    material code consumes: {role: file, ..., "packed": file,
    "packed_channels": {...}|None}.

    The registry's `primary` (meshes_import) is this same pick, so the
    served hero maps and the built materials cannot disagree:
      - a slot marked `resolved: false` gives {} (keep the FBX's material);
      - maps flagged `placeholder` are skipped;
      - packed: the first packed map WITH channels, else the first packed
        map; file and channels always come from that one map and channels
        are never invented. Role `mask` is never used as a packed ORM;
      - a map whose `source` is `default` (a master material's fallback,
        the same file under every material of a pack) is skipped when its
        role is emissive, opacity or height, or a role the chosen packed
        map names in its channels while not being a `default` itself: the
        slot's own ORM supplies roughness/metallic/ao, and the builder
        never reads that channel behind a direct map. Without such a
        packed map (none, channel-less, or a fallback too) the default
        keeps the role -- it is what the engine renders;
      - any other role: the FIRST remaining map wins (served maps are
        rank-sorted, best source first).
    Only the pick changes: the slot's `maps` are never altered.
    """
    if slot.get("resolved") is False:
        return {}
    maps = [m for m in slot.get("maps") or []
            if m.get("file") and not m.get("placeholder")]
    packed = None
    for m in maps:
        if (m.get("role") or "").startswith("packed") and (
                packed is None
                or (m.get("channels") and not packed.get("channels"))):
            packed = m
    # roles a `default` map may not take. The packed map is chosen first:
    # a hand-written manifest slot is not rank-sorted like a served one
    barred = {"emissive", "opacity", "height"}
    if packed is not None and packed.get("source") != "default":
        barred.update((packed.get("channels") or {}).values())
    out: dict = {}
    for m in maps:
        role = m.get("role") or "other"
        if role.startswith("packed") or (
                role in barred and m.get("source") == "default"):
            continue
        out.setdefault(role, m["file"])
    if packed is not None:
        out["packed"] = packed["file"]
        ch = packed.get("channels")
        out["packed_channels"] = dict(ch) if ch else None
    return out


# Packed-channel letter -> image output: r/g/b are the Separate Color
# node's outputs, `a` is the image texture node's own Alpha output.
PACKED_CHANNEL_SOCKETS = {"r": "Red", "g": "Green", "b": "Blue", "a": "Alpha"}


def packed_channel_sockets(channels) -> list:
    """[(socket, role), ...] for a packed map's `channels` {r|g|b|a: role}.
    Unknown letters are skipped (never read as Green); null gives []."""
    return [(PACKED_CHANNEL_SOCKETS[k], v) for k, v in (channels or {}).items()
            if k in PACKED_CHANNEL_SOCKETS]


def budget_check(data: dict, meshes_db: Optional[str] = None) -> dict:
    """Sum triangle budgets from the manifest assets.
    If meshes_db is provided, looks up actual triangle counts."""
    total_tri = 0
    per_asset = []
    conn = None
    if meshes_db and Path(meshes_db).is_file():
        import sqlite3
        conn = sqlite3.connect(meshes_db)
        conn.row_factory = sqlite3.Row

    for a in data.get("assets", []):
        fbx = a.get("fbx", "")
        tri = 0
        if conn:
            # manifests are authored with forward slashes, the registry may
            # store backslashes (or vice versa) -- normalise BOTH sides or
            # every lookup silently misses and the budget under-reports
            rows = conn.execute(
                "SELECT triangles, object_name, name FROM meshes WHERE"
                " lower(REPLACE(fbx, '\\', '/')) = ?",
                (fbx.replace("\\", "/").lower(),)).fetchall()
            obj = a.get("object")
            if obj:
                # one object taken from a multi-object file
                rows = [r for r in rows if obj in (r["object_name"],
                                                   r["name"])]
            # no object named: the builder imports the WHOLE file
            row = ({"triangles": sum(r["triangles"] or 0 for r in rows)}
                   if rows else None)
            if row:
                tri = row["triangles"] or 0
            else:
                print(f"  WARNING: budget lookup missed '{fbx}' -- not in "
                      "the registry; its triangles are NOT counted",
                      file=sys.stderr)
        scale = a.get("scale", 1.0)
        total_tri += int(tri * scale * scale)  # approximate scaled cost
        per_asset.append({"id": a.get("id", "?"), "triangles": tri,
                          "matched": tri > 0 or not conn})

    for c in data.get("crowd", []):
        count = c.get("count", 1)
        total_tri += 45000 * count  # CC body ~45k tri each (measured)
        per_asset.append({"id": f"crowd_{c.get('count', 1)}", "triangles": 45000 * count})

    if conn:
        conn.close()
    return {"total_triangles": total_tri, "per_asset": per_asset,
            "budget_note": "UE5 Nanite handles 10M+; Blender Eevee ~5M; "
                           "Cycles ~2M for interactive"}


def load_manifest(path: str | Path) -> tuple[Optional[dict], list[str]]:
    """Load + validate a manifest file. Returns (manifest, errors).

    Path problems are ALWAYS checked: an early schema error once
    suppressed the file-existence pass, so one cosmetic unknown key hid
    every missing asset."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, [f"cannot read manifest: {exc}"]
    errors = validate_manifest(data)
    errors += resolve_keys(data)
    errors += check_paths(data)
    return data if not errors else None, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Validate a scene manifest")
    parser.add_argument("manifest", help="path to scene manifest JSON")
    parser.add_argument("--budget", action="store_true",
                        help="also check triangle budget")
    parser.add_argument("--db", default=None,
                        help="registry DB for triangle lookups")
    args = parser.parse_args(argv)

    data, errors = load_manifest(args.manifest)
    if errors:
        print("INVALID:")
        for e in errors:
            print(f"  ERROR: {e}")
        return 1

    print("VALID [OK]")
    n_assets = len(data.get("assets", []))
    n_crowd = sum(c.get("count", 1) for c in data.get("crowd", []))
    print(f"  assets: {n_assets}")
    print(f"  crowd:  {n_crowd} instances")
    for note in whole_file_notes(data, args.db):
        print(f"  WARNING: {note}")
    for t in data.get("textures") or []:
        maps, source = texture_maps(t, args.db)
        if not maps:
            print(f"  WARNING: texture '{t.get('set', '?')}': no colour, "
                  f"normal, roughness or metallic map found ({source}): "
                  f"it will NOT be applied")
            continue
        print(f"  texture '{t.get('set', '?')}': "
              f"{', '.join(sorted(maps))} ({source})")
        if "albedo" not in maps:
            print("  WARNING: this texture set has no colour map: the "
                  "surface will be grey")
    if args.budget:
        if not args.db:
            import os
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from asset_service import config
            default_db = os.environ.get("PHAROS_DB", config.DB_PATH)
            if Path(default_db).is_file():
                args.db = default_db
            else:
                print("  WARNING: no --db and no default found; "
                      "triangles will be 0 (crowd only)")
        budget = budget_check(data, args.db)
        print(f"  triangles: {budget['total_triangles']:,}")
        print(f"  note: {budget['budget_note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
