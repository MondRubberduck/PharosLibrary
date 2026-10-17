"""Registry schema tests (runs under pytest or standalone): the derived
tables, the meta stamps, the clip count, and the Kiosk-era tables gone."""

import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from asset_service import clips_import, db  # noqa: E402


def test_roundtrip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "registry" / "assets.sqlite"
        # a registry from before 1.0 still carries the pack tables
        db_path.parent.mkdir(parents=True)
        old = sqlite3.connect(str(db_path))
        old.executescript("""
        CREATE TABLE assets (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE asset_files (asset_id TEXT, relative_path TEXT);
        CREATE TABLE operations (id INTEGER PRIMARY KEY);
        CREATE TABLE collection (id INTEGER PRIMARY KEY, name TEXT);
        PRAGMA user_version = 2;
        """)
        old.commit()
        old.close()
        conn = db.init_db(db_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert not names & {"assets", "asset_files", "assets_fts", "operations"}, names
            assert "registry_meta" in names
            # the collection table (another importer's) was migrated, not dropped
            cols = {r[1] for r in conn.execute("PRAGMA table_info(collection)")}
            assert {"thumb_override", "meta"} <= cols, cols
            # init is idempotent
            db.init_db(db_path).close()
            # meta stamps
            assert db.get_meta(conn, "nope") is None and db.get_meta(conn, "nope", 7) == 7
            db.set_meta(conn, "k", 12)
            assert db.get_meta(conn, "k") == "12"
            db.touch_sources(db_path)
            stamp = db.get_meta(conn, "sources_stamp")
            assert stamp and db.get_meta(conn, "sources_stamp_at")
            sig1 = db.sources_signature(db_path, [db_path])
            assert sig1 == db.sources_signature(db_path, [db_path])
            db.touch_sources(db_path)
            assert db.sources_signature(db_path, [db_path]) != sig1, "a writer changes the signature"
            f = Path(tmp) / "source.csv"
            f.write_text("a", encoding="utf-8")
            sig2 = db.sources_signature(db_path, [f])
            f.write_text("ab", encoding="utf-8")
            assert db.sources_signature(db_path, [f]) != sig2, "a changed source file changes it"
            # a folder in the list: a file added directly in it moves its mtime
            import os
            import time
            d = Path(tmp) / "Sets"
            d.mkdir()
            os.utime(d, (1_700_000_000, 1_700_000_000))
            sig3 = db.sources_signature(db_path, [d])
            (d / "new_set.png").write_bytes(b"x")
            os.utime(d, (1_700_000_100, 1_700_000_100))
            assert db.sources_signature(db_path, [d]) != sig3, "a new file in a listed folder changes it"
            # stats: every derived table, 0 when its importer never ran
            stats = db.db_stats(conn)
            assert stats["schema_version"] == db.SCHEMA_VERSION
            assert stats["meshes"] == 0 and stats["clips"] == 0 and stats["collection"] == 0
            # the clip count reads the clips table for the configured section
            assert db.anim_clip_count(conn, "Animation") == 0
            clips_import.ensure_table(conn)
            conn.executemany(
                "INSERT INTO clips (rel, folder, name, ext, bone_names, facts, measured_at) "
                "VALUES (?,?,?,?,'[]','{}','t')",
                [("Mocap/Walks/w1.fbx", "Mocap/Walks", "w1", ".fbx"),
                 ("Mocap/Walks/w2.bvh", "Mocap/Walks", "w2", ".bvh"),
                 ("Mocap/loose.fbx", "Mocap", "loose", ".fbx"),
                 ("Other/x.fbx", "Other", "x", ".fbx")])
            conn.commit()
            assert db.anim_clip_count(conn, "Mocap") == 3
            assert db.anim_clip_count(conn, "Mocap", packs=True) == 2
            assert db.anim_clip_count(conn, "Animation") == 0
            assert db.db_stats(conn)["clips"] == 4
        finally:
            conn.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
