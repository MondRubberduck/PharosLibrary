"""Fresh-install smoke test: the complete stranger flow without sockets.

Simulates what a new user's machine does, in-process:
    fixture library -> pharos init (detection) -> importers (degradation)
    -> API functions (empty sections serve zero, not errors)
    -> scanner indexes a mesh folder with REAL FBX dimensions
    -> importers again (scanner rows must survive the rebuild)
    -> pharos docs generation

Run standalone (python tests/test_fresh_install.py) or via pytest.
Writes only into a temp dir + the repo's own pharos_config.json slot
(the real config is backed up and restored).
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))


def _same_path(a: str, b: str) -> bool:
    """Windows TEMP dirs can carry 8.3 short names (RUNNER~1) that init's
    Path.resolve() expands -- compare resolved, case-insensitively there."""
    pa, pb = Path(a).resolve(), Path(b).resolve()
    if os.name == "nt":
        return str(pa).lower() == str(pb).lower()
    return str(pa) == str(pb)

# minimal but valid binary FBX (one triangle, cm units) for the scanner
FBX_TRIANGLES = 1


def _min_fbx() -> bytes:
    """Hand-built binary FBX 7400: one Geometry with Vertices and
    PolygonVertexIndex as CHILD nodes (the real layout the parser walks).
    Triangle spans 100x100 cm -> 1x1 m after unit scaling."""
    def d_arr(vals):
        body = struct.pack(f"<{len(vals)}d", *vals)
        return b"d" + struct.pack("<III", len(vals), 0, len(body)) + body

    def i_arr(vals):
        body = struct.pack(f"<{len(vals)}i", *vals)
        return b"i" + struct.pack("<III", len(vals), 0, len(body)) + body

    def build(name, prop_list, children, start):
        props = b"".join(prop_list)
        pos = start + 13 + len(name) + len(props)
        blob = b""
        for (cn, cp, cc) in children:
            b2, pos = build(cn, cp, cc, pos)
            blob += b2
        end = start + 13 + len(name) + len(props) + len(blob)
        return (struct.pack("<III", end, len(prop_list), len(props))
                + bytes([len(name)]) + name + props + blob), end

    geom_props = [b"I" + struct.pack("<i", 1234), b"S\x00\x00\x00\x00"]
    verts = (b"Vertices", [d_arr([-50.0, 0.0, 0.0, 50.0, 0.0, 0.0,
                                  0.0, 100.0, 0.0])], [])
    polys = (b"PolygonVertexIndex", [i_arr([0, 1, -2])], [])
    body, _ = build(b"Geometry", geom_props, [verts, polys], 27)
    out = bytearray(b"Kaydara FBX Binary  \x00\x1a\x00")
    out += struct.pack("<I", 7400)
    out += body
    out += b"\x00" * 13
    return bytes(out)


def build_fixture(root: Path) -> None:
    (root / "Animation" / "WalkPacks").mkdir(parents=True)
    (root / "Animation" / "WalkPacks" / "walk01.fbx").write_bytes(b"stubfbx")
    (root / "Textures_Materials" / "Brick").mkdir(parents=True)
    (root / "Textures_Materials" / "Brick" / "brick_albedo.png").write_bytes(
        b"\x89PNG fake")
    (root / "Audio_Assets" / "Alarms").mkdir(parents=True)
    with wave.open(str(root / "Audio_Assets" / "Alarms" / "beep.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)          # 1.0 s
    # WAV with a metadata (LIST) chunk before data -- the naive 44-byte
    # header read reported 8628s for a 2s file on the laptop run
    info = b"INFOIART" + struct.pack("<I", 5) + b"test\x00"
    if len(info) & 1:
        info += b"\x00"
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    frames = b"\x11\x22" * 16000                   # 2.0 s
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"LIST" + struct.pack("<I", len(info)) + info
    body += b"data" + struct.pack("<I", len(frames)) + frames
    (root / "Audio_Assets" / "Alarms" / "meta.wav").write_bytes(
        b"RIFF" + struct.pack("<I", len(body)) + body)
    # classic CGTrader-style product: 1 model + 6 texture maps
    dl = root / "Downloads" / "Widget"
    dl.mkdir(parents=True)
    (dl / "widget.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                                   encoding="utf-8")
    for i in range(6):
        (dl / f"map{i}.png").write_bytes(b"\x89PNG")
    models = root / "MyModels"
    models.mkdir(parents=True)
    (models / "plate.fbx").write_bytes(_min_fbx())
    obj = "v 0 0 0\nv 2 0 0\nv 0 1 0\nf 1 2 3\n"
    (models / "crate.obj").write_text(obj, encoding="utf-8")
    (models / "lid.obj").write_text(obj, encoding="utf-8")
    # NESTED pack layout (go-live finding A1: "UE Packs/<pack>/Exports/")
    deep = root / "UE Packs" / "TestPack" / "Exports"
    deep.mkdir(parents=True)
    (deep / "manifest.json").write_text(json.dumps({
        "schema": "pharos.pack.export/v2", "pack": "TestPack",
        "meshes": []}), encoding="utf-8")
    (root / "Collected Files").mkdir(parents=True)
    (root / "Collected Files" / "purchases.csv").write_text(
        "Name,Product URL,Price (USD)\nOld Mill,https://example.com/m,9.99\n",
        encoding="utf-8")


def main() -> int:
    # outer invocation spawns an isolated child: pytest may already have
    # imported asset_service with the REAL config bound at module level,
    # and this test must run against a fixture config from scratch
    if "--inner" not in sys.argv:
        import subprocess
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--inner"],
            capture_output=True, text=True, cwd=str(REPO), timeout=600)
        print(r.stdout[-3000:])
        if r.returncode != 0:
            print(r.stderr[-1500:])
        return r.returncode

    # inner: NOTHING from asset_service is imported until the fixture
    # config is in place (module-level binds read it at import time)
    import subprocess
    cfg_path = REPO / "service" / "asset_service" / "pharos_config.json"
    backup = None
    if cfg_path.is_file():
        backup = cfg_path.read_bytes()

    tmp = Path(tempfile.mkdtemp(prefix="pharos_ci_"))
    lib = tmp / "lib"
    reg = tmp / "reg"
    reg.mkdir()
    build_fixture(lib)
    failures: list[str] = []
    checks_run = 0

    def check(label, cond, detail=""):
        nonlocal checks_run
        checks_run += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} {detail}")
        if not cond:
            failures.append(label)

    try:
        # 1. init as a subprocess: detection + config write (--force;
        #    the real config is backed up above)
        r = subprocess.run(
            [sys.executable, str(REPO / "pharos.py"), "init", str(lib),
             "--registry-dir", str(reg), "--force"],
            capture_output=True, text=True, cwd=str(REPO), timeout=300)
        check("init exits 0", r.returncode == 0, r.stderr[-150:])
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        check("config library_root", _same_path(cfg["library_root"], str(lib)),
              f"cfg={cfg['library_root']}")
        check("init detected collection csv",
              cfg.get("collection_csv") == "purchases.csv")

        # 2. NOW import the app (all module-level config binds = fixture)
        from asset_service import config
        from asset_service import init as pharos_init
        check("is_configured", config.is_configured())
        check("init detected model folder hint",
              "MyModels" in pharos_init.detect(lib)["mesh_folders"])
        det = pharos_init.detect(lib)
        check("A1: nested pack discovered",
              "TestPack" in det.get("manifest_packs_found", []),
              str(det.get("manifest_packs_found")))
        check("A1: manifest root = the nested parent dir",
              str(lib / "UE Packs") in det.get("manifest_roots", []),
              str(det.get("manifest_roots")))
        check("A1: config carries the manifest root",
              any(_same_path(str(lib / "UE Packs"), m)
                  for m in cfg.get("manifest_roots", [])),
              str(cfg.get("manifest_roots")))

        # 2. importers on a fresh registry (degradation path)
        from asset_service import db, collection_import, textures_import, \
            audio_import, meshes_import
        dbp = str(reg / "assets.sqlite")
        conn = db.init_db(dbp)
        check("fresh db bootstraps (schema v5)",
              conn.execute("PRAGMA user_version").fetchone()[0] == 5)
        conn.close()
        check("collection imports", collection_import.import_collection(dbp) == 1)
        check("textures import", textures_import.import_textures(dbp) >= 1)
        # no crawl jsonl -> returns the kept-row count, wipes nothing
        # (laptop-run finding: this used to raise AFTER deleting)
        check("missing audio index keeps table",
              audio_import.import_audio(dbp, root=lib / "Nope") == 0)
        meshes_import.import_meshes(dbp)   # must not wipe/lock anything

        # 3. API surface: empty mesh/audio sections serve zero, not errors
        import asset_service.browse as browse
        browse._DB_PATH = dbp
        st = browse.api_stats()
        check("stats sections live",
              st["sections"]["collection"] == 1
              and st["sections"]["meshes"] == 0, str(st["sections"]))
        m = browse.api_meshes({"q": ["a"]})
        check("q=a -> 0 (token-bomb regression)", m["total"] == 0)
        a = browse.api_audio_items({"q": [""]})
        check("empty audio section serves 200-shape", a["total"] == 0)

        # 4. scanner: real FBX dims on the fixture mesh
        rc_scan = subprocess.run(
            [sys.executable, str(REPO / "service/asset_service/scanner.py"),
             str(lib / "MyModels")], capture_output=True, text=True,
            cwd=str(REPO))
        check("scanner exit 0", rc_scan.returncode == 0,
              rc_scan.stderr[-120:] if rc_scan.returncode else "")
        m2 = browse.api_meshes({})
        check("scanner meshes queryable", m2["total"] == 3,
              f"total={m2['total']}")
        # The token-bomb check above runs while the meshes table is EMPTY, so
        # it passes with or without the guard (0 rows either way).  With rows
        # present the guard is the only thing standing between `q=a` and the
        # whole library (verified by mutation: guard removed -> total=3).
        check("q=a -> 0 with rows indexed (token-bomb regression)",
              browse.api_meshes({"q": ["a"]})["total"] == 0,
              f"total={browse.api_meshes({'q': ['a']})['total']}")
        plate = next((i for i in m2["items"] if i["name"] == "plate"), None)
        check("plate row present", plate is not None)
        if plate:
            check("FBX dims extracted (metres)",
                  abs(plate["bbox_m"][0] - 1.0) < 0.05
                  and abs(plate["bbox_m"][1] - 1.0) < 0.05,
                  str(plate["bbox_m"]))
            check("triangles exact", plate["triangles"] == FBX_TRIANGLES,
                  str(plate["triangles"]))

        # 5. scanner rows survive a server-style rebuild
        meshes_import.import_meshes(dbp)
        m3 = browse.api_meshes({})
        check("scan rows survive rebuild", m3["total"] == 3)

        # 5a. crawler rows + scan rows across TWO rebuilds (laptop-run
        # finding: preserving scan rows WITH their ids collided fatally
        # with crawler auto-increment ids on the second import)
        af = lib / "_Agent_Files"
        af.mkdir(exist_ok=True)
        recs = [{"name": "FixtureCrate", "pack": "FixturePack", "source": "leartes",
                 "fbx": str(lib / "MyModels" / "crate.obj"), "exists": True,
                 "triangles": 1, "vertices": 3, "bbox_m": [2.0, 1.0, 0.0],
                 "materials": [], "texture_files": []}]
        (af / "models.jsonl").write_text(
            "\n".join(json.dumps(x) for x in recs) + "\n", encoding="utf-8")
        meshes_import.import_meshes(dbp)
        meshes_import.import_meshes(dbp)      # the collision case
        m4 = browse.api_meshes({})
        check("crawler + scan rows stable across double rebuild",
              m4["total"] == 4, f"total={m4['total']}")
        srcs = {i["source"] for i in m4["items"]}
        check("both sources present after double rebuild",
              "scan" in srcs and "leartes" in srcs, str(srcs))

        # 5c. A5: scanner audio keeps the folder taxonomy
        r = subprocess.run(
            [sys.executable, str(REPO / "service/asset_service/scanner.py"),
             str(lib / "Audio_Assets")], capture_output=True, text=True,
            cwd=str(REPO), timeout=120)
        check("scanner audio exit 0", r.returncode == 0,
              (r.stderr or "")[-100:])

        # 5b. A3+A4: importers must run as DIRECT SCRIPTS on a fresh
        # registry dir (go-live finding: NameError / unable-to-open-db).
        # audio_import without a crawl jsonl now KEEPS scanner rows
        # (laptop-run finding: it used to wipe the table, then fail).
        for mod in ("meshes_import.py", "textures_import.py",
                    "collection_import.py"):
            r = subprocess.run(
                [sys.executable,
                 str(REPO / "service/asset_service" / mod), dbp],
                capture_output=True, text=True, cwd=str(REPO), timeout=120)
            check(f"direct-script {mod}", r.returncode == 0,
                  (r.stderr or "")[-100:])
        # indexer.py is quoted verbatim by the app's own INGESTION SUMMARY
        # ("python service/asset_service/indexer.py --root <lib>") and in
        # CAPABILITIES: its sys.path bootstrap sat BELOW the first
        # `from asset_service import ...`, so the documented command died
        # with ModuleNotFoundError
        r = subprocess.run(
            [sys.executable, str(REPO / "service/asset_service/indexer.py"),
             "--root", str(lib)],
            capture_output=True, text=True, cwd=str(REPO), timeout=300)
        check("direct-script indexer.py --root (INGESTION SUMMARY form)",
              r.returncode == 0, (r.stderr or "")[-140:])
        r = subprocess.run(
            [sys.executable,
             str(REPO / "service/asset_service" / "audio_import.py"), dbp],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        a_kept = browse.api_audio_items({"q": [""]})["total"]
        check("audio_import no-jsonl: exit 0, keeps scanner rows",
              r.returncode == 0 and "keeping" in (r.stdout or "")
              and a_kept >= 1,
              f"rc={r.returncode} kept={a_kept}")

        a2 = browse.api_audio_items({"q": [""]})
        check("A5: audio indexed from folders", a2["total"] >= 2,
              f"total={a2['total']}")
        cats = {i.get("cat") for i in a2["items"]}
        check("A5: folder taxonomy preserved (cat=Alarms)",
              "Alarms" in cats, str(cats))
        meta = next((i for i in a2["items"] if i["name"] == "meta"), None)
        check("F9: LIST-chunk WAV duration correct (~2.0s)",
              meta is not None and meta.get("dur")
              and abs(meta["dur"] - 2.0) < 0.05,
              f"dur={meta.get('dur') if meta else None}")

        # 5g. audio scan-row retention WITH a crawl jsonl present. The
        # docs claim scanner rows survive every importer rebuild; the
        # audio table's UNIQUE(rel) forbids two rows for one file, so the
        # contract is: the crawl row REPLACES the scan row for files the
        # jsonl knows (richer sr/ch/dur), and scan-only files survive.
        # (Before the source column existed, this rebuild wiped BOTH.)
        import sqlite3 as _sq
        (lib / "Audio_Assets" / "library_files.jsonl").write_text(
            json.dumps({"p": "Alarms/beep.wav", "cat": "Alarms", "sub": "",
                        "ext": ".wav", "bytes": 16044, "dur": 1.0,
                        "sr": 8000, "ch": 1}) + "\n", encoding="utf-8")
        audio_import.import_audio(dbp)
        _c = _sq.connect(dbp)
        _c.row_factory = _sq.Row
        _arows = {r["rel"]: r["source"]
                  for r in _c.execute("SELECT rel, source FROM audio")}
        _c.close()
        check("audio rebuild: crawl row replaces overlapped scan row",
              _arows.get("Alarms/beep.wav") == "crawl", str(_arows))
        check("audio rebuild: scan-only row survives",
              _arows.get("Alarms/meta.wav") == "scan", str(_arows))
        check("audio rows carry source markers (no NULL)",
              len(_arows) == 2 and None not in _arows.values(),
              str(_arows))

        # 5d. F3: model files beat image counts in the census
        det2 = pharos_init.detect(lib)
        check("F3: CGTrader-style folder classified as model folder",
              "Downloads" in det2["mesh_folders"],
              str(det2["mesh_folders"]))

        # 5e. textures retention: scanner texture sets survive a
        # server-style rebuild (readiness-review blocker: the importer
        # used to DELETE FROM textures unconditionally)
        from asset_service import textures_import as _ti
        r = subprocess.run(
            [sys.executable, str(REPO / "service/asset_service/scanner.py"),
             str(lib / "Textures_Materials")], capture_output=True,
            text=True, cwd=str(REPO), timeout=120)
        check("scanner texture pass exit 0", r.returncode == 0,
              (r.stderr or "")[-80:])
        t_after_scan = browse.api_textures_items({"q": [""]})["total"]
        _ti.import_textures(dbp)          # rebuild with root present
        _ti.import_textures(dbp)          # and again
        t_after_rebuild = browse.api_textures_items({"q": [""]})["total"]
        check("texture sets survive double rebuild",
              t_after_rebuild >= t_after_scan and t_after_scan >= 2,
              f"scan={t_after_scan} rebuild={t_after_rebuild}")
        import sqlite3 as _sq
        _c = _sq.connect(dbp)
        _srcs = {r[0] for r in _c.execute(
            "SELECT DISTINCT source FROM textures")}
        _c.close()
        check("texture rows carry source markers (scan + crawl, no NULL)",
              "scan" in _srcs and "crawl" in _srcs and None not in _srcs,
              str(_srcs))

        # 5f. Host-header guard (DNS-rebinding blocker): the server must
        # 403 any non-loopback Host on GET/POST/HEAD
        import time as _time
        import urllib.request as _ur
        srv = subprocess.Popen(
            [sys.executable, str(REPO / "pharos.py"), "serve",
             "--no-open", "--port", "8844"],
            cwd=str(REPO), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            base = "http://127.0.0.1:8844/api/stats"
            ok_code = None
            for _ in range(30):
                _time.sleep(0.5)
                try:
                    ok_code = _ur.urlopen(base, timeout=2).status
                    break
                except Exception:
                    continue
            check("fixture server boots", ok_code == 200, str(ok_code))
            req = _ur.Request(base, headers={"Host": "evil.example:8844"})
            try:
                _ur.urlopen(req, timeout=3)
                bad_code = 200
            except _ur.HTTPError as e:
                bad_code = e.code
            check("Host guard: rebinding host -> 403", bad_code == 403,
                  str(bad_code))

            # CSRF hardening on the mutating endpoints: a cross-site page
            # can SEND requests to the loopback server even though it can
            # never read the responses (the Host guard). Foreign Origin
            # must 403, opaque content types must 415, and a well-formed
            # JSON request must pass the guard and reach the handler
            # (handler-level 400 "nothing to do" proves it got through).
            def _post(headers, body=b"{}"):
                req2 = _ur.Request("http://127.0.0.1:8844/api/retag",
                                   data=body, headers=headers, method="POST")
                try:
                    return _ur.urlopen(req2, timeout=3).status
                except _ur.HTTPError as e:
                    return e.code
            check("CSRF: foreign Origin POST -> 403",
                  _post({"Origin": "http://evil.example"}) == 403)
            check("CSRF: non-JSON content-type POST -> 415",
                  _post({"Content-Type": "text/plain"}) == 415)
            check("CSRF: JSON POST passes guard (handler 400 = no-op)",
                  _post({"Content-Type": "application/json"}) == 400)
        finally:
            srv.terminate()

        # 6. docs generation (fixture _Agent_Files, then clean it up)
        from asset_service import agent_docs
        paths = agent_docs.generate(dbp, repo_hint=str(REPO))
        start_file = config.AGENT_FILES / "AGENT_START_HERE.md"
        body = paths[0].read_text(encoding="utf-8")
        check("AGENT_START_HERE generated with live counts",
              "Meshes" in body and "(2 packs)" in body)

        # This check used to be `generate(...) is not []`, an identity test
        # against a fresh list literal -- True for every possible return
        # value.  Assert the documented behaviour instead: the previous
        # generation moves to .md.bak-previous, and the NEXT run REPLACES
        # that bak rather than piling up or dying on an existing file
        # (the Windows rename->replace bug).
        gen1_on_disk = start_file.read_text(encoding="utf-8")
        gen2 = agent_docs.generate(dbp, repo_hint=str(REPO))
        bak = config.AGENT_FILES / "AGENT_START_HERE.md.bak-previous"
        one_bak = bak.is_file() and bak.read_text(encoding="utf-8") == gen1_on_disk
        gen2_on_disk = start_file.read_text(encoding="utf-8")
        gen3 = agent_docs.generate(dbp, repo_hint=str(REPO))
        baks = list(config.AGENT_FILES.glob("AGENT_START_HERE.md.bak*"))
        check("docs regenerates (bak overwrite bug)",
              bool(gen2) and bool(gen3) and one_bak and len(baks) == 1
              and bak.read_text(encoding="utf-8") == gen2_on_disk,
              f"baks={[b.name for b in baks]}")

        # 6b. doctor: the one-command setup check must parse, report live
        # section counts, and pass on a correctly-prepared fixture install
        r = subprocess.run(
            [sys.executable, str(REPO / "pharos.py"), "doctor", "--json"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        check("doctor exits 0 on a working fixture install",
              r.returncode == 0, (r.stderr or "")[-120:])
        try:
            d = json.loads(r.stdout)
        except ValueError:
            d = {}
        check("doctor --json parses with a verdict",
              d.get("verdict") in ("READY", "READY-WITH-GAPS"),
              str(d.get("verdict")))
        check("doctor reports live section counts",
              any(c["name"] == "section:meshes" and c["status"] == "ok"
                  for c in d.get("checks", [])),
              str([c for c in d.get("checks", [])
                   if c["name"] == "section:meshes"]))

        # 6c. ingest --dry-run lists the safe chains and runs nothing;
        # a real ingest chains scanner/indexer/importers/docs and prints
        # the ASK YOUR USER decision block
        r = subprocess.run(
            [sys.executable, str(REPO / "pharos.py"), "ingest", "--dry-run"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        check("ingest dry-run lists commands, runs nothing",
              r.returncode == 0 and "scanner.py" in r.stdout
              and "(dry-run: skipped)" in r.stdout,
              (r.stderr or "")[-120:])
        r = subprocess.run(
            [sys.executable, str(REPO / "pharos.py"), "ingest"],
            capture_output=True, text=True, cwd=str(REPO), timeout=600)
        check("ingest exits 0 with decision block",
              r.returncode == 0 and "ASK YOUR USER" in r.stdout,
              (r.stderr or "")[-140:])
        _c = _sq.connect(dbp)
        _anim = _c.execute("SELECT COUNT(*) FROM assets WHERE "
                           "id LIKE 'pack::Animation/%'").fetchone()[0]
        _mesh_n = _c.execute("SELECT COUNT(*) FROM meshes").fetchone()[0]
        _c.close()
        check("ingest indexed animation packs",
              _anim >= 1, f"anim={_anim}")
        check("ingest kept meshes intact across rebuild",
              _mesh_n >= 3, f"meshes={_mesh_n}")

        # ...and a fresh install with no registry yet must get a plain,
        # actionable message, never a sqlite3 traceback
        cfg_now = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_now["registry_dir"] = str(tmp / "no_such_registry")
        cfg_path.write_text(json.dumps(cfg_now), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(REPO / "pharos.py"), "docs"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        check("docs without a registry fails plainly (no traceback)",
              r.returncode == 1 and "Traceback" not in out
              and "cannot read the registry" in out,
              f"rc={r.returncode} out={out.strip()[-90:]!r}")
        shutil.rmtree(config.AGENT_FILES, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if backup is not None:
            cfg_path.write_bytes(backup)
        elif cfg_path.is_file():
            cfg_path.unlink()
    print("FRESH-INSTALL SMOKE:",
          "PASS" if not failures else f"FAIL {failures}",
          f"({checks_run} checks executed)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
