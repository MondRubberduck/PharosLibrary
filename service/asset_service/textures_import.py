"""Import the texture library (Textures_Materials) into the `textures` table.

Groups:
  - "4K Textures Gumroad": leaf folders under 4K_Textures_Gumroad/<subcat>/<set>/
    (each set folder holds the map channels + a <name>_render.jpg preview)
  - "Misc": everything else -- direct subfolders (leaf-most folders with
    texture files, previewable or not) and loose files (in the root, or
    beside subfolders), grouped into families by stripping map-channel
    suffixes (X_albedo.tif + X_normal.tif + ... -> one card).

Dual-audience tagging like the collection: `tags` human words + themes,
`meta` = {stems, themes, facets} for the algorithm.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config

TEX_ROOT = config.TEX_ROOT
MAIN_NAME = "4K_Textures_Gumroad"
GROUP_MAIN = "4K Textures Gumroad"
GROUP_MISC = "Misc"

DISPLAY_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
FILE_EXT = DISPLAY_EXT | {".tif", ".tiff", ".bmp", ".tga", ".exr", ".hdr",
                          ".tx", ".sbsar"}

TEXTURES_DDL = """
CREATE TABLE IF NOT EXISTS textures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  grp TEXT,
  sub TEXT,
  source TEXT,
  folder TEXT,
  files TEXT,
  images TEXT,
  file_count INTEGER,
  bytes INTEGER,
  first_image TEXT,
  thumb TEXT,
  tags TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_textures_grp ON textures(grp);
"""


def ensure_scan_unique_index(conn: sqlite3.Connection) -> None:
    """The (name, folder) unique index that makes the scanner's
    INSERT OR REPLACE actually replace. Called by every writer (scanner
    + importer): without it OR REPLACE degenerates to a plain INSERT and
    every re-scan of the same folder duplicates its rows. Registries
    built before the index may already carry duplicates -- keep the
    oldest row per (name, folder), then create the index."""
    idx = ("CREATE UNIQUE INDEX IF NOT EXISTS idx_textures_name_folder_scan "
           "ON textures(name, folder) WHERE source='scan'")
    try:
        conn.execute(idx)
    except sqlite3.IntegrityError:
        conn.execute(
            "DELETE FROM textures WHERE source='scan' AND id NOT IN "
            "(SELECT MIN(id) FROM textures WHERE source='scan' "
            " GROUP BY name, folder)")
        conn.execute(idx)

# map-channel suffix tokens stripped when grouping loose files into families
CHANNEL_TOKENS = {
    "albedo", "normal", "ao", "occlusion", "roughness", "rough", "height",
    "metallic", "metalness", "displacement", "disp", "glossiness",
    "reflection", "bump", "specular", "spec", "color", "colour", "col",
    "basecolor", "diffuse", "alpha", "opacity", "mask", "masked", "overlay",
    "mirror", "nrm", "occ", "s", "m", "b", "seamless", "raw", "toplayer",
}

TEX_THEMES = {
    "walls": ["wall", "brick", "plaster", "stucco", "facade", "concrete",
              "cement", "masonry", "bunker"],
    "wood": ["wood", "planks", "plank", "timber", "parquet", "bark", "log",
             "shingles"],
    "metal": ["metal", "steel", "rust", "rusted", "iron", "copper", "bronze",
              "aluminium", "chrome", "galvanized"],
    "ground": ["ground", "dirt", "soil", "sand", "gravel", "desert",
               "terrain", "earth", "farmland", "petrified"],
    "nature": ["grass", "moss", "plant", "plants", "foliage", "leaves",
               "ivy", "hedge", "flower", "flowerbeds", "bark", "trees"],
    "fabric": ["fabric", "cloth", "leather", "textile", "silk", "wool",
               "carpet", "wicker", "blanket", "clothes", "jeans", "denim"],
    "stone": ["stone", "marble", "granite", "tiles", "tile", "cobblestone",
              "pavement", "slate", "herringbone"],
    "wet": ["water", "ice", "snow", "rain", "raindrops", "frost", "leaking"],
    "damage": ["destruction", "debris", "rubble", "damaged", "broken",
               "cracked", "bones", "scrapyard", "slum", "stain", "grunge",
               "smudges", "splatter"],
    "scifi": ["scifi", "sci", "panel", "futuristic"],
    "urban": ["road", "roads", "asphalt", "street", "streets", "manhole",
              "city", "buildings", "highrise", "roof", "roofing", "doors",
              "windows", "shops", "floors", "floor"],
    "sky": ["sky", "skies", "cloud", "clouds", "night", "skybox", "moon",
            "star"],
    "glass": ["glass", "window", "backlit", "broken"],
    "decals": ["decal", "decals", "overlay", "overlays", "poster", "posters",
               "label"],
}


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", w).lower().split()
        out.extend(parts)
    return list(dict.fromkeys(w for w in out if len(w) > 2))


def _stems(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", w).lower().split()
        out.extend(parts)
    stems = []
    for w in out:
        stems.append(w)
        for suf, cut in (("ing", 3), ("ers", 3), ("er", 2), ("es", 2), ("s", 1)):
            if w.endswith(suf) and len(w) > len(suf) + 2:
                stems.append(w[:-cut])
                break
    return list(dict.fromkeys(stems))


def _match_themes(text: str) -> list[str]:
    low = " " + text.lower() + " "
    hits = []
    for theme, kws in TEX_THEMES.items():
        found = [k for k in kws if k in low]
        if len(found) >= 2 or (len(found) == 1 and len(found[0]) >= 5):
            hits.append(theme)
    return hits


def _family_base(stem: str) -> str:
    """Strip map-channel / resolution suffixes: 'Wall_X_2x2_1K_albedo' ->
    'wall_x' (family key for loose-file grouping)."""
    base = stem.lower().replace("texturescom_", "")
    changed = True
    while changed:
        changed = False
        m = re.search(r"[_ ](\d+x\d+|\d+k)$", base)
        if m:
            base = base[: m.start()]
            changed = True
            continue
        m = re.search(r"[_ ]([a-z0-9]{1,12})$", base)
        if m and (m.group(1) in CHANNEL_TOKENS or re.fullmatch(r"\d+", m.group(1))):
            base = base[: m.start()]
            changed = True
    return base.strip("_ ") or stem.lower()


def _pretty(base: str) -> str:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", base).replace("_", " ").split()
    out = " ".join(p for p in parts if p)
    return out[:1].upper() + out[1:] if out else base


def _is_macos_meta(f: Path) -> bool:
    """macOS metadata, not an asset (scanner.py skips the same names)."""
    return f.name.startswith("._") or f.name == ".DS_Store"


def _walk_leaves(d: Path, own: list | None = None) -> list:
    """Collect leaf-most directories under d that contain texture files
    (any FILE_EXT, previewable or not -- HDR/EXR-only sets were dropped).
    A directory ABOVE such leaves that also holds texture files of its own
    is appended to `own`: its files become families (they were lost)."""
    leaves: list = []
    try:
        children = [c for c in d.iterdir() if c.is_dir()]
        has_files = any(f.suffix.lower() in FILE_EXT and not _is_macos_meta(f)
                        for f in d.iterdir() if f.is_file())
    except OSError:
        return leaves
    for c in children:
        leaves.extend(_walk_leaves(c, own))
    if has_files:
        if not leaves:
            leaves.append(d)
        elif own is not None:
            own.append(d)
    return leaves


def _record_from_dir(d: Path, grp: str, sub: str) -> dict | None:
    try:
        all_files = [f for f in d.iterdir()
                     if f.is_file() and not _is_macos_meta(f)]
    except OSError:
        return None
    if not all_files:
        return None
    images = sorted((f for f in all_files if f.suffix.lower() in DISPLAY_EXT),
                    key=lambda p: p.name.lower())
    # a set without a preview image is still a set (empty thumb, like
    # loose families): HDR/EXR panoramas were silently dropped here
    render = next((i for i in images
                   if i.stem.lower().endswith("render")
                   or i.stem.lower() == d.name.lower()), None)
    thumb = (render or images[0]).as_posix() if images else ""
    return {"name": _pretty(d.name), "grp": grp, "sub": sub,
            "folder": d.as_posix(),
            "files": json.dumps([f.as_posix() for f in all_files]),
            "images": json.dumps([i.as_posix() for i in images]),
            "file_count": len(all_files),
            "bytes": sum(f.stat().st_size for f in all_files if f.is_file()),
            "first_image": images[0].as_posix() if images else "",
            "thumb": thumb,
            "src_name": d.name}


def _families(d: Path, grp: str, sub: str) -> list:
    """A directory's OWN texture files as family records, grouped by
    stripping map-channel suffixes (X_albedo.tif + X_normal.tif -> one
    card). Used for loose root files and for files beside subfolders."""
    families: dict[str, dict] = {}
    try:
        loose = [f for f in d.iterdir()
                 if f.is_file() and not _is_macos_meta(f)]
    except OSError:
        loose = []
    for f in loose:
        if f.suffix.lower() not in FILE_EXT:
            continue
        base = _family_base(f.stem)
        fam = families.setdefault(base, {"files": [], "images": []})
        fam["files"].append(f)
        if f.suffix.lower() in DISPLAY_EXT:
            fam["images"].append(f)
    out = []
    for base, fam in sorted(families.items()):
        displayable = sorted(fam["images"], key=lambda p: p.name.lower())
        out.append({
            "name": _pretty(base), "grp": grp,
            "sub": sub, "folder": d.as_posix(),
            "files": json.dumps([f.as_posix() for f in fam["files"]]),
            "images": json.dumps([i.as_posix() for i in displayable]),
            "file_count": len(fam["files"]),
            "bytes": sum(f.stat().st_size for f in fam["files"]),
            "first_image": (displayable[0].as_posix()
                            if displayable else ""),
            "thumb": (displayable[0].as_posix() if displayable else ""),
            "src_name": base})
    return out


def import_textures(db_path: str | Path, root: Path = TEX_ROOT) -> int:
    db_path = Path(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(TEXTURES_DDL)
        # self-migrate: older tables have no source column
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(textures)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE textures ADD COLUMN source TEXT")
        ensure_scan_unique_index(conn)
        # no textures root -> this importer owns NOTHING; scanner rows
        # must survive a restart (retention invariant, meshes/audio-style)
        if not root.is_dir():
            kept = conn.execute("SELECT COUNT(*) FROM textures"
                                ).fetchone()[0]
            print(f"textures import: root absent ({root}) -- keeping "
                  f"{kept} scanner-indexed row(s)")
            return kept
        # scanner rows (source='scan') survive the rebuild; only this
        # importer's own rows (source NULL / 'crawl') are replaced
        scan_cols = [d[0] for d in conn.execute(
            "SELECT * FROM textures WHERE source='scan' LIMIT 0"
            ).description if d[0] != "id"]
        scan_rows = [tuple(r) for r in conn.execute(
            "SELECT " + ",".join(scan_cols) +
            " FROM textures WHERE source='scan'")]
        # keep-first dedupe on (name, folder): legacy buffers can still
        # hold pre-index duplicates, and re-inserting them would trip the
        # unique index created above
        if scan_rows:
            ni, fi = scan_cols.index("name"), scan_cols.index("folder")
            seen: set = set()
            unique_rows = []
            for r in scan_rows:
                key = (r[ni], r[fi])
                if key in seen:
                    continue
                seen.add(key)
                unique_rows.append(r)
            scan_rows = unique_rows
        conn.execute("DELETE FROM textures WHERE source IS NOT 'scan'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='textures'")

        # crawl-agent index: category descriptions/keywords + naming notes
        idx_cats: dict = {}
        try:
            idx = json.loads((root / "library_index.json").read_text(encoding="utf-8"))
            for k, info in (idx.get("categories") or {}).items():
                idx_cats[k.lower()] = {
                    "desc": info.get("description") or "",
                    "keywords": info.get("keywords") or [],
                }
        except (OSError, ValueError):
            pass

        RES_RE = re.compile(r"\b(8k|4k|2k|1k|512|1024|2048)\b", re.I)

        def _res(text: str) -> str:
            toks = set(re.split(r"[^a-z0-9]+", (text or "").lower()))
            m = toks & {"8k", "4k", "2k", "1k", "512", "1024", "2048"}
            if not m:
                return ""
            v = m.pop()
            return {"512": "512", "1024": "1K", "2048": "2K"}.get(v, v.upper())

        records: list[dict] = []
        main = root / MAIN_NAME
        if main.is_dir():
            for subcat in sorted(p for p in main.iterdir() if p.is_dir()):
                own: list = []
                for leaf in _walk_leaves(subcat, own):
                    r = _record_from_dir(leaf, GROUP_MAIN, subcat.name)
                    if r:
                        records.append(r)
                for d in own:
                    records.extend(_families(d, GROUP_MAIN, subcat.name))

        if root.is_dir():
            # Misc subfolders (leaf-most dirs with texture files)
            for child in sorted(p for p in root.iterdir() if p.is_dir()):
                if child.name == MAIN_NAME:
                    continue
                own = []
                for leaf in _walk_leaves(child, own):
                    r = _record_from_dir(leaf, GROUP_MISC, child.name)
                    if r:
                        records.append(r)
                for d in own:
                    records.extend(_families(d, GROUP_MISC, child.name))
            # loose files in root -> families
            records.extend(_families(root, GROUP_MISC, "Loose Files"))

        for r in records:
            src = r.pop("src_name")
            words = _words(r["name"])
            themes = _match_themes(src + " " + r["sub"])
            stems = list(dict.fromkeys(_stems(r["name"]) + _stems(r["sub"])))
            # crawl-index enrichment: match my sub to index categories by token
            sub_norm = (r["sub"].lower()
                        .replace("4k_physical_", "").replace("_4k", "")
                        .replace("_", " ").strip())
            sub_toks = {t.rstrip("s") for t in sub_norm.split() if len(t) > 3}
            icat = None
            for k, v in idx_cats.items():
                ktoks = {t.rstrip("s") for t in k.lower().split("_")}
                if sub_toks & ktoks:
                    icat = v
                    break
            desc, kw = "", []
            if icat:
                desc, kw = icat.get("desc") or icat.get("description") or "",                 icat.get("keywords") or []
                stems = list(dict.fromkeys(stems + _stems(" ".join(kw))))
                themes = list(dict.fromkeys(
                    themes + [k for k in kw if k in
                              {t for cl in TEX_THEMES.values() for t in cl}]))
            facets = {"group": re.sub(r"[^a-z0-9]+", "-", r["grp"].lower()),
                      "sub": re.sub(r"[^a-z0-9]+", "-", r["sub"].lower())}
            r["tags"] = json.dumps(list(dict.fromkeys(words + themes)),
                                   ensure_ascii=False)
            r["meta"] = json.dumps(
                {"stems": stems, "themes": themes, "facets": facets,
                 "res": _res(src + " " + r["sub"]), "desc": desc},
                ensure_ascii=False)
            r["source"] = "crawl"
            cols = ("name,grp,sub,folder,files,images,file_count,bytes,"
                    "first_image,thumb,tags,meta,source").split(",")
            conn.execute(
                f"INSERT INTO textures ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [r[c] for c in cols])
        if scan_rows:
            # OR REPLACE against the (name, folder) scan-unique index:
            # the buffer IS the desired state, and these rows were never
            # deleted (a plain INSERT re-added them every rebuild -- the
            # duplication the index exists to stop)
            conn.executemany(
                f"INSERT OR REPLACE INTO textures ({','.join(scan_cols)}) "
                f"VALUES ({','.join('?' for _ in scan_cols)})", scan_rows)
        conn.commit()
    finally:
        conn.close()
    return len(records)


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    print(f"textures imported: {import_textures(db)} rows")
