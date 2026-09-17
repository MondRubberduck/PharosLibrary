"""SQLite registry schema for the Pharos asset library.

Tables:
    assets       one row per logical asset pack; the entire virtual taxonomy
                 (style / domain / sub_category / confidence) lives here --
                 classification is virtual, files on disk are never touched
    asset_files  physical files belonging to a pack (meshes, anims, textures,
                 .uasset, licenses, archives)
    operations   audit trail for gated DCC actions (dry-run plans, approvals,
                 execution results, viewport screenshots)
    assets_fts   FTS5 full-text index over name/vendor/taxonomy/tags, kept in
                 sync with `assets` via triggers

Safety: this module only ever writes to the registry database under the
configured registry dir -- never to files under the canonical asset roots.

CLI:
    python db.py --db <registry>/assets.sqlite --init
    python db.py --db ... --stats
    python db.py --self-test
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from asset_service import config
from typing import Any, Iterable, Mapping, Optional

SCHEMA_VERSION = 5

STYLE_VALUES = ("Photorealistic", "Stylized", "Neutral/Unclassified")
DOMAIN_VALUES = (
    "Human Animations",
    "Archviz",
    "Props",
    "Environment",
    "Characters",
    "Textures_HDRI",
)
# Statuses an agent may set itself; 'human_verified' / 'validated_*' transitions
# are reserved for the human owner.
VALIDATION_STATUSES = ("auto_tagged", "needs_review", "flagged")
ASSET_STATUSES = ("indexed", "broken")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    hero_file_path TEXT NOT NULL,
    vendor TEXT,
    license_path TEXT,
    license_status TEXT,

    -- Virtual taxonomy (projection only; disk stays untouched)
    style TEXT CHECK (style IN ('Photorealistic', 'Stylized', 'Neutral/Unclassified')),
    domain TEXT NOT NULL,
    sub_category TEXT NOT NULL,
    confidence_score REAL,
    validation_status TEXT DEFAULT 'auto_tagged',

    -- Technical & DCC (host_* fields mirror the host library app's own
    -- categorization where one was imported from)
    formats TEXT,
    targets TEXT,
    technical_meta TEXT,
    host_category TEXT,
    host_tags TEXT,
    host_thumbnail_path TEXT,

    unreal_recipe TEXT,
    blender_recipe TEXT,
    status TEXT DEFAULT 'indexed',
    content_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER,
    sha256 TEXT
);

CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    asset_ids TEXT NOT NULL,
    plan_json TEXT,
    dry_run INTEGER DEFAULT 1,
    status TEXT,
    result_json TEXT,
    screenshot_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_assets_domain        ON assets(domain);
CREATE INDEX IF NOT EXISTS idx_assets_style         ON assets(style);
CREATE INDEX IF NOT EXISTS idx_assets_vendor        ON assets(vendor);
CREATE INDEX IF NOT EXISTS idx_assets_content_hash ON assets(content_hash);
CREATE INDEX IF NOT EXISTS idx_assets_status        ON assets(status);
CREATE INDEX IF NOT EXISTS idx_asset_files_asset   ON asset_files(asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_files_sha256  ON asset_files(sha256);
CREATE INDEX IF NOT EXISTS idx_operations_status   ON operations(status);

-- Full-text search over the taxonomy-facing columns. `assets` is a plain
-- rowid table (TEXT primary key), so external-content FTS5 can hang off rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
    name, vendor, style, domain, sub_category, host_tags, hero_file_path,
    content='assets', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS assets_fts_ai AFTER INSERT ON assets BEGIN
    INSERT INTO assets_fts(rowid, name, vendor, style, domain, sub_category,
                           host_tags, hero_file_path)
    VALUES (new.rowid, new.name, new.vendor, new.style, new.domain,
            new.sub_category, new.host_tags, new.hero_file_path);
END;

CREATE TRIGGER IF NOT EXISTS assets_fts_ad AFTER DELETE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, name, vendor, style, domain,
                           sub_category, host_tags, hero_file_path)
    VALUES ('delete', old.rowid, old.name, old.vendor, old.style, old.domain,
            old.sub_category, old.host_tags, old.hero_file_path);
END;

CREATE TRIGGER IF NOT EXISTS assets_fts_au AFTER UPDATE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, name, vendor, style, domain,
                           sub_category, host_tags, hero_file_path)
    VALUES ('delete', old.rowid, old.name, old.vendor, old.style, old.domain,
            old.sub_category, old.host_tags, old.hero_file_path);
    INSERT INTO assets_fts(rowid, name, vendor, style, domain, sub_category,
                           host_tags, hero_file_path)
    VALUES (new.rowid, new.name, new.vendor, new.style, new.domain,
            new.sub_category, new.host_tags, new.hero_file_path);
END;
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
    if current < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
        if "source_url" not in cols:
            conn.execute("ALTER TABLE assets ADD COLUMN source_url TEXT")
    if current < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
        if cols and "thumb_override" not in cols:
            conn.execute("ALTER TABLE collection ADD COLUMN thumb_override TEXT")
    if current < 4:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
        if cols and "meta" not in cols:
            conn.execute("ALTER TABLE collection ADD COLUMN meta TEXT")
    if current < 5:
        # kiosk_* -> host_*: drop the old-name FTS/triggers first (their SQL
        # text names both tables' columns), rename, recreate, rebuild index
        #
        # The kiosk_* identifiers below are the PRE-RENAME column names, read
        # from and written back to databases that were created before the
        # rename: the PRAGMA check and the three ALTERs must spell the old name
        # or the migration silently skips the columns an existing registry
        # still has. This is a required legacy-migration identifier -- an
        # allowed exception to the no-kiosk-names rule. Do not "clean" them.
        acols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
        if "kiosk_tags" in acols:
            conn.executescript("""
            DROP TRIGGER IF EXISTS assets_fts_ai;
            DROP TRIGGER IF EXISTS assets_fts_ad;
            DROP TRIGGER IF EXISTS assets_fts_au;
            DROP TABLE IF EXISTS assets_fts;
            """)
            conn.execute(
                "ALTER TABLE assets RENAME COLUMN kiosk_category "
                "TO host_category")
            conn.execute(
                "ALTER TABLE assets RENAME COLUMN kiosk_tags TO host_tags")
            conn.execute(
                "ALTER TABLE assets RENAME COLUMN kiosk_thumbnail_path "
                "TO host_thumbnail_path")
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO assets_fts(assets_fts) VALUES('rebuild')")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def upsert_asset(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> None:
    """Insert or update one logical asset pack row (id is the key)."""
    required = ("id", "name", "canonical_root", "hero_file_path", "domain", "content_hash")
    missing = [k for k in required if not asset.get(k)]
    if missing:
        raise ValueError(f"upsert_asset: missing required fields: {missing}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "id": asset["id"],
        "name": asset["name"],
        "canonical_root": asset["canonical_root"],
        "hero_file_path": asset["hero_file_path"],
        "vendor": asset.get("vendor"),
        "license_path": asset.get("license_path"),
        "license_status": asset.get("license_status"),
        "style": asset.get("style", "Neutral/Unclassified"),
        "domain": asset["domain"],
        "sub_category": asset.get("sub_category", "Uncategorized"),
        "confidence_score": asset.get("confidence_score"),
        "validation_status": asset.get("validation_status", "auto_tagged"),
        "formats": _json_or_none(asset.get("formats")),
        "targets": _json_or_none(asset.get("targets")),
        "technical_meta": _json_or_none(asset.get("technical_meta")),
        "host_category": asset.get("host_category"),
        "host_tags": _json_or_none(asset.get("host_tags")),
        "host_thumbnail_path": asset.get("host_thumbnail_path"),
        "unreal_recipe": asset.get("unreal_recipe"),
        "blender_recipe": asset.get("blender_recipe"),
        "status": asset.get("status", "indexed"),
        "content_hash": asset["content_hash"],
        "updated_at": now,
    }
    columns = ",".join(row)
    placeholders = ",".join(f":{c}" for c in row)
    # human verdicts survive re-indexing: anything the owner set while a pack
    # was 'human_verified' (domain/style/sub_category/status + validation) is
    # kept; classifier output only fills non-verified packs
    HUMAN = ("validation_status", "confidence_score", "domain", "style",
             "sub_category", "status")
    updates = []
    for c in row:
        if c in ("id", "created_at"):
            continue
        if c in HUMAN:
            updates.append(
                f"{c}=CASE WHEN assets.validation_status='human_verified' "
                f"THEN assets.{c} ELSE excluded.{c} END")
        else:
            updates.append(f"{c}=excluded.{c}")
    conn.execute(
        f"INSERT INTO assets ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {','.join(updates)}",
        row,
    )
    conn.commit()


def replace_asset_files(
    conn: sqlite3.Connection, asset_id: str, files: Iterable[Mapping[str, Any]]
) -> None:
    """Replace the file list of one asset pack (indexer refresh path)."""
    conn.execute("DELETE FROM asset_files WHERE asset_id = ?", (asset_id,))
    conn.executemany(
        "INSERT INTO asset_files (asset_id, relative_path, file_type, file_size, sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                asset_id,
                f["relative_path"],
                f["file_type"],
                f.get("file_size"),
                f.get("sha256"),
            )
            for f in files
        ],
    )
    conn.commit()


def record_operation(
    conn: sqlite3.Connection,
    op_id: str,
    operation_type: str,
    asset_ids: list[str],
    plan_json: Optional[Mapping[str, Any]] = None,
    dry_run: bool = True,
    status: Optional[str] = None,
    result_json: Optional[Mapping[str, Any]] = None,
    screenshot_path: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO operations (op_id, operation_type, asset_ids, plan_json, "
        "dry_run, status, result_json, screenshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            op_id,
            operation_type,
            json.dumps(asset_ids),
            _json_or_none(plan_json),
            1 if dry_run else 0,
            status,
            _json_or_none(result_json),
            screenshot_path,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 prefix query (no syntax injection)."""
    tokens = re.findall(r'[\w.-]+', query)
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


def search_assets(
    conn: sqlite3.Connection,
    query: Optional[str] = None,
    *,
    style: Optional[str] = None,
    domain: Optional[str] = None,
    sub_category: Optional[str] = None,
    fmt: Optional[str] = None,
    vendor: Optional[str] = None,
    validated_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search the registry. Free-text `query` hits the FTS5 index; the rest are
    structured filters. `validated_only` restricts to human-verified packs."""
    where: list[str] = []
    params: dict[str, Any] = {}

    if query and query.strip():
        fts = _fts_query(query)
        if fts:
            where.append(
                "(a.rowid IN (SELECT rowid FROM assets_fts WHERE assets_fts MATCH :fts) "
                "OR a.name LIKE :like OR a.hero_file_path LIKE :like)")
            params["fts"] = fts
            params["like"] = f"%{query.strip()}%"
    if style:
        where.append("a.style = :style")
        params["style"] = style
    if domain:
        where.append("a.domain = :domain")
        params["domain"] = domain
    if sub_category:
        where.append("a.sub_category = :sub_category")
        params["sub_category"] = sub_category
    if vendor:
        where.append("a.vendor = :vendor")
        params["vendor"] = vendor
    if fmt:
        where.append("a.formats LIKE :fmt")
        params["fmt"] = f'%"{fmt}"%'
    if validated_only:
        where.append("(a.validation_status = 'human_verified' OR a.status LIKE 'validated_%')")

    sql = "SELECT a.* FROM assets a"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.updated_at DESC LIMIT :limit"
    params["limit"] = max(1, min(int(limit), 500))
    return [dict(r) for r in conn.execute(sql, params)]


def get_asset_details(conn: sqlite3.Connection, asset_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    detail["files"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM asset_files WHERE asset_id = ? ORDER BY relative_path", (asset_id,)
        )
    ]
    return detail


def db_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str) -> Any:
        return conn.execute(sql).fetchone()[0]

    return {
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "assets": one("SELECT COUNT(*) FROM assets"),
        "asset_files": one("SELECT COUNT(*) FROM asset_files"),
        "operations": one("SELECT COUNT(*) FROM operations"),
        "domains": {
            r["domain"]: r["n"]
            for r in conn.execute(
                "SELECT domain, COUNT(*) AS n FROM assets GROUP BY domain ORDER BY n DESC"
            )
        },
        "styles": {
            r["style"]: r["n"]
            for r in conn.execute(
                "SELECT style, COUNT(*) AS n FROM assets GROUP BY style ORDER BY n DESC"
            )
        },
    }


# ---------------------------------------------------------------------------
# Self test (also validates FTS5 availability on this interpreter)
# ---------------------------------------------------------------------------

def self_test() -> None:
    conn = init_db(":memory:")

    upsert_asset(
        conn,
        {
            "id": "pack-001",
            "name": "Leather Sofa Set",
            "canonical_root": "X:/lib",
            "hero_file_path": "X:/lib/Archviz/Sofa/sofa.fbx",
            "vendor": "CGTrader",
            "style": "Photorealistic",
            "domain": "Archviz",
            "sub_category": "Furniture",
            "formats": ["fbx", "png"],
            "host_tags": ["sofa", "furniture", "livingroom"],
            "content_hash": "deadbeef",
        },
    )
    upsert_asset(
        conn,
        {
            "id": "pack-002",
            "name": "Mocap Walk Cycle Pack",
            "canonical_root": "X:/lib",
            "hero_file_path": "X:/lib/Anims/walk.fbx",
            "vendor": "Mixamo",
            "style": "Neutral/Unclassified",
            "domain": "Human Animations",
            "sub_category": "Locomotion",
            "formats": ["fbx", "bvh"],
            "host_tags": ["walk", "locomotion", "mocap"],
            "content_hash": "cafef00d",
        },
    )
    replace_asset_files(
        conn,
        "pack-001",
        [
            {"relative_path": "Archviz/Sofa/sofa.fbx", "file_type": "mesh", "file_size": 10},
            {"relative_path": "Archviz/Sofa/diffuse.png", "file_type": "texture", "file_size": 5},
        ],
    )
    record_operation(
        conn, op_id="op-1", operation_type="prepare_blender_import",
        asset_ids=["pack-001"], plan_json={"collection": "Sofa"}, status="awaiting_approval",
    )

    hits = search_assets(conn, "sofa")
    assert [h["id"] for h in hits] == ["pack-001"], hits
    hits = search_assets(conn, "walk", domain="Human Animations")
    assert [h["id"] for h in hits] == ["pack-002"], hits
    hits = search_assets(conn, "sofa", style="Stylized")
    assert hits == [], hits

    # taxonomy update must reindex FTS (trigger path)
    upsert_asset(conn, {"id": "pack-002", "name": "Run Cycle Pack",
                        "canonical_root": "X:/lib",
                        "hero_file_path": "X:/lib/Anims/run.fbx",
                        "domain": "Human Animations", "content_hash": "cafef00d",
                        "host_tags": ["run"]})
    assert search_assets(conn, "run") and not search_assets(conn, "walk")

    details = get_asset_details(conn, "pack-001")
    assert details is not None and len(details["files"]) == 2
    stats = db_stats(conn)
    assert stats["assets"] == 2 and stats["asset_files"] == 2 and stats["operations"] == 1

    conn.execute("DELETE FROM assets WHERE id = 'pack-001'")
    assert search_assets(conn, "sofa") == [], "FTS delete trigger did not fire"

    print("db.self_test: PASS "
          f"(sqlite {sqlite3.sqlite_version}, FTS5 OK, schema v{SCHEMA_VERSION})")


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    import argparse

    parser = argparse.ArgumentParser(description="Pharos registry utilities")
    parser.add_argument("--db", default=config.DB_PATH)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="create/migrate the schema")
    group.add_argument("--stats", action="store_true", help="print registry statistics")
    group.add_argument("--self-test", action="store_true", help="run in-memory roundtrip tests")
    args = parser.parse_args(argv)

    if args.self_test:
        self_test()
        return 0
    conn = init_db(args.db) if args.init else connect(args.db)
    if args.init:
        print(f"initialized {args.db} (schema v{SCHEMA_VERSION})")
    for key, value in db_stats(conn).items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
