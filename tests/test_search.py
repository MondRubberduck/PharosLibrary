"""Search, asset-fact and index-hygiene checks (runs under pytest or standalone).

What an agent sees when it asks the library: one word matcher behind every
section, best match first, short answers with the recipe on request, facts
measured from the files (texture pixels, clip skeletons), one row per file,
and no work whose result nothing reads. All but three checks fail on the
0.9.0 code; those three guard against regressions met while writing 0.10
(a control, an offline thumbnail choice, a whole-word classifier rule).

Run standalone: python -B tests/test_search.py
"""

import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fresh_install import _min_fbx  # noqa: E402  (shared FBX fixture)


# ---------------------------------------------------------------------------
# search contract: one matcher, relevance order, short items
# ---------------------------------------------------------------------------

# ---- search contract (0.10) -------------------------------------------------

_SEARCH_META = '{"stems":[],"themes":[],"facets":{}}'


def _search_registry(tmp, meshes, audio=(), clips=()):
    """A throw-away registry: `meshes` = (name, pack[, extra-dict]) rows,
    `audio` = (name, cat, dur) rows, `clips` = library-relative clip paths
    under an 'Animation' pack. Returns the db path."""
    import sqlite3
    from asset_service import audio_import, db, meshes_import
    dbp = str(Path(tmp) / "reg.sqlite")
    db.init_db(dbp).close()
    conn = sqlite3.connect(dbp)
    try:
        _fill_registry(conn, meshes, audio, clips, meshes_import, audio_import)
        conn.commit()
    finally:
        conn.close()        # an open handle blocks the temp-dir cleanup
    return dbp


def _fill_registry(conn, meshes, audio, clips, meshes_import, audio_import):
    conn.executescript(meshes_import.MESHES_DDL)
    conn.executescript(audio_import.AUDIO_DDL)
    audio_import.ensure_source_column(conn)
    # 'object_name' only where the schema has it (the 0.9 tables lack it,
    # and the fail-before run uses those)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
    has_obj = "object_name" in cols
    has_facts = "facts" in cols
    for i, row in enumerate(meshes):
        name, pack = row[0], row[1]
        x = row[2] if len(row) > 2 else {}
        dim = x.get("dim", 1)
        conn.execute(
            "INSERT INTO meshes (name,pack,source,kind,fbx,on_disk,bytes,"
            "triangles,vertices,submeshes,bbox_x,bbox_y,bbox_z,max_dim,"
            "materials,texture_count,texture_files,tags,meta,recipe"
            + (",object_name" if has_obj else "") + (",facts" if has_facts else "") + ") "
            "VALUES (?,?,?,?,?,1,1,?,3,0,?,?,?,?,?,0,'[]',?,?,?"
            + (",?" if has_obj else "") + (",?" if has_facts else "") + ")",
            (name, pack, x.get("source", "leartes"),
             x.get("kind", "StaticMesh"),
             x.get("fbx", f"x://{pack}/{name}.fbx"), x.get("tris", 10 + i),
             dim, dim, dim, dim,
             x.get("materials", "[]"), x.get("tags", "[]"),
             x.get("meta", _SEARCH_META), x.get("recipe"))
            + ((x.get("object_name"),) if has_obj else ())
            + ((json.dumps(x["facts"]) if x.get("facts") else None,) if has_facts else ()))
    for name, cat, dur in audio:
        conn.execute(
            "INSERT INTO audio (name,cat,sub,rel,ext,bytes,dur,sr,ch,"
            "playable,desc,tags,meta,source) VALUES (?,?,'',?,'.wav',1,?,"
            "48000,2,1,'','[]',?,'crawl')",
            (name, cat, f"{cat}/{name}.wav", dur, _SEARCH_META))
    if clips:
        conn.execute(
            "INSERT INTO assets (id,name,canonical_root,hero_file_path,"
            "domain,sub_category,content_hash) VALUES "
            "('pack::Animation/Moves','Moves','x','x','Human Animations',"
            "'Locomotion','h')")
        for rel in clips:
            conn.execute(
                "INSERT INTO asset_files (asset_id,relative_path,file_type,"
                "file_size) VALUES ('pack::Animation/Moves',?,'mesh',1)",
                (rel,))


def _names(resp, key="items"):
    return [i.get("name") for i in resp[key]]


def test_search_plural_alias_and_compound_words():
    """Both sides cut words the same way. 'benches' answered 0 for a library
    with 38 benches (the importer stemmed -es, the query did not), 'couch'
    found 1 model and 'sofa' 12, 'car' returned cardboard and carpets, and
    'chair -office' returned ONLY office chairs."""
    from asset_service import browse
    rows = [("Bench 01", "P1"), ("Shelf A", "P1"), ("Box 02", "P1"),
            ("Cardboard Box", "P1"), ("Carpet", "P1"),
            ("Damaged Car V1", "P2"), ("Sofa 01", "P2"),
            ("Street Lamp", "P2"), ("Streetlamp 02", "P3"),
            ("Trellis 01a", "P3"), ("Tree 02", "P3"),
            ("Office Chair 01a", "P3"), ("Chair 02", "P3"),
            ("Armchair 03", "P3")]
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows)
        try:
            def q(text):
                return browse.api_meshes({"q": [text], "size": ["50"]})
            got = q("benches")
            assert set(_names(got)) == {"Bench 01"} and got["mode"] == "and", \
                f"benches -> {_names(got)} ({got['mode']})"
            assert set(_names(q("shelves"))) == {"Shelf A"}, _names(q("shelves"))
            assert set(_names(q("boxes"))) == {"Box 02", "Cardboard Box"}, \
                _names(q("boxes"))
            assert set(_names(q("car"))) == {"Damaged Car V1"}, \
                f"car -> {_names(q('car'))} (cardboard / carpet are not cars)"
            assert _names(q("couch")) == ["Sofa 01"], _names(q("couch"))
            want = {"Street Lamp", "Streetlamp 02"}
            assert set(_names(q("streetlamp"))) == want, _names(q("streetlamp"))
            assert set(_names(q("street lamp"))) == want, _names(q("street lamp"))
            assert set(_names(q("tree"))) == {"Tree 02"}, \
                f"tree -> {_names(q('tree'))} (a trellis is not a tree)"
            assert set(_names(q("chair -office"))) == {"Chair 02", "Armchair 03"}, \
                _names(q("chair -office"))
            chairs = q("chair")["items"]
            assert chairs[-1]["name"] == "Armchair 03" and \
                chairs[-1]["tier"] == 2 and chairs[0]["tier"] == 1, \
                [(i["name"], i["tier"]) for i in chairs]
        finally:
            browse._DB_PATH = old


def test_search_orders_by_relevance_and_spreads_packs():
    """Mesh answers were sorted by pack name: a weak match from an early
    pack came before the exact name, and one pack filled the first page."""
    from asset_service import browse
    weak = '{"stems":["street"],"themes":[],"facets":{}}'
    rows = [("Prop Lamp A", "AAA", {"meta": weak}),
            ("Street Lamp", "ZZZ"),
            # names of EQUAL rank: without the spread, one pack fills the page
            ("Wall 01", "Alpha"), ("Wall 02", "Alpha"), ("Wall 03", "Beta")]
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows)
        try:
            got = browse.api_meshes({"q": ["street lamp"]})["items"]
            assert [i["name"] for i in got] == ["Street Lamp", "Prop Lamp A"], \
                f"exact name must come first: {[(i['name'], i['tier']) for i in got]}"
            walls = browse.api_meshes({"q": ["wall"], "size": ["2"]})["items"]
            assert {i["pack"] for i in walls} == {"Alpha", "Beta"}, \
                f"page 1 came from one pack: {[(i['name'], i['pack']) for i in walls]}"
            assert len(walls) == 2, f"size=2 returned {len(walls)} items"
        finally:
            browse._DB_PATH = old


def test_search_items_are_short_and_recipe_comes_on_request():
    """A mesh in a search answer carried its whole material recipe (60-80%
    of the bytes; one page of 8 reached 569 KB). Lists are short now; the
    recipe is fetched for the meshes that were chosen, by a stable key."""
    from asset_service import browse
    recipe = json.dumps({"slots": [{"slot": 0, "maps": []}], "resolved": True,
                         "primary_slot": 0,
                         "primary": {"albedo": "x/crate_alb.png"}})
    rows = [("Crate 01", "Kit", {"recipe": recipe, "materials": '["M_Wood"]',
                                 "fbx": "x://Kit/SM_Crate_01.fbx"}),
            ("Lid A", "Kit", {"fbx": "x://Kit/all.blend"}),
            ("Lid B", "Kit", {"fbx": "x://Kit/all.blend"})]
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows)
        try:
            item = browse.api_meshes({"q": ["crate"]})["items"][0]
            assert "recipe" not in item and "hero_textures" not in item, \
                f"list item still carries the recipe: {sorted(item)}"
            assert item.get("key") and item.get("wired") is True, item
            assert len(json.dumps(item)) < 700, len(json.dumps(item))
            full = browse.api_meshes({"q": ["crate"], "detail": ["1"]})["items"][0]
            assert full["recipe"]["resolved"] and \
                full["hero_textures"] == {"albedo": "x/crate_alb.png"} and \
                full["materials"] == ["M_Wood"] and full["fbx"], full
            fn = getattr(browse, "api_meshes_item", None)
            assert fn is not None, "no /api/meshes/item"
            got = fn({"key": [item["key"], "nope/missing.fbx"]})
            assert got["items"][0]["recipe"]["resolved"] and \
                got["missing"] == ["nope/missing.fbx"], got
            assert fn({"key": ["nope"]}).get("error") == "not found"
            # objects inside one file: distinct keys, exact object name served
            lids = browse.api_meshes({"q": ["lid"]})["items"]
            keys = {i["key"] for i in lids}
            assert len(keys) == 2 and all("#" in k for k in keys) and \
                all(i.get("container") for i in lids), lids
            one = browse.api_meshes({"q": ["lid"], "size": ["1"]})
            assert len(one["items"]) == 1, f"size=1 gave {len(one['items'])}"
        finally:
            browse._DB_PATH = old


def test_search_says_what_it_ignored():
    """A mistyped or unsupported filter returned the UNFILTERED result as
    if it had applied (max_dur=3 gave the same sounds as no filter)."""
    from asset_service import browse
    rows = [("Crate 01", "FixturePack", {"tris": 50}),
            ("Crate 02", "FixturePack", {"tris": 900}),
            ("Barrel 01", "FixturePack")]
    audio = [("Door Creak Short", "Foley", 2.0),
             ("Door Creak Long", "Foley", 9.0)]
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows, audio)
        try:
            w = browse.api_meshes({"q": ["crate"], "max_dmi": ["2"]}).get("warnings")
            assert w and "max_dmi" in " ".join(w), f"unknown parameter not reported: {w}"
            w = browse.api_meshes({"q": ["crate"], "max_tri": ["abc"]}).get("warnings")
            assert w and "max_tri" in " ".join(w), f"unreadable value not reported: {w}"
            r = browse.api_meshes({"pack": ["NoSuchPack"]})
            assert r["total"] == 0 and "NoSuchPack" in " ".join(r.get("warnings") or []), r
            assert browse.api_meshes({"q": ["crate"], "pack": ["fixturepack"]})["total"] == 2, \
                "pack= must not depend on letter case"
            desc = browse.api_meshes({"q": ["crate"], "sort": ["-triangles"]})["items"]
            assert [i["name"] for i in desc] == ["Crate 02", "Crate 01"], desc
            w = browse.api_meshes({"q": ["crate"], "sort": ["bogus"]}).get("warnings")
            assert w and "bogus" in " ".join(w), w
            near = browse.api_meshes({"q": ["barel"]}).get("did_you_mean") or {}
            assert "barrel" in (near.get("barel") or []), f"no suggestion: {near}"
            short = browse.api_audio_items({"q": ["door creak"], "max_dur": ["3"]})
            assert _names(short) == ["Door Creak Short"], \
                f"max_dur ignored: {_names(short)}"
        finally:
            browse._DB_PATH = old


def test_themes_match_whole_words_and_not_the_pack_name():
    """Themes were assigned with `keyword in text` on the mesh name PLUS
    the pack name: 'bus' tagged bushes as vehicles (158 of 177 vehicle
    models were bushes, cardboard and carpets) and a pack whose name
    contains 'house' or 'tower' made every one of its meshes a building."""
    from asset_service import meshes_import

    def themes_ctx(name, pack, **extra):
        e = {"name": name, "pack": pack, "bbox_m": [1, 1, 1], **extra}
        rec = meshes_import._record(e, dict(meshes_import.EMPTY_RECIPE),
                                    None, None)
        meta = json.loads(rec["meta"])
        return set(meta["themes"]), set(meta.get("ctx") or []), rec

    th, _, _ = themes_ctx("SM_Bushes_01a", "StoreHouseKit")
    assert "vehicle" not in th and "building" not in th and "nature" in th, th
    assert "vehicle" not in themes_ctx("SM_Cardboard_Box", "X")[0]
    assert "vehicle" in themes_ctx("SM_Damaged_Car_V1", "X")[0]
    th, _, _ = themes_ctx("SM_Crate", "WatchTowerPack")
    assert "medieval" not in th and "building" not in th, \
        f"pack name leaked object themes: {th}"
    assert "medieval" in themes_ctx("SM_Wall_01", "Medieval_Hamlet")[0]
    th, ctx, rec = themes_ctx(
        "SM_Wall_A", "NeonPunk", materials=["MI_Rust_01a"],
        asset_path="/Game/NeonPunk/Meshes/Foliage/SM_Wall_A")
    assert {"neonpunk", "rust", "foliage"} <= ctx, \
        f"pack / material / folder words not searchable: {sorted(ctx)}"
    assert "nature" in th, f"vendor folder 'Foliage' gave no theme: {th}"
    assert rec.get("object_name") == "SM_Wall_A", rec.get("object_name")


def test_audio_themes_come_from_the_file_not_the_category_keywords():
    """Every sound of a category inherited ALL keywords of that category:
    q=horror returned 550 sounds, about 100 of them horror sounds."""
    import sqlite3
    from asset_service import audio_import
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "library_files.jsonl").write_text(
            json.dumps({"p": "Nature/Wind/wind_gust_01.wav", "cat": "Nature",
                        "sub": "Wind", "ext": ".wav", "bytes": 1, "dur": 2.0})
            + "\n" +
            json.dumps({"p": "Nature/Wind/creepy_horror_drone.wav",
                        "cat": "Nature", "sub": "Wind", "ext": ".wav",
                        "bytes": 1, "dur": 2.0}) + "\n", encoding="utf-8")
        (root / "library_index.json").write_text(json.dumps({"categories": {
            "Nature": {"description": "d", "keywords": [
                "horror", "creepy", "gore", "animal", "bird", "dog"]}}}),
            encoding="utf-8")
        # the importer reads the crawl files under the root it is given
        dbp = str(root / "a.sqlite")
        assert audio_import.import_audio(dbp, root=root) == 2
        conn = sqlite3.connect(dbp)
        got = {n: set(json.loads(m)["themes"]) for n, m in
               conn.execute("SELECT name, meta FROM audio")}
        conn.close()
    wind = next(v for k, v in got.items() if "ind" in k.lower() and "orror" not in k.lower())
    assert "horror" not in wind and "animals" not in wind, \
        f"a wind gust inherited its category's keywords: {wind}"
    drone = next(v for k, v in got.items() if "orror" in k.lower())
    assert "horror" in drone, f"a sound named horror lost its theme: {drone}"


def test_anim_search_needs_every_word_and_reads_folders():
    """Animation search matched ANY word as a substring and sorted by
    name: 'sit down chair' returned ledge-climbing clips first, with no
    mode / exact disclosure, against the documented behaviour."""
    from asset_service import browse, config
    clips = ["Animation/Moves/sit-chair-f.fbx",
             "Animation/Moves/walk-loop-f.fbx",
             "Animation/Moves/walk-hurry-m.fbx",
             "Animation/Moves/UN05/Root/char_climb_exit_down.fbx",
             "Animation/Moves/UN05/Physics/char_climb_exit_down.fbx",
             "Animation/Moves/hesitate-m.fbx"]
    old, old_sec = browse._DB_PATH, config.SECTIONS.get("animation")
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, [], clips=clips)
        config.SECTIONS["animation"] = "Animation"
        try:
            r = browse.api_anim_clips({"q": ["female walk"]})
            assert r.get("mode") == "and" and r.get("exact") == 1 and \
                r["clips"][0]["rel"].endswith("walk-loop-f.fbx"), \
                f"mode={r.get('mode')} exact={r.get('exact')} " \
                f"{[c['rel'] for c in r['clips']]}"
            r = browse.api_anim_clips({"q": ["sit down chair"]})
            assert r.get("mode") == "or-fallback" and r.get("exact") == 0 and \
                r["clips"][0]["rel"].endswith("sit-chair-f.fbx"), \
                f"{r.get('mode')} {[c['rel'] for c in r['clips']]}"
            r = browse.api_anim_clips({"q": ["sit"]})
            assert [c["rel"].rsplit("/", 1)[-1] for c in r["clips"]
                    if c["tier"] < 3] == ["sit-chair-f.fbx"], \
                f"'sit' must not match 'hesitate': {[c['rel'] for c in r['clips']]}"
            r = browse.api_anim_clips({"q": ["root climb"]})
            assert r.get("exact") == 1 and "/Root/" in r["clips"][0]["rel"], \
                f"folder words not searchable: {[c['rel'] for c in r['clips']]}"
        finally:
            browse._DB_PATH = old
            config.SECTIONS["animation"] = old_sec


def test_mcp_and_http_share_one_search():
    """The MCP server had its own matcher (raw substring over the stored
    JSON): 'crates' found 0 where HTTP found 63, and the word 'themes'
    matched EVERY mesh because it is a key of the stored metadata."""
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "pharos_mcp_search", str(REPO / "service" / "pharos_mcp_server.py"))
        srv = ilu.module_from_spec(spec)
        spec.loader.exec_module(srv)
    except Exception as exc:                       # noqa: BLE001
        assert os.environ.get("PHAROS_REQUIRE_MCP") != "1", \
            f"PHAROS_REQUIRE_MCP=1 but the MCP server did not load: {exc}"
        print(f"SKIP (no mcp package on this machine): {exc}")
        return
    from asset_service import browse
    recipe = json.dumps({"slots": [], "resolved": True, "primary_slot": None,
                         "primary": {"albedo": "x/a.png", "mask": "x/m.png"}})
    rows = [("Crate 01", "Kit", {"recipe": recipe}), ("Barrel 01", "Kit")]
    clips = ["Animation/Moves/walk-loop-f.fbx"]
    orig = srv.config.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        srv.config.DB_PATH = _search_registry(tmp, rows, clips=clips)
        old_cfg = srv.config.is_configured
        srv.config.is_configured = lambda: True
        old_sec = srv.config.SECTIONS.get("animation")
        srv.config.SECTIONS["animation"] = "Animation"
        try:
            got = json.loads(srv.pharos_search_meshes(query="crates"))
            assert got["total"] == 1 and got["items"][0]["name"] == "Crate 01", got
            assert json.loads(srv.pharos_search_meshes(query="themes"))["total"] == 0, \
                "the word 'themes' still matches the stored metadata keys"
            http = browse.api_meshes  # same function, same answer
            before = browse._DB_PATH
            browse._DB_PATH = srv.config.DB_PATH
            try:
                assert http({"q": ["crates"]})["items"][0]["key"] == \
                    got["items"][0]["key"]
            finally:
                browse._DB_PATH = before
            tool = getattr(srv, "pharos_get_meshes", None)
            assert tool is not None, "no MCP tool for the recipe of chosen meshes"
            full = json.loads(tool([got["items"][0]["key"]]))["items"][0]
            assert "albedo" in full["hero_textures"] and \
                "mask" not in full["hero_textures"], full["hero_textures"]
            anim = getattr(srv, "pharos_search_animations", None)
            assert anim is not None, "no MCP tool for animation clips"
            assert json.loads(anim(query="walk"))["total"] >= 1
        finally:
            srv.config.DB_PATH = orig
            srv.config.is_configured = old_cfg
            srv.config.SECTIONS["animation"] = old_sec


def test_item_answers_carry_no_copy_and_use_error_codes():
    """One-record answers repeated every field under `item` (half the
    bytes) and reported a missing record as HTTP 200."""
    from asset_service import browse
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(
            tmp, [], audio=[("Beep", "UI", 1.0)])
        try:
            rid = browse.api_audio_items({"q": ["beep"]})["items"][0]["id"]
            one = browse.api_audio_item({"id": [str(rid)]})
            assert one.get("name") == "Beep" and "item" not in one, sorted(one)
        finally:
            browse._DB_PATH = old
    sent = []

    class _H:
        def _json(self, payload, code=200):
            sent.append(code)
    fn = getattr(browse.Handler, "_json_item", None)
    assert fn is not None, "no status mapping for one-record answers"
    fn(_H(), {"error": "not found"})
    fn(_H(), {"error": "bad id"})
    fn(_H(), {"id": 1})
    assert sent == [404, 400, 200], sent


def test_purchase_thumbnail_choice_must_be_a_file():
    """One registry carried a status word in thumb_override on every
    purchase row (a table copy that shifted columns); the importer
    preserved it on every re-import and real choices were gone."""
    import sqlite3
    from asset_service import collection_import, db
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv = tmp / "collection.csv"
        csv.write_text("Name,Product URL,Price (USD)\n"
                       "Mill A,https://example.com/a,1\n"
                       "Mill B,https://example.com/b,2\n", encoding="utf-8")
        img = tmp / "pick.jpg"
        img.write_bytes(b"\xff\xd8\xff")
        dbp = str(tmp / "c.sqlite")
        db.init_db(dbp).close()
        assert collection_import.import_collection(dbp, csv_path=csv) == 2
        conn = sqlite3.connect(dbp)
        conn.execute("UPDATE collection SET thumb_override='owned-not-downloaded' "
                     "WHERE name='Mill A'")
        conn.execute("UPDATE collection SET thumb_override=? WHERE name='Mill B'",
                     (str(img),))
        conn.commit()
        conn.close()
        collection_import.import_collection(dbp, csv_path=csv)
        conn = sqlite3.connect(dbp)
        got = dict(conn.execute("SELECT name, thumb_override FROM collection"))
        conn.close()
    assert got["Mill A"] is None, f"a status word survived as a thumbnail: {got['Mill A']!r}"
    assert got["Mill B"] == str(img), "a real choice was dropped"


def test_pack_list_links_the_purchase_with_its_store_images():
    """The store images of a purchase are the cheapest way to see what a
    pack looks like; the pack list did not link them."""
    import sqlite3
    from asset_service import browse, collection_import
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Lib" / "PackA"
        rows = [("Crate", "PackA", {"fbx": str(lib / "Exports" / "Crate.fbx")})]
        dbp = _search_registry(tmp, rows)
        conn = sqlite3.connect(dbp)
        conn.executescript(collection_import.COLLECTION_DDL)
        conn.execute(
            "INSERT INTO collection (name, folder, image_count, availability,"
            " asset_path) VALUES ('Pack A (store)', 'x', 7, 'local', ?)",
            (str(lib),))
        conn.commit()
        conn.close()
        browse._DB_PATH = dbp
        try:
            pack = browse.api_meshes_packs()["packs"][0]
        finally:
            browse._DB_PATH = old
    assert [p.get("image_count") for p in pack.get("purchases") or []] == [7], pack


def _drive(path, method="GET", body=b""):
    """One request through the real Handler, in-process (no socket):
    returns (status, parsed JSON or raw bytes)."""
    import io
    from email.message import Message
    from asset_service import browse
    h = browse.Handler.__new__(browse.Handler)
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.path, h.command, h.request_version = path, method, "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.server = type("S", (), {"server_address": ("127.0.0.1", 8765)})()
    hd = Message()
    hd["Host"] = "127.0.0.1:8765"
    if body:
        hd["Content-Type"] = "application/json"
        hd["Content-Length"] = str(len(body))
    h.headers = hd
    h.log_message = lambda *a, **k: None
    (h.do_GET if method == "GET" else h.do_POST)()
    raw = h.wfile.getvalue()
    status = int(raw.split(b" ", 2)[1])
    data = raw.split(b"\r\n\r\n", 1)[1]
    try:
        return status, json.loads(data)
    except ValueError:
        return status, data


def _with_registry(rows, fn, audio=(), clips=()):
    from asset_service import browse
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows, audio, clips)
        try:
            return fn(browse)
        finally:
            browse._DB_PATH = old


def test_search_codes_connectives_and_camelcase():
    """Review findings: 'lod0' was searched as 'lod' (LOD1 rows came back as
    exact matches), 'v2' and 't34' lost their letter or digit, 'door with
    frame' required the word 'with', 'PropFuseBox' found nothing, 'sci fi
    door' fell back to partial matches, and 'Tire A C' answered 'ac'."""
    scifi = '{"stems":[],"themes":["scifi"],"facets":{}}'
    rows = [("Street Lamp LOD0", "P"), ("Street Lamp LOD1", "P"),
            ("Damaged Car V1", "P"), ("Damaged Car V2", "P"),
            ("Part34", "P"), ("Tire A C", "P"), ("AC Unit 01a", "P"),
            ("Prop Fuse Box A grp", "Kit",
             {"object_name": "KB3D_XYZ_PropFuseBox_A_grp"}),
            ("Wagon", "P", {"object_name": "SM_Wagon"}),
            ("Door Frame 01", "P"), ("Door", "Q", {"meta": scifi}),
            ("Bldg Cafe A grp", "P"), ("Cave Rock", "P")]

    def run(browse):
        def names(q, **kw):
            p = {"q": [q], "size": ["50"], **{k: [v] for k, v in kw.items()}}
            return [i["name"] for i in browse.api_meshes(p)["items"]]
        assert names("street lamp lod0") == ["Street Lamp LOD0"], names("street lamp lod0")
        assert names("damaged car v2") == ["Damaged Car V2"], names("damaged car v2")
        assert names("t34") == [], names("t34")
        assert names("ac") == ["AC Unit 01a"], f"'Tire A C' is not 'ac': {names('ac')}"
        assert names("PropFuseBox") == ["Prop Fuse Box A grp"], names("PropFuseBox")
        r = browse.api_meshes({"q": ["KB3D_XYZ_PropFuseBox_A_grp"]})
        assert r["mode"] == "and" and r["items"][0]["tier"] == 1, r
        assert names("SM_Wagon") == ["Wagon"], names("SM_Wagon")
        r = browse.api_meshes({"q": ["door with frame"]})
        assert r["mode"] == "and" and [i["name"] for i in r["items"]] == ["Door Frame 01"], r
        r = browse.api_meshes({"q": ["sci fi door"]})
        assert r["mode"] == "and" and "Door" in [i["name"] for i in r["items"]], r
        assert "Bldg Cafe A grp" not in names("caves"), "'caves' is not 'cafe'"
        w = browse.api_meshes({"q": ["x"]}).get("warnings") or []
        assert any("no usable search word" in x for x in w), w
    _with_registry(rows, run)


def test_part_of_needs_three_letters_left_over():
    """'round' matched 'Ground', 'actor' 'Factory', 'cabin' 'Cabinet' and,
    in clip titles, 'male' matched 'female'. Listed compounds still reach
    their parts ('door' finds 'Doorway')."""
    rows = [("Ground 01", "P"), ("Round Table", "P"), ("Factory", "P"),
            ("Cabinet", "P"), ("Armchair", "P"), ("Wall Doorway01", "P")]
    clips = ["Animation/Moves/idle-f.fbx", "Animation/Moves/idle-m.fbx"]

    def run(browse):
        from asset_service import config
        old_sec = config.SECTIONS.get("animation")
        config.SECTIONS["animation"] = "Animation"
        try:
            def names(q):
                return [i["name"] for i in
                        browse.api_meshes({"q": [q], "size": ["50"]})["items"]]
            assert names("round") == ["Round Table"], names("round")
            assert names("actor") == [], names("actor")
            assert names("cabin") == [], names("cabin")
            assert names("chair") == ["Armchair"], names("chair")
            assert names("door") == ["Wall Doorway01"], names("door")
            r = browse.api_anim_clips({"q": ["male idle"]})
            got = [c["rel"] for c in r["clips"] if c["tier"] < 3]
            assert got == ["Animation/Moves/idle-m.fbx"], got
        finally:
            config.SECTIONS["animation"] = old_sec
    _with_registry(rows, run, clips=clips)


def test_exclusions_hyphenated_alone_and_in_the_related_tail():
    """'chair -sci-fi' made 'fi' a REQUIRED word, an exclusion-only query
    answered 0, and the animation tail ignored exclusions."""
    scifi = '{"stems":[],"themes":["scifi"],"facets":{}}'
    rows = [("Chair 01", "P"), ("Chair 02", "P", {"meta": scifi}),
            ("Office Desk", "P"), ("Lamp", "P")]
    clips = ["Animation/Moves/walk-loop.fbx", "Animation/Moves/run-fast.fbx",
             "Animation/Moves/jog-slow.fbx"]

    def run(browse):
        from asset_service import config
        old_sec = config.SECTIONS.get("animation")
        config.SECTIONS["animation"] = "Animation"
        try:
            r = browse.api_meshes({"q": ["chair -sci-fi"]})
            assert r["mode"] == "and" and [i["name"] for i in r["items"]] == ["Chair 01"], r
            r = browse.api_meshes({"q": ["-office"]})
            assert sorted(i["name"] for i in r["items"]) == ["Chair 01", "Chair 02", "Lamp"], r
            r = browse.api_anim_clips({"q": ["walk -run"]})
            assert not any("run" in c["rel"] for c in r["clips"]), \
                [c["rel"] for c in r["clips"]]
        finally:
            config.SECTIONS["animation"] = old_sec
    _with_registry(rows, run, clips=clips)


def test_filter_values_are_honest_everywhere():
    """A value an endpoint could not use came back UNFILTERED or EMPTY with
    no word: avail=owned-not-downloaded (what every item shows), textures=
    true, theme=Vehicle, cat=alarms, packs=<folder>."""
    import sqlite3
    from asset_service import collection_import
    vehicle = '{"stems":[],"themes":["vehicle"],"facets":{}}'
    rows = [("Car", "P", {"meta": vehicle, "dim": 4.0}),
            ("Box", "P", {"dim": 0.5})]
    audio = [("Siren", "Alarms", 2.0), ("Rain", "Weather", 9.0)]
    clips = ["Animation/Moves/walk.fbx"]

    def run(browse):
        from asset_service import config
        conn = sqlite3.connect(browse._DB_PATH)
        conn.executescript(collection_import.COLLECTION_DDL)
        conn.execute("INSERT INTO collection (name, store, availability) "
                     "VALUES ('A','FAB','local'), ('B','FAB','owned-not-downloaded')")
        conn.commit()
        conn.close()
        old_sec = config.SECTIONS.get("animation")
        config.SECTIONS["animation"] = "Animation"
        try:
            r = browse.api_collection_items({"avail": ["owned-not-downloaded"]})
            assert [i["name"] for i in r["items"]] == ["B"], r
            r = browse.api_collection_items({"avail": ["bogus"]})
            assert r["total"] == 2 and r.get("warnings"), r
            r = browse.api_collection_items({"store": ["fab"]})
            assert r["total"] == 2, r
            r = browse.api_meshes({"theme": ["Vehicle"]})
            assert [i["name"] for i in r["items"]] == ["Car"], r
            r = browse.api_meshes({"theme": ["nosuch"]})
            assert r["total"] == 0 and r.get("warnings"), r
            r = browse.api_meshes({"textures": ["maybe"]})
            assert any("textures" in w for w in r.get("warnings") or []), r
            r = browse.api_audio_items({"cat": ["alarms"]})
            assert [i["name"] for i in r["items"]] == ["Siren"], r
            r = browse.api_anim_clips({"packs": ["Animation/Moves"]})
            assert r["total"] == 1 and not r.get("warnings"), r
            r = browse.api_anim_clips({"packs": ["Animation/Nope"]})
            assert r["total"] == 0 and r.get("warnings"), r
            # documented: max_dim = largest first, -max_dim = smallest first
            desc = browse.api_meshes({"sort": ["max_dim"]})["items"]
            asc = browse.api_meshes({"sort": ["-max_dim"]})["items"]
            assert [i["name"] for i in desc] == ["Car", "Box"], desc
            assert [i["name"] for i in asc] == ["Box", "Car"], asc
        finally:
            config.SECTIONS["animation"] = old_sec
    _with_registry(rows, run, audio=audio, clips=clips)


def test_full_records_have_a_budget_and_say_what_they_left_out():
    """60 keys gave 50 records and silently dropped 10; a page of buildings
    with detail=1 was ~100k tokens; a key cut at its '#' (a client dropped
    the 'fragment') answered a bare 404."""
    big = json.dumps({"slots": [{"slot": 0, "maps": [
        {"role": "albedo", "file": "x/" + "t" * 4000 + ".png"}]}],
        "resolved": True, "primary_slot": 0, "primary": {"albedo": "x/a.png"}})
    rows = [(f"Bldg {i:02d}", "Kit", {"recipe": big}) for i in range(60)]
    rows += [("Lid A", "Kit", {"fbx": "x://Kit/all.blend", "object_name": "Lid_A"}),
             ("Lid B", "Kit", {"fbx": "x://Kit/all.blend", "object_name": "Lid_B"})]

    def run(browse):
        keys = [i["key"] for i in browse.api_meshes(
            {"q": ["bldg"], "size": ["60"]})["items"]]
        r = browse.api_meshes_item({"key": keys})
        got = len(r["items"]) + len(r.get("not_returned") or [])
        assert got == 60 and r.get("not_returned") and r.get("warnings"), \
            {k: (v if k != "items" else len(v)) for k, v in r.items()}
        assert len(json.dumps(r["items"])) < 100_000, len(json.dumps(r["items"]))
        d = browse.api_meshes({"q": ["bldg"], "size": ["40"], "detail": ["1"]})
        assert d.get("not_returned") and len(json.dumps(d["items"])) < 100_000
        lids = sorted(i["key"] for i in
                      browse.api_meshes({"q": ["lid"]})["items"])
        file = lids[0].split("#")[0]
        cut = browse.api_meshes_item({"key": [file]})
        assert cut.get("error") == "not found" and \
            sorted(cut["candidates"][file]) == lids, cut
    _with_registry(rows, run)


def test_routes_status_codes_and_nested_pack_ids():
    """Through the real request handler: one-record routes answer 404 / 400,
    the pack page accepts pack ids below a section folder (79 of 83 packs
    were 'bad pack id'), and an un-encoded container key gets candidates.
    The animation page labels tier 3, not tier 2, as 'related'."""
    import sqlite3
    rows = [("Lid A", "Kit", {"fbx": "x://Kit/all.blend", "object_name": "Lid_A"}),
            ("Lid B", "Kit", {"fbx": "x://Kit/all.blend", "object_name": "Lid_B"})]

    def run(browse):
        conn = sqlite3.connect(browse._DB_PATH)
        conn.execute(
            "INSERT INTO assets (id,name,canonical_root,hero_file_path,"
            "domain,sub_category,content_hash) VALUES ('pack::Props/Crates',"
            "'Crates','x','x','Props','General','h')")
        conn.commit()
        conn.close()
        assert _drive("/api/audio/item?id=999")[0] == 404
        assert _drive("/api/audio/item?id=abc")[0] == 400
        assert _drive("/api/meshes/item")[0] == 400
        from urllib.parse import quote
        key = sorted(i["key"] for i in
                     browse.api_meshes({"q": ["lid"]})["items"])[0]
        st, body = _drive("/api/meshes/item?key=" + key)    # '#' not encoded
        assert st == 404 and body.get("candidates"), (st, body)
        st, body = _drive("/api/meshes/item?key=" + quote(key, safe=""))
        assert st == 200 and body["items"][0]["name"] == "Lid A", (st, body)
        st, body = _drive("/api/pack_files?id=pack::Props/Crates")
        assert body.get("error") != "bad pack id", body
        assert "c.tier===3" in browse.ANIM_PAGE and "c.tier===2" not in browse.ANIM_PAGE
    _with_registry(rows, run)


def test_search_behaviours_the_review_could_break_silently():
    """Mutations that passed every check: context-only hits, OR rows
    replacing an AND answer, kind= letter case, -ies plurals, `wired`."""
    ctx = '{"stems":[],"themes":[],"facets":{},"ctx":["neonpunk","rust"]}'
    recipe = json.dumps({"slots": [], "resolved": True, "primary_slot": None,
                         "primary": {"albedo": "x/a.png"}})
    rows = [("Wall A", "NeonPunk", {"meta": ctx}), ("Battery", "P"),
            ("Wood Crate", "P", {"recipe": recipe}), ("Crate", "P"),
            ("Wood Plank", "P"), ("Skel", "P", {"kind": "SkeletalMesh"})]

    def run(browse):
        def names(q, **kw):
            p = {"q": [q], **{k: [v] for k, v in kw.items()}}
            return [i["name"] for i in browse.api_meshes(p)["items"]]
        assert names("neonpunk") == ["Wall A"] and names("rust") == ["Wall A"]
        assert names("batteries") == ["Battery"], names("batteries")
        r = browse.api_meshes({"q": ["wood crate"]})
        assert r["mode"] == "and" and [i["name"] for i in r["items"]] == ["Wood Crate"], r
        assert names("skel", kind="skeletalmesh") == ["Skel"]
        wired = {i["name"]: i["wired"] for i in
                 browse.api_meshes({"q": ["crate"]})["items"]}
        assert wired == {"Wood Crate": True, "Crate": False}, wired
    _with_registry(rows, run)


def test_scan_rows_come_back_when_the_richer_row_goes():
    """A file a richer index covered lost its scan row for good: once that
    index stopped covering it (rebuilt for another folder), the file had
    no row at all. Covered scan rows are parked, not deleted."""
    import sqlite3
    from asset_service import db, meshes_import, scanner
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "Models"
        lib.mkdir()
        (lib / "plate.fbx").write_bytes(_min_fbx())
        (lib / "other.fbx").write_bytes(_min_fbx())
        dbp = str(tmp / "reg.sqlite")
        db.init_db(dbp).close()
        scanner.scan_folder(lib, db_path=dbp)
        idx = tmp / "native_models.jsonl"
        idx.write_text(json.dumps({
            "name": "Plate", "pack": "Models", "source": "native",
            "fbx": str(lib / "plate.fbx"), "exists": True, "triangles": 1,
            "vertices": 3, "bbox_m": [1, 1, 1], "materials": []}) + "\n",
            encoding="utf-8")
        saved = meshes_import.SOURCES, meshes_import.MANIFEST_ROOTS
        # this library only: the configured manifest roots are the live one's
        meshes_import.SOURCES, meshes_import.MANIFEST_ROOTS = (idx,), []
        try:
            def rows():
                c = sqlite3.connect(dbp)
                got = sorted((r[0], r[1]) for r in c.execute(
                    "SELECT name, source FROM meshes"))
                c.close()
                return got
            meshes_import.import_meshes(dbp)
            assert rows() == [("Plate", "native"), ("other", "scan")], rows()
            idx.write_text("", encoding="utf-8")       # index rebuilt elsewhere
            meshes_import.import_meshes(dbp)
            assert rows() == [("other", "scan"), ("plate", "scan")], rows()
        finally:
            meshes_import.SOURCES, meshes_import.MANIFEST_ROOTS = saved


def test_scanned_files_keep_their_folder_words():
    """A scan of 'Downloads' made every row 'Downloads': product and
    category folders between the scanned folder and the file were not
    searchable. Sounds scanned outside the audio section got the category
    'C:\\' > 'Users'."""
    import sqlite3
    from asset_service import browse, db, scanner
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "Downloads"
        (lib / "Medieval Village Pack").mkdir(parents=True)
        (lib / "Medieval Village Pack" / "well.fbx").write_bytes(_min_fbx())
        snd = tmp / "MySounds" / "Impacts" / "Metal"
        snd.mkdir(parents=True)
        import wave
        with wave.open(str(snd / "clang.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(bytes(1600))
        dbp = str(tmp / "reg.sqlite")
        db.init_db(dbp).close()
        scanner.scan_folder(lib, db_path=dbp)
        scanner.scan_folder(tmp / "MySounds", db_path=dbp)
        old = browse._DB_PATH
        browse._DB_PATH = dbp
        try:
            r = browse.api_meshes({"q": ["village"]})
            assert [i["name"] for i in r["items"]] == ["well"], r
            assert browse.api_meshes({"theme": ["medieval"]})["total"] == 1
        finally:
            browse._DB_PATH = old
        c = sqlite3.connect(dbp)
        cat = c.execute("SELECT cat, sub FROM audio").fetchone()
        c.close()
    assert tuple(cat) == ("Impacts", "Metal"), cat


def test_thumbnail_choice_survives_an_offline_drive():
    """A choice whose image was unreachable during ONE import (unplugged
    drive) was erased for good; only values that cannot be an image path
    are dropped."""
    import sqlite3
    from asset_service import collection_import, db
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv = tmp / "c.csv"
        csv.write_text("Name,Product URL\nMill,https://example.com/a\n",
                       encoding="utf-8")
        dbp = str(tmp / "c.sqlite")
        db.init_db(dbp).close()
        collection_import.import_collection(dbp, csv_path=csv)
        gone = str(tmp / "offline_drive" / "pick.png")
        c = sqlite3.connect(dbp)
        c.execute("UPDATE collection SET thumb_override=?", (gone,))
        c.commit()
        c.close()
        collection_import.import_collection(dbp, csv_path=csv)
        c = sqlite3.connect(dbp)
        got = c.execute("SELECT thumb_override FROM collection").fetchone()[0]
        c.close()
    assert got == gone, f"an offline choice was erased: {got!r}"


def test_purchases_sharing_a_folder_stay_apart():
    """Two purchases with the same folder resolved to one row: every
    re-import overwrote the first with the second."""
    import sqlite3
    from asset_service import collection_import, db
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "Shared").mkdir()
        csv = tmp / "c.csv"
        csv.write_text("Name,Local Folder\nPack A,{0}\nPack A dup,{0}\n".format(
            tmp / "Shared"), encoding="utf-8")
        dbp = str(tmp / "c.sqlite")
        db.init_db(dbp).close()
        collection_import.import_collection(dbp, csv_path=csv)
        # a thumbnail choice on a row WITH a folder (the live library's
        # case: the first version of this fix crashed the whole import)
        pick = str(tmp / "Shared" / "b.png")
        c = sqlite3.connect(dbp)
        c.execute("UPDATE collection SET thumb_override=? WHERE name='Pack A'",
                  (pick,))
        c.commit()
        c.close()
        assert collection_import.import_collection(dbp, csv_path=csv) == 2
        c = sqlite3.connect(dbp)
        got = dict(c.execute("SELECT name, thumb_override FROM collection"))
        c.close()
    assert sorted(got) == ["Pack A", "Pack A dup"], got
    assert got["Pack A"] == pick and got["Pack A dup"] is None, got


def test_anim_tree_and_mcp_tree_tools_run():
    """The skeleton report and three MCP tools were never executed by a
    check (a crash there passed every suite)."""
    from asset_service import browse, config
    tree = None
    old_sec = config.SECTIONS.get("animation")
    config.SECTIONS["animation"] = "Animation"
    try:
        tree = _with_registry([], lambda b: b.api_anim_tree(),
                              clips=["Animation/Moves/walk.fbx"])
    finally:
        config.SECTIONS["animation"] = old_sec
    assert tree["folders"] and "rig_note" in tree, tree
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "pharos_mcp_tree", str(REPO / "service" / "pharos_mcp_server.py"))
        srv = ilu.module_from_spec(spec)
        spec.loader.exec_module(srv)
    except Exception as exc:                       # noqa: BLE001
        assert os.environ.get("PHAROS_REQUIRE_MCP") != "1", \
            f"PHAROS_REQUIRE_MCP=1 but the MCP server did not load: {exc}"
        print(f"SKIP (no mcp package on this machine): {exc}")
        return
    orig, old_cfg = srv.config.DB_PATH, srv.config.is_configured
    with tempfile.TemporaryDirectory() as tmp:
        srv.config.DB_PATH = _search_registry(tmp, [("Crate", "Kit")])
        srv.config.is_configured = lambda: True
        try:
            for sec in ("meshes", "textures", "audio", "animations", "collection"):
                assert "error" not in json.loads(srv.pharos_tree(sec)), sec
            assert json.loads(srv.pharos_list_packs())["packs"][0]["pack"] == "Kit"
            assert json.loads(srv.pharos_get_texture(1)).get("error") == "not found"
        finally:
            srv.config.DB_PATH, srv.config.is_configured = orig, old_cfg


# ---------------------------------------------------------------------------
# texture facts: measured pixel size, map roles
# ---------------------------------------------------------------------------

# ---- texfacts: measured texture facts (px / res / maps / thumb) -----------

def _tf_png(w, h, pad=0):
    """A valid PNG whose IHDR DECLARES w x h. The pixel data is one byte:
    the importer reads headers only, so the payload need not match.
    `pad` appends bytes after IEND to control the file size."""
    import zlib

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
            + chunk(b"IEND", b"") + b"\x00" * pad)


def _tf_jpg(w, h, sof=0xC0, lead=b""):
    """JPEG reduced to its markers: SOI, optional leading segments, the
    frame header (SOF) declaring w x h, EOI."""
    frame = (b"\xff" + bytes([sof])
             + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00")
    return b"\xff\xd8" + lead + frame + b"\xff\xd9"


def _tf_headers(w, h):
    """One minimal file per format image_size() has to read; only the
    header fields are real."""
    def app(marker, payload):
        return (b"\xff" + bytes([marker])
                + struct.pack(">H", len(payload) + 2) + payload)

    def riff(chunk):
        return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk

    def attr(name, typ, val):
        return (name + b"\x00" + typ + b"\x00"
                + struct.pack("<I", len(val)) + val)

    def le3(v):
        return (v - 1).to_bytes(3, "little")
    # the frame header of an embedded EXIF thumbnail: must NOT be reported
    decoy = (b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 120, 160, 1)
             + b"\x01\x11\x00")
    return {
        "a.png": _tf_png(w, h),
        "a.jpg": _tf_jpg(w, h),
        "progressive.jpeg": _tf_jpg(w, h, sof=0xC2),
        # thumbnail SOF inside APP1, then 120 KB of ICC-style blocks: the
        # real frame header sits far behind any fixed-size head read
        "exif.jpg": _tf_jpg(w, h, lead=app(0xE1, decoy)
                            + app(0xE2, b"\x00" * 60000) * 2),
        "a.gif": b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00",
        # negative height = top-down rows
        "a.bmp": (b"BM" + struct.pack("<IHHI", 54, 0, 0, 54)
                  + struct.pack("<IiiHH", 40, w, -h, 1, 24) + b"\x00" * 24),
        "a.tga": struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0,
                             w, h, 24, 0),
        "lossy.webp": riff(b"VP8 " + struct.pack("<I", 10)
                           + b"\x00\x00\x00\x9d\x01\x2a"
                           + struct.pack("<HH", w, h)),
        "lossless.webp": riff(b"VP8L" + struct.pack("<I", 5) + b"\x2f"
                              + struct.pack("<I", (w - 1) | ((h - 1) << 14))),
        "extended.webp": riff(b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
                              + le3(w) + le3(h)),
        "a.hdr": (b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y %d +X %d\n"
                  % (h, w)),
        "a.exr": (b"\x76\x2f\x31\x01\x02\x00\x00\x00"
                  + attr(b"channels", b"chlist", b"\x00")
                  + attr(b"compression", b"compression", b"\x00")
                  + attr(b"dataWindow", b"box2i",
                         struct.pack("<4i", 0, 0, w - 1, h - 1)) + b"\x00"),
        "intel.tif": (b"II*\x00" + struct.pack("<IH", 8, 2)
                      + struct.pack("<HHII", 256, 4, 1, w)
                      + struct.pack("<HHIHH", 257, 3, 1, h, 0) + b"\x00" * 4),
        "motorola.tiff": (b"MM\x00*" + struct.pack(">IH", 8, 2)
                          + struct.pack(">HHII", 256, 4, 1, w)
                          + struct.pack(">HHIHH", 257, 3, 1, h, 0)
                          + b"\x00" * 4),
    }


def _tf_library(root):
    """A texture root with the layouts the importer has to get right.
    Returns the set folders by key. Every image is header-only."""
    from asset_service import textures_import as ti
    sets = {}
    # A: folder still named 4K, maps re-exported at 2048 px. The height
    #    map is 4096 px AND the largest file: the colour map must win.
    a = sets["a"] = (root / ti.MAIN_NAME / "Masonry_4K"
                     / "teal_gneiss_wall_07_4K")
    a.mkdir(parents=True)
    for ch in ("diffuse", "roughness", "glossiness"):
        (a / f"teal_gneiss_wall_07_{ch}.jpg").write_bytes(_tf_jpg(2048, 2048))
    (a / "teal_gneiss_wall_07_normal.png").write_bytes(_tf_png(2048, 2048))
    (a / "teal_gneiss_wall_07_height.png").write_bytes(
        _tf_png(4096, 4096, pad=4000))
    (a / "teal_gneiss_wall_07_render.jpg").write_bytes(_tf_jpg(512, 512))
    # B: the only image cannot be measured -> the name is all there is
    b = sets["b"] = root / ti.MAIN_NAME / "Masonry_4K" / "amber_resin_02_4K"
    b.mkdir(parents=True)
    (b / "amber_resin_02_diffuse.png").write_bytes(b"\x89PNG fake")
    # C: no colour map -> the largest real map is the reference
    c = sets["c"] = root / "FreeSets" / "pale_grit_05"
    c.mkdir(parents=True)
    (c / "pale_grit_05_normal.png").write_bytes(_tf_png(1024, 1024, pad=900))
    (c / "pale_grit_05_ao.png").write_bytes(_tf_png(512, 512))
    # D: loose files of ONE material beside each other (family grouping)
    for tail in ("2K_Color", "2K_NormalDX", "2K_NormalGL",
                 "2K_AmbientOcclusion", "2K_Roughness", "PREVIEW"):
        (root / f"Alloy007_{tail}.jpg").write_bytes(
            _tf_jpg(256, 256) if tail == "PREVIEW" else _tf_jpg(2048, 2048))
    # E: photos whose names end in a SIZE letter (M = medium, S = small),
    #    not in a map set -- and one that is the base photo OF a map set
    (root / "Gantry0042_M.jpg").write_bytes(_tf_jpg(1600, 1200))
    (root / "Hoist0007_2_S (1).jpg").write_bytes(_tf_jpg(1024, 768))
    for tail in ("", "_COLOR", "_NRM", "_SPEC"):
        (root / f"Girder0006_1_S{tail}.jpg").write_bytes(_tf_jpg(1024, 1024))
    # F: a real one-letter map set
    f = sets["f"] = root / "FreeSets" / "crate_kit"
    f.mkdir(parents=True)
    for letter in "dnm":
        (f / f"crate_{letter}.png").write_bytes(_tf_png(1024, 1024))
    # G: preview named like the folder minus its resolution suffix; a map
    #    sorts before it (the space), so "first image" picks the AO map
    g = sets["g"] = root / "FreeSets" / "ochre_plank_floor_12_4K"
    g.mkdir(parents=True)
    (g / "ochre plank_floor_12_ao.jpg").write_bytes(_tf_jpg(2048, 2048))
    (g / "ochre_plank_floor_12_diffuse.jpg").write_bytes(_tf_jpg(2048, 2048))
    (g / "ochre_plank_floor_12.jpg").write_bytes(_tf_jpg(512, 512))
    # H: an explicit *_PREVIEW image beside a large colour map
    h = sets["h"] = root / "FreeSets" / "Slate050"
    h.mkdir(parents=True)
    (h / "Slate021_4K_Color.jpg").write_bytes(
        _tf_jpg(4096, 4096) + b"\x00" * 5000)
    (h / "Slate021_4K_Roughness.jpg").write_bytes(_tf_jpg(4096, 4096))
    (h / "Slate021_PREVIEW.jpg").write_bytes(_tf_jpg(256, 256))
    # I: no preview image at all; the AO map sorts before the colour map
    i = sets["i"] = root / "FreeSets" / "umber_felt_03"
    i.mkdir(parents=True)
    (i / "umber_felt_03_ao.png").write_bytes(_tf_png(1024, 1024))
    (i / "umber_felt_03_color.png").write_bytes(_tf_png(1024, 1024))
    # J: the render is named like its maps, the FOLDER is spelled
    #    differently (hyphen), so "stem equals folder name" does not hit
    j = sets["j"] = root / "FreeSets" / "sage_painted-wood_33_4K"
    j.mkdir(parents=True)
    (j / "sage_painted_wood_33.jpg").write_bytes(_tf_jpg(512, 512))
    (j / "sage_painted_wood_33_diffuse.jpg").write_bytes(_tf_jpg(2048, 2048))
    (j / "sage_painted_wood_33_ao.jpg").write_bytes(_tf_jpg(2048, 2048))
    return sets


def _tf_import(db, root):
    """Run the texture importer on a fixture root; rows with parsed meta."""
    import sqlite3
    from asset_service import textures_import as ti
    ti.import_textures(db, root=root)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM textures")]
    finally:
        conn.close()
    for r in rows:
        r["meta"] = json.loads(r["meta"] or "{}")
    return rows


def _tf_row(rows, folder, name=None):
    hit = [r for r in rows if r["folder"] == Path(folder).as_posix()
           and (name is None or r["name"] == name)]
    assert len(hit) == 1, \
        f"expected one set for {Path(folder).name}/{name}: " \
        f"{[r['name'] for r in hit]}"
    return hit[0]


def test_image_size_reads_every_supported_header():
    """image_size() returns (width, height) from the header bytes of each
    format the texture library holds, walks JPEG segments (an EXIF
    thumbnail's frame header and 120 KB of ICC data come first), and
    answers None -- never an exception -- for anything broken."""
    import importlib.util as ilu
    assert ilu.find_spec("asset_service.imgmeta"), \
        "asset_service.imgmeta does not exist (texfacts)"
    from asset_service import imgmeta
    tmp = Path(tempfile.mkdtemp(prefix="pharos_imgsize_"))
    try:
        for name, blob in _tf_headers(2048, 1024).items():
            (tmp / name).write_bytes(blob)
            got = imgmeta.image_size(tmp / name)
            assert got == (2048, 1024), f"{name}: {got}"
        assert imgmeta.image_size(str(tmp / "a.png")) == (2048, 1024), \
            "a str path must work like a Path"
        broken = {
            "empty.png": b"",
            "fake.png": b"\x89PNG fake",
            "cut.jpg": b"\xff\xd8\xff\xe0\x00",
            "fake.jpg": b"\xff\xd8 fake",
            "noframe.jpg": b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xd9",
            "cut.gif": b"GIF89a\x01",
            "cut.bmp": b"BM\x00\x00",
            "text.tga": b"this is not a targa header",
            "cut.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
            "cut.hdr": b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n",
            "fake.exr": b"v/1\x01 fake",
            "cut.tif": b"II*\x00\xff\xff\xff\x7f",
            "zero.png": _tf_png(0, 0),
            "notes.txt": b"\x00" * 64,
        }
        for name, blob in broken.items():
            (tmp / name).write_bytes(blob)
            try:
                got = imgmeta.image_size(tmp / name)
            except Exception as exc:        # noqa: BLE001  (the contract)
                raise AssertionError(f"{name}: raised {exc!r}")
            assert got is None, f"{name}: {got}"
        assert imgmeta.image_size(tmp / "missing.png") is None
        assert imgmeta.image_size(tmp) is None, "a directory is not an image"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_map_role_names_each_map():
    """map_role() reads the role off the LAST meaningful token of a file
    name. Glossiness is its own role (it is the inverse of roughness), a
    one-letter token counts only after a separator, and a render/preview
    image is "preview", never a map."""
    import importlib.util as ilu
    assert ilu.find_spec("asset_service.imgmeta"), \
        "asset_service.imgmeta does not exist (texfacts)"
    from asset_service.imgmeta import map_role
    cases = {
        "gneiss_basecolor.png": "albedo", "gneiss_Base_Color.png": "albedo",
        "gneissBaseColor.png": "albedo", "gneiss_diffuse.jpg": "albedo",
        "gneiss_diff_4k.jpg": "albedo", "gneiss_col.jpg": "albedo",
        "Gneiss_2K_Color.jpg": "albedo", "gneiss_albedo.tif": "albedo",
        "T_Gneiss_BC.tga": "albedo", "gneiss_d.png": "albedo",
        "gneiss_normal.png": "normal", "Gneiss_2K_NormalDX.jpg": "normal",
        "Gneiss_2K_NormalGL.jpg": "normal", "gneiss_nor_gl_4k.exr": "normal",
        "gneiss_nrm.png": "normal", "gneiss_n.png": "normal",
        "gneiss_normal - Copy.jpg": "normal",
        "gneiss_normal_01.png": "normal",
        "gneiss_roughness.jpg": "roughness",
        "gneiss_rough_2k.jpg": "roughness", "gneiss_r.png": "roughness",
        "gneiss_glossiness.jpg": "glossiness",
        "gneiss_gloss.jpg": "glossiness",
        "gneiss_smoothness.png": "glossiness",
        "gneiss_metallic.jpg": "metallic", "gneiss_metalness.jpg": "metallic",
        "gneiss_m.png": "metallic",
        "gneiss_ao.jpg": "ao", "Gneiss_2K_AmbientOcclusion.jpg": "ao",
        "gneiss_ambient_occlusion.png": "ao", "gneiss_occ.png": "ao",
        "gneiss_height.png": "height", "gneiss_displacement.exr": "height",
        "gneiss_disp_4k.png": "height", "gneiss_bump.jpg": "height",
        "gneiss_h.png": "height",
        "gneiss_opacity.png": "opacity", "gneiss_alpha.png": "opacity",
        "gneiss_mask.png": "opacity",
        "gneiss_emissive.png": "emissive", "gneiss_emission.png": "emissive",
        "gneiss_e.png": "emissive",
        "gneiss_specular.jpg": "specular", "gneiss_spec.jpg": "specular",
        "gneiss_reflection.jpg": "specular",
        "gneiss_orm.png": "packed", "gneiss_arm_4k.jpg": "packed",
        "T_Gneiss_ORMA.png": "packed", "gneiss_rma.tga": "packed",
        "gneiss_render.jpg": "preview", "Gneiss_PREVIEW.jpg": "preview",
        "gneiss_thumb.png": "preview", "gneiss_thumbnail.jpg": "preview",
        "Gneiss_Sphere.jpg": "preview",
        # no role: nothing after the name, a role word that is not LAST,
        # and single letters that are part of a word
        "gneiss.jpg": "", "gneiss_normal_wet.jpg": "", "gneissn.png": "",
        "GneissN.png": "", "garden.png": "", "Gneiss_2K.png": "",
        "GneissGrün.jpg": "",      # an umlaut is not a separator
    }
    for name, want in cases.items():
        assert map_role(name) == want, \
            f"{name}: {map_role(name)!r} != {want!r}"
    assert map_role("gneiss_glossiness.jpg") != "roughness", \
        "glossiness must never be reported as roughness"
    # a full path works, and the image named like its folder is the preview
    assert map_role("lib/gneiss_wall/gneiss_wall_normal.jpg") == "normal"
    assert map_role("lib/gneiss_wall/gneiss_wall.jpg") == "preview"
    assert map_role("lib\\gneiss_wall_4K\\gneiss_wall.jpg") == "preview", \
        "folder name minus its resolution suffix"
    assert map_role(Path("lib") / "gneiss_wall" / "gneiss_wall_diffuse.jpg") \
        == "albedo"
    # role_parts adds the token and the name in front of it (what tells
    # the maps of one material from a look-alike suffix)
    from asset_service.imgmeta import role_parts
    assert role_parts("lib/crate_kit/crate_m.png") == \
        ("metallic", "m", "crate_")
    assert role_parts("Gneiss_2K_NormalGL - Kopie.jpg") == \
        ("normal", "normalgl", "gneiss_2k_")


def test_texture_res_is_measured_not_read_from_the_folder_name():
    """A set folder still called "..._4K" whose maps are 2048 px is a 2K
    set: res/px come from the colour map's header, not from name tokens
    (measured on a real library: 2,000 of 2,021 "4K" labels were wrong)."""
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    try:
        root = tmp / "Textures_Materials"
        sets = _tf_library(root)
        rows = _tf_import(tmp / "reg" / "assets.sqlite", root)
        m = _tf_row(rows, sets["a"])["meta"]
        assert m.get("res") == "2K", f"res={m.get('res')!r} (folder says 4K)"
        assert m.get("res_source") == "measured", m.get("res_source")
        assert m.get("px") == [2048, 2048], \
            f"px={m.get('px')} (the colour map, not the larger height map)"
        # no colour map: the largest non-preview image is the reference
        m = _tf_row(rows, sets["c"])["meta"]
        assert (m.get("px"), m.get("res"), m.get("res_source")) == \
            ([1024, 1024], "1K", "measured"), m
        m = _tf_row(rows, sets["h"])["meta"]
        assert (m.get("px"), m.get("res")) == ([4096, 4096], "4K"), m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_texture_res_falls_back_to_name_when_unmeasurable():
    """Only when no header can be read does the label come from the name,
    and the row says so (res_source "name", px null)."""
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    try:
        root = tmp / "Textures_Materials"
        sets = _tf_library(root)
        rows = _tf_import(tmp / "reg" / "assets.sqlite", root)
        m = _tf_row(rows, sets["b"])["meta"]
        assert "px" in m and m["px"] is None, f"px={m.get('px', 'MISSING')}"
        assert m.get("res") == "4K", m.get("res")
        assert m.get("res_source") == "name", m.get("res_source")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_texture_maps_say_which_file_is_which_map():
    """meta.maps = {role: file}: roughness and glossiness are separate
    entries, the render is not a map, and a size letter at the end of a
    lone photo ("..._M" = medium) is not a metallic map."""
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    try:
        root = tmp / "Textures_Materials"
        sets = _tf_library(root)
        rows = _tf_import(tmp / "reg" / "assets.sqlite", root)
        maps = _tf_row(rows, sets["a"])["meta"].get("maps")
        assert isinstance(maps, dict), f"maps={maps!r}"
        base = sets["a"].as_posix() + "/teal_gneiss_wall_07_"
        assert maps == {"albedo": base + "diffuse.jpg",
                        "normal": base + "normal.png",
                        "roughness": base + "roughness.jpg",
                        "glossiness": base + "glossiness.jpg",
                        "height": base + "height.png"}, maps
        assert not any("render" in v for v in maps.values()), maps
        # the 512 px render named like its maps is measured as neither
        m = _tf_row(rows, sets["j"])["meta"]
        assert sorted(m["maps"]) == ["albedo", "ao"] and \
            m["px"] == [2048, 2048], m
        # a real one-letter set keeps its roles ...
        maps = _tf_row(rows, sets["f"])["meta"]["maps"]
        assert sorted(maps) == ["albedo", "metallic", "normal"], maps
        # ... a lone photo with a size suffix has none
        lone = _tf_row(rows, root, "Gantry0042")["meta"]
        assert lone["maps"] == {}, lone["maps"]
        assert lone["px"] == [1600, 1200], lone["px"]
        lone = _tf_row(rows, root, "Hoist0007 2 s (1)")["meta"]
        assert lone["maps"] == {}, lone["maps"]
        # ... and the base photo of a set is not its specular map
        maps = _tf_row(rows, root, "Girder0006")["meta"]["maps"]
        assert {k: Path(v).name for k, v in maps.items()} == {
            "albedo": "Girder0006_1_S_COLOR.jpg",
            "normal": "Girder0006_1_S_NRM.jpg",
            "specular": "Girder0006_1_S_SPEC.jpg"}, maps
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_texture_thumb_prefers_the_preview_image():
    """The thumbnail is the set's preview render when it has one -- not
    the first file in name order (an AO map) and not a full-size colour
    map (a 23 MB map was served as "thumbnail")."""
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    try:
        root = tmp / "Textures_Materials"
        sets = _tf_library(root)
        rows = _tf_import(tmp / "reg" / "assets.sqlite", root)
        want = {"a": "teal_gneiss_wall_07_render.jpg",
                "g": "ochre_plank_floor_12.jpg",
                "h": "Slate021_PREVIEW.jpg",
                "j": "sage_painted_wood_33.jpg"}
        for key, name in want.items():
            thumb = _tf_row(rows, sets[key])["thumb"]
            assert thumb == (sets[key] / name).as_posix(), \
                f"{key}: thumb={Path(thumb).name}"
        # no preview: the colour map, not the AO map that sorts first
        thumb = _tf_row(rows, sets["i"])["thumb"]
        assert thumb == (sets["i"] / "umber_felt_03_color.png").as_posix(), \
            thumb
        # neither preview nor colour map: the first image, as before
        thumb = _tf_row(rows, sets["c"])["thumb"]
        assert thumb == (sets["c"] / "pale_grit_05_ao.png").as_posix(), thumb
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_texture_family_joins_normaldx_ambientocclusion_preview():
    """Loose files of one material form ONE set: NormalDX / NormalGL /
    AmbientOcclusion / PREVIEW used to become their own one-file sets
    ("Alloy007 2k normaldx"). The preview is the thumb; of the two normal
    maps the OpenGL one is listed (Blender and Unity read GL normals)."""
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    try:
        root = tmp / "Textures_Materials"
        _tf_library(root)
        rows = _tf_import(tmp / "reg" / "assets.sqlite", root)
        fam = [r for r in rows if r["name"].lower().startswith("alloy007")]
        assert [(r["name"], r["file_count"]) for r in fam] == \
            [("Alloy007", 6)], [(r["name"], r["file_count"]) for r in fam]
        r = fam[0]
        assert r["thumb"] == (root / "Alloy007_PREVIEW.jpg").as_posix(), \
            r["thumb"]
        maps = {k: Path(v).name for k, v in r["meta"].get("maps", {}).items()}
        assert maps == {"albedo": "Alloy007_2K_Color.jpg",
                        "normal": "Alloy007_2K_NormalGL.jpg",
                        "ao": "Alloy007_2K_AmbientOcclusion.jpg",
                        "roughness": "Alloy007_2K_Roughness.jpg"}, maps
        assert r["meta"].get("px") == [2048, 2048], r["meta"].get("px")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_texture_px_cache_skips_header_reads_on_reimport():
    """The importer runs at every server start: it reads ONE header per
    set, and none at all for a set whose folder, file count and byte total
    are unchanged since the last import. An unmeasurable set is retried
    (a failure is not cached); a changed set is measured again."""
    import importlib.util as ilu
    assert ilu.find_spec("asset_service.imgmeta"), \
        "asset_service.imgmeta does not exist (texfacts)"
    from asset_service import imgmeta
    tmp = Path(tempfile.mkdtemp(prefix="pharos_texfacts_"))
    calls = []
    real = imgmeta.image_size

    def counting(path):
        calls.append(Path(path).name)
        return real(path)
    imgmeta.image_size = counting
    try:
        root = tmp / "Textures_Materials"
        sets = _tf_library(root)
        db = tmp / "reg" / "assets.sqlite"
        rows = _tf_import(db, root)
        assert len(calls) == len(rows), \
            f"first import: {len(calls)} header reads for {len(rows)} sets"
        first = {(r["folder"], r["name"]): r["meta"].get("px") for r in rows}
        # a scanner row must pass through the rebuild untouched (it is not
        # this importer's row: no px lookup, no rewrite)
        import sqlite3
        scan_meta = json.dumps({"res": "4K", "note": "scanner-owned"})
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO textures (name, grp, sub, source, folder, files, "
            "images, file_count, bytes, tags, meta) VALUES "
            "('Scan Set', 'Scanned', 'x', 'scan', '/elsewhere/scan_set', "
            "'[]', '[]', 1, 10, '[]', ?)", (scan_meta,))
        conn.commit()
        conn.close()
        del calls[:]
        rows = _tf_import(db, root)
        assert calls == ["amber_resin_02_diffuse.png"], \
            f"second import re-read headers: {calls}"
        scan = [r for r in rows if r["source"] == "scan"]
        assert len(scan) == 1 and scan[0]["meta"] == json.loads(scan_meta), \
            f"scanner row changed by the rebuild: {scan}"
        rows = [r for r in rows if r["source"] != "scan"]
        again = {(r["folder"], r["name"]): r["meta"].get("px") for r in rows}
        assert again == first, "px changed on an unchanged library"
        assert _tf_row(rows, sets["a"])["meta"]["res_source"] == "measured"
        # a set that changed on disk is measured again
        (sets["c"] / "pale_grit_05_diffuse.png").write_bytes(
            _tf_png(512, 512))
        del calls[:]
        rows = _tf_import(db, root)
        assert sorted(calls) == ["amber_resin_02_diffuse.png",
                                 "pale_grit_05_diffuse.png"], calls
        assert _tf_row(rows, sets["c"])["meta"]["px"] == [512, 512]
    finally:
        imgmeta.image_size = real
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# skeleton families of clips and characters
# ---------------------------------------------------------------------------

_SKEL_UE = ["root", "pelvis", "spine_01", "clavicle_l", "thigh_l", "calf_l",
            "foot_l", "ball_l"]
_SKEL_CC = ["CC_Base_BoneRoot", "CC_Base_Hip", "CC_Base_Pelvis",
            "CC_Base_Spine01", "CC_Base_L_Thigh", "CC_Base_L_Calf"]


def _skel_mod():
    """The module under test. A missing module is an ASSERTION failure, so
    the standalone runner reports FAIL instead of dying on ImportError."""
    import importlib.util as ilu
    assert ilu.find_spec("asset_service.skeleton"), \
        "asset_service.skeleton does not exist"
    from asset_service import skeleton
    return skeleton


def _skel_fbx(version, nodes):
    """Binary FBX with one Objects record whose children are the given
    (name, type[, class]) objects, class defaulting to Model. Each child
    carries the property layout real exporters write: id, the string
    '<name> NUL 0x01 <class>', then the type string. Record headers are
    32-bit below 7500 and 64-bit from 7500 on, like the real format."""
    hdr, hlen = ("<QQQ", 25) if version >= 7500 else ("<III", 13)
    out = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", version)
    pos = len(out) + hlen + len(b"Objects")
    kids = b""
    for i, node in enumerate(nodes):
        cls = (node[2] if len(node) > 2 else "Model").encode()
        full = node[0].encode() + b"\x00\x01" + cls
        props = (b"L" + struct.pack("<q", 1000 + i)
                 + b"S" + struct.pack("<I", len(full)) + full
                 + b"S" + struct.pack("<I", len(node[1])) + node[1].encode())
        end = pos + len(kids) + hlen + len(cls) + len(props)
        kids += (struct.pack(hdr, end, 3, len(props))
                 + bytes([len(cls)]) + cls + props)
    return (out + struct.pack(hdr, pos + len(kids), 0, 0) + b"\x07Objects"
            + kids + b"\x00" * hlen)


def _skel_clip(version, names, extra=()):
    """A clip/character on the given bones: first name is the Root node,
    the rest LimbNodes, behind an armature container (type Null) and next
    to a mesh object -- neither of which is a bone."""
    return _skel_fbx(version, [("SK_Container", "Null"), (names[0], "Root")]
                     + [(n, "LimbNode") for n in names[1:]]
                     + [("SM_Skin", "Mesh")] + list(extra))


def test_skeleton_bone_names_binary_fbx_32_and_64_bit_headers():
    sk = _skel_mod()
    with tempfile.TemporaryDirectory() as tmp:
        for version in (7400, 7700):
            f = Path(tmp) / f"clip_{version}.fbx"
            f.write_bytes(_skel_clip(version, _SKEL_UE, extra=[
                ("pelvis", "LimbNode"),                  # repeated name
                ("ShotCam", "Camera"), ("KeyLight", "Light"),
                # same name pattern, but not a Model: attribute + geometry
                ("ghost_bone", "LimbNode", "NodeAttribute"),
                ("skin_geo", "Mesh", "Geometry"),
                ("M_Skin", "", "Material")]))
            names = sk.bone_names(f)
            assert names == _SKEL_UE, \
                f"FBX {version}: expected the 8 joints in file order, " \
                f"no container/mesh/camera/attribute names, got {names}"
            assert sk.bone_names(str(f)) == names, "str path differs"
            d = sk.describe(f)
            assert d == {"bones": 8, "family": "ue-mannequin",
                         "signature": sk.signature(_SKEL_UE)}, d
            # a byte cap that ends before the Objects section finds nothing
            assert sk.bone_names(f, limit_bytes=27) == [], \
                "limit_bytes=27 must read the header only"
            assert sk.bone_names(f, limit_bytes=10 ** 6) == names

        # a mesh object named like a bone family must not become a bone
        cc = Path(tmp) / "body_cc.fbx"
        cc.write_bytes(_skel_fbx(7400, [("CC_Base_Body", "Mesh")]
                                 + [(n, "LimbNode") for n in _SKEL_CC]
                                 + [("CC_Base_Tongue", "Mesh")]))
        assert sk.bone_names(cc) == _SKEL_CC, sk.bone_names(cc)
        assert sk.describe(cc)["family"] == "cc", sk.describe(cc)

        # a skeleton written as plain helper nodes (no LimbNode/Root at
        # all): the Null nodes ARE the skeleton then
        helpers = Path(tmp) / "helpers.fbx"
        helpers.write_bytes(_skel_fbx(7400, [
            ("Rig_Hips", "Null"), ("Rig_Spine", "Null"), ("Skin", "Mesh")]))
        assert sk.bone_names(helpers) == ["Rig_Hips", "Rig_Spine"], \
            sk.bone_names(helpers)


def test_skeleton_bone_names_ascii_fbx_and_bvh():
    sk = _skel_mod()
    ascii_fbx = "\n".join([
        "; FBX 7.4.0 project file",
        "Objects:  {",
        '\tGeometry: 100, "Geometry::skin_geo", "Mesh" {',
        "\t}",
        '\tNodeAttribute: 150, "NodeAttribute::ghost_bone", "LimbNode" {',
        "\t}",
        '\tModel: 200, "Model::Armature", "Null" {',
        "\t}",
        '\tModel: 201, "Model::mixamorig:Hips", "LimbNode" {',
        "\t}",
        '\tModel: 202, "Model::mixamorig:Spine", "LimbNode" {',
        "\t}",
        '\tModel: 203, "Model::mixamorig:LeftUpLeg", "LimbNode" {',
        "\t}",
        '\tModel: 204, "Model::mixamorig:LeftLeg", "LimbNode" {',
        "\t}",
        '\tModel: 205, "Model::Body", "Mesh" {',
        "\t}",
        "}", ""])
    # FBX 6.x ASCII: no id column, joints are typed "Limb"
    ascii_6 = "\n".join([
        "; FBX 6.1.0 project file",
        "Objects:  {",
        '\tModel: "Model::Hips", "Limb" {',
        "\t}",
        '\tModel: "Model::Chest", "Limb" {',
        "\t}",
        '\tModel: "Model::Body", "Mesh" {',
        "\t}",
        "}", ""])
    bvh = "\n".join([
        "HIERARCHY",
        "ROOT Hips",
        "{",
        "\tOFFSET 0.0 0.0 0.0",
        "\tCHANNELS 6 Xposition Yposition Zposition "
        "Zrotation Xrotation Yrotation",
        "\tJOINT Chest",
        "\t{",
        "\t\tOFFSET 0.0 5.0 0.0",
        "\t\tCHANNELS 3 Zrotation Xrotation Yrotation",
        "\t\tJOINT Neck",
        "\t\t{",
        "\t\t\tOFFSET 0.0 4.0 0.0",
        "\t\t\tCHANNELS 3 Zrotation Xrotation Yrotation",
        "\t\t\tEnd Site",
        "\t\t\t{",
        "\t\t\t\tOFFSET 0.0 1.0 0.0",
        "\t\t\t}",
        "\t\t}",
        "\t}",
        "}",
        "MOTION",
        "Frames: 1",
        "Frame Time: 0.0333333",
        "0 0 0 0 0 0 0 0 0 0 0 0", ""])
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "walk_ascii.fbx"
        a.write_text(ascii_fbx, encoding="utf-8")
        names = sk.bone_names(a)
        assert names == ["mixamorig:Hips", "mixamorig:Spine",
                         "mixamorig:LeftUpLeg", "mixamorig:LeftLeg"], names
        assert sk.family(names) == "mixamo", sk.family(names)

        a6 = Path(tmp) / "walk_ascii6.fbx"
        a6.write_text(ascii_6, encoding="utf-8")
        assert sk.bone_names(a6) == ["Hips", "Chest"], sk.bone_names(a6)

        b = Path(tmp) / "walk.bvh"
        b.write_text(bvh, encoding="utf-8")
        assert sk.bone_names(b) == ["Hips", "Chest", "Neck"], \
            sk.bone_names(b)
        assert sk.describe(b) == {
            "bones": 3, "family": "unknown",
            "signature": sk.signature(["Hips", "Chest", "Neck"])}, \
            sk.describe(b)


def test_skeleton_unreadable_input_never_raises():
    sk = _skel_mod()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "empty.fbx").write_bytes(b"")
        (tmp / "junk.fbx").write_bytes(bytes(range(256)) * 40)
        (tmp / "geo_only.fbx").write_bytes(_min_fbx())
        (tmp / "notes.bvh").write_bytes(b"\xff\xfe not a hierarchy \x00")
        whole = _skel_clip(7700, _SKEL_UE)
        # cut inside the last joint's name, and inside its type string
        cut = whole.rfind(b"ball_l")
        (tmp / "cut_name.fbx").write_bytes(whole[:cut + 3])
        (tmp / "cut_type.fbx").write_bytes(
            whole[:cut + len(b"ball_l") + len(b"\x00\x01ModelS") + 2])
        for name in ("empty.fbx", "junk.fbx", "geo_only.fbx", "notes.bvh",
                     "missing.fbx", "."):
            p = tmp / name
            assert sk.bone_names(p) == [], f"{name}: {sk.bone_names(p)}"
            assert sk.describe(p) is None, f"{name}: {sk.describe(p)}"
        for name in ("cut_name.fbx", "cut_type.fbx"):
            # a truncated file yields the joints that are complete, never
            # a half-read name and never an exception
            got = sk.bone_names(tmp / name)
            assert got == _SKEL_UE[:-1], f"{name}: {got}"
        assert sk.bone_names(None) == [], "None path must not raise"

        # a name whose length field does not add up (seen on a real,
        # partly overwritten clip) is skipped, the rest still counts
        broken = _skel_clip(7400, _SKEL_UE)
        at = broken.find(b"spine_01") - 4
        broken = broken[:at] + b"\x77" + broken[at + 1:]
        (tmp / "broken_len.fbx").write_bytes(broken)
        got = sk.bone_names(tmp / "broken_len.fbx")
        assert got == [n for n in _SKEL_UE if n != "spine_01"], got


def test_skeleton_signature_family_overlap():
    sk = _skel_mod()
    sig = sk.signature(_SKEL_UE)
    assert isinstance(sig, str) and 8 <= len(sig) <= 16 \
        and all(c in "0123456789abcdef" for c in sig), f"not short hex: {sig}"
    shuffled = [n.upper() for n in reversed(_SKEL_UE)] + ["Pelvis"]
    assert sk.signature(shuffled) == sig, \
        "signature must ignore order, letter case and repeats"
    assert sk.signature(_SKEL_UE[:-1]) != sig, "one bone less, same signature"
    assert sk.signature(_SKEL_CC) != sig

    assert sk.family(_SKEL_UE) == "ue-mannequin", sk.family(_SKEL_UE)
    assert sk.family([n.upper() for n in _SKEL_UE]) == "ue-mannequin"
    assert sk.family(_SKEL_CC) == "cc", sk.family(_SKEL_CC)
    assert sk.family([f"mixamorig:{n}" for n in
                      ("Hips", "Spine", "LeftUpLeg", "LeftLeg")]) == "mixamo"
    # a quadruped or a prop that happens to own a "pelvis" and a "root"
    assert sk.family(["root", "pelvis", "Leg_FL", "Tail_01"]) == "unknown"
    assert sk.family([]) == "unknown"

    assert sk.overlap(_SKEL_UE, _SKEL_UE) == 1.0
    assert sk.overlap(_SKEL_UE, _SKEL_CC) == 0.0
    assert sk.overlap(_SKEL_UE, [n.upper() for n in _SKEL_UE]) == 1.0, \
        "overlap must be case-insensitive"
    # measured against the SMALLER set: a 4-bone subset of a larger rig
    assert sk.overlap(_SKEL_UE[:4], _SKEL_UE + ["ik_foot_l"]) == 1.0
    assert sk.overlap(["root", "pelvis", "tail_01", "tail_02"],
                      _SKEL_UE) == 0.5
    assert sk.overlap([], _SKEL_UE) == 0.0 and sk.overlap(_SKEL_UE, []) == 0.0


def test_skeleton_pack_skeletons_two_variants_and_matching_bodies():
    sk = _skel_mod()
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Lib"
        pack_a = lib / "Anim" / "MovePack"
        pack_b = lib / "Anim" / "IdlePack"
        (pack_a / "VariantA" / "Root").mkdir(parents=True)
        (pack_a / "VariantB").mkdir(parents=True)
        pack_b.mkdir(parents=True)
        clips_a = []
        for i in range(4):          # mannequin variant, nested one deeper
            f = pack_a / "VariantA" / "Root" / f"vault_{i}.fbx"
            f.write_bytes(_skel_clip(7700, _SKEL_UE))
            clips_a.append(str(f))
        for i in range(3):          # Character-Creator variant
            f = pack_a / "VariantB" / f"roll_{i}.fbx"
            f.write_bytes(_skel_clip(7400, _SKEL_CC))
            clips_a.append(str(f))
        idle = pack_b / "idle_0.fbx"
        idle.write_bytes(_skel_clip(7400, _SKEL_CC))

        chars = lib / "Chars"
        chars.mkdir()
        body_ue = chars / "villager.fbx"     # mannequin bones + own extras
        body_ue.write_bytes(_skel_clip(
            7400, _SKEL_UE + ["hair_01", "skirt_01"]))
        body_cc = chars / "hero.fbx"
        body_cc.write_bytes(_skel_clip(7400, _SKEL_CC))
        body_other = chars / "quadruped.fbx"
        body_other.write_bytes(_skel_clip(
            7400, ["Trunk", "Leg_FL", "Leg_FR", "Tail_01"]))
        # one bone, and the mannequin owns a "root" too: 100 % of the
        # smaller set, but a lamp cannot play a vaulting clip
        body_stub = chars / "lamp.fbx"
        body_stub.write_bytes(_skel_clip(7400, ["root"]))
        bodies = [str(body_ue), str(body_cc), str(body_other),
                  str(body_stub)]

        clips = {str(pack_a): clips_a, str(pack_b): [str(idle)]}
        cache = {}
        res = sk.pack_skeletons(clips, bodies, cache=cache)
        assert set(res) == set(clips), f"one entry list per folder: {res}"

        a = {r["family"]: r for r in res[str(pack_a)]}
        assert len(res[str(pack_a)]) == 2 and \
            set(a) == {"ue-mannequin", "cc"}, \
            f"two skeleton variants in two sub-folders = two entries: " \
            f"{res[str(pack_a)]}"
        ue, cc = a["ue-mannequin"], a["cc"]
        assert ue["bones"] == 8 and ue["signature"] == sk.signature(_SKEL_UE)
        assert ue["subfolders"] == ["VariantA"] and ue["clips"] == 4, ue
        assert ue["bodies"] == [str(body_ue)], \
            f"only the mannequin-boned character matches: {ue['bodies']}"
        assert cc["bones"] == 6 and cc["signature"] == sk.signature(_SKEL_CC)
        assert cc["subfolders"] == ["VariantB"] and cc["clips"] == 3, cc
        assert cc["bodies"] == [str(body_cc)], cc["bodies"]

        b = res[str(pack_b)]
        assert len(b) == 1 and b[0]["family"] == "cc" \
            and b[0]["subfolders"] == [""] and b[0]["clips"] == 1 \
            and b[0]["bodies"] == [str(body_cc)], b
        json.dumps(res)             # plain data, ready for an API answer

        # sampling: at most 2 clips opened per (folder, direct sub-folder)
        opened = [k[0] for k in cache]
        in_a = [p for p in opened if "VariantA" in p]
        assert 1 <= len(in_a) <= 2, \
            f"4 clips in the sub-folder, {len(in_a)} opened (max 2)"
        assert all(isinstance(k, tuple) and len(k) == 3 for k in cache), \
            "cache keys are (path, size, mtime)"
        for body in bodies:
            assert body in opened, f"{body} missing from the cache"

        # the cache is USED: same size + mtime, different bytes -> the
        # stored answer is served without opening the file
        st = os.stat(idle)
        idle.write_bytes(b"\x00" * st.st_size)
        os.utime(idle, ns=(st.st_atime_ns, st.st_mtime_ns))
        again = sk.pack_skeletons(clips, bodies, cache=cache)
        assert again == res, "second run with the cache changed the answer"
        # ... and without a cache the file is read for real
        fresh = sk.pack_skeletons(clips, bodies)
        assert fresh[str(pack_b)] == [], fresh[str(pack_b)]
        assert fresh[str(pack_a)] == res[str(pack_a)]
        # a changed file (new mtime) is a cache miss, not a stale answer
        os.utime(idle, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
        stale = sk.pack_skeletons(clips, bodies, cache=cache)
        assert stale[str(pack_b)] == [], \
            f"stale cache entry served for a changed file: {stale}"


# ---------------------------------------------------------------------------
# one row per file: scanner vs richer sources
# ---------------------------------------------------------------------------

def test_scanner_adds_no_second_row_for_a_file_a_richer_source_covers():
    """Release check: a file with a 'native' row (measured in Blender) got a
    second, contradicting 'scan' row when its folder was scanned. The
    sources spell one path differently (separators, case on Windows), so
    the scanner must compare normalised paths -- and say what it skipped."""
    import contextlib
    import io
    import sqlite3
    from asset_service import db, meshes_import, scanner
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        models = tmp / "models"
        models.mkdir()
        a, b = models / "fx_crate_a.fbx", models / "fx_crate_b.fbx"
        a.write_bytes(_min_fbx())
        b.write_bytes(_min_fbx())
        reg = tmp / "reg" / "assets.sqlite"
        db.init_db(reg).close()
        # the richer row names file A the way a Blender-side index does:
        # OS separators and (Windows) another case; a doubled separator
        # elsewhere. The scanner itself writes Path.as_posix().
        other = (str(a).upper() if os.name == "nt"
                 else str(models) + os.sep * 2 + a.name)
        assert other != a.as_posix(), "fixture: the spellings must differ"
        conn = sqlite3.connect(str(reg))
        conn.executescript(meshes_import.MESHES_DDL)
        conn.execute(
            "INSERT INTO meshes (name,pack,source,kind,fbx,on_disk,triangles,"
            "bbox_x,bbox_y,bbox_z,max_dim) VALUES ('Crate A','FX_Props',"
            "'native','fbx',?,1,777,2.0,2.0,2.0,2.0)", (other,))
        conn.commit()
        conn.close()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summary = scanner.scan_folder(models, db_path=str(reg))
            dry = scanner.scan_folder(models, dry_run=True, db_path=str(reg))

        key = lambda p: os.path.normcase(os.path.normpath(p))   # noqa: E731
        conn = sqlite3.connect(str(reg))
        rows = conn.execute("SELECT id, source, triangles, fbx FROM meshes "
                            "ORDER BY id").fetchall()
        conn.close()
        of_a = [r for r in rows if key(r[3]) == key(str(a))]
        of_b = [r for r in rows if key(r[3]) == key(str(b))]
        assert of_a == [(1, "native", 777, other)], \
            f"file A must keep exactly its native row, untouched: {of_a}"
        assert [(r[1], r[2]) for r in of_b] == [("scan", 1)], \
            f"file B (no richer row) must get one scan row: {of_b}"
        assert summary["meshes"] == 1, \
            f"the summary must count only what was written: {summary}"
        assert dry["meshes"] == 1, \
            f"a dry run must count what a real run would write: {dry}"
        said = [ln for ln in out.getvalue().splitlines()
                if "parked 1 model file" in ln and "native" in ln]
        assert len(said) == 2, \
            "each run must say in one line that 1 file was parked " \
            f"because a richer row exists; printed: {out.getvalue()!r}"


def test_scan_row_goes_once_a_richer_source_covers_its_file():
    """Release check: a file scanned BEFORE the Blender chain measured it
    kept its scan row (scan rows survive every rebuild), so the file showed
    up twice. The next scan must drop that scan row -- and only that one:
    the scan row of a file nobody else covers stays."""
    import contextlib
    import io
    import sqlite3
    from asset_service import db, scanner
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        models = tmp / "models"
        models.mkdir()
        a, b = models / "fx_crate_a.fbx", models / "fx_crate_b.fbx"
        a.write_bytes(_min_fbx())
        b.write_bytes(_min_fbx())
        reg = tmp / "reg" / "assets.sqlite"
        db.init_db(reg).close()
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan_folder(models, db_path=str(reg))
        conn = sqlite3.connect(str(reg))
        first = sorted(s for (s,) in conn.execute("SELECT source FROM meshes"))
        assert first == ["scan", "scan"], f"fixture: first scan wrote {first}"
        other = (str(b).upper() if os.name == "nt"
                 else str(models) + os.sep * 2 + b.name)
        conn.execute(
            "INSERT INTO meshes (name,pack,source,kind,fbx,on_disk,triangles,"
            "bbox_x,bbox_y,bbox_z,max_dim) VALUES ('Crate B','FX_Props',"
            "'native','fbx',?,1,777,2.0,2.0,2.0,2.0)", (other,))
        conn.commit()
        conn.close()

        with contextlib.redirect_stdout(io.StringIO()):
            scanner.scan_folder(models, db_path=str(reg))

        key = lambda p: os.path.normcase(os.path.normpath(p))   # noqa: E731
        conn = sqlite3.connect(str(reg))
        rows = conn.execute("SELECT source, triangles, fbx FROM meshes "
                            "ORDER BY id").fetchall()
        conn.close()
        of_a = [r[:2] for r in rows if key(r[2]) == key(str(a))]
        of_b = [r[:2] for r in rows if key(r[2]) == key(str(b))]
        assert of_b == [("native", 777)], \
            f"file B's scan row must go once a native row covers it: {of_b}"
        assert of_a == [("scan", 1)], \
            f"file A (nobody else covers it) must keep its scan row: {of_a}"


def test_scanner_still_indexes_files_no_richer_source_covers():
    """Control for the two checks above: only a richer row for the SAME file
    stops the scanner. A same-named file in a sibling folder whose name
    shares the prefix is another file; both scanned files get their row,
    and a re-scan neither duplicates nor drops them."""
    import contextlib
    import io
    import sqlite3
    from asset_service import db, meshes_import, scanner
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        models = tmp / "models"
        models.mkdir()
        for name in ("fx_crate_a.fbx", "fx_crate_b.fbx"):
            (models / name).write_bytes(_min_fbx())
        reg = tmp / "reg" / "assets.sqlite"
        db.init_db(reg).close()
        elsewhere = str(tmp / "models_old" / "fx_crate_a.fbx")
        conn = sqlite3.connect(str(reg))
        conn.executescript(meshes_import.MESHES_DDL)
        conn.execute(
            "INSERT INTO meshes (name,pack,source,kind,fbx,on_disk,triangles)"
            " VALUES ('Crate A','FX_Old','native','fbx',?,1,777)",
            (elsewhere,))
        conn.commit()
        conn.close()
        for run in (1, 2):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = scanner.scan_folder(models, db_path=str(reg))
            conn = sqlite3.connect(str(reg))
            got = sorted(conn.execute(
                "SELECT source, name, triangles FROM meshes"))
            conn.close()
            assert got == [("native", "Crate A", 777),
                           ("scan", "fx_crate_a", 1),
                           ("scan", "fx_crate_b", 1)], \
                f"scan {run}: rows are {got}"
            assert summary["meshes"] == 2, f"scan {run}: {summary}"


# ---------------------------------------------------------------------------
# material pick: master defaults never beat the mesh's own maps
# ---------------------------------------------------------------------------

def test_default_map_yields_to_packed_channel():
    """A `default`-source roughness (a master material's fallback: one
    generic grunge texture for a whole pack) won the roughness role, and
    the builder then ignored the slot's own ORM channel (`if not rgh`)."""
    import copy
    from asset_service import scene_manifest as sm
    f = lambda n: f"fx_lib/Textures/{n}.png"                  # noqa: E731
    orm = {"r": "ao", "g": "roughness", "b": "metallic"}
    alb = {"role": "albedo", "param": "BaseColor",
           "file": f("T_FX_Pane_ALB"), "channels": None, "source": "override"}
    nrm = {"role": "normal", "param": "Normal",
           "file": f("T_FX_Pane_NRM"), "channels": None, "source": "override"}
    pack = {"role": "packed", "param": "ORM", "file": f("T_FX_Pane_ORM"),
            "channels": orm, "source": "override"}
    d_rgh = {"role": "roughness", "param": "Pane Roughness",
             "file": f("T_FX_Pane_Grunge"), "channels": None,
             "source": "default"}
    d_hgt = {"role": "height", "param": "Pane Height",
             "file": f("T_FX_Pane_Grunge"), "channels": None,
             "source": "default"}

    # guards (pass on the old code too): what must NOT change
    # no packed map: nothing better claims the role, the fallback is what
    # the engine renders, so it keeps it
    alone = sm.collapse_slot_maps({"maps": [alb, d_rgh]})
    assert alone.get("roughness") == f("T_FX_Pane_Grunge"), alone
    # a channel-less packed map supplies nothing (channels are never
    # invented), so the fallback keeps the role there too
    bare = sm.collapse_slot_maps({"maps": [
        alb, dict(pack, channels=None), d_rgh]})
    assert bare.get("roughness") == f("T_FX_Pane_Grunge") and \
        bare.get("packed_channels") is None, bare
    # packed map and roughness BOTH fallbacks: no better source, as before
    both = sm.collapse_slot_maps({"maps": [
        alb, dict(pack, source="default"), d_rgh]})
    assert both.get("roughness") == f("T_FX_Pane_Grunge"), both
    # the artist's own roughness still wins over a packed channel
    for src in ("override", "unnamed"):
        own = dict(d_rgh, file=f("T_FX_Pane_R"), source=src)
        got = sm.collapse_slot_maps({"maps": [alb, own, pack, d_rgh]})
        assert got.get("roughness") == f("T_FX_Pane_R") and \
            got.get("packed") == f("T_FX_Pane_ORM"), (src, got)

    # the live shape: override ORM + fallback roughness
    slot = {"slot": 0, "material": "MI_FX_Pane",
            "maps": [alb, nrm, pack, d_rgh, d_hgt]}
    before = copy.deepcopy(slot)
    got = sm.collapse_slot_maps(slot)
    assert got.get("albedo") == f("T_FX_Pane_ALB") and \
        got.get("normal") == f("T_FX_Pane_NRM"), got
    assert got.get("packed") == f("T_FX_Pane_ORM") and \
        got.get("packed_channels") == orm, got
    assert "roughness" not in got, \
        f"a default roughness overrides the packed channel: {got['roughness']}"
    assert slot == before, "collapse_slot_maps altered the slot's maps"
    # not a matter of list order (manifest slots are not rank-sorted)
    early = sm.collapse_slot_maps({"maps": [d_rgh, alb, nrm, pack]})
    assert "roughness" not in early and early.get("packed_channels") == orm, \
        early
    # the rule is the packed map's channels, not the word "roughness"
    wide = sm.collapse_slot_maps({"maps": [
        alb, dict(pack, source="unnamed"),
        dict(d_rgh, role="metallic", param="Metallic"),
        dict(d_rgh, role="ao", param="AO Map")]})
    assert "metallic" not in wide and "ao" not in wide, sorted(wide)


def test_default_height_not_picked():
    """A `default`-source height is the master material's stand-in (the
    same grunge / debris texture under every material of a pack); it was
    served as the mesh's own height."""
    import copy
    from asset_service import scene_manifest as sm
    f = lambda n: f"fx_lib/Textures/{n}.png"                  # noqa: E731
    alb = {"role": "albedo", "file": f("T_FX_Sill_ALB"), "source": "override"}
    d_hgt = {"role": "height", "param": "Pane Height",
             "file": f("T_FX_Sill_Grunge"), "channels": None,
             "source": "default"}

    # guards (pass on the old code too): existing rules
    own = sm.collapse_slot_maps({"maps": [
        alb, dict(d_hgt, file=f("T_FX_Sill_H"), source="override"), d_hgt]})
    assert own.get("height") == f("T_FX_Sill_H"), own
    fill = sm.collapse_slot_maps({"maps": [
        alb, {"role": "roughness", "file": f("TX_Fill_FX_R"),
              "source": "default", "placeholder": True}]})
    assert "roughness" not in fill, "a placeholder is picked"
    assert sm.collapse_slot_maps({"resolved": False, "maps": [alb, d_hgt]}) \
        == {}, "a resolved:false slot yields maps"
    assert "packed" not in sm.collapse_slot_maps({"maps": [
        {"role": "mask", "file": f("T_FX_Sill_Mask"), "channels": None,
         "source": "override"}]}), "a mask is used as the packed map"

    slot = {"slot": 0, "material": "MI_FX_Sill", "maps": [alb, d_hgt]}
    before = copy.deepcopy(slot)
    got = sm.collapse_slot_maps(slot)
    assert got.get("albedo") == f("T_FX_Sill_ALB"), got
    assert "height" not in got, \
        f"a default height is served as the mesh's own: {got['height']}"
    assert slot == before, "collapse_slot_maps altered the slot's maps"


def test_primary_skips_default_roughness_and_height():
    """End to end: the registry's `primary` is the same pick, so the
    fallback roughness/height left it too -- and stays in `maps`."""
    from asset_service import meshes_import as mi
    from asset_service import scene_manifest as sm
    ovr = "instance_override:MI_FX_Pane"
    mesh = {"name": "SM_FX_Pane", "asset_path": "/Game/FX/SM_FX_Pane",
            "materials": [{"slot": 0, "name": "MI_FX_Pane",
                           "base": "M_FX_Master", "textures": [
                # manifest order puts the master defaults FIRST
                {"param": "Pane Roughness", "role": "roughness",
                 "file": "Textures/T_FX_Pane_Grunge.png",
                 "source": "material_default"},
                {"param": "Pane Height", "role": "height",
                 "file": "Textures/T_FX_Pane_Grunge.png",
                 "source": "material_default"},
                {"param": "BaseColor", "role": "albedo",
                 "file": "Textures/T_FX_Pane_ALB.png", "source": ovr},
                {"param": "Normal", "role": "normal",
                 "file": "Textures/T_FX_Pane_NRM.png", "source": ovr},
                {"param": "ORM", "role": "packed",
                 "channels": {"r": "ao", "g": "roughness", "b": "metallic"},
                 "file": "Textures/T_FX_Pane_ORM.png", "source": ovr}]}]}
    rec = mi._finish_recipe(mi._leartes_recipe(
        mesh, Path("fx_lib") / "FX_Pack" / "Exports"))
    s0 = rec["slots"][0]
    prim = rec["primary"]
    # guards (pass on the old code too): flagged, never dropped
    assert len(s0["maps"]) == 5, f"maps dropped: {len(s0['maps'])} (want 5)"
    assert [m["source"] for m in s0["maps"]].count("default") == 2, s0["maps"]
    assert Path(prim.get("albedo", "")).name == "T_FX_Pane_ALB.png" and \
        Path(prim.get("normal", "")).name == "T_FX_Pane_NRM.png", prim
    assert Path(prim.get("packed", "")).name == "T_FX_Pane_ORM.png" and \
        prim.get("packed_channels") == \
        {"r": "ao", "g": "roughness", "b": "metallic"}, prim
    assert rec["resolved"] is True and rec["primary_slot"] == 0
    assert prim == sm.collapse_slot_maps(s0), \
        "primary is not the builder's collapse_slot_maps pick"

    assert "roughness" not in prim and "height" not in prim, \
        f"primary serves a master default: {sorted(prim)}"


# ---------------------------------------------------------------------------
# work nobody used: hashing, dead tier, dead constants
# ---------------------------------------------------------------------------

def test_indexer_never_opens_or_hashes_the_hero():
    """The hero file is only ever used as a PATH (pack page, prune check).
    Hashing it read gigabytes per ingest into content_hash / sha256, and
    nothing in the app reads either column."""
    import builtins
    import contextlib
    import io
    import re
    import sqlite3
    from asset_service import indexer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pack = tmp / "lib" / "Animation" / "WallMoves"
        pack.mkdir(parents=True)
        # .blend wins the hero priority and no classifier probe reads it
        (pack / "rig_scene.blend").write_bytes(b"BLENDER-v300" + bytes(64))
        (pack / "climb_up_01.fbx").write_bytes(_min_fbx())
        opened = []
        real_path_open, real_open = Path.open, builtins.open

        def path_open(self, *a, **k):
            opened.append(self.name)
            return real_path_open(self, *a, **k)

        def plain_open(file, *a, **k):
            if isinstance(file, (str, os.PathLike)):
                opened.append(Path(file).name)
            return real_open(file, *a, **k)

        Path.open, builtins.open = path_open, plain_open
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                indexer.index_root(tmp / "lib", tmp / "reg.sqlite")
        finally:
            Path.open, builtins.open = real_path_open, real_open
        conn = sqlite3.connect(str(tmp / "reg.sqlite"))
        row = conn.execute(
            "SELECT content_hash, hero_file_path, technical_meta FROM assets "
            "WHERE id = 'pack::Animation/WallMoves'").fetchone()
        hashed = conn.execute(
            "SELECT COUNT(*) FROM asset_files WHERE sha256 IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        assert row is not None, "pack not indexed"
        assert row[1].endswith("/rig_scene.blend"), row[1]
        problems = []
        if "rig_scene.blend" in opened:
            problems.append("the hero file was opened")
        if re.fullmatch("[0-9a-f]{64}", row[0] or ""):
            problems.append("assets.content_hash holds a file digest")
        if hashed:
            problems.append(f"{hashed} asset_files row(s) carry a sha256")
        if "hashed_partially" in json.loads(row[2] or "{}"):
            problems.append("technical_meta still records hashed_partially")
        assert not problems, "; ".join(problems)


def test_indexer_keeps_pack_whose_hero_is_unreadable():
    """A hero whose BYTES cannot be read (locked by another program, no
    permission) used to skip the whole pack: every other file in it
    disappeared from the registry with it."""
    import contextlib
    import io
    import sqlite3
    from asset_service import indexer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pack = tmp / "lib" / "Animation" / "WallMoves"
        pack.mkdir(parents=True)
        (pack / "rig_scene.blend").write_bytes(b"BLENDER-v300" + bytes(64))
        (pack / "climb_up_01.fbx").write_bytes(_min_fbx())
        real_path_open = Path.open

        def path_open(self, *a, **k):
            if self.name == "rig_scene.blend":
                raise PermissionError(13, "Permission denied", str(self))
            return real_path_open(self, *a, **k)

        Path.open = path_open
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                indexer.index_root(tmp / "lib", tmp / "reg.sqlite")
        finally:
            Path.open = real_path_open
        conn = sqlite3.connect(str(tmp / "reg.sqlite"))
        row = conn.execute(
            "SELECT hero_file_path FROM assets "
            "WHERE id = 'pack::Animation/WallMoves'").fetchone()
        n_files = conn.execute("SELECT COUNT(*) FROM asset_files").fetchone()[0]
        conn.close()
        assert row is not None, "pack skipped because its hero is unreadable"
        assert row[0].endswith("/rig_scene.blend"), row[0]
        assert n_files == 2, n_files


def test_sub_category_follows_the_majority_of_files():
    """A pack of climb clips with a few idle clips was labelled "Idle"
    because Idle is listed before Locomotion: one file was enough to claim
    the pack."""
    from asset_service.auto_classifier import PackInput, classify_pack

    def pack(folder, names):
        return PackInput(name=folder, rel_path=f"Animation/{folder}", files=[
            {"relpath": f"Animation/{folder}/{n}", "ext": ".fbx",
             "size": 10, "file_type": "mesh"} for n in names])

    climbs = [f"climb_up_{i:02d}.fbx" for i in range(1, 7)]
    result = classify_pack(pack("WallMoves", climbs + ["hang_idle.fbx"]))
    assert result.domain == "Human Animations", result.domain
    assert result.sub_category == "Locomotion", result.sub_category
    # a tie keeps the existing list order (Idle is listed before Locomotion)
    tie = classify_pack(pack("WallMoves", ["climb_up_01.fbx", "hang_idle.fbx"]))
    assert tie.sub_category == "Idle", tie.sub_category
    # the pack folder's own name breaks a tie, and decides when no file
    # name helps -- it does not outvote what most files say
    assert classify_pack(pack("ClimbSet", ["climb_up_01.fbx", "hang_idle.fbx"])
                         ).sub_category == "Locomotion"
    assert classify_pack(pack("DanceFloor", ["take_01.fbx", "take_02.fbx"])
                         ).sub_category == "Dance"
    assert classify_pack(pack("DanceFloor", climbs)).sub_category == "Locomotion"
    # whole words: 'stand' is not in 'Standard', 'bow' is not in 'bowl'
    from asset_service.auto_classifier import _sub_category
    walks = PackInput(name="Walks", rel_path="Standard Animations/Walks",
                      files=[{"relpath": f"Standard Animations/Walks/walk_{i}.fbx"}
                             for i in range(5)])
    assert _sub_category("Human Animations", set(), walks) == "Locomotion"
    bowls = PackInput(name="Kitchen_Bowls", rel_path="Props/Kitchen_Bowls",
                      files=[{"relpath": "Props/Kitchen_Bowls/bowl_a.fbx"}])
    assert _sub_category("Props", set(), bowls) != "Weapons"


def test_host_tags_keep_the_most_frequent_tokens():
    """An alphabetical cut at 12 dropped "walk" from packs full of walk
    clips, so a pack search for walk missed them."""
    from asset_service.auto_classifier import PackInput, classify_pack
    names = [f"walk_fwd_{i:02d}.fbx" for i in range(1, 9)]
    names += [f"{w}.fbx" for w in (
        "attack", "chat", "climb", "combat", "dance", "fight", "gesture",
        "idle", "jump", "kick", "punch", "sitting", "sword", "talk")]
    pack = PackInput(name="Street Moves", rel_path="Animation/StreetMoves", files=[
        {"relpath": f"Animation/StreetMoves/{n}", "ext": ".fbx",
         "size": 10, "file_type": "mesh"} for n in names])
    tags = classify_pack(pack).host_tags
    assert len(tags) == 12, tags
    assert "walk" in tags, f"most frequent filename token cut: {tags}"
    assert "street" in tags, f"pack-name token cut: {tags}"


def test_classifier_has_no_network_tier():
    """The vision tier could never run (the indexer passed no preview and
    no config ever carried its endpoint) but kept urllib in a module whose
    contract is that nothing leaves the workstation."""
    import inspect
    from asset_service import auto_classifier as ac
    assert not hasattr(ac, "OllamaVision"), "dead vision tier still defined"
    assert not hasattr(ac, "get_vision_classifier")
    assert "urllib" not in vars(ac), "classifier still imports urllib"
    params = list(inspect.signature(ac.classify_pack).parameters)
    assert params == ["pack"], params


def test_config_constants_all_have_a_reader():
    """A constant in config.py that no module reads is a second place a
    filename can drift from the importer that really owns it."""
    import re
    cfg_py = REPO / "service" / "asset_service" / "config.py"
    text = cfg_py.read_text(encoding="utf-8")
    names = re.findall("^([A-Z][A-Z0-9_]*) = ", text, re.M)
    for folder in ("service", "pipeline", "tools", "extensions", "tests"):
        if (REPO / folder).is_dir():
            for p in (REPO / folder).rglob("*.py"):
                if p != cfg_py:
                    text += p.read_text(encoding="utf-8", errors="replace")
    # one hit is the definition itself
    orphans = [n for n in names if len(re.findall(rf"\b{n}\b", text)) < 2]
    assert not orphans, f"config constants nothing references: {orphans}"


# ---- pack tags: the owner's labels per pack, in a file ----------------------

def _tags_at(tmp):
    """Point the tag file at a temp folder; returns the path to restore."""
    from asset_service import library_map
    old = library_map.PATH
    library_map.PATH = Path(tmp) / "library_map.csv"
    return old


def test_pack_tag_file_reads_what_a_spreadsheet_saves():
    """The tag file is edited by hand: a German spreadsheet saves
    semicolons in cp1252, people tick with x, 1, yes or a check mark and
    write 'Low-Poly'. Pharos adds rows for new packs, never drops one,
    and copies a label set in the registry only when it makes the file."""
    from asset_service import library_map
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "library_map.csv"
        p.write_bytes(("Folder;Kind;Name;Photorealistic;Stylized;Low-Poly;Domain;Tags\r\n"
                       'Kits/Caf\u00e9;Blender File;Caf\u00e9;;yes;X;props;"cosy, wood"\r\n'
                       "Env/Town;;Town;1;;;;\r\n").encode("cp1252"))
        rows = library_map.load(p)
        cafe = rows["kits/caf\u00e9"]
        assert cafe["styles"] == ["Stylized", "Low Poly"], cafe
        assert cafe["domain"] == "Props" and cafe["tags"] == ["cosy", "wood"], cafe
        assert cafe["kind"] == "blender" and cafe["include"], cafe
        assert rows["env/town"]["styles"] == ["Photorealistic"]
        library_map.update("Env/Town", add_styles=["Low Poly"], path=p)
        assert library_map.sync([("Env/Town", "Town"), ("New/Pack", "New")],
                              path=p) == 1
        rows = library_map.load(p)
        assert sorted(rows) == ["env/town", "kits/caf\u00e9", "new/pack"], rows
        assert rows["env/town"]["styles"] == ["Photorealistic", "Low Poly"]
        assert rows["kits/caf\u00e9"]["name"] == "Caf\u00e9", rows
        assert library_map.marker("lowpoly") == "Low Poly"
        assert library_map.marker("cel shaded") == ""
        q = Path(tmp) / "first.csv"
        library_map.sync([("A", "A")], seed={"A": ("Props", "Stylized")}, path=q)
        assert library_map.load(q)["a"]["styles"] == ["Stylized"]
        library_map.sync([("B", "B")], seed={"B": ("Props", "Stylized")}, path=q)
        assert library_map.load(q)["b"]["styles"] == [], "seeded after creation"


def test_model_search_uses_the_owners_pack_tags():
    """A pack's style lived on its pack card only; nothing an agent asks
    reached it. The owner's tags now filter (style=), are words a query
    matches ('low poly tree'), and come back on each model and pack."""
    from asset_service import config, library_map
    lib = config.LIBRARY_ROOT.as_posix()     # paths only: nothing is read
    rows = [("Tree", "Toon", {"fbx": f"{lib}/FX_Toon/Exports/Tree.fbx"}),
            ("Tree", "Real", {"fbx": f"{lib}/FX_Real/Tree.fbx"}),
            ("Rock", "Toon", {"fbx": f"{lib}/FX_Toon/Exports/Rock.fbx"})]

    def run(browse):
        r = browse.api_meshes({"style": ["low-poly"]})
        assert sorted(_names(r)) == ["Rock", "Tree"] and not r.get("warnings"), r
        r = browse.api_meshes({"q": ["low poly tree"]})
        assert [(i["name"], i["pack"]) for i in r["items"]] == [("Tree", "Toon")], r
        assert r["items"][0]["styles"] == ["Stylized", "Low Poly"], r["items"][0]
        assert r["items"][0]["pack_tags"] == ["cartoon"], r["items"][0]
        r = browse.api_meshes({"q": ["tree"], "style": ["Photorealistic"]})
        assert [i["pack"] for i in r["items"]] == ["Real"], r
        assert len(browse.api_meshes({"style": ["Stylized,Low Poly"]})["items"]) == 2
        r = browse.api_meshes({"style": ["cel shaded"]})
        assert r["total"] == 0 and r.get("warnings"), r
        assert sorted(_names(browse.api_meshes({"q": ["cartoon"]}))) == ["Rock", "Tree"]
        packs = {p["pack"]: p for p in browse.api_meshes_packs()["packs"]}
        assert packs["Toon"].get("styles") == ["Stylized", "Low Poly"], packs
        assert "styles" not in packs["Real"] or packs["Real"]["styles"] == ["Photorealistic"]

    with tempfile.TemporaryDirectory() as tmp:
        old = _tags_at(tmp)
        try:
            library_map.sync([("FX_Toon", "Toon"), ("FX_Real", "Real")])
            library_map.update("FX_Toon", styles=["Stylized", "Low Poly"],
                             tags=["cartoon"])
            library_map.update("FX_Real", styles=["Photorealistic"])
            _with_registry(rows, run)
        finally:
            library_map.PATH = old


def test_tag_page_and_retag_write_the_tag_file_not_the_registry():
    """Labels set on the registry page lived only in the registry, which
    users are told to delete and rebuild. The map page and the bulk retag
    write the library map, whose rows come from a survey of the library
    (every folder, with its kind), not from the legacy pack table."""
    import sqlite3
    from asset_service import config, library_map
    from test_fresh_install import _min_fbx

    def post(path, obj):
        return _drive(path, "POST", json.dumps(obj).encode("utf-8"))

    def run(browse):
        conn = sqlite3.connect(browse._DB_PATH)
        conn.execute(
            "INSERT INTO assets (id,name,canonical_root,hero_file_path,"
            "domain,style,sub_category,validation_status,content_hash) "
            "VALUES ('pack::Env/Town','Town','x','x','Environment',"
            "'Photorealistic','General','auto_tagged','h')")
        conn.commit()
        conn.close()
        st, body = _drive("/api/pack_tags")
        assert st == 200, body
        got = {p["folder"]: p for p in body["packs"]}
        assert {"Env/Town", "Props/Crates", "Purchases"} <= set(got), sorted(got)
        assert got["Props/Crates"]["kind"] == "3d-assets", got["Props/Crates"]
        assert got["Purchases"]["kind"] == "purchases" and got["Purchases"]["confirmed"]
        assert got["Env/Town"]["survey"]["kind"] == "3d-assets", got["Env/Town"]
        assert got["Env/Town"]["guess"]["style"] == "Photorealistic", got["Env/Town"]
        st, body = post("/api/pack_tags", {"folder": "Env/Town",
                                           "styles": ["Photorealistic", "low poly"],
                                           "tags": "wild west; dusty"})
        assert st == 200, body
        assert body["pack"]["styles"] == ["Photorealistic", "Low Poly"], body
        assert body["pack"]["tags"] == ["wild west", "dusty"], body
        # kind, include and the decision are the map's too
        st, body = post("/api/pack_tags", {"folder": "Env/Town", "kind": "Unreal Scene"})
        assert st == 200 and body["pack"]["kind"] == "unreal", body
        # a folder of that kind takes the kind's decision (it covers the
        # packs below it); a word that is no decision is refused
        st, body = post("/api/pack_tags", {"folder": "Env/Town", "action": "export"})
        assert st == 200 and body["pack"]["action"] == "export", body
        assert post("/api/pack_tags", {"folder": "Env/Town", "action": "fly"})[0] == 400
        st, body = post("/api/pack_tags", {"folder": "Props/Crates", "include": False})
        assert st == 200 and body["pack"]["include"] is False, body
        assert post("/api/pack_tags", {"folder": "Env/Town", "styles": ["Cel"]})[0] == 400
        assert post("/api/pack_tags", {"folder": "Nope", "styles": []})[0] == 404
        st, body = post("/api/retag", {"ids": ["pack::Env/Town"],
                                       "set": {"style": "Stylized", "domain": "Props"}})
        assert st == 200 and body["updated"] == 1, body
        row = library_map.load()["env/town"]
        assert row["styles"] == ["Stylized", "Low Poly"], row
        assert row["domain"] == "Props", row
        conn = sqlite3.connect(browse._DB_PATH)
        assert conn.execute("SELECT style, domain FROM assets WHERE "
                            "id='pack::Env/Town'").fetchone() == (
            "Photorealistic", "Environment"), "retag wrote the registry"
        conn.close()
        packs = browse.api_packs({"style": ["Low Poly"]})
        assert [p["id"] for p in packs] == ["pack::Env/Town"], packs
        assert packs[0]["owner_tagged"] and packs[0]["domain"] == "Props", packs
        st, page = _drive("/tags")
        assert st == 200 and b"/api/pack_tags" in page
        # the config mirrors the map: the excluded folder left the scan
        # list (its packs with it) and is skipped by the indexer
        assert "Props" in (config.load().get("scan_folders") or []), config.load()
        st, body = post("/api/pack_tags", {"folder": "Props", "include": False})
        assert st == 200 and body["pack"]["include"] is False, body
        cfg = config.load()
        assert not [f for f in cfg.get("scan_folders") or [] if f.split("/")[0] == "Props"], cfg
        assert "Props" in (cfg.get("indexer_skip_dirs") or []), cfg.get("indexer_skip_dirs")

    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Lib"
        for folder in ("Env/Town", "Props/Crates"):
            (lib / folder).mkdir(parents=True)
            (lib / folder / "m.fbx").write_bytes(_min_fbx())
        (lib / "Purchases").mkdir()
        (lib / "Purchases" / "orders.csv").write_text(
            "Name,Product URL,Price (USD)\nA,https://e/a,1\n", encoding="utf-8")
        from asset_service import browse
        old_path, old_root = library_map.PATH, config.LIBRARY_ROOT
        old_canon = list(browse.CANONICAL_ROOTS)      # bound at import
        old_cf = config._CONFIG_FILE                   # the page mirrors the map into it
        library_map.PATH = lib / "_Agent_Files" / "library_map.csv"
        config.LIBRARY_ROOT = lib
        config._CONFIG_FILE = Path(tmp) / "pharos_config.json"
        config.save({**config._DEFAULTS, "library_root": str(lib),
                     "registry_dir": str(Path(tmp) / "reg")})
        browse.CANONICAL_ROOTS[:] = [lib]
        try:
            _with_registry([], run)
        finally:
            library_map.PATH, config.LIBRARY_ROOT = old_path, old_root
            browse.CANONICAL_ROOTS[:] = old_canon
            config._CONFIG_FILE = old_cf

def test_setup_flow_guards_folders_and_jobs():
    """Behind /setup: the folder browser lists folders only and refuses
    files, relative and missing paths; browsing is for loopback binds; a
    job refuses a missing root, a whole drive and a second job while one
    runs; the roots follow a reloaded config."""
    from asset_service import setup_flow, config
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "A").mkdir()
        (root / ".hidden").mkdir()
        (root / "$RECYCLE.BIN").mkdir()
        (root / "f.txt").write_text("x", encoding="utf-8")
        d, code = setup_flow.api_fs_list(str(root))
        assert code == 200 and [x["name"] for x in d["dirs"]] == ["A"], d
        assert d["parent"] and not d["dirs"][0]["has_children"]
        assert setup_flow.api_fs_list(str(root / "f.txt"))[1] == 400
        assert setup_flow.api_fs_list("relative/path")[1] == 400
        assert setup_flow.api_fs_list(str(root / "nope"))[1] == 404
        assert setup_flow.api_fs_list("")[1] == 400
        assert setup_flow.api_fs_list("\\\\host\\share")[1] == 400, "UNC"
        assert setup_flow.api_fs_list("//host/share")[1] == 400, "UNC"
        assert setup_flow.fs_allowed("127.0.0.1") and setup_flow.fs_allowed("::1")
        assert not setup_flow.fs_allowed("0.0.0.0") and not setup_flow.fs_allowed("192.168.1.5")
        drives = setup_flow.api_fs_drives()
        assert drives["drives"] and drives["home"]
        st = setup_flow.api_state("serving")
        assert set(st) >= {"configured", "mode", "pending", "summary", "next", "progress", "rows"}
        assert st["progress"]["state"] in ("idle", "running", "done", "failed")
        # jobs refused before anything starts (nothing is spawned here)
        assert setup_flow.begin("survey", {"root": str(root / "nope")})[1] == 400
        assert setup_flow.begin("survey", {"root": "relative"})[1] == 400
        assert setup_flow.begin("survey", {"root": root.anchor})[1] == 400, "a whole drive"
        assert setup_flow.begin("survey", {})[1] == 400
        assert setup_flow.begin("survey", {"root": str(root), "registry_dir": "rel"})[1] == 400
        assert setup_flow.begin("survey", {"root": "\\\\host\\share"})[1] == 400
        assert setup_flow.begin("bogus", {})[1] == 400
        saved = dict(setup_flow._STATE)
        setup_flow._STATE.update(state="running", job="survey")
        try:
            assert setup_flow.begin("survey", {"root": str(root)})[1] == 409
            assert setup_flow.begin("ingest", {})[1] == 409
        finally:
            setup_flow._STATE.update(saved)
        # the chains the decisions ask for
        rows = {"ue": {"folder": "UE", "kind": "unreal", "state": "raw", "include": True,
                       "action": "export", "confirmed": True},
                "ue/p": {"folder": "UE/P", "kind": "unreal", "state": "raw", "include": True,
                         "action": "", "confirmed": False},
                "anim": {"folder": "Anim", "kind": "animation", "state": "", "include": True,
                         "action": "no-previews", "confirmed": True},
                "kits": {"folder": "Kits", "kind": "blender", "state": "kit", "include": False,
                         "action": "extract", "confirmed": True}}
        nxt = setup_flow.next_steps(rows)
        assert [n["command"] for n in nxt] == ["python pharos.py convert"], nxt
        assert nxt[0]["folders"] == ["UE", "UE/P"], nxt
        # a reloaded config moves every derived root, in this module and
        # the ones that register with it
        from asset_service import library_map, browse
        old_cf = config._CONFIG_FILE
        config._CONFIG_FILE = root / "pharos_config.json"
        try:
            config.save({**config._DEFAULTS, "library_root": str(root / "Lib"),
                         "registry_dir": str(root / "Reg")})
            config.reload()
            assert config.LIBRARY_ROOT == (root / "Lib").resolve()
            assert config.DB_PATH == str((root / "Reg").resolve() / "assets.sqlite")
            assert library_map.PATH == (root / "Lib" / "_Agent_Files" / "library_map.csv").resolve()
            assert browse.CANONICAL_ROOTS == [config.LIBRARY_ROOT] and browse._DB_PATH == config.DB_PATH
            assert config.TEX_ROOT == config.LIBRARY_ROOT / config._DEFAULTS["sections"]["textures"]
        finally:
            config._CONFIG_FILE = old_cf
            config.reload()


def test_mesh_facts_are_served_and_filter():
    """Measured facts ride on the short item (pivot, units, up axis,
    lods, skin, scale_suspect, category) and filter: pivot=, skin=,
    lod=0, units=; unmeasured rows fall out of a fact filter."""
    from asset_service import browse
    base = {"pivot": "base-centre", "units": "cm", "up_axis": "Y", "geo_up_axis": "Z",
            "category_hint": ["Props", "Seating"]}
    rows = [("Chair A", "P", {"facts": base}),
            ("Chair A LOD1", "P", {"facts": {**base, "lod_level": 1}}),
            ("Chair B", "P", {"facts": {**base, "pivot": "corner", "units": "m"}}),
            ("Hero", "P", {"facts": {**base, "skin": True, "bones": 65, "scale_suspect": True}}),
            ("SK Doctor", "P", {"kind": "SkeletalMesh"}),   # a manifest row: skinned by kind
            ("Old Chair", "P")]              # indexed before measure: no facts
    old = browse._DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        browse._DB_PATH = _search_registry(tmp, rows)
        try:
            got = {i["name"]: i for i in browse.api_meshes({"q": ["chair"], "size": ["50"]})["items"]}
            a = got["Chair A"]
            assert a["pivot"] == "base-centre" and a["units"] == "cm" and a["up_axis"] == "Z" \
                and a["category"] == "Props/Seating" and a["lods"] == 1, a
            assert "lods" not in got["Chair A LOD1"] and got["Chair A LOD1"]["lod"] == 1
            assert "pivot" not in got["Old Chair"]
            h = browse.api_meshes({"q": ["hero"]})["items"][0]
            assert h["skin"] and h["bones"] == 65 and h["scale_suspect"]
            names = lambda **kw: sorted(i["name"] for i in browse.api_meshes(   # noqa: E731
                {k: [v] for k, v in kw.items()})["items"])
            assert names(q="chair", pivot="base-centre") == ["Chair A", "Chair A LOD1"]
            assert names(q="chair", pivot="corner") == ["Chair B"]
            # the unmeasured chair is neither in nor out: it is disclosed
            r = browse.api_meshes({"q": ["chair"], "pivot": ["corner"]})
            assert r["unmeasured_excluded"] == 1, r
            assert names(q="chair", lod="0") == ["Chair A", "Chair B", "Old Chair"]
            assert names(q="chair", units="m") == ["Chair B"]
            assert names(skin="1") == ["Hero", "SK Doctor"]
            assert browse.api_meshes({"q": ["doctor"]})["items"][0]["skin"] is True
            assert "Hero" not in names(q="chair hero", skin="0")
        finally:
            browse._DB_PATH = old


def test_clips_api_serves_measured_facts_and_filters():
    """With a clips table the animation tree and search answer from it:
    counts per folder, the facts per clip, min_dur / max_dur /
    root_motion / family filters, and the skeleton records from stored
    bone names (no file is opened)."""
    import sqlite3
    from asset_service import browse, clips_import, skeleton
    rows = [("Walk Fwd", "Animation/Walks", "Animation/Walks/walk_fwd.fbx", 1.2, 1, ["root", "hips", "spine"]),
            ("Walk Back", "Animation/Walks", "Animation/Walks/Sub/walk_back.fbx", 0.8, 0, ["root", "hips", "spine"]),
            ("Idle Long", "Animation/Idles", "Animation/Idles/idle_long.fbx", 12.0, 0, ["mixamorig:Hips", "mixamorig:Spine", "mixamorig:Head", "mixamorig:Neck"]),
            ("Hop", "Animation/Idles", "Animation/Idles/hop.bvh", None, None, [])]
    with tempfile.TemporaryDirectory() as tmp:
        dbp = _search_registry(tmp, [("Body", "Chars", {"kind": "SkeletalMesh", "fbx": str(Path(tmp) / "Chars" / "body.fbx"),
                                                       "facts": {"skin": True, "bone_names": ["root", "hips", "spine"]}})])
        conn = sqlite3.connect(dbp)
        clips_import.ensure_table(conn)
        for name, folder, rel, dur, rm, bones in rows:
            conn.execute("INSERT INTO clips (rel,folder,name,ext,bytes,frames,fps,duration_s,root_motion,"
                         "has_mesh,bones,skeleton,family,bone_names,facts,measured_at) VALUES "
                         "(?,?,?,?,1,?,30.0,?,?,0,?,?,?,?,'{}','t')",
                         (rel, folder, name.replace(" ", "_").lower(), rel[-4:],
                          int(dur * 30) if dur else None, dur, rm, len(bones),
                          skeleton.signature(bones) if bones else None,
                          skeleton.family(bones) if bones else None, json.dumps(bones)))
        conn.execute("INSERT INTO clips (rel,folder,name,ext,bytes,bone_names,facts,measured_at) "
                     "VALUES ('Old/Stale/x.fbx','Old/Stale','x','.fbx',1,'[]','{}','t')")
        conn.commit()
        conn.close()
        old_roots = list(browse.CANONICAL_ROOTS)
        old = browse._DB_PATH
        browse._DB_PATH = dbp
        browse.CANONICAL_ROOTS[:] = [Path(tmp)]
        try:
            tree = browse.api_anim_tree()
            assert tree["measured"] and {f["folder"]: f["clips"] for f in tree["folders"]} == \
                {"Animation/Idles": 2, "Animation/Walks": 2}, tree["folders"]
            walks = next(f for f in tree["folders"] if f["folder"] == "Animation/Walks")
            sk = walks["skeletons"][0]
            assert sk["bones"] == 3 and sk["clips"] == 2 and sorted(sk["subfolders"]) == ["", "Sub"] \
                and sk["bodies"] == ["Chars/body.fbx"], sk
            idles = next(f for f in tree["folders"] if f["folder"] == "Animation/Idles")
            assert idles["skeletons"][0]["family"] == "mixamo" and idles["skeletons"][0]["bodies"] == []
            got = browse.api_anim_clips({"q": ["walk"], "size": ["10"]})
            assert got["total"] == 2 and all("duration_s" in c for c in got["clips"])
            fwd = next(c for c in got["clips"] if c["title"] == "walk fwd")
            assert fwd["duration_s"] == 1.2 and fwd["fps"] == 30.0 and fwd["frames"] == 36 \
                and fwd["root_motion"] is True and fwd["bones"] == 3, fwd
            assert got["bodies"]["pack::Animation/Walks"][0]["bodies"] == ["Chars/body.fbx"]
            titles = lambda **kw: sorted(c["title"] for c in browse.api_anim_clips(   # noqa: E731
                {k: [v] for k, v in kw.items()})["clips"])
            assert titles(min_dur="1", max_dur="5") == ["walk fwd"]
            r = browse.api_anim_clips({"max_dur": ["1"]})
            assert sorted(c["title"] for c in r["clips"]) == ["walk back"] and r["unmeasured_excluded"] == 1
            assert titles(root_motion="1") == ["walk fwd"]
            assert titles(root_motion="0") == ["idle long", "walk back"]
            assert titles(family="mixamo") == ["idle long"]
            assert titles(packs="Animation/Idles") == ["hop", "idle long"]
            assert "Old/Stale" not in {f["folder"] for f in tree["folders"]}
            assert browse.api_clips({"pack": ["pack::Animation/Walks"]})["files"] == \
                ["Animation/Walks/Sub/walk_back.fbx", "Animation/Walks/walk_fwd.fbx"]
        finally:
            browse._DB_PATH = old
            browse.CANONICAL_ROOTS[:] = old_roots


def test_short_items_repeat_nothing_and_name_owned_purchases():
    """A third of a search answer repeated itself (fbx = root + key,
    max_dim_m / height_m = bbox_m, on_disk = 1 on every row), while the
    one signal the owner meant on_disk to be -- "I own this, ask me" --
    never reached a model search: a not-downloaded purchase has no model
    rows, so an agent searching models saw files on disk only."""
    import sqlite3
    from asset_service import collection_import, config
    lib = config.LIBRARY_ROOT.as_posix()
    rows = [("Bench", "Park", {"fbx": f"{lib}/Park/Bench.fbx", "dim": 2}),
            ("Crate", "Elsewhere", {"fbx": "E:/Elsewhere/Crate.fbx"})]

    def run(browse):
        conn = sqlite3.connect(browse._DB_PATH)
        conn.executescript(collection_import.COLLECTION_DDL)
        for name, avail, tags in (
                ("Medieval Market Benches", "owned-not-downloaded", '["bench"]'),
                ("Park Benches Local", "local", '["bench"]'),
                ("Desert Rocks", "owned-not-downloaded", '["rock"]'),
                ("Bench Tool", "owned-not-downloaded", '["bench"]')):
            conn.execute(
                "INSERT INTO collection (name, folder, store, image_count,"
                " availability, tags, url, type_group) VALUES (?, 'x', 'FAB',"
                " 4, ?, ?, 'https://example.invalid/p', ?)",
                (name, avail, tags, "Tool" if "Tool" in name else "3D Model"))
        conn.commit()
        conn.close()
        r = browse.api_meshes({"q": ["bench"]})
        item = r["items"][0]
        for gone in ("fbx", "max_dim_m", "height_m", "id", "on_disk"):
            assert gone not in item, (gone, item)
        assert r["root"] + "/" + item["key"] == f"{lib}/Park/Bench.fbx", (r["root"], item)
        assert item["bbox_m"] == [2, 2, 2], item
        own = r.get("owned_not_downloaded") or {}
        assert [i["name"] for i in own.get("items", [])] == [
            "Medieval Market Benches"], own
        assert own["total"] == 1 and "avail=notdown" in own["more"], own
        assert own["items"][0]["url"] and own["items"][0]["image_count"] == 4, own
        assert "owned_not_downloaded" not in browse.api_meshes(
            {"q": ["bench"], "page": ["2"]}), "repeated on every page"
        # checked, none -- not silence
        assert browse.api_meshes({"q": ["lamp"]})["owned_not_downloaded"] == {"total": 0}
        # the search's style applies to the purchases too
        assert browse.api_meshes({"q": ["bench"], "style": ["Stylized"]})[
            "owned_not_downloaded"] == {"total": 0}
        assert "owned_not_downloaded" not in browse.api_meshes(
            {"q": ["bench"], "pack": ["Park"]}), "one pack asked for"
        # a model theme is no purchase word: it must not empty the list
        assert browse.api_meshes({"q": ["bench"], "theme": ["prop"]})[
            "owned_not_downloaded"]["total"] == 1
        full = browse.api_meshes_item({"key": [item["key"]]})["items"][0]
        assert full["fbx"] == f"{lib}/Park/Bench.fbx" and full["id"], full
        assert "fbx_path" not in full and "max_dim" not in full, full
        # a file outside the library: root + key would be wrong
        out = browse.api_meshes({"q": ["crate"]})["items"][0]
        assert out["fbx"] == "E:/Elsewhere/Crate.fbx" == out["key"], out

    _with_registry(rows, run)


def test_filters_that_remove_every_exact_match_say_so():
    """'medieval chair' with max_h=1 removed the one exact chair and fell
    back to partial matches -- sci-fi chairs -- without a word. The answer
    now says how many models match every word but not the filters."""
    rows = [("Medieval Chair", "Old", {"dim": 1.4}),
            ("Office Chair", "Modern", {"dim": 0.9})]

    def run(browse):
        r = browse.api_meshes({"q": ["medieval chair"], "max_h": ["1"]})
        assert r["mode"] == "or-fallback", r
        w = " ".join(r.get("warnings") or [])
        assert "1 model(s) match every query word" in w and "max_h=1" in w, r
        assert "warnings" not in browse.api_meshes({"q": ["medieval chair"]})
    _with_registry(rows, run)


def test_sorts_keep_relevance_and_put_copies_last():
    """sort=triangles ordered by triangles alone: logs and loaves that
    only matched 'cart' through a material came before the first cart,
    and lower LODs, collision hulls and flat cards won 'lowest poly'."""
    ctx_cart = '{"stems":[],"themes":[],"facets":{},"ctx":["cart"]}'
    rows = [("Cart", "P", {"tris": 500}),
            ("Cart LOD1", "P", {"tris": 100}),
            ("UCX Cart", "P", {"tris": 12}),
            ("Cart Impostor", "P", {"tris": 8}),
            ("Log", "P", {"tris": 10, "meta": ctx_cart})]

    def run(browse):
        r = browse.api_meshes({"q": ["cart"], "sort": ["triangles"]})
        assert _names(r)[0] == "Cart" and _names(r)[-1] == "Log", _names(r)
        by = {i["name"]: i for i in r["items"]}
        assert by["Cart LOD1"]["lod"] == 1 and by["UCX Cart"]["collider"]
        assert by["Cart Impostor"]["card"] and "lod" not in by["Cart"], by
        # a real billboard prop is no card, a pack's only copy no lower LOD,
        # and 'Col' inside a camelCase word no collider
        other = {i["name"]: i for i in browse.api_meshes(
            {"q": ["billboard stall papercol"], "size": ["50"]})["items"]}
        assert set(other) == {"Billboard Large", "Stall LOD1", "PaperCol A"}, other
        assert not any(k in i for i in other.values()
                       for k in ("card", "lod", "collider")), other
        # a flat 2-triangle quad is flagged, but it may be a real floor:
        # it is not ranked behind the heavier floors
        floors = browse.api_meshes({"q": ["floor"], "sort": ["triangles"]})
        assert _names(floors) == ["Floor Quad", "Floor Heavy"], _names(floors)
        assert floors["items"][0]["card"] is True, floors["items"][0]
    _with_registry(rows + [("Billboard Large", "Q", {"tris": 4000}),
                           ("Stall LOD1", "Q"),
                           ("PaperCol A", "Q", {"object_name": "PaperCol_A"}),
                           ("Floor Quad", "Q", {"tris": 2, "dim": 0}),
                           ("Floor Heavy", "Q", {"tris": 900})],
                   run)


def test_excluding_one_lod_keeps_the_others():
    """'-lod1' was cut to 'lod' and removed every LOD row, LOD0 included:
    the only way to drop lower LODs returned nothing."""
    tags = '["crate", "lod"]'
    rows = [("Crate01 LOD0", "W", {"tags": tags}),
            ("Crate01 LOD1", "W", {"tags": tags})]

    def run(browse):
        assert _names(browse.api_meshes({"q": ["crate -lod1"]})) == [
            "Crate01 LOD0"]
    _with_registry(rows, run)


def test_unmeasured_count_is_about_the_matches():
    """unmeasured_excluded counted every unmeasured row of the library,
    so a filtered 'bench' search reported hidden rows that were lamps."""
    rows = [("Bench", "P", {"dim": 1}), ("Lamp", "P", {"dim": 0})]

    def run(browse):
        r = browse.api_meshes({"q": ["bench"], "max_dim": ["2"]})
        assert r["unmeasured_excluded"] == 0 and _names(r) == ["Bench"], r
        r = browse.api_meshes({"q": ["lamp"], "max_dim": ["2"]})
        assert r["unmeasured_excluded"] == 1, r
    _with_registry(rows, run)


def test_scene_words_reach_their_names():
    """A market stall is sold as a booth or tent; cobblestone paving is
    named after its stones. One way only: as a two-way group, 'tent'
    found candle stands and 'cobblestone' a sound 'setting down'."""
    from asset_service import textmatch as tm
    rows = [("Market Tent", "P"), ("Old Paving", "P"), ("Candle Stand", "P"),
            ("Market Stall", "P"), ("Kettle Setting Down", "P")]

    def run(browse):
        assert _names(browse.api_meshes({"q": ["stall"]})) == [
            "Market Stall", "Market Tent"]
        assert _names(browse.api_meshes({"q": ["cobblestone"]})) == [
            "Old Paving"]
        assert _names(browse.api_meshes({"q": ["tent"]})) == ["Market Tent"]
    _with_registry(rows, run)
    assert not tm.synonyms("tent") & {"stall", "kiosk", "booth"}


def test_object_words_do_not_make_a_model_medieval():
    """'lantern', 'cart', 'market', 'tower' gave the theme 'medieval' to
    every lantern of the library, so theme=medieval filtered nothing. The
    theme comes from period words and from a medieval pack."""
    from asset_service import meshes_import

    def themes(name, pack):
        rec = meshes_import._record(
            {"name": name, "pack": pack, "bbox_m": [1, 1, 1]},
            dict(meshes_import.EMPTY_RECIPE), None, None)
        return set(json.loads(rec["meta"])["themes"])

    assert "medieval" not in themes("SM_Lantern_01", "FX_NeonCity")
    assert "medieval" not in themes("SM_Shopping_Cart", "FX_Mall")
    assert "medieval" in themes("SM_Lantern_01", "FX_MedievalTown")
    assert "medieval" in themes("SM_Castle_Wall", "FX_Any")
    # a kit below a generic section folder: the table knows the kit by the
    # folder its file sits in, the pack name is the section's
    meshes_import.PACK_THEMES["fx_oldtown"] = ["medieval"]
    try:
        rec = meshes_import._record(
            {"name": "SM_Tower_A", "pack": "FX_Kits", "bbox_m": [1, 1, 1],
             "fbx": "x:/FX_Kits/FX_OldTown/all.fbx"},
            dict(meshes_import.EMPTY_RECIPE), None, None)
        assert "medieval" in json.loads(rec["meta"])["themes"], rec["meta"]
    finally:
        del meshes_import.PACK_THEMES["fx_oldtown"]


def test_sound_texture_and_clip_answers_name_their_files():
    """Mesh answers had a root; sounds, texture sets and clips did not, so
    writing a scene file took extra calls and path guesses. Each answer
    now gives the folder its paths start from, texture items their folder
    and clip answers the bodies each folder's clips play on."""
    from asset_service import browse
    r = _with_registry([], lambda b: b.api_audio_items({}),
                       audio=[("rain", "Weather", 10)])
    assert r["root"] == browse.AUDIO_ROOT.as_posix() and r["items"][0]["rel"], r
    r = _with_registry([], lambda b: b.api_anim_clips({}),
                       clips=["Animation/Moves/walk.fbx"])
    assert r["root"] == browse.CANONICAL_ROOTS[0].as_posix(), r
    assert isinstance(r.get("bodies"), dict), r
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Textures_Materials"
        _tf_library(root)
        dbp = Path(tmp) / "reg.sqlite"
        _tf_import(dbp, root)
        old = browse._DB_PATH
        browse._DB_PATH = str(dbp)
        try:
            t = browse.api_textures_items({})
        finally:
            browse._DB_PATH = old
    item = t["items"][0]
    assert t["root"] and item.get("folder"), (t["root"], item)
    assert (t["root"] + "/" + item["folder"]).replace("\\", "/").lower() \
        .startswith(t["root"].lower()), item


def test_scene_file_takes_search_keys():
    """A scene file needed absolute paths copied out of full records and
    pasted recipes; a key with '#object' could not name the object. The
    validator now resolves keys to the file and the object, pulls the
    registry recipe for "materials": "recipe", rejects files the builder
    cannot import, and budgets one object -- not its whole file."""
    import sqlite3
    sys.path.insert(0, str(REPO / "service" / "asset_service"))
    import scene_manifest as sm
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Lib"
        (root / "Kit").mkdir(parents=True)
        (root / "Pack").mkdir()
        for f in ("Kit/room.fbx", "Pack/bench.fbx"):
            (root / f).write_bytes(b"x")
        recipe = json.dumps({"slots": [{"slot": "M_Wood", "maps": []}],
                             "primary": {}, "resolved": True})
        rows = [("Chair", "Kit", {"fbx": str(root / "Kit" / "room.fbx"),
                                  "object_name": "chair", "tris": 100}),
                ("Desk", "Kit", {"fbx": str(root / "Kit" / "room.fbx"),
                                 "object_name": "desk", "tris": 900}),
                ("Bench", "Pack", {"fbx": str(root / "Pack" / "bench.fbx"),
                                   "recipe": recipe, "tris": 50})]
        dbp = _search_registry(tmp, rows)
        data = {"assets": [
            {"id": "c", "key": "Kit/room.fbx#chair"},
            {"id": "b", "key": "Pack/bench.fbx", "materials": "recipe"},
            {"id": "x", "key": "Pack/nothing.fbx"}]}
        errs = sm.resolve_keys(data, dbp, root)
        c, b = data["assets"][0], data["assets"][1]
        assert c["fbx"].endswith("Kit/room.fbx") and c["object"] == "chair", c
        assert b["materials"] == {"slots": [{"slot": "M_Wood", "maps": []}]}, b
        assert len(errs) == 1 and "Pack/nothing.fbx" in errs[0], errs
        # a record copied from the API carries key AND fbx: the object of
        # the key still counts (the whole file was built for one chair)
        both = {"assets": [{"id": "d", "key": "Kit/room.fbx#desk",
                            "fbx": str(root / "Kit" / "room.fbx")}]}
        assert sm.resolve_keys(both, dbp, root) == [], both
        assert both["assets"][0]["object"] == "desk", both
        # a multi-object file without '#object' is refused, with the reason
        bare = sm.resolve_keys({"assets": [{"id": "k", "key": "Kit/room.fbx"}]},
                               dbp, root)
        assert len(bare) == 1 and "2 objects" in bare[0], bare
        # a wrong object is named as the problem, with the names there are
        wrong = sm.resolve_keys(
            {"assets": [{"id": "k", "key": "Kit/room.fbx#sofa"}]}, dbp, root)
        assert len(wrong) == 1 and "'sofa'" in wrong[0]             and "chair, desk" in wrong[0], wrong
        split = {"assets": [{"id": "k", "key": "Kit/room.fbx",
                             "object": "desk"}]}
        assert sm.resolve_keys(split, dbp, root) == [], split
        # ... and by PATH it is still imported whole, but not silently
        fbx = str(root / "Kit" / "room.fbx")
        notes = sm.whole_file_notes({"assets": [{"id": "w", "fbx": fbx}]}, dbp)
        assert len(notes) == 1 and "2 objects" in notes[0]             and "1,000 triangles" in notes[0], notes
        assert sm.whole_file_notes({"assets": [
            {"id": "c", "fbx": fbx, "object": "chair"},
            {"id": "b", "fbx": str(root / "Pack" / "bench.fbx")}]}, dbp) == []

        assert sm.check_paths({"assets": [c, b]}) == []
        budget = sm.budget_check({"assets": [c]}, dbp)
        assert budget["total_triangles"] == 100, budget
        whole = sm.budget_check({"assets": [{"fbx": c["fbx"]}]}, dbp)
        assert whole["total_triangles"] == 1000, whole
        bad = sm.validate_manifest({"assets": [
            {"id": "m", "fbx": str(root / "a.max")},
            {"id": "r", "fbx": str(root / "Pack" / "bench.fbx"),
             "materials": "recipe"}]})
        assert any("'.max'" in e for e in bad), bad
        assert any("needs 'key'" in e for e in bad), bad


def test_texture_sets_get_their_colour_map():
    """The builder guessed map roles from substrings of file names: a set
    named '<name>_Color' / '<name>_NormalDX' / '<name>_NormalGL' was built
    grey with the DirectX normal map. One reader now decides, the same
    one the texture search uses, and an indexed set takes the maps the
    registry serves."""
    sys.path.insert(0, str(REPO / "service" / "asset_service"))
    import scene_manifest as sm
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Textures_Materials"
        loose = root / "Loose"
        loose.mkdir(parents=True)
        names = ["FX_Paving_4K_Color.png", "FX_Paving_4K_NormalDX.png",
                 "FX_Paving_4K_NormalGL.png", "FX_Paving_4K_Roughness.png",
                 "FX_Paving_4K_Displacement.png",
                 "FX_Other_4K_Color.png", "FX_Other_4K_NormalGL.png"]
        for n in names:
            (loose / n).write_bytes(_tf_png(8, 8))
        none = str(Path(tmp) / "no_registry.sqlite")
        entry = {"set": "FX Paving", "folder": str(loose)}
        maps, source = sm.texture_maps(entry, none)
        got = {r: Path(f).name for r, f in maps.items()}
        assert source == "names" and got == {
            "albedo": "FX_Paving_4K_Color.png",
            "normal": "FX_Paving_4K_NormalGL.png",
            "roughness": "FX_Paving_4K_Roughness.png"}, (source, got)
        # a name that matches no file: every map of the folder, said so
        maps, source = sm.texture_maps(
            {"set": "Display Name", "folder": str(loose)}, none)
        assert source == "folder" and "albedo" in maps, (source, maps)
        # an indexed set: the registry's maps
        own = root / "FX_Paving_4K"          # a set in its own folder
        own.mkdir()
        for n in names[:5]:
            (own / n).write_bytes(_tf_png(8, 8))
        dbp = Path(tmp) / "reg.sqlite"
        rows = _tf_import(dbp, root)
        row = next(r for r in rows if r["folder"].endswith("FX_Paving_4K"))
        maps, source = sm.texture_maps(
            {"set": row["name"], "folder": row["folder"]}, str(dbp))
        assert source == "registry", (source, row["name"], row["folder"])
        assert Path(maps["normal"]).name == "FX_Paving_4K_NormalGL.png"             and Path(maps["albedo"]).name == "FX_Paving_4K_Color.png", maps
        # an unindexed name that several sets share: every map from ONE of
        # them (a 'Roof' was assembled from three different roof sets)
        for sub, files in (("red_roof", ["red_roof_diffuse.png"]),
                           ("green_roof", ["Green_Roofing_NRM.png",
                                           "Green_Roofing_rough.png"])):
            (loose / sub).mkdir()
            for n in files:
                (loose / sub / n).write_bytes(_tf_png(8, 8))
        maps, _ = sm.texture_maps({"set": "Roof", "folder": str(loose)}, none)
        assert len({Path(f).parent for f in maps.values()}) == 1, maps
        assert Path(maps["albedo"]).name == "red_roof_diffuse.png", maps
        # an indexed set that is one plain photo: that photo is its colour
        # map, and nothing is guessed from other files
        import sqlite3
        photo = root / "FX_Roof.png"
        photo.write_bytes(_tf_png(8, 8))
        conn = sqlite3.connect(str(dbp))
        conn.execute(
            "INSERT INTO textures (name, grp, sub, folder, files, images,"
            " file_count, bytes, first_image, thumb, tags, meta, source)"
            " VALUES ('FX Roof', 'g', 's', ?, '[]', '[]', 1, 1, ?, ?, '[]',"
            " '{}', 'crawl')", (root.as_posix(), photo.as_posix(),
                                photo.as_posix()))
        conn.commit()
        conn.close()
        maps, source = sm.texture_maps(
            {"set": "FX Roof", "folder": str(root)}, str(dbp))
        assert source == "registry" and maps == {"albedo": photo.as_posix()},             (source, maps)


def test_agent_start_page_lists_what_to_read_when():
    """START_HERE said 'read the files listed here IN THIS ORDER' and had
    no list; read literally it pulled in six files, setup guides among
    them, and it wrote '<repo>' where it knew the path. Scene building
    sent agents into a 13 KB capabilities file and the code; it now has
    its own short guide."""
    from asset_service import agent_docs
    start = agent_docs.START_TEMPLATE
    assert "IN THIS ORDER" not in start and "<repo>" not in start
    assert "SCENE_BUILD.md" in start and "Read it when" in start
    scene = agent_docs.SCENE_TEMPLATE.format(
        generated="g", repo="R", base="B", py="P", blender="BL")
    for need in ('"key"', '"materials": "recipe"', "scene_manifest.py",
                 "--factory-startup", "bodies", ".obj"):
        assert need in scene, need


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:       # noqa: BLE001 -- report, go on
                failures += 1
                kind = "" if isinstance(exc, AssertionError) else                     f"{type(exc).__name__}: "
                print(f"FAIL {name}: {kind}{exc}")
    raise SystemExit(1 if failures else 0)
