"""SQLite registry schema for the Pharos asset library.

The registry is derived data, rebuilt from the library by the importers:
    meshes / meshes_parked   one row per model (scanner, Blender, manifests)
    textures, audio          the texture sets and sounds
    clips                    one row per animation clip (clips_import)
    folders                  the library map's nodes with measured facts
    collection               the purchase catalogue (optional)
    registry_meta            which library it belongs to, the source stamps
The Kiosk-era pack tables (assets, asset_files, assets_fts) are dropped
at the first start of a registry that still carries them.

Safety: this module only ever writes to the registry database under the
configured registry dir -- never to files under the canonical asset roots.

CLI:
    python db.py --db <registry>/assets.sqlite --init
    python db.py --db ... --stats
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from asset_service import config
from typing import Any, Optional

SCHEMA_VERSION = 6

# The registry holds derived data only: meshes, textures, audio, clips,
# folders (built by their importers), collection (optional) and
# registry_meta (which library it belongs to, the source stamps).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT);
"""
# the Kiosk-era pack tables (1.0: the clips and folders tables replaced
# them; dropped at the next start of a registry that still has them)
_LEGACY_SQL = """
DROP TRIGGER IF EXISTS assets_fts_ai;
DROP TRIGGER IF EXISTS assets_fts_ad;
DROP TRIGGER IF EXISTS assets_fts_au;
DROP TABLE IF EXISTS assets_fts;
DROP TABLE IF EXISTS asset_files;
DROP TABLE IF EXISTS assets;
DROP TABLE IF EXISTS operations;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the registry read/write with the pragmas we rely on."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create/migrate the schema and stamp PRAGMA user_version. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"registry at {db_path} was written by a newer schema "
            f"({current} > {SCHEMA_VERSION}); upgrade this package first"
        )
    conn.executescript(SCHEMA_SQL)
    # --- migrations -------------------------------------------------------
    # The `collection` table is owned by collection_import.py (it creates the
    # table after init_db on a fresh registry, with these columns included),
    # so an empty PRAGMA result means "not created yet" -- never ALTER it.
    if current < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
        if cols and "thumb_override" not in cols:
            conn.execute("ALTER TABLE collection ADD COLUMN thumb_override TEXT")
    if current < 4:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
        if cols and "meta" not in cols:
            conn.execute("ALTER TABLE collection ADD COLUMN meta TEXT")
    conn.executescript(_LEGACY_SQL)
    conn.execute("DROP INDEX IF EXISTS idx_assets_content_hash")
    conn.execute("DROP INDEX IF EXISTS idx_asset_files_sha256")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Which library a registry belongs to. The default registry dir
# (~/.pharos/registry) is shared by every clone and every library on a
# machine: a registry left behind by ANOTHER library must stop the writers,
# never be silently mixed into (laptop run: 1,475 foreign rows kept).
# ---------------------------------------------------------------------------

REGISTRY_META_DDL = ("CREATE TABLE IF NOT EXISTS registry_meta "
                     "(key TEXT PRIMARY KEY, value TEXT)")
_OWNER_SAMPLE = 200          # absolute paths read per table (unstamped case)


def _norm_path(p) -> str:
    """Separator-normalised path text, case-folded on Windows."""
    import os
    return os.path.normcase(os.path.normpath(str(p)))


def _real_path(p) -> str:
    """_norm_path on the real path: a junction, a symlink or a mapped
    drive spells one file two ways, and they must compare equal."""
    import os
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except (OSError, ValueError):
        return _norm_path(p)


def path_under(path, root) -> bool:
    """True when `path` is `root` or lies below it (Path.relative_to on
    normalised text -- never a string-prefix test)."""
    try:
        Path(_norm_path(path)).relative_to(Path(_norm_path(root)))
        return True
    except ValueError:
        return False


def registry_foreign(db_path: str | Path, library_root: str | Path) -> str:
    """'' when the registry may serve `library_root`, else what it belongs to.

    Read-only. A stamped registry is foreign when its stamp names another
    root. An unstamped one (every registry written before the stamp existed)
    is foreign when it holds rows and NONE of a sample of their absolute
    paths lies under `library_root`; a single row under the root makes it
    this library's (it gets stamped after the next successful ingest/boot).
    """
    p = Path(db_path)
    if not p.is_file():
        return ""
    roots = (Path(library_root), Path(library_root).resolve())
    try:
        # plain path, SELECT only: a file:// URI can't open a UNC registry
        conn = sqlite3.connect(str(p))
    except sqlite3.Error:
        return ""
    sample: list[str] = []
    try:
        try:
            row = conn.execute("SELECT value FROM registry_meta "
                               "WHERE key = 'library_root'").fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            # the stamped library, or a subfolder of this root: every row
            # lies under this root (init re-run one folder higher)
            if any(path_under(row[0], r) for r in roots):
                return ""
            return f"stamped for {row[0]}"
        for sql in ("SELECT fbx FROM meshes WHERE fbx != '' LIMIT ?",
                    "SELECT folder FROM textures WHERE folder != '' LIMIT ?",
                    "SELECT hero_file_path FROM assets LIMIT ?"):
            try:
                sample += [v for (v,) in conn.execute(sql, (_OWNER_SAMPLE,))
                           if v and Path(v).is_absolute()]
            except sqlite3.Error:
                continue                     # table absent in this registry
    finally:
        conn.close()
    if not sample or any(path_under(v, r) for v in sample for r in roots):
        return ""
    return f"not stamped; its rows point elsewhere, e.g. {sample[0]}"


def foreign_registry_message(db_path: str | Path, owner: str,
                             library_root: str | Path,
                             lead: str = "STOP") -> str:
    """The one message every writer prints before it refuses (exit 2)."""
    reg = Path(db_path)
    return (f"{lead}: the registry {reg.parent} belongs to another library "
            f"({owner}), not to library_root {library_root}.\n"
            f"Fix ONE of:\n"
            f"  1. delete the registry file {reg} (and {reg.name}-wal / "
            f"{reg.name}-shm if present) -- it is derived data; the next "
            f"ingest rebuilds it for this library\n"
            f"  2. give this library its own registry:\n"
            f'     python pharos.py init "{library_root}" '
            f'--registry-dir "<new folder>" --force')


def stamp_registry(db_path: str | Path, library_root: str | Path) -> None:
    """Record the library this registry belongs to (after a successful
    ingest or server boot; read by registry_foreign)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(REGISTRY_META_DDL)
        conn.execute("INSERT OR REPLACE INTO registry_meta (key, value) "
                     "VALUES ('library_root', ?)",
                     (str(Path(library_root).resolve()),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str, default=None):
    try:
        row = conn.execute("SELECT value FROM registry_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(REGISTRY_META_DDL)
    conn.execute("INSERT OR REPLACE INTO registry_meta (key, value) VALUES (?, ?)",
                 (key, None if value is None else str(value)))
    conn.commit()


def touch_sources(db_path: str | Path) -> None:
    """A writer (ingest, the scanner, the clip measure) changed the
    registry's sources: the server's warm start must import again."""
    import time
    conn = sqlite3.connect(str(db_path))
    try:
        set_meta(conn, "sources_stamp", time.time_ns())
        set_meta(conn, "sources_stamp_at", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    finally:
        conn.close()


def folder_signature(folder) -> str:
    """Every folder under `folder` (itself included) by mtime: a file or a
    folder added, removed or renamed anywhere below moves one of them. The
    agent files folder is left out (ingest writes there)."""
    import hashlib
    import os
    parts = []
    for dirpath, dirnames, _files in os.walk(str(folder)):
        dirnames[:] = [d for d in dirnames if d != "_Agent_Files"]
        try:
            parts.append(f"{dirpath}:{os.stat(dirpath).st_mtime_ns}")
        except OSError:
            parts.append(f"{dirpath}:absent")
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def remember_scan(db_path: str | Path, label: str, signature: str) -> None:
    """A scan (ingest's or the server's) finished for a folder: the server
    will not scan it again until the folder changes."""
    conn = sqlite3.connect(str(db_path))
    try:
        set_meta(conn, "scan_sig:" + label, signature)
    finally:
        conn.close()


def sources_signature(db_path: str | Path, files) -> str:
    """One string that changes when a writer touched the registry or one
    of the files the importers read changed (size or mtime)."""
    import hashlib
    conn = sqlite3.connect(str(db_path))
    try:
        parts = [str(get_meta(conn, "sources_stamp", ""))]
    finally:
        conn.close()
    for f in files:
        try:
            st = Path(f).stat()
            parts.append(f"{f}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{f}:absent")
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def db_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Row counts per table (0 for a table its importer did not create)."""
    out: dict = {"schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}
    for tbl in ("meshes", "textures", "audio", "clips", "folders", "collection"):
        try:
            out[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            out[tbl] = 0
    return out


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    import argparse

    parser = argparse.ArgumentParser(description="Pharos registry utilities")
    parser.add_argument("--db", default=config.DB_PATH)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="create/migrate the schema")
    group.add_argument("--stats", action="store_true", help="print registry statistics")
    args = parser.parse_args(argv)

    conn = init_db(args.db) if args.init else connect(args.db)
    if args.init:
        print(f"initialized {args.db} (schema v{SCHEMA_VERSION})")
    for key, value in db_stats(conn).items():
        print(f"  {key}: {value}")
    return 0


def anim_clip_count(conn: sqlite3.Connection, section: Optional[str],
                    packs: bool = False) -> int:
    """Clips (packs=True: their folders) of the configured animation
    section in the clips table (`section` = config.SECTIONS["animation"];
    doctor, the agent docs, MCP and the API count the same rows)."""
    sec = section or "Animation"
    sql = ("SELECT COUNT(DISTINCT folder) FROM clips" if packs
           else "SELECT COUNT(*) FROM clips")
    try:
        return conn.execute(sql + " WHERE folder = ? OR substr(folder, 1, ?) = ?",
                            (sec, len(sec) + 1, sec + "/")).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
