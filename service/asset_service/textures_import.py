"""Import the texture library (Textures_Materials) into the `textures` table.

Groups:
  - "4K Textures Gumroad": leaf folders under 4K_Textures_Gumroad/<subcat>/<set>/
    (each set folder holds the map channels + a <name>_render.jpg preview)
  - "Misc": everything else -- direct subfolders (leaf-most folders with
    images) and loose files in the root, grouped into families by stripping
    map-channel suffixes (X_albedo.tif + X_normal.tif + ... -> one card).

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


def _walk_leaves(d: Path) -> list:
    """Collect leaf-most directories under d that contain image files."""
    leaves: list = []
    try:
        children = [c for c in d.iterdir() if c.is_dir()]
    except OSError:
        return leaves
    any_leaves = False
    for c in children:
        sub = _walk_leaves(c)
        if sub:
            leaves.extend(sub)
            any_leaves = True
    if not any_leaves:
        try:
            has_images = any(f.suffix.lower() in DISPLAY_EXT
                             for f in d.iterdir() if f.is_file())
        except OSError:
            return leaves
        if has_images:
            leaves.append(d)
    return leaves


def _record_from_dir(d: Path, grp: str, sub: str) -> dict | None:
    try:
        all_files = [f for f in d.iterdir() if f.is_file()]
    except OSError:
        return None
    images = sorted((f for f in all_files if f.suffix.lower() in DISPLAY_EXT),
                    key=lambda p: p.name.lower())
    if not images:
        return None
    render = next((i for i in images
                   if i.stem.lower().endswith("render")
                   or i.stem.lower() == d.name.lower()), None)
    thumb = (render or images[0]).as_posix()
    return {"name": _pretty(d.name), "grp": grp, "sub": sub,
            "folder": d.as_posix(),
            "files": json.dumps([f.as_posix() for f in all_files]),
            "images": json.dumps([i.as_posix() for i in images]),
            "file_count": len(all_files),
            "bytes": sum(f.stat().st_size for f in all_files if f.is_file()),
            "first_image": images[0].as_posix(), "thumb": thumb,
            "src_name": d.name}


def import_textures(db_path: str | Path, root: Path = TEX_ROOT) -> int:
    db_path = Path(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(TEXTURES_DDL)
        conn.execute("DELETE FROM textures")
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
                for leaf in _walk_leaves(subcat):
                    r = _record_from_dir(leaf, GROUP_MAIN, subcat.name)
                    if r:
                        records.append(r)

        if root.is_dir():
            # Misc subfolders (leaf-most dirs with images)
            for child in sorted(p for p in root.iterdir() if p.is_dir()):
                if child.name == MAIN_NAME:
                    continue
                for leaf in _walk_leaves(child):
                    r = _record_from_dir(leaf, GROUP_MISC, child.name)
                    if r:
                        records.append(r)
            # loose files in root -> families
            families: dict[str, dict] = {}
            try:
                loose = [f for f in root.iterdir() if f.is_file()]
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
            for base, fam in sorted(families.items()):
                if not fam["files"]:
                    continue
                displayable = sorted(fam["images"], key=lambda p: p.name.lower())
                records.append({
                    "name": _pretty(base), "grp": GROUP_MISC,
                    "sub": "Loose Files", "folder": root.as_posix(),
                    "files": json.dumps([f.as_posix() for f in fam["files"]]),
                    "images": json.dumps([i.as_posix() for i in displayable]),
                    "file_count": len(fam["files"]),
                    "bytes": sum(f.stat().st_size for f in fam["files"]),
                    "first_image": (displayable[0].as_posix()
                                    if displayable else ""),
                    "thumb": (displayable[0].as_posix() if displayable else ""),
                    "src_name": base})

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
            cols = ("name,grp,sub,folder,files,images,file_count,bytes,"
                    "first_image,thumb,tags,meta").split(",")
            conn.execute(
                f"INSERT INTO textures ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [r[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    return len(records)


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    print(f"textures imported: {import_textures(db)} rows")
