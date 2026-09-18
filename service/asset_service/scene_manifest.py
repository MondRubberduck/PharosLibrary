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
      "fbx": "D:/path/to/tower.fbx",  # absolute path (from Pharos query)
      "position": [0.0, 0.0, 0.0],    # [x, y, z] in metres
      "rotation": [0.0, 0.0, 0.0],   # [rx, ry, rz] in radians
      "scale": 1.0,                   # uniform scale
      "snap_to_ground": true,         # place so bbox_min.y lands on ground_y
      "materials": {                  # optional; omit for placeholder
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
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_manifest(data: dict) -> list[str]:
    """Validate a scene manifest. Returns a list of error strings
    (empty = valid). Does NOT check file existence (use check_paths)."""
    errors = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]

    # unknown top-level keys -> warning (D09)
    KNOWN_KEYS = {"schema", "scene", "units", "up_axis", "ground_y",
                  "assets", "textures", "crowd", "audio"}
    unknown = set(data.keys()) - KNOWN_KEYS
    if unknown:
        errors.append(f"unknown top-level key(s) ignored: "
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
        if not a.get("fbx"):
            errors.append(f"{label}: missing required field 'fbx'")
            continue
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


def check_paths(data: dict) -> list[str]:
    """Check that all referenced files exist on disk.
    Returns a list of missing-file errors."""
    missing = []
    for i, a in enumerate(data.get("assets", [])):
        fbx = a.get("fbx", "")
        if fbx and not Path(fbx).is_file():
            missing.append(f"assets[{i}] ('{a.get('id', '?')}'): FBX not found: {fbx}")
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
            row = conn.execute(
                "SELECT triangles FROM meshes WHERE fbx = ?",
                (os.path.normpath(fbx),)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT triangles FROM meshes WHERE"
                    " REPLACE(fbx, '\\', '/') = ?",
                    (fbx.replace("\\", "/"),)).fetchone()
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
    """Load + validate a manifest file. Returns (manifest, errors)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return None, [f"cannot read manifest: {exc}"]
    errors = validate_manifest(data)
    if not errors:
        errors = check_paths(data)
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
