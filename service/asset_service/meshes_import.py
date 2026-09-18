"""Import the geometry layer (meshes) into the `meshes` table.

Sources (under `<library_root>/_Agent_Files`, produced by the crawler):
  - models.jsonl        converted pack meshes (fbx path, triangles,
                        vertices, bbox_m, materials, texture_files)
  - kb3d_models.jsonl   KitBash3D group assemblies (submeshes, bbox_m,
                        triangles)
  - native_models.jsonl native FBX/OBJ/blend containers; `container` +
                        `collection` describe where inside the source
                        file each object lives.

This is THE layer agents need for scene building: "find me a door around
2.1 m" becomes a query, not a guess. Dual-audience tagging as everywhere:
`tags` human words + `meta` {stems, themes, facets}.

Per-mesh material `recipe` + placement bounds (2026-09-16) are joined in from
the per-pack export manifests (`Exports/manifest.json` and
`Exports/kit_manifest.json` under every directory listed in
`manifest_roots` in pharos_config.json) -- never derived from the flat
`texture_files` list. That filename guessing produced a wrong recipe for every
mesh of a pack (AlbertMansion: all 8 first meshes pointed at TX_Debris_01a),
and a wrong recipe is worse than no recipe because an agent will trust it.
A mesh with no manifest entry, or a material chain that exposes no texture
parameters, gets `resolved: false` and an empty `primary`: absent, not wrong.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config

AGENT_FILES = config.AGENT_FILES
MODELS = AGENT_FILES / "models.jsonl"
KB3D = AGENT_FILES / "kb3d_models.jsonl"
NATIVE = AGENT_FILES / "native_models.jsonl"
SOURCES = (MODELS, KB3D, NATIVE)

MANIFEST_ROOTS = config.MANIFEST_ROOTS
LEARTES_SCHEMA = "pharos.pack.export/v2"
LEARTES_SCHEMA_LEGACY = "kiosk.pack.export/v2"   # pre-rename manifests
KB3D_SCHEMA = "pharos.kb3d.export/v1"
KB3D_SCHEMA_LEGACY = "kiosk.kb3d.export/v1"      # pre-rename manifests

MESHES_DDL = """
CREATE TABLE IF NOT EXISTS meshes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pack TEXT,
  source TEXT,
  kind TEXT,
  fbx TEXT,
  on_disk INTEGER,
  bytes INTEGER,
  triangles INTEGER,
  vertices INTEGER,
  submeshes INTEGER,
  bbox_x REAL, bbox_y REAL, bbox_z REAL,
  max_dim REAL,
  materials TEXT,
  texture_count INTEGER,
  texture_files TEXT,
  tags TEXT,
  meta TEXT,
  -- 2026-09-16 additions: real wiring + world-space placement corners.
  -- bbox_x/y/z (size) semantics are unchanged; bbox_min/max live beside them.
  recipe TEXT,
  bbox_min_x REAL, bbox_min_y REAL, bbox_min_z REAL,
  bbox_max_x REAL, bbox_max_y REAL, bbox_max_z REAL
);
CREATE INDEX IF NOT EXISTS idx_meshes_pack ON meshes(pack);
CREATE INDEX IF NOT EXISTS idx_meshes_maxdim ON meshes(max_dim);
-- scanner.py owns rows with source='scan'; one per (name, pack) so
-- re-scanning a folder replaces instead of duplicating
CREATE UNIQUE INDEX IF NOT EXISTS idx_meshes_name_pack_scan
  ON meshes(name, pack) WHERE source='scan';
-- per-pack material-wiring health, straight from the export manifests
CREATE TABLE IF NOT EXISTS mesh_pack_wiring (
  pack TEXT PRIMARY KEY,
  source TEXT,
  slots_total INTEGER,
  slots_resolved INTEGER,
  wiring_ratio REAL,
  method TEXT
);
"""

# Created by CREATE TABLE IF NOT EXISTS for a fresh registry; added by ALTER
# TABLE for a registry that already carries an older `meshes` table (the
# importer owns this table, so it migrates it in place rather than dropping).
MESH_EXTRA_COLUMNS = (
    ("recipe", "TEXT"),
    ("bbox_min_x", "REAL"), ("bbox_min_y", "REAL"), ("bbox_min_z", "REAL"),
    ("bbox_max_x", "REAL"), ("bbox_max_y", "REAL"), ("bbox_max_z", "REAL"),
)

# Where a texture binding came from, best first. An instance_override is the
# artist's real per-mesh wiring; material_default is the master material's
# fallback (TX_Fill_01a / TX_Stains_01a, identical across a whole pack) and
# must never win the `primary` pick.
SOURCE_RANK = ("instance_override", "material_used_textures",
               "material_default")

# KitBash3D kits ship no per-material texture table, only a flat
# `texture_files` list whose basenames are `<material>_<suffix>`. The suffix
# names the channel, so the join stays factual (prefix must equal the material
# name) instead of guessing from keywords.
KB3D_ROLE_SUFFIX = {
    "basecolor": "albedo", "albedo": "albedo", "diffuse": "albedo",
    "normal": "normal", "nrm": "normal",
    "roughness": "roughness", "rough": "roughness",
    "metallic": "metallic", "metalness": "metallic",
    "height": "height", "displacement": "height",
    "ao": "ao", "occlusion": "ao",
    "emissive": "emissive", "emission": "emissive",
    "opacity": "opacity", "alpha": "opacity",
    "specular": "specular", "refraction": "refraction",
}

# Unreal name-prefix semantics (SM_ = static mesh etc.) kept as facets
MESH_THEMES = {
    "building": ["bldg", "building", "house", "tower", "skyscraper",
                 "highrise", "facade", "shop", "storefront"],
    "door": ["door", "gate", "entrance"],
    "window": ["window", "wnd"],
    "wall": ["wall", "fence", "barrier"],
    "roof": ["roof", "rooftop", "shingle"],
    "street": ["street", "road", "sidewalk", "pavement", "curb", "asphalt"],
    "prop": ["prop", "barrel", "crate", "box", "sign", "lamp", "post",
             "bench", "trash", "hydrant", "bin"],
    "vehicle": ["car", "truck", "vehicle", "bus", "van"],
    "nature": ["tree", "bush", "plant", "rock", "grass", "foliage",
               "branch", "log"],
    "furniture": ["chair", "table", "sofa", "bed", "cabinet", "shelf",
                  "desk", "stool"],
    "stairs": ["stair", "step", "ladder", "ramp"],
    "light": ["lamp", "light", "lantern", "neon", "sign"],
    "structure": ["beam", "pillar", "column", "pipe", "duct", "frame",
                  "platform", "scaffold", "bridge"],
    "destruction": ["debris", "rubble", "broken", "destroyed", "wreck",
                    "ruin", "boarded"],
    "scifi": ["scifi", "sci", "tech", "panel", "hologram", "drone"],
    "military": ["military", "sandbag", "bunker", "barrier"],
    "water": ["water", "fountain", "pond", "river"],
    "interior": ["interior", "floor", "ceiling", "room", "basement",
                 "stairwell"],
    "medieval": ["medieval", "castle", "keep", "fort", "tower", "rampart",
                 "drawbridge", "market", "stall", "cart", "torch",
                 "lantern", "banner", "hall", "guild"],
    "fantasy": ["fantasy", "magic", "magical", "rune", "ritual", "shrine",
                "temple", "statue", "gargoyle", "mythic", "arcane",
                "crystal", "altar", "totem", "wizard", "dragon"],
    "ornate": ["ornate", "carved", "pillar", "column", "arch", "archway",
               "balcony", "molding", "facade"],
    "creature": ["creature", "monster", "beast", "animal", "dragon",
                 "shark", "wolf", "bird"],
}

# pack-identity backfill: packs whose whole identity implies themes the
# per-name matcher may miss (2026-09-15 dry-run finding: 34% themeless)
PACK_THEMES = {
    "medieval_village": ["medieval"],
    "dark medieval environment megapack unreal engine": ["medieval",
                                                         "fantasy"],
    "medieval_rus_village": ["medieval"],
    "eastern_province": ["medieval", "ornate"],
    "chinesealley": ["ornate"],
    "demonic_village": ["fantasy", "horror"],
    "ancients": ["medieval", "ornate"],
    "hauntedprison_5.0": ["horror"],
    # (a pack whose name already contains a theme keyword needs no
    # PACK_THEMES backfill -- the per-name matcher finds it)
    "scifitrainfacility": ["scifi"],
    "scifi_industrial": ["scifi"],
    "cyberpunk": ["scifi"],
    "cyberstreets": ["scifi"],
    "cyberdistrict": ["scifi"],
    "neocity": ["scifi"],
    "neoshanghai": ["scifi"],
    "goliath": ["scifi"],
    "elysium": ["scifi"],
    "warzone": ["military", "destruction"],
    "ww2warzone": ["military", "destruction"],
}

# filled by import_meshes() so a caller (or a test) can see the join health
LAST_IMPORT_STATS: dict = {}


# ---------------------------------------------------------------------------
# material recipes, joined from the export manifests
# ---------------------------------------------------------------------------

def _abs_file(exports_dir: Path, rel: str) -> str:
    """Manifest `file` values are relative to the pack's Exports folder."""
    p = Path(rel)
    if not p.is_absolute():
        p = exports_dir / rel
    return os.path.normpath(str(p))


def _src_rank(source: str) -> int:
    for i, prefix in enumerate(SOURCE_RANK):
        if (source or "").startswith(prefix):
            return i
    return len(SOURCE_RANK)


def _leartes_recipe(mesh: dict, exports_dir: Path) -> dict:
    """Per-slot wiring exactly as the UE exporter resolved it."""
    slots = []
    for sl in (mesh.get("materials") or []):
        maps = []
        for t in (sl.get("textures") or []):
            rel = t.get("file")
            if not rel:
                continue          # param exists but has no texture assigned
            maps.append({
                "role": (t.get("role") or "other"),
                "param": (t.get("param") or "").strip() or None,
                "file": _abs_file(exports_dir, rel),
                "channels": t.get("channels") or None,
                "_rank": _src_rank(t.get("source")),
            })
        slots.append({"slot": sl.get("slot"), "material": sl.get("name"),
                      "base": sl.get("base"), "maps": maps})
    return {"slots": slots}


def _kb3d_recipe(group: dict, exports_dir: Path) -> dict:
    """KitBash3D groups: attribute textures by `<material>_<suffix>` basename."""
    mats = group.get("materials") or []
    by_mat: dict = {m: [] for m in mats}
    for t in (group.get("texture_files") or []):
        base, _ext = os.path.splitext(os.path.basename(t))
        for m in mats:
            if base.startswith(m + "_"):
                suffix = base[len(m) + 1:].lower()
                if suffix in KB3D_ROLE_SUFFIX:
                    by_mat[m].append({"role": KB3D_ROLE_SUFFIX[suffix],
                                      "param": suffix,
                                      "file": _abs_file(exports_dir, t),
                                      "channels": None, "_rank": 0})
                break
    slots = [{"slot": i, "material": m, "base": m, "maps": by_mat[m]}
             for i, m in enumerate(mats)]
    return {"slots": slots}


def _finish_recipe(recipe: dict) -> dict:
    """Copy a cached recipe into its final shape and add the `primary` layer.

    Returns a fresh structure (the cached one stays untouched so repeated
    finishes -- same mesh reached by both join keys -- stay identical).
    `primary` comes from the FIRST slot that resolved *any* map, and within it
    the best-ranked binding per role wins, so a real instance override beats
    the master material's TX_Fill fallback.
    """
    slots = []
    primary: dict = {}
    primary_slot = None
    found = False
    for sl in recipe.get("slots") or []:
        best: dict = {}
        clean = []
        for m in sl.get("maps") or []:
            out = {k: v for k, v in m.items() if k != "_rank"}
            clean.append(out)
            role = out["role"]
            key = "packed" if role.startswith("packed") else role
            rank = m.get("_rank", len(SOURCE_RANK))
            if key not in best or rank < best[key][0]:
                best[key] = (rank, out)
        slots.append({"slot": sl.get("slot"), "material": sl.get("material"),
                      "base": sl.get("base"), "maps": clean})
        if best and not found:
            found = True
            primary_slot = sl.get("slot")
            for key, (_rank, out) in best.items():
                primary[key] = out["file"]
                if key == "packed":
                    primary["packed_channels"] = out.get("channels")
    maps = {k: v for k, v in primary.items() if k != "packed_channels"}
    # A slot the exporter marked `unresolved` still lists the textures the
    # material uses, but with no role ("other") and no param (e.g. a trim
    # material wired only through hardcoded masters). That is not a usable
    # recipe, so it must not count as resolved: `resolved:true` always
    # means at least one map has a real role, and never produces an empty
    # primary.
    named = [k for k in maps if k != "other"]
    return {"slots": slots, "primary": primary,
            "resolved": bool(named), "primary_slot": primary_slot}


def _iter_manifests():
    """Yield (pack, source, exports_dir, doc, usable) for a readable manifest.

    `usable` is False for a Leartes manifest that is not v2 yet: the v1 schema
    carries no per-material `role`, so it can supply no recipe and its meshes
    stay out of the join exactly as before. It is still YIELDED, because the
    pack does have a manifest file, and the pack-health label has to be able to
    say that -- reporting a present v1 manifest as "no-manifest" sends a reader
    hunting for a file that is sitting right there.
    """
    for root in MANIFEST_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("Exports/manifest.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pack = doc.get("pack") or path.parent.parent.name
            yield pack, "leartes", path.parent, doc, \
                doc.get("schema") in (LEARTES_SCHEMA, LEARTES_SCHEMA_LEGACY)
        for path in sorted(root.rglob("Exports/kit_manifest.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if doc.get("schema") not in (KB3D_SCHEMA, KB3D_SCHEMA_LEGACY):
                continue      # KitBash3D ships exactly one schema; not ours
            yield path.parent.parent.name, "kitbash3d", path.parent, doc, True


def _manifest_index() -> tuple:
    """Join tables from the manifests.

    Returns (by_asset_path, by_pack_name, raw, wiring, stats). `asset_path` is
    the strong key (Leartes); KitBash3D assemblies have none, so (pack, name)
    is the fallback. `raw` keeps the untouched entries for bbox corners.
    """
    by_ap: dict = {}
    by_pn: dict = {}
    raw: dict = {}
    wiring: dict = {}
    stats = {"packs": 0, "entries": 0, "packs_failed": []}
    for pack, source, exports_dir, doc, usable in _iter_manifests():
        stats["packs"] += 1
        if source == "leartes":
            if usable:
                for mesh in doc.get("meshes") or []:
                    if not mesh.get("name"):
                        continue
                    stats["entries"] += 1
                    raw[(pack, mesh["name"])] = mesh
                    recipe = _leartes_recipe(mesh, exports_dir)
                    if mesh.get("asset_path"):
                        raw[mesh["asset_path"]] = mesh
                        by_ap[mesh["asset_path"]] = recipe
                    by_pn[(pack, mesh["name"])] = recipe
            # The pack-health label is driven by the manifest's OWN wiring
            # block, not by the schema tag: manifest present with a block ->
            # whatever method the engine recorded ("ue-param"); manifest
            # present without one -> "v1-manifest" (the file is real, there is
            # simply no wiring in it to report). Only a pack with no manifest
            # file at all may reach "no-manifest" (see import_meshes()).
            w = doc.get("wiring") or {}
            total = w.get("slots_total") or 0
            resolved = w.get("slots_resolved") or 0
            wiring[pack] = {"source": source, "slots_total": total,
                            "slots_resolved": resolved,
                            "wiring_ratio": (round(resolved / total, 4)
                                             if total else None),
                            "method": (w.get("method") or "ue-param") if w
                                      else "v1-manifest"}
        else:
            for group in doc.get("groups") or []:
                if not group.get("group"):
                    continue
                stats["entries"] += 1
                raw[(pack, group["group"])] = group
                by_pn[(pack, group["group"])] = _kb3d_recipe(group,
                                                             exports_dir)
            # no exporter-side wiring block: the counts are the recipe's own
            total = resolved = 0
            for (p, _n), recipe in by_pn.items():
                if p != pack:
                    continue
                for sl in recipe["slots"]:
                    total += 1
                    if sl["maps"]:
                        resolved += 1
            wiring[pack] = {"source": source, "slots_total": total,
                            "slots_resolved": resolved,
                            "wiring_ratio": (round(resolved / total, 4)
                                             if total else None),
                            "method": "basename"}
    return by_ap, by_pn, raw, wiring, stats


EMPTY_RECIPE = {"slots": [], "primary": {}, "resolved": False,
                "primary_slot": None}


def _recipe_for(e: dict, by_ap: dict, by_pn: dict) -> dict:
    """Join one JSONL mesh record to its manifest entry. No fallbacks."""
    ap = e.get("asset_path")
    recipe = by_ap.get(ap) if ap else None
    if recipe is None:
        recipe = by_pn.get((e.get("pack"), e.get("name")))
    if recipe is None:
        return dict(EMPTY_RECIPE)
    return _finish_recipe(recipe)


def _bbox_pair(e: dict, raw_index: dict) -> tuple:
    """World-space placement corners (metres).

    A manifest entry wins -- it carries the exporter's own box. A native record
    with no manifest (CGTrader, TurboSquid, ...) is not left empty: the Blender
    import pass measures the world-space corners itself and writes them as
    `bbox_min_m` / `bbox_max_m`, so those records report placement corners
    instead of None. Records predating that field still return (None, None),
    exactly as before.
    """
    entry = raw_index.get((e.get("pack"), e.get("name")))
    if entry is None and e.get("asset_path"):
        entry = raw_index.get(e["asset_path"])
    if entry is None:
        return e.get("bbox_min_m"), e.get("bbox_max_m")
    lo = entry.get("bbox_min") or entry.get("bbox_min_m")
    hi = entry.get("bbox_max") or entry.get("bbox_max_m")
    return lo, hi


# ---------------------------------------------------------------------------
# record shaping (unchanged semantics)
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", w).lower().split()
        out.extend(parts)
    return list(dict.fromkeys(w for w in out if len(w) > 2))


def _stems(text: str) -> list[str]:
    stems = []
    for w in _words(text):
        stems.append(w)
        for suf, cut in (("ing", 3), ("ers", 3), ("es", 2), ("s", 1)):
            if w.endswith(suf) and len(w) > len(suf) + 2:
                stems.append(w[:-cut])
                break
    return list(dict.fromkeys(stems))


def _match_themes(text: str) -> list[str]:
    low = " " + text.lower() + " "
    hits = []
    for theme, kws in MESH_THEMES.items():
        found = [k for k in kws if k in low]
        if found:
            hits.append(theme)
    return hits


def _pretty(stem: str) -> str:
    s = re.sub(r"^(SM_|SK_|S_|KB3D_[A-Z]{3}_)", "", stem)
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ").split()
    out = " ".join(p for p in parts if p)
    return out[:1].upper() + out[1:] if out else stem


def _record(e: dict, recipe: dict, bbox_min, bbox_max) -> dict:
    name = e.get("name") or ""
    fbx = e.get("fbx") or ""
    bbox = e.get("bbox_m") or [0, 0, 0]
    # dimension sanity: >500 m = skydome/groundplane scale poison
    # (HauntedPrison 4000 m) -- null dims and flag instead of sorting them
    suspect = ((e.get("max_dim_m") or 0) > 500
               or (max(bbox) if bbox else 0) > 500)
    if suspect:
        bbox = [0, 0, 0]
    mats = e.get("materials")
    if isinstance(mats, list):
        mat_json = json.dumps(mats, ensure_ascii=False)
        mat_count = len(mats)
    else:
        mat_json = json.dumps([], ensure_ascii=False)
        mat_count = int(mats or 0)
    texs = e.get("texture_files") or []
    pack = e.get("pack") or ""
    src = e.get("source") or ("leartes" if fbx and "Leartes" in fbx
                              else "kitbash3d" if fbx and "Kitbash" in fbx
                              else "")
    pretty = _pretty(name)
    words = _words(pretty)
    themes = _match_themes(pretty + " " + pack)
    themes = list(dict.fromkeys(
        themes + PACK_THEMES.get(pack.lower(), [])))
    stems = list(dict.fromkeys(_stems(pretty) + _stems(pack)))
    facets = {"pack": re.sub(r"[^a-z0-9]+", "-", pack.lower()),
              "source": src,
              "kind": re.sub(r"[^a-z0-9]+", "-",
                             (e.get("kind") or "").lower())}
    if suspect:
        facets["dim_suspect"] = True
    meta = json.dumps({"stems": stems, "themes": themes, "facets": facets},
                      ensure_ascii=False)
    tags = json.dumps(list(dict.fromkeys(words + themes)),
                      ensure_ascii=False)
    # poisoned dims carry no trustworthy box, so the corners are withheld too
    if suspect:
        bbox_min, bbox_max = None, None
    return {
        "name": pretty, "pack": pack, "source": src,
        "kind": e.get("kind") or "", "fbx": fbx,
        "on_disk": 1 if e.get("exists") else 0,
        "bytes": e.get("bytes") or 0,
        "triangles": e.get("triangles") or 0,
        "vertices": e.get("vertices") or 0,
        "submeshes": e.get("submeshes") or 0,
        "bbox_x": bbox[0] if len(bbox) > 0 else 0,
        "bbox_y": bbox[1] if len(bbox) > 1 else 0,
        "bbox_z": bbox[2] if len(bbox) > 2 else 0,
        "max_dim": 0 if suspect else
                   (e.get("max_dim_m") or (max(bbox) if bbox else 0)),
        "materials": mat_json, "mat_count": mat_count,
        "texture_count": len(texs),
        "texture_files": json.dumps(texs, ensure_ascii=False),
        "tags": tags, "meta": meta,
        "raw_name": name,
        "recipe": json.dumps(recipe, ensure_ascii=False,
                             separators=(",", ":")),
        "bbox_min_x": bbox_min[0] if bbox_min else None,
        "bbox_min_y": bbox_min[1] if bbox_min and len(bbox_min) > 1 else None,
        "bbox_min_z": bbox_min[2] if bbox_min and len(bbox_min) > 2 else None,
        "bbox_max_x": bbox_max[0] if bbox_max else None,
        "bbox_max_y": bbox_max[1] if bbox_max and len(bbox_max) > 1 else None,
        "bbox_max_z": bbox_max[2] if bbox_max and len(bbox_max) > 2 else None,
    }


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
    for col, typ in MESH_EXTRA_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE meshes ADD COLUMN {col} {typ}")


INSERT_SQL = (
    "INSERT INTO meshes (name,pack,source,kind,fbx,on_disk,"
    "bytes,triangles,vertices,submeshes,bbox_x,bbox_y,"
    "bbox_z,max_dim,materials,texture_count,texture_files,"
    "tags,meta,recipe,bbox_min_x,bbox_min_y,bbox_min_z,"
    "bbox_max_x,bbox_max_y,bbox_max_z) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def import_meshes(db_path: str | Path) -> int:
    db_path = Path(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(MESHES_DDL)
        _migrate(conn)

        by_ap, by_pn, raw_index, wiring, mstats = _manifest_index()

        # scanner-indexed rows (source='scan') are owned by scanner.py, not
        # by the crawler JSONLs -- this rebuild must preserve them (a fresh
        # install without crawler output has NOTHING else in this table).
        # 'id' is EXCLUDED: crawler inserts restart auto-increment at 1, so
        # re-inserting old ids collides fatally (laptop-run finding).
        scan_cols = [d[0] for d in conn.execute(
            "SELECT * FROM meshes WHERE source='scan' LIMIT 0").description
            if d[0] != "id"]
        scan_rows = [tuple(r) for r in conn.execute(
            "SELECT " + ",".join(scan_cols) +
            " FROM meshes WHERE source='scan'")]

        conn.execute("DELETE FROM meshes")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='meshes'")
        conn.execute("DELETE FROM mesh_pack_wiring")

        n = 0
        joined = 0
        resolved = 0
        per_pack_joined: dict = {}
        for src in SOURCES:
            if not src.is_file():
                continue
            with open(src, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    recipe = _recipe_for(e, by_ap, by_pn)
                    if recipe["slots"]:
                        joined += 1
                        per_pack_joined[e.get("pack")] = \
                            per_pack_joined.get(e.get("pack"), 0) + 1
                    if recipe["resolved"]:
                        resolved += 1
                    if not recipe["resolved"]:
                        recipe = dict(EMPTY_RECIPE)   # never ship a half recipe
                    lo, hi = _bbox_pair(e, raw_index)
                    r = _record(e, recipe, lo, hi)
                    raw = r.pop("raw_name")
                    conn.execute(INSERT_SQL, (
                        r["name"], r["pack"], r["source"], r["kind"], r["fbx"],
                        r["on_disk"], r["bytes"], r["triangles"], r["vertices"],
                        r["submeshes"], r["bbox_x"], r["bbox_y"], r["bbox_z"],
                        r["max_dim"], r["materials"], r["texture_count"],
                        r["texture_files"], r["tags"], r["meta"], r["recipe"],
                        r["bbox_min_x"], r["bbox_min_y"], r["bbox_min_z"],
                        r["bbox_max_x"], r["bbox_max_y"], r["bbox_max_z"]))
                    n += 1

        if scan_rows:
            conn.executemany(
                f"INSERT INTO meshes ({','.join(scan_cols)}) "
                f"VALUES ({','.join('?' for _ in scan_cols)})", scan_rows)
            n += len(scan_rows)

        # A pack that owns mesh rows but no manifest FILE at all (CGTrader, ...)
        # still gets a row, with a ratio of None: no wiring exists to report. This
        # is the only path that may label a pack "no-manifest" -- a pack whose
        # manifest is present but pre-relink was labelled "v1-manifest" above.
        have_rows = {r[0] for r in conn.execute("SELECT DISTINCT pack FROM meshes")}
        for pack in have_rows:
            info = wiring.get(pack)
            if info is None:
                info = {"source": None, "slots_total": 0, "slots_resolved": 0,
                        "wiring_ratio": None, "method": "no-manifest"}
            conn.execute(
                "INSERT OR REPLACE INTO mesh_pack_wiring (pack,source,"
                "slots_total,slots_resolved,wiring_ratio,method) "
                "VALUES (?,?,?,?,?,?)",
                (pack, info["source"], info["slots_total"],
                 info["slots_resolved"], info["wiring_ratio"], info["method"]))

        conn.commit()
    finally:
        conn.close()

    entries = mstats["entries"]
    LAST_IMPORT_STATS.clear()
    LAST_IMPORT_STATS.update({
        "rows": n, "manifest_entries": entries,
        "manifest_packs": mstats["packs"],
        "joined": joined, "resolved": resolved,
        "join_rate_of_manifest": (round(joined / entries, 4)
                                  if entries else None),
        "join_rate_of_rows": round(joined / n, 4) if n else None,
        "per_pack_joined": per_pack_joined,
    })
    print(f"meshes import: {n} rows | recipe joined {joined} "
          f"({(100.0 * joined / entries if entries else 0):.1f}% of "
          f"{entries} manifest entries, "
          f"{(100.0 * joined / n if n else 0):.1f}% of rows) | "
          f"resolved {resolved} | {mstats['packs']} manifest packs",
          flush=True)
    return n


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    print(f"meshes imported: {import_meshes(db)} records")
    print(json.dumps(LAST_IMPORT_STATS, indent=2, ensure_ascii=False)[:1200])
