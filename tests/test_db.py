"""Registry schema roundtrip tests (runs under pytest or standalone)."""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from asset_service import db  # noqa: E402


def _populate(conn):
    db.upsert_asset(
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
            "host_tags": ["sofa", "furniture"],
            "content_hash": "hash-1",
        },
    )
    db.upsert_asset(
        conn,
        {
            "id": "pack-002",
            "name": "Walk Cycle Pack",
            "canonical_root": "X:/lib",
            "hero_file_path": "X:/lib/Anims/walk.fbx",
            "vendor": "Mixamo",
            "domain": "Human Animations",
            "sub_category": "Locomotion",
            "formats": ["fbx"],
            "content_hash": "hash-2",
        },
    )
    db.replace_asset_files(
        conn,
        "pack-001",
        [
            {"relative_path": "Archviz/Sofa/sofa.fbx", "file_type": "mesh", "file_size": 10},
            {"relative_path": "Archviz/Sofa/albedo.png", "file_type": "texture", "file_size": 5},
        ],
    )
    db.record_operation(
        conn, op_id="op-1", operation_type="prepare_blender_import",
        asset_ids=["pack-001"], plan_json={"collection": "Sofa"},
        status="awaiting_approval",
    )


def test_roundtrip():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "registry" / "assets.sqlite"
        conn = db.init_db(db_path)
        try:
            assert db_path.exists()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            _populate(conn)

            # init is idempotent
            conn2 = db.init_db(db_path)
            try:
                stats = db.db_stats(conn2)
                assert stats["assets"] == 2 and stats["asset_files"] == 2
                assert stats["operations"] == 1
                assert stats["domains"]["Archviz"] == 1

                # FTS search + structured filters
                assert [a["id"] for a in db.search_assets(conn2, "sofa")] == ["pack-001"]
                assert [a["id"] for a in db.search_assets(conn2, "walk", domain="Human Animations")] == ["pack-002"]
                assert db.search_assets(conn2, "sofa", style="Stylized") == []
                assert sorted(a["id"] for a in db.search_assets(conn2, fmt="fbx")) == ["pack-001", "pack-002"]
                assert db.search_assets(conn2, None, validated_only=True) == []

                # update reindexes FTS via trigger; delete removes via trigger
                db.upsert_asset(
                    conn2,
                    {"id": "pack-002", "name": "Run Cycle Pack", "canonical_root": "X:/lib",
                     "hero_file_path": "X:/lib/Anims/run.fbx", "domain": "Human Animations",
                     "content_hash": "hash-2"},
                )
                assert db.search_assets(conn2, "run") and not db.search_assets(conn2, "walk")
                conn2.execute("DELETE FROM assets WHERE id = 'pack-001'")
                assert db.search_assets(conn2, "sofa") == []

                details = db.get_asset_details(conn2, "pack-002")
                assert details["name"] == "Run Cycle Pack" and details["files"] == []
                assert db.get_asset_details(conn2, "missing") is None

                # upsert validation
                try:
                    db.upsert_asset(conn2, {"id": "x"})
                except ValueError:
                    pass
                else:
                    raise AssertionError("expected ValueError for missing required fields")
            finally:
                conn2.close()
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
