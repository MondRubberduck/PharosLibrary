"""Import a purchase-crawler CSV into the `collection` table.

This is the ground truth for the Assets section -- detached from the
pack indexing (the `assets`/`asset_files` tables stay the source for
the Animation page only).

CSV: <library_root>/<collection section>/3D_Assets_Overview.csv
Columns: Name, Service, Type, Seller / Author, Purchased, Price (USD),
         Product URL, Local Folder, Images
Local Folder holds scraped product images; the first one (sorted) becomes
the card thumbnail.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from asset_service import config

CSV_PATH = config.CSV_PATH
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

COLLECTION_DDL = """
CREATE TABLE IF NOT EXISTS collection (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  service TEXT,
  store TEXT,
  type_raw TEXT,
  type_group TEXT,
  type_cat TEXT,
  type_sub TEXT,
  seller TEXT,
  purchased TEXT,
  price TEXT,
  url TEXT,
  folder TEXT,
  image_count INTEGER,
  first_image TEXT,
  tags TEXT,
  thumb_override TEXT,
  meta TEXT,
  availability TEXT,
  asset_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_collection_store ON collection(store);
"""


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens from space/dash/underscore separated text,
    camelCase aware, with common plural stemming for search recall."""
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


# controlled theme vocabulary -- human-readable AND the algorithm's
# grouping keys (kebab-case, stable)
THEMES = {
    "scifi": ["scifi", "sci", "fi", "cyberpunk", "futuristic", "spaceship",
              "space", "starship", "mech", "robot", "technology", "lab"],
    "medieval": ["medieval", "castle", "fortress", "kingdom", "knight",
                 "fantasy", "magic"],
    "horror": ["horror", "haunted", "spooky", "creepy", "demonic", "demon",
               "hell", "crypt", "graveyard", "zombie"],
    "nature": ["nature", "forest", "jungle", "tree", "plant", "foliage",
               "rock", "stone", "mountain", "landscape", "grass"],
    "city": ["city", "urban", "street", "town", "village", "building",
             "architecture", "skyscraper"],
    "winter": ["winter", "snow", "ice", "arctic", "frozen"],
    "desert": ["desert", "sand", "dune", "canyon"],
    "water": ["aquatic", "underwater", "ocean", "sea", "island", "coast",
              "harbor", "docks"],
    "interior": ["interior", "room", "office", "apartment", "furniture",
                 "house", "kitchen", "bathroom", "livingroom"],
    "military": ["military", "gun", "weapon", "armor", "soldier", "war",
                 "tank"],
    "characters": ["character", "human", "creature", "animal", "clothing",
                   "outfit", "monk", "people"],
    "vehicles": ["vehicle", "car", "truck", "watercraft", "boat",
                 "transportation"],
    "ruins": ["ruins", "ruin", "destroyed", "apocalyptic", "wreck",
              "abandoned", "derelict"],
    "temple": ["temple", "church", "religious", "shrine", "sacred"],
    "japan": ["japan", "japanese", "asian", "tokyo"],
    "historical": ["victorian", "steampunk", "historical", "ancient"],
    "industrial": ["industrial", "factory", "machine", "warehouse"],
}
STOPWORDS = {"the", "and", "with", "for", "vol", "pack", "set", "all", "in",
             "of", "a", "to", "by"}


def _words(text: str) -> list[str]:
    """Full lowercase words (human-readable) -- no stemming."""
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", w).lower().split()
        out.extend(parts)
    return list(dict.fromkeys(w for w in out if len(w) > 2 and w not in STOPWORDS))


def _match_themes(text: str) -> list[str]:
    """Theme hits: >=1 keyword of length>=5 or >=2 distinct keywords."""
    low = " " + text.lower() + " "
    hits = []
    for theme, kws in THEMES.items():
        found = [k for k in kws if k in low]
        if len(found) >= 2 or (len(found) == 1 and len(found[0]) >= 5):
            hits.append(theme)
    return hits


def parse_type(type_raw: str) -> tuple[str, str, str]:
    """'UE 3D Asset - Environments / Medieval' -> (group, cat, sub)."""
    t = (type_raw or "").strip()
    if " - " in t:
        group, rest = t.split(" - ", 1)
    else:
        group, rest = t, ""
    if " / " in rest:
        cat, sub = rest.split(" / ", 1)
    else:
        cat, sub = rest, ""
    # strip noise suffixes like '(listing removed)', '(Free)'
    cat = re.sub(r"\s*\([^)]*\)\s*$", "", cat).strip()
    return group.strip(), cat.strip(), sub.strip()


def import_collection(db_path: str | Path, csv_path: Path = CSV_PATH) -> int:
    """Merge the CSV purchase catalog into the `collection` table.

    Merge semantics (NOT a full replace -- hand-curated rows must survive):
    - CSV rows are authoritative for catalog fields (store, type, seller,
      price, url, folder, images, derived tags/meta).
    - Human/agent state is preserved across re-imports: thumb_override,
      human-added tags (union with derived tags), asset_path, and rows
      added by other agents that are not in the CSV at all.
    - availability.jsonl (crawler output) is authoritative for the
      availability state whenever it knows the item.
    Returns the number of CSV rows processed.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(COLLECTION_DDL)
        # self-migrate: the table may predate newer columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
        for col in ("meta", "thumb_override", "availability",
                         "asset_path"):
            if col not in cols:
                conn.execute(f"ALTER TABLE collection ADD COLUMN {col} TEXT")
        conn.commit()

        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        # human thumbnail overrides survive re-imports
        overrides = {}
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
            if "thumb_override" in cols:
                for row in conn.execute(
                        "SELECT folder, thumb_override FROM collection "
                        "WHERE thumb_override IS NOT NULL"):
                    overrides[row["folder"]] = row["thumb_override"]
        except sqlite3.Error:
            pass

        # existing rows: by folder and by name (for merge lookups)
        existing: dict = {}
        for row in conn.execute("SELECT * FROM collection"):
            if row["folder"]:
                existing.setdefault("f:" + row["folder"], row)
            existing.setdefault("n:" + row["name"], row)

        avail = {}
        av_path = config.AVAILABILITY_JSONL
        if av_path.is_file():
            with open(av_path, encoding="utf-8") as af:
                for line in af:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        avail[e.get("name", "")] = {
                            "state": e.get("availability") or "owned-not-downloaded",
                            "local_path": e.get("local_path")}
                    except ValueError:
                        continue

        def _kebab(x):
            return re.sub(r"[^a-z0-9]+", "-", (x or "").lower()).strip("-")

        consumed_folders = set()
        n = 0
        for r in rows:
            name = (r.get("Name") or "").strip()
            folder = (r.get("Local Folder") or "").strip()
            group, cat, sub = parse_type(r.get("Type") or "")
            store = ""
            if folder:
                store = Path(folder).parent.name
            first_image = ""
            img_count = 0
            if folder and Path(folder).is_dir():
                images = sorted(p for p in Path(folder).iterdir()
                                if p.is_file() and p.suffix.lower() in IMG_EXTS)
                img_count = len(images)
                if images:
                    first_image = images[0].as_posix()
            words = _words(name) + [w for w in _words(cat) + _words(sub)
                                    if w not in STOPWORDS]
            themes = _match_themes(" ".join([name, cat, sub]))
            stems = list(dict.fromkeys(
                _tokens(name)
                + _tokens(cat) + _tokens(sub) + _tokens(group)
                + _tokens(r.get("Service") or "") + _tokens(store)
                + _tokens(r.get("Seller / Author") or "")))
            facets = {"store": _kebab(store), "service": _kebab(r.get("Service")),
                      "group": _kebab(group), "cat": _kebab(cat),
                      "sub": _kebab(sub),
                      "seller": _kebab(r.get("Seller / Author"))}
            meta = json.dumps({"stems": stems, "themes": themes,
                               "facets": facets}, ensure_ascii=False)

            old = existing.get("f:" + folder) or existing.get("n:" + name)
            # preserve human-added tags: union derived with whatever is in the DB
            old_tags = []
            if old is not None:
                try:
                    old_tags = json.loads(old["tags"] or "[]")
                except (ValueError, TypeError):
                    old_tags = []
            tags = list(dict.fromkeys(words + themes + old_tags))

            av = avail.get(name) or {}
            # availability: crawler jsonl is authoritative; keep DB value only
            # for items the jsonl does not know
            availability = (av.get("state") or old["availability"]
                            if old is not None else av.get("state")) \
                or "owned-not-downloaded"
            asset_path = av.get("local_path") or \
                (old["asset_path"] if old is not None else None)
            thumb_override = overrides.get(folder) or \
                (old["thumb_override"] if old is not None else None)

            if old is not None:
                # UPDATE by id: catalog fields refresh, human/agent state
                # (tags, thumb_override, asset_path) is preserved or merged
                conn.execute(
                    "UPDATE collection SET name=?, service=?, store=?,"
                    " type_raw=?, type_group=?, type_cat=?, type_sub=?,"
                    " seller=?, purchased=?, price=?, url=?, folder=?,"
                    " image_count=?, first_image=?, tags=?, meta=?,"
                    " availability=?, asset_path=? WHERE id=?",
                    (name, (r.get("Service") or "").strip(), store,
                     (r.get("Type") or "").strip(), group, cat, sub,
                     (r.get("Seller / Author") or "").strip(),
                     (r.get("Purchased") or "").strip(),
                     (r.get("Price (USD)") or "").strip(),
                     (r.get("Product URL") or "").strip(),
                     folder, img_count, first_image,
                     json.dumps(tags, ensure_ascii=False),
                     meta, availability, asset_path, old["id"]))
            else:
                conn.execute(
                    "INSERT INTO collection (name, service, store, type_raw, type_group,"
                    " type_cat, type_sub, seller, purchased, price, url, folder,"
                    " image_count, first_image, tags, thumb_override, meta, "
                    "availability, asset_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, (r.get("Service") or "").strip(), store,
                     (r.get("Type") or "").strip(), group, cat, sub,
                     (r.get("Seller / Author") or "").strip(),
                     (r.get("Purchased") or "").strip(),
                     (r.get("Price (USD)") or "").strip(),
                     (r.get("Product URL") or "").strip(),
                     folder, img_count, first_image,
                     json.dumps(tags, ensure_ascii=False),
                     thumb_override, meta, availability, asset_path))
            if folder:
                consumed_folders.add(folder)
            n += 1

        # preserve rows added by other agents that are NOT in the CSV
        # (e.g. crawler-added kit cards) -- refresh availability only
        for key, row in list(existing.items()):
            if not key.startswith("f:"):
                continue
            if row["folder"] in consumed_folders:
                continue
            av = avail.get(row["name"])
            if av:
                conn.execute(
                    "UPDATE collection SET availability=?, asset_path=? "
                    "WHERE folder=?",
                    (av.get("state") or row["availability"],
                     av.get("local_path") or row["asset_path"], row["folder"]))
            n += 1

        conn.commit()
    finally:
        conn.close()
    return n


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    count = import_collection(db)
    print(f"collection imported: {count} rows -> {db}")
