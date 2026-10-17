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

`meta` also carries what the files themselves say (imgmeta.py): `px`
[w, h] read from the colour map's header, `res` derived from it
(`res_source` "measured"; "name" only when no header was readable) and
`maps` {role: file}. Folder names are not evidence: a "..._4K" folder
keeps its name when its maps are re-exported at 2048 px.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config, imgmeta
from asset_service import kinds
from asset_service import textmatch as tm

TEX_ROOT = config.TEX_ROOT
MAIN_NAME = "4K_Textures_Gumroad"
GROUP_MAIN = "4K Textures Gumroad"
GROUP_MISC = "Misc"

DISPLAY_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
FILE_EXT = kinds.IMAGE

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
    oldest row per (name, folder), then create the index. Older tables
    have no `source` column at all: add it FIRST (the index filters on it;
    the scanner used to crash here with 'no such column: source')."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(textures)")}
    if cols and "source" not in cols:
        conn.execute("ALTER TABLE textures ADD COLUMN source TEXT")
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
    # spellings that used to split one material into one-file fragment
    # sets ("<name> 2k normaldx", "<name> preview")
    "normaldx", "normalgl", "ambientocclusion", "emissive", "emission",
    "gloss", "smoothness", "preview", "render", "thumbnail",
}

# measured larger side -> label. The floors sit below the nominal size so
# 3840- and 4000-pixel maps still read as 4K.
RES_FLOORS = ((7000, "8K"), (3500, "4K"), (1800, "2K"), (900, "1K"),
              (450, "512"))

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
        # 16 letters fit 'ambientocclusion'; counters stay at 12 digits
        m = re.search(r"[_ ]([a-z0-9]{1,16})$", base)
        if m and (m.group(1) in CHANNEL_TOKENS
                  or re.fullmatch(r"\d{1,12}", m.group(1))):
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


def _res_label(side: int) -> str:
    for floor, label in RES_FLOORS:
        if side >= floor:
            return label
    return str(side)


def _facts(d: Path, sizes: dict, images: list) -> dict:
    """What the file NAMES of one set say, without opening a file. `sizes`
    is {file name: bytes} for the set's files in folder `d`, `images` its
    displayable files in name order. Returns `maps` {role: path}, `thumb`
    (the preview render; else the colour map; else the first image) and
    `ref` (what to measure for the set's resolution: the colour map, else
    the largest image that is not a preview)."""
    # plain strings from here on: this runs over every file of the
    # library at every server start, and pathlib properties cost more
    # than the name analysis itself
    def ext(name: str) -> str:
        return name[name.rfind("."):].lower()
    # first file per role = shortest name ('x_normal.jpg' before
    # 'x_normal - Copy.jpg'); of a DirectX/OpenGL normal pair the GL one,
    # because Blender and Unity both read GL normals
    ordered = sorted(sizes, key=lambda n: (
        n.rpartition(".")[0].lower().endswith("dx"), len(n), n.lower()))
    parts = [(n,) + imgmeta.role_parts(d.name + "/" + n) for n in ordered]
    anchors = {prefix for _, role, _, prefix in parts
               if role in ("albedo", "normal")}
    # the image named exactly like the front part its maps share
    # ('oak_floor.jpg' beside 'oak_floor_diffuse.jpg') is the set's own
    # picture, whatever its last word looks like
    bases = {prefix.rstrip(" _-.") for prefix in anchors}
    previews = {n for n, role, _, _ in parts
                if role == "preview" or n.rpartition(".")[0].lower() in bases}
    maps: dict = {}
    for n, role, token, prefix in parts:
        if (not role or n in previews or role in maps
                or ext(n) not in FILE_EXT):
            continue
        # a one-letter suffix is a map only beside a colour or normal map
        # of the same name: photo libraries end names in SIZE letters
        # ('Gantry0042_M' = medium), which read as metallic/specular
        if len(token) == 1 and prefix not in anchors:
            continue
        maps[role] = n
    measurable = [n for n in ordered
                  if n not in previews and ext(n) in imgmeta.SIZE_EXT]
    largest = max(measurable, key=sizes.get, default=None)
    ref = [n for n in dict.fromkeys((maps.get("albedo"), largest))
           if n in measurable]
    # without a preview the thumb is a full-size map (heavy, but the only
    # picture there is): then the colour map, not whatever sorts first
    # (in a set with an AO map that was the AO map)
    shown = [i.name for i in images]
    thumb = ([n for n in shown if n in previews]
             or [n for n in shown if n == maps.get("albedo")] or shown)
    folder = d.as_posix() + "/"
    return {"maps": {role: folder + n for role, n in maps.items()},
            "thumb": folder + thumb[0] if thumb else "",
            "ref": [folder + n for n in ref]}


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
    sizes = {f.name: f.stat().st_size for f in all_files if f.is_file()}
    # a set without a preview image is still a set (empty thumb, like
    # loose families): HDR/EXR panoramas were silently dropped here
    facts = _facts(d, sizes, images)
    return {"name": _pretty(d.name), "grp": grp, "sub": sub,
            "folder": d.as_posix(),
            "files": json.dumps([f.as_posix() for f in all_files]),
            "images": json.dumps([i.as_posix() for i in images]),
            "file_count": len(all_files),
            "bytes": sum(sizes.values()),
            "first_image": images[0].as_posix() if images else "",
            "src_name": d.name, **facts}


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
        sizes = {f.name: f.stat().st_size for f in fam["files"]}
        out.append({
            "name": _pretty(base), "grp": grp,
            "sub": sub, "folder": d.as_posix(),
            "files": json.dumps([f.as_posix() for f in fam["files"]]),
            "images": json.dumps([i.as_posix() for i in displayable]),
            "file_count": len(fam["files"]),
            "bytes": sum(sizes.values()),
            "first_image": (displayable[0].as_posix()
                            if displayable else ""),
            "src_name": base, **_facts(d, sizes, displayable)})
    return out


def import_textures(db_path: str | Path, root: Path = TEX_ROOT) -> int:
    db_path = Path(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(TEXTURES_DDL)
        # self-migrates the source column, then creates the scan index
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
        # px measured by the previous run, keyed by what identifies an
        # unchanged set. This importer runs at every server start, and
        # the header read per set is the one cost a restart can skip.
        # Only successes are kept: an unreadable file is tried again.
        old_px: dict = {}
        for name, folder, count, size, meta in conn.execute(
                "SELECT name, folder, file_count, bytes, meta FROM textures "
                "WHERE source IS NOT 'scan'"):
            try:
                m = json.loads(meta or "{}")
            except (ValueError, TypeError):
                continue
            if (isinstance(m, dict) and m.get("res_source") == "measured"
                    and isinstance(m.get("px"), list) and len(m["px"]) == 2):
                old_px[(name, folder, count, size)] = m["px"]
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

        def _res(text: str) -> str:
            # name-token label, used only when nothing could be measured.
            # First token in reading order (the set name comes before its
            # category): popping from a set made the label differ between
            # runs when both carried one
            for v in re.split(r"[^a-z0-9]+", (text or "").lower()):
                if v in {"8k", "4k", "2k", "1k", "512", "1024", "2048"}:
                    return {"512": "512", "1024": "1K",
                            "2048": "2K"}.get(v, v.upper())
            return ""

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
            words = tm.words(r["name"], tm.STOPWORDS)
            themes = tm.match_themes(src + " " + r["sub"], TEX_THEMES,
                                     strict=True)
            stems = list(dict.fromkeys(tm.stems(r["name"]) + tm.stems(r["sub"])))
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
                stems = list(dict.fromkeys(stems + tm.stems(" ".join(kw))))
                themes = list(dict.fromkeys(
                    themes + [k for k in kw if k in
                              {t for cl in TEX_THEMES.values() for t in cl}]))
            facets = {"group": re.sub(r"[^a-z0-9]+", "-", r["grp"].lower()),
                      "sub": re.sub(r"[^a-z0-9]+", "-", r["sub"].lower())}
            r["tags"] = json.dumps(list(dict.fromkeys(words + themes)),
                                   ensure_ascii=False)
            # resolution = what the files ARE, not what the folder is
            # called (folders keep "4K" after the maps were downscaled).
            # One header read per set; a second only when the colour map
            # is unreadable.
            px = old_px.get((r["name"], r["folder"], r["file_count"],
                             r["bytes"]))
            for f in r.pop("ref"):
                px = px or imgmeta.image_size(f)
            r["meta"] = json.dumps(
                {"stems": stems, "themes": themes, "facets": facets,
                 "res": (_res_label(max(px)) if px
                         else _res(src + " " + r["sub"])),
                 "res_source": "measured" if px else "name",
                 "px": list(px) if px else None,
                 "maps": r.pop("maps"), "desc": desc},
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
