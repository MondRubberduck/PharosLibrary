"""Import the audio library (Audio_Assets) into the `audio` table.

Ground truth is the crawl agent's own index -- library_files.jsonl
(one JSON per file: p/cat/sub/ext/bytes/dur/sr/ch) -- NOT a disk walk.
Category descriptions + keywords come from library_index.json; keywords
feed a record's `themes` only, never its `stems` (dual-audience tagging
like collection/textures: `tags` human words + themes, `meta` for the algo).

Retention (mirrors meshes/textures): rows with source='scan' (written by
scanner.py) survive every rebuild. UNIQUE(rel) forbids two rows for the
same file, so a file the crawl also knows ends up with the CRAWL row
(richer sr/ch/dur); scan rows for files absent from the jsonl are kept.
Rows predating the source column count as crawl-owned and are replaced.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# allow standalone execution
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_service import config
from asset_service import textmatch as tm


PLAYABLE = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}

AUDIO_DDL = """
CREATE TABLE IF NOT EXISTS audio (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cat TEXT,
  sub TEXT,
  rel TEXT UNIQUE,
  ext TEXT,
  bytes INTEGER,
  dur REAL,
  sr INTEGER,
  ch INTEGER,
  playable INTEGER,
  desc TEXT,
  tags TEXT,
  meta TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_audio_cat ON audio(cat);
"""


def ensure_source_column(conn: sqlite3.Connection) -> None:
    """Self-migrate: tables built before scan-row retention have no
    `source` column. Called by every writer (scanner + importer) because
    either may run first against an older registry; legacy rows carry
    NULL and are treated as crawl-owned (the next rebuild replaces them
    from the jsonl)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audio)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE audio ADD COLUMN source TEXT")

AUDIO_THEMES = {
    "explosions": ["explosion", "blast", "detonation", "boom", "firework",
                   "destruction", "debris", "demolition"],
    "gunshots": ["gunshot", "gun", "firearm", "pistol", "rifle", "shotgun",
                 "weapon", "discharge"],
    "weapons": ["weapon", "reload", "melee", "sword", "bow", "knife",
                "handling", "blade"],
    "footsteps": ["footstep", "steps", "walk", "run", "sprint", "boot"],
    "ui": ["ui", "button", "click", "notification", "terminal", "computer",
           "interface", "menu", "beep"],
    "transitions": ["whoosh", "transition", "riser", "swoosh", "swish",
                    "stinger"],
    "ambiance": ["ambiance", "ambient", "background", "bed", "atmosphere",
                 "room", "street", "crowd"],
    "weather": ["rain", "thunder", "storm", "snow", "wind", "lightning"],
    "nature": ["water", "fire", "flame", "wind", "river", "ocean", "bird",
               "insect", "forest", "stream"],
    "animals": ["animal", "creature", "monster", "roar", "growl", "bird",
                "dog", "cat", "dragon", "beast", "screech"],
    "human": ["voice", "scream", "grunt", "breath", "crowd", "human",
              "laugh", "pain", "effort"],
    "vehicles": ["vehicle", "car", "motorcycle", "boat", "aircraft",
                 "train", "tank", "engine", "helicopter", "plane", "truck"],
    "machines": ["machine", "mechanical", "robot", "steampunk", "device",
                 "motor", "servo", "gear"],
    "music": ["music", "loop", "stem", "instrument", "guitar", "piano",
              "drum", "melody"],
    "horror": ["horror", "creepy", "gore", "magic", "dark", "scary",
               "haunt"],
    "impacts": ["impact", "hit", "crash", "smash", "collision", "slam",
                "punch"],
    "alarms": ["alarm", "siren", "warning", "buzzer", "emergency",
               "klaxon"],
    "scifi": ["scifi", "sci", "drone", "robot", "energy", "laser", "tech",
              "futuristic"],
    "foley": ["foley", "door", "cloth", "paper", "kitchen", "tool", "prop"],
}


def _pretty(stem: str) -> str:
    s = re.sub(r"[._][A-Z]{3,6}\d*\.\d{2,4}$", "", stem)      # vendor codes
    s = re.sub(r"\s*\(\d+\)$", "", s)                          # ' (1)' dups
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ").split()
    out = " ".join(p for p in parts if p)
    return out[:1].upper() + out[1:] if out else stem


def _kebab(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (x or "").lower()).strip("-")


def import_audio(db_path: str | Path, root=None) -> int:
    root = Path(root) if root else config.AUDIO_ROOT   # the config's root at call time
    jsonl_file = root / "library_files.jsonl"
    index_file = root / "library_index.json"
    db_path = Path(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(AUDIO_DDL)
        ensure_source_column(conn)
        # no crawler jsonl -> this importer owns NOTHING; scanner-indexed
        # rows must survive a restart (previously the DELETE below ran
        # first and the later open() failure could take them with it)
        if not jsonl_file.is_file():
            kept = conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
            print(f"audio import: no crawl index ({jsonl_file}) -- keeping "
                  f"{kept} scanner-indexed row(s)")
            return kept
        # scanner rows (source='scan') survive the rebuild: only this
        # importer's own rows (NULL / 'crawl') are replaced. UNIQUE(rel)
        # means a file indexed by BOTH sides cannot keep two rows -- the
        # crawl row REPLACES the scan row (same file, richer metadata:
        # sr/ch/dur from the crawl agent); scan rows for files the crawl
        # does not know stay untouched. sqlite_sequence is deliberately
        # NOT reset: surviving scan rows keep their ids. A file keeps its
        # id across rebuilds too (agents persist ids; the re-insert used to
        # renumber the whole table on every boot); new files get new ids.
        old_ids = {r[0]: r[1] for r in
                   conn.execute("SELECT rel, id FROM audio")}
        conn.execute("DELETE FROM audio WHERE source IS NOT 'scan'")

        cat_meta: dict = {}
        if index_file.is_file():
            try:
                idx = json.loads(index_file.read_text(encoding="utf-8"))
                for cat, info in (idx.get("categories") or {}).items():
                    cat_meta[cat] = {
                        "desc": info.get("description") or "",
                        "keywords": info.get("keywords") or [],
                    }
            except (ValueError, OSError):
                pass

        n = 0
        # cp1252/latin-1 fallback: strict utf-8 emptied the whole audio
        # section when the crawl index carried non-ASCII filenames
        raw = jsonl_file.read_bytes()
        text = None
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise UnicodeDecodeError("audio index", raw[:1], 0, 1,
                                     "undecodable")
        for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                rel = e.get("p") or ""
                cat = e.get("cat") or ""
                sub = e.get("sub") or ""
                ext = (e.get("ext") or "").lower()
                stem = Path(rel).stem
                cm = cat_meta.get(cat, {})
                words = tm.words(stem, tm.STOPWORDS) + [
                    w for w in tm.words(cat) + tm.words(sub)
                    if w not in ("files",)]
                # the file's OWN words plus its category, matched as WORDS.
                # The category's keyword list used to be part of this text:
                # every sound of a category then carried all its themes
                # (q=horror: 550 sounds, about 100 of them horror) -- the
                # same leak the stems below were already protected from
                themes = tm.match_themes(" ".join([stem, cat, sub]),
                                         AUDIO_THEMES, strict=True)
                stems = list(dict.fromkeys(
                    tm.stems(stem) + tm.stems(cat) + tm.stems(sub)))
                facets = {"cat": _kebab(cat), "sub": _kebab(sub)}
                meta = json.dumps({"stems": stems, "themes": themes,
                                   "facets": facets}, ensure_ascii=False)
                tags = json.dumps(list(dict.fromkeys(words + themes)),
                                  ensure_ascii=False)
                conn.execute(
                    "INSERT OR REPLACE INTO audio "
                    "(id,name,cat,sub,rel,ext,bytes,dur,sr,ch,"
                    "playable,desc,tags,meta,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (old_ids.get(rel), _pretty(stem), cat, sub, rel, ext,
                     e.get("bytes") or 0,
                     e.get("dur") if e.get("dur") is not None else 0.0, e.get("sr"), e.get("ch"),
                     1 if ext in PLAYABLE else 0,
                     cm.get("desc") or "", tags, meta, "crawl"))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    print(f"audio imported: {import_audio(db)} files")
