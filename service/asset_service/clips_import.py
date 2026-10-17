"""The clips table: one row per animation clip with its measured facts.

Written by `pharos.py measure --clips <animation folder>` (ingest runs it
for the animation section) from measure.py's records: duration, fps,
frames, root motion, skeleton signature and family, bone names, whether
the file also carries a mesh. The animation API, the agent docs and the
MCP server read it; a registry without it (an older ingest) still
answers from the legacy pack tables until the next ingest.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from asset_service import measure, skeleton

CLIPS_DDL = """
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rel TEXT NOT NULL UNIQUE,      -- library-relative path, forward slashes
  folder TEXT,                   -- the clip's pack folder (section/sub)
  name TEXT,                     -- file stem
  ext TEXT,
  bytes INTEGER,
  mtime_ns INTEGER,
  frames INTEGER,
  fps REAL,
  duration_s REAL,
  loop INTEGER,                  -- NULL = not measured
  root_motion INTEGER,           -- 1 / 0 / NULL = unknown
  has_mesh INTEGER,
  bones INTEGER,
  skeleton TEXT,                 -- signature of the bone-name set
  family TEXT,                   -- cc | mixamo | ue-mannequin | unknown
  bone_names TEXT,               -- JSON list
  facts TEXT,                    -- JSON: everything else measure knows
  measured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_clips_folder ON clips(folder);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(CLIPS_DDL)


def has_rows(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM clips LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def pack_folder(rel: str, section_rel: str) -> str:
    """The clip's pack: the first folder below the section, as the
    legacy pack ids spelt it ('<section>/<sub>'; a clip directly in the
    section belongs to the section itself)."""
    sec = section_rel.strip("/")
    inner = rel[len(sec) + 1:] if sec and rel.startswith(sec + "/") else rel
    parts = inner.split("/")
    return f"{sec}/{parts[0]}" if (sec and len(parts) > 1) else (sec or parts[0])


def _is_clip(rec: dict) -> bool:
    return bool(rec.get("clips")) and rec.get("kind") in ("clip", "mesh+clip", "skeletal")


def rows_from_records(records, root: Path, section) -> list:
    """Registry rows for the records that are clips."""
    sec_rel = Path(section).resolve().relative_to(Path(root).resolve()).as_posix() \
        if Path(section).is_absolute() else str(section)
    out = []
    for rec in records:
        if not _is_clip(rec):
            continue
        clip = rec["clips"][0]
        names = rec.get("bone_names") or []
        facts = measure.facts_of(rec)
        facts.pop("bone_names", None)
        facts["clips"] = rec["clips"]
        out.append((rec["rel"], pack_folder(rec["rel"], sec_rel),
                    Path(rec["rel"]).stem, rec.get("ext"), rec.get("bytes"),
                    rec.get("mtime_ns"), clip.get("frames"), clip.get("fps"),
                    clip.get("duration_s"), None,
                    None if clip.get("root_motion") is None else int(bool(clip["root_motion"])),
                    int(bool(rec.get("has_mesh"))), rec.get("bones") or 0,
                    skeleton.signature(names) if names else None,
                    skeleton.family(names) if names else None,
                    json.dumps(names, ensure_ascii=False),
                    json.dumps(facts, ensure_ascii=False),
                    rec.get("measured_at") or time.strftime("%Y-%m-%dT%H:%M:%S")))
    return out


def write_clips(db_path, root, section, verbose: bool = False) -> tuple:
    """Measure the animation section and replace its rows in `clips`.
    Returns (rows written, measure stats)."""
    root, section = Path(root), Path(section)
    records, stats = measure.measure_folder(section, root, exts=frozenset({".fbx", ".bvh"}),
                                            verbose=verbose)
    rows = rows_from_records(records, root, section)
    sec_rel = section.resolve().relative_to(root.resolve()).as_posix() \
        if section.is_absolute() else str(section)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_table(conn)
        # the section's rows go first (a prefix test, not LIKE: '_' and
        # '%' in a folder name are letters here)
        conn.execute("DELETE FROM clips WHERE rel = ? OR substr(rel, 1, ?) = ?",
                     (sec_rel, len(sec_rel) + 1, sec_rel + "/"))
        conn.executemany(
            "INSERT OR REPLACE INTO clips (rel,folder,name,ext,bytes,mtime_ns,frames,fps,"
            "duration_s,loop,root_motion,has_mesh,bones,skeleton,family,bone_names,"
            "facts,measured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    from asset_service import db
    db.touch_sources(db_path)
    return len(rows), stats


def clip_count(conn: sqlite3.Connection, packs: bool = False):
    """Rows (or distinct folders) of the clips table; None when the table
    is absent or empty (then the legacy pack tables are the answer)."""
    if not has_rows(conn):
        return None
    sql = "SELECT COUNT(DISTINCT folder) FROM clips" if packs else "SELECT COUNT(*) FROM clips"
    return conn.execute(sql).fetchone()[0]
