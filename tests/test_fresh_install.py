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
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

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

    def check(label, cond, detail=""):
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
        check("config library_root", cfg["library_root"] == str(lib))
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
              str(lib / "UE Packs") in cfg.get("manifest_roots", []))

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

        # 5d. F3: model files beat image counts in the census
        det2 = pharos_init.detect(lib)
        check("F3: CGTrader-style folder classified as model folder",
              "Downloads" in det2["mesh_folders"],
              str(det2["mesh_folders"]))

        # 6. docs generation (fixture _Agent_Files, then clean it up)
        from asset_service import agent_docs
        paths = agent_docs.generate(dbp, repo_hint=str(REPO))
        body = paths[0].read_text(encoding="utf-8")
        check("AGENT_START_HERE generated with live counts",
              "Meshes" in body and "(2 packs)" in body)
        check("docs regenerates (bak overwrite bug)",
              agent_docs.generate(dbp, repo_hint=str(REPO)) is not [])
        shutil.rmtree(config.AGENT_FILES, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if backup is not None:
            cfg_path.write_bytes(backup)
        elif cfg_path.is_file():
            cfg_path.unlink()
    print("FRESH-INSTALL SMOKE:",
          "PASS" if not failures else f"FAIL {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
