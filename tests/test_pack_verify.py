"""Pipeline-layer regression checks (runs under pytest or standalone).

Covers three verified pipeline defects that the service-layer suites
cannot see (they only consume pipeline OUTPUTS):

  1. verify_pack_export must HARD-FAIL a v2 manifest with no wiring
     block -- that is a broken relink (the check existed as dead code:
     `m.get("schema") == SCHEMA_V2` compared a string against a tuple,
     so the guard never fired).
  2. build_kb3d_index must absolutize the RELATIVE group FBX paths that
     export_kb3d writes ("FBX/<grp>/<grp>.fbx"); they used to land in
     kb3d_models.jsonl verbatim with exists=False.

Run standalone: python -B tests/test_pack_verify.py
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fresh_install import _min_fbx  # noqa: E402  (shared FBX fixture)


def _run(script: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-B", str(REPO / script)],
        capture_output=True, text=True, cwd=str(REPO), timeout=300, env=env)


def _verify(exports: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B",
         str(REPO / "pipeline" / "conversion" / "verify_pack_export.py"),
         str(exports)],
        capture_output=True, text=True, cwd=str(REPO), timeout=300)


def test_v2_manifest_without_wiring_hard_fails():
    with tempfile.TemporaryDirectory() as tmp:
        exports = Path(tmp) / "FixturePack" / "Exports"
        (exports / "FBX").mkdir(parents=True)
        (exports / "FBX" / "mesh0.fbx").write_bytes(_min_fbx())
        base = {
            "schema": "pharos.pack.export/v2",
            "pack": "FixturePack",
            "meshes": [{"name": "mesh0", "kind": "StaticMesh",
                        "dimension_method": "engine",
                        "fbx": "FBX/mesh0.fbx", "triangles": 1,
                        "vertices": 3, "bbox_m": [1.0, 1.0, 0.5],
                        "materials": []}],
            "textures": [],
            "counts": {"static_mesh": 1, "skeletal_mesh": 0,
                       "fbx_written": 1, "texture_files_written": 0,
                       "failures": 0},
        }
        # v2 WITHOUT a wiring block: a broken relink -- must HARD FAIL
        (exports / "manifest.json").write_text(
            json.dumps(base), encoding="utf-8")
        r = _verify(exports)
        combined = (r.stdout or "") + (r.stderr or "")
        assert r.returncode != 0, \
            "v2 manifest without wiring verified as PASS (dead check is back)"
        assert "wiring block is missing" in combined, combined[-400:]

        # control: the SAME manifest with a well-formed wiring block must
        # verify clean -- the hard fail above is the ONLY difference
        base["wiring"] = {"method": "ue-param", "slots_total": 0,
                          "slots_resolved": 0, "slots_unresolved": 0}
        (exports / "manifest.json").write_text(
            json.dumps(base), encoding="utf-8")
        r2 = _verify(exports)
        assert r2.returncode == 0, (r2.stdout or "")[-400:]
        assert "wiring block is missing" not in (r2.stdout or "")


def test_kb3d_index_absolutizes_relative_fbx_paths():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        kits = tmp / "kits"
        exports = kits / "FixtureKit" / "Exports"
        groups = []
        for i in range(11):                      # > the 10-record guard
            gid = f"grp_{i:02d}"
            fbx_dir = exports / "FBX" / gid
            fbx_dir.mkdir(parents=True)
            (fbx_dir / f"{gid}.fbx").write_bytes(b"Kaydara FBX Binary  \x00")
            groups.append({"group": gid,
                           "fbx": f"FBX/{gid}/{gid}.fbx",   # RELATIVE
                           "triangles": 10, "vertices": 5,
                           "submeshes": 1, "materials": ["M_1"],
                           "texture_count": 0})
        (exports / "kit_manifest.json").write_text(
            json.dumps({"schema": "pharos.kb3d.export/v1",
                        "kit": "FixtureKit", "groups": groups}),
            encoding="utf-8")
        r = _run("pipeline/kitbash/build_kb3d_index.py", {
            "PHAROS_CONFIG": str(tmp / "missing_config.json"),
            "PHAROS_LIBRARY_ROOT": str(lib),
            "PHAROS_KB3D_ROOT": str(kits),
        })
        assert r.returncode == 0, (r.stderr or "")[-400:]
        assert "present on disk: 11" in (r.stdout or ""), (r.stdout or "")[-300:]
        out = lib / "_Agent_Files" / "kb3d_models.jsonl"
        assert out.is_file(), "index not written"
        rows = [json.loads(line) for line in
                out.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 11
        for row in rows:
            assert os.path.isabs(row["fbx"]), row["fbx"]
            assert row["exists"] is True, row["fbx"]
            assert Path(row["fbx"]).is_file(), row["fbx"]


def test_make_sandbox_creates_usable_uproject():
    """The conversion sandbox is generated locally, not shipped (its
    EngineAssociation must match the machine's UE). The generator must
    produce a valid uproject with the Python plugin enabled, derive the
    association from UE_EXE, and refuse to clobber without --force."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sandbox"
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "pipeline" / "conversion" / "make_sandbox.py"),
             "--out", out],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
            env={**os.environ,
                 "UE_EXE": r"C:\Program Files\Epic Games\UE_5.7\Engine"
                           r"\Binaries\Win64\UnrealEditor-Cmd.exe"})
        assert r.returncode == 0, (r.stderr or "")[-300:]
        doc = json.loads((out / "Sandbox.uproject").read_text(
            encoding="utf-8"))
        assert doc["EngineAssociation"] == "5.7", doc
        assert any(p.get("Name") == "PythonScriptPlugin" and p.get("Enabled")
                   for p in doc.get("Plugins") or []), doc
        assert (out / "Content").is_dir()
        # idempotent: a second run must not clobber, and must say so
        r2 = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "pipeline" / "conversion" / "make_sandbox.py"),
             "--out", out],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r2.returncode == 0 and "already exists" in (r2.stdout or "")


def _find_usable_bash():
    """A 'bash' on PATH may be the WSL launcher stub (C:\\Windows\\System32
    \\bash.exe), which fails when no WSL distro exists -- CI windows
    runners are exactly that case. Probe candidates and return one that
    can actually run a command, or None."""
    candidates = [shutil.which("bash"),
                  r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\bash.exe"]
    for cand in candidates:
        if not cand or not Path(cand).is_file():
            continue
        try:
            r = subprocess.run([cand, "-c", "true"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def test_drivers_print_sandbox_remediation():
    """A virgin machine without a sandbox must get the exact regeneration
    command, not a bare 'missing' error (the sandbox is generated locally
    on purpose, so the error message IS the setup documentation)."""
    # the test needs a machine WITHOUT a sandbox; a generated one may
    # legitimately exist in the repo tree -- move it aside, restore after
    # (same backup/restore pattern as the fresh-install suite's config)
    bash = _find_usable_bash()
    if bash is None:
        print("SKIP: no usable bash on this machine -- the shell drivers "
              "cannot run here, so their remediation cannot be tested")
        return
    real_sandbox = REPO / "pipeline" / "conversion" / "sandbox"
    parked = None
    if real_sandbox.is_dir():
        parked = real_sandbox.with_name("sandbox.parked-for-test")
        real_sandbox.rename(parked)
    try:
        for driver in ("convert_packs.sh", "relink_pack.sh"):
            r = subprocess.run(
                [bash, str(REPO / "pipeline" / "conversion" / driver),
                 "--pack", "FixturePack"],
                capture_output=True, text=True, cwd=str(REPO), timeout=120,
                env={**os.environ,
                     # any real file passes the UE check so the sandbox
                     # check (and its remediation message) is what fires
                     "UE_EXE": sys.executable,
                     "ASSETS_ROOT": REPO.as_posix()})
            combined = (r.stdout or "") + (r.stderr or "")
            assert r.returncode == 2, (driver, r.returncode, combined[-200:])
            assert "make_sandbox.py" in combined, \
                f"{driver} no longer points at the generator: {combined[-300:]}"
    finally:
        if parked is not None and parked.is_dir() and not real_sandbox.exists():
            parked.rename(real_sandbox)


def _png(w=1, h=1):
    import zlib
    def chunk(t, d):
        c = t + d
        return (struct.pack(">I", len(d)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _jpeg(w=3, h=2):
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", h, w) + b"\x03" + b"\x01\x22\x00")
    return b"\xff\xd8" + sof + b"\xff\xd9"


def test_tex_index_self_builds_cache():
    """gen_tex_index required a hand-made tex_meta.json that NO script
    produced -- the chain was author-machine-only. The self-builder must
    create the cache from the root, parse real image dims from headers
    (PNG/JPEG/GIF/BMP), mark unparseable files as unknown, and reuse the
    cache on the next run."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tex = tmp / "tex"
        (tex / "Wood").mkdir(parents=True)
        (tex / "Metal").mkdir()
        names = ([f"plank_{i:02d}" for i in range(4)]
                 + [f"steel_{i:02d}" for i in range(4)])
        for i, base in enumerate(names):
            sub = tex / ("Wood" if base.startswith("plank") else "Metal")
            (sub / f"{base}_albedo.png").write_bytes(_png(1, 1))
            (sub / f"{base}_normal.png").write_bytes(_png(1, 1))
        (tex / "Wood" / "odd_dim.jpg").write_bytes(_jpeg(3, 2))
        (tex / "Metal" / "odd.gif").write_bytes(
            b"GIF89a" + struct.pack("<HH", 5, 7) + b"\x00\x00;")
        (tex / "Metal" / "odd.bmp").write_bytes(
            b"BM" + b"\x00" * 16 + struct.pack("<ii", 9, -4))
        (tex / "Wood" / "junk.png").write_bytes(b"\x89PNG garbage")
        r = _run("pipeline/agent_index/gen_tex_index.py", {
            "AGENT_TEX_ROOT": str(tex),
            "AGENT_TEX_META": str(tmp / "tex_meta.json"),
            "PHAROS_CONFIG": str(tmp / "missing.json"),
        })
        assert r.returncode == 0, (r.stderr or "")[-400:]
        assert "cache missing" in (r.stdout or ""), (r.stdout or "")[:300]
        assert "wrote library_index.json" in (r.stdout or "")
        files_jsonl = tex / "library_files.jsonl"
        assert files_jsonl.is_file()
        rows = {json.loads(l)["p"]: json.loads(l)
                for l in files_jsonl.read_text(
                    encoding="utf-8").splitlines() if l.strip()}
        assert len(rows) >= 10
        assert rows["Wood/odd_dim.jpg"]["w"] == 3, rows["Wood/odd_dim.jpg"]
        assert rows["Metal/odd.gif"]["h"] == 7
        assert rows["Metal/odd.bmp"]["w"] == 9
        assert rows["Metal/odd.bmp"]["h"] == 4          # negative height abs
        assert rows["Wood/junk.png"]["w"] is None       # unknown, never guessed
        # second run reuses the cache (no rebuild chatter)
        r2 = _run("pipeline/agent_index/gen_tex_index.py", {
            "AGENT_TEX_ROOT": str(tex),
            "AGENT_TEX_META": str(tmp / "tex_meta.json"),
            "PHAROS_CONFIG": str(tmp / "missing.json"),
        })
        assert r2.returncode == 0, (r2.stderr or "")[-300:]
        assert "cache missing" not in (r2.stdout or "")


def _poison_fbx() -> bytes:
    """Corrupt binary FBX: a valid record header whose first property is
    an S claiming a 2GB length -- the property walk jumps pos past the
    buffer and the SECOND property read used to escape as IndexError
    (not in fbx_dims' catch list), aborting entire scanner runs."""
    import struct as _s
    props = b"S" + _s.pack("<I", 0x7FFFFFFF) + b"I" + _s.pack("<i", 1)
    name = b"X"
    body_start = 27
    end = body_start + 13 + len(name) + len(props)
    out = bytearray(b"Kaydara FBX Binary  \x00\x1a\x00")
    out += _s.pack("<I", 7400)
    out += _s.pack("<III", end, 2, len(props))
    out += bytes([len(name)]) + name + props
    assert len(out) == end
    return bytes(out)


def test_fbx_dims_poison_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "poison.fbx"
        p.write_bytes(_poison_fbx())
        from asset_service.fbx_dims import fbx_file_info
        info = fbx_file_info(p)
        assert info is None, f"poison FBX must yield None, got {info}"


def test_scanner_survives_poison_fbx():
    """One corrupt FBX must never abort the run (it used to raise
    IndexError out of fbx_dims and lose every row). Contract: exit 0,
    the good file keeps REAL dims, the poison file gets a zero-dims row
    (recorded, not guessed) instead of fabricated numbers."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        models = tmp / "models"
        models.mkdir()
        (models / "good.fbx").write_bytes(_min_fbx())
        (models / "poison.fbx").write_bytes(_poison_fbx())
        db = tmp / "reg.sqlite"
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "service" / "asset_service" / "scanner.py"),
             str(models), "--db", str(db)],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode == 0, (r.stderr or "")[-300:]
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r_["name"]: dict(r_) for r_ in
                conn.execute("SELECT name, triangles, bbox_x FROM meshes")}
        conn.close()
        assert set(rows) == {"good", "poison"}, \
            f"both files must be scanned, got {set(rows)}"
        assert rows["good"]["triangles"] == 1, rows["good"]
        assert abs(rows["good"]["bbox_x"] - 1.0) < 0.05, rows["good"]
        assert rows["poison"]["triangles"] == 0, \
            f"poison must yield zeros, not fabricated numbers: {rows['poison']}"


def test_promote_native_never_reads_blends_file():
    """native_index_blends.jsonl sorts AFTER every timestamped run
    ('b' > '2'); a plain newest-wins glob fed blend records to this
    wholesale overwrite and erased every FBX/OBJ container record."""
    native_dir = REPO / "pipeline" / "native"
    ts = native_dir / "native_index_20260101-000000.jsonl"
    bl = native_dir / "native_index_blends.jsonl"

    def row(name):
        return json.dumps({"section": "S", "object": name,
                           "kind": "mesh", "source": "c:/x.fbx",
                           "triangles": 1, "vertices": 3,
                           "bbox_m": [1, 1, 1], "materials": [],
                           "collection": "C"})
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "lib"
        lib.mkdir()
        try:
            ts.write_text("\n".join(row(f"ts_{i}")
                                    for i in range(12)) + "\n",
                          encoding="utf-8")
            bl.write_text("\n".join(row(f"FROMBLENDS_{i}")
                                    for i in range(12)) + "\n",
                          encoding="utf-8")
            r = _run("pipeline/native/promote_native.py", {
                "PHAROS_CONFIG": str(Path(tmp) / "missing.json"),
                "PHAROS_LIBRARY_ROOT": str(lib),
            })
            assert r.returncode == 0, (r.stderr or "")[-300:]
            out = lib / "_Agent_Files" / "native_models.jsonl"
            assert out.is_file(), "index not written"
            body = out.read_text(encoding="utf-8")
            assert "FROMBLENDS" not in body, "blend records leaked in"
            assert "ts_0" in body, "timestamped records missing"
        finally:
            for f in (ts, bl):
                try:
                    f.unlink()
                except OSError:
                    pass


def test_version_single_source_of_truth():
    """__init__.__version__ drifted to 0.2.0 while pyproject said 0.2.1."""
    import re as _re
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject, _re.M)
    import asset_service
    assert m, "pyproject version not found"
    assert m.group(1) == asset_service.__version__, \
        f"pyproject {m.group(1)} != asset_service.__version__ " \
        f"{asset_service.__version__}"


def test_templates_have_no_extraction_artifacts():
    """The templates were extracted from Python strings once; the double
    backslashes that survived made the pack page's 3D-link regex never
    match and the assets page strip DIGITS from prices."""
    for f in (REPO / "service" / "asset_service" / "templates").glob("*.html"):
        data = f.read_bytes()
        assert b"\\\\" not in data, \
            f"{f.name} still carries Python-string escape artifacts"
    pk = (REPO / "service" / "asset_service" / "templates"
          / "pack.html").read_text(encoding="utf-8")
    assert "data-e=" in pk and "setExt('${" not in pk, \
        "pack page still splices raw ext into inline onclick"
    src = (REPO / "service" / "asset_service" / "browse.py").read_text(
        encoding="utf-8")
    assert "toggle('${" not in src and "open('${" not in src, \
        "registry grid still splices pack ids into inline handlers"
    assert 'data-id="${esc(a.id)}"' in src, "registry grid lost esc/data-id"


def test_mcp_db_preflight_and_clip_scope():
    """The MCP server must never CREATE a registry (stray assets.sqlite in
    the client's CWD) and clips must count only Animation packs."""
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "pharos_mcp_server_test",
            str(REPO / "service" / "pharos_mcp_server.py"))
        srv = ilu.module_from_spec(spec)
        spec.loader.exec_module(srv)
    except Exception as exc:                       # noqa: BLE001
        print(f"SKIP (no mcp package on this machine): {exc}")
        return
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope" / "assets.sqlite"
        orig = srv.config.DB_PATH
        srv.config.DB_PATH = str(missing)
        try:
            raised = False
            try:
                srv._db()
            except FileNotFoundError:
                raised = True
            assert raised and not missing.exists(), \
                "MCP preflight must fail loudly AND create nothing"
        finally:
            srv.config.DB_PATH = orig

        dbp = Path(tmp) / "t.sqlite"
        conn = sqlite3.connect(str(dbp))
        conn.executescript("""
        CREATE TABLE assets(id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE asset_files(asset_id TEXT, relative_path TEXT);
        INSERT INTO assets VALUES('pack::Animation/A','A');
        INSERT INTO assets VALUES('pack::Stuff/B','B');
        INSERT INTO asset_files VALUES('pack::Animation/A','x.fbx');
        INSERT INTO asset_files VALUES('pack::Stuff/B','y.fbx');
        """)
        conn.commit()
        assert srv._animation_clip_count(conn) == 1, \
            "clip count must be scoped to pack::Animation/%"
        conn.close()


def _wav(path_bytes=16044):
    import wave as _w
    def make(p):
        with _w.open(str(p), "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000)
            f.writeframes(b"\x00\x00" * 8000)
    return make


def _find_blender():
    for pat in (r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender *\blender.exe"):
        hits = sorted(__import__("glob").glob(pat))
        if hits:
            return hits[-1]
    return ""


def test_verifier_derives_skeletal_triangles():
    """SkeletalMesh editor metrics expose no triangle count (exporter
    writes null); every skeletal pack therefore FAILED verification
    forever. The verifier now derives the count from the exported FBX."""
    with tempfile.TemporaryDirectory() as tmp:
        exports = Path(tmp) / "SkmPack" / "Exports"
        (exports / "FBX").mkdir(parents=True)
        (exports / "FBX" / "sm.fbx").write_bytes(_min_fbx())
        (exports / "FBX" / "skm.fbx").write_bytes(_min_fbx())
        base = {
            "schema": "pharos.pack.export/v2", "pack": "SkmPack",
            "meshes": [
                {"name": "sm", "kind": "StaticMesh",
                 "dimension_method": "engine", "fbx": "FBX/sm.fbx",
                 "triangles": 1, "vertices": 3, "bbox_m": [1.0, 1.0, 0.5],
                 "materials": []},
                {"name": "skm", "kind": "SkeletalMesh",
                 "dimension_method": "engine", "fbx": "FBX/skm.fbx",
                 "triangles": None, "vertices": 3,
                 "bbox_m": [1.0, 1.0, 0.5], "materials": []},
            ],
            "textures": [],
            "counts": {"static_mesh": 1, "skeletal_mesh": 1,
                       "fbx_written": 2, "texture_files_written": 0,
                       "failures": 0},
            "wiring": {"method": "ue-param", "slots_total": 0,
                       "slots_resolved": 0, "slots_unresolved": 0},
        }
        (exports / "manifest.json").write_text(json.dumps(base),
                                               encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "pipeline" / "conversion" / "verify_pack_export.py"),
             str(exports)],
            capture_output=True, text=True, cwd=str(REPO), timeout=300)
        out = r.stdout or ""
        assert r.returncode == 0, out[-400:]
        assert "skm tris" in out and "1 SkeletalMesh" in out, out[-300:]

        # negative: skeletal null-triangles AND a missing FBX still fails
        base["meshes"][1]["fbx"] = "FBX/missing.fbx"
        (exports / "manifest.json").write_text(json.dumps(base),
                                               encoding="utf-8")
        r2 = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "pipeline" / "conversion" / "verify_pack_export.py"),
             str(exports)],
            capture_output=True, text=True, cwd=str(REPO), timeout=300)
        assert r2.returncode != 0


def test_kb3d_key_symmetry():
    """The remap cache once stripped 'kb3d_' on one side and kept it on
    the other -- the cache could never hit. Both sides now share helpers;
    this pins the symmetry, including the concrete dead-path case."""
    from asset_service.kb3d_paths import (kit_key_from_native_dir,
                                          kit_key_from_texture_dir)
    for name in ("Apocalypse", "apocalypse", "NeoCity", "mixedCase_Kit"):
        assert (kit_key_from_native_dir(f"kb3d_{name}.blender.native")
                == kit_key_from_texture_dir(f"kb3d_{name}.png.2k")
                == name.lower()), name
    # the adjudication's concrete walk: dead FBX texture path ->
    # ancestor key -> cache built from the shipped-texture folder name
    dead = Path("E:/origin/Kits/Apocalypse Ship/"
                "kb3d_Apocalypse.blender.native/KB3DTextures/4k/TX.png")
    anc = next(a.name for a in dead.parents
               if a.name.lower().startswith("kb3d_")
               and ".blender.native" in a.name.lower())
    assert kit_key_from_native_dir(anc) == \
        kit_key_from_texture_dir("kb3d_Apocalypse.png.2k") == "apocalypse"


def test_gen_index_cache_is_root_stamped():
    """The audio cache had no root stamp: switching roots served the old
    root's paths into a live index. Now: stamped, --rescan forces, and a
    root change rescans."""
    mk = _wav()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cache = tmp / "aa_meta.json"
        roots = []
        for n in (1, 2):
            r = tmp / f"aud{n}" / "Alarms"
            r.mkdir(parents=True)
            # distinct names per root: the jsonl stores RELATIVE paths, so
            # same-named files cannot prove which root was served
            for i in range(12):
                mk(r / f"root{n}_beep_{i:02d}.wav")
            roots.append(r.parent)
        env = {"PHAROS_CONFIG": str(tmp / "missing.json"),
               "AGENT_AUDIO_META": str(cache)}
        for n, r in enumerate(roots, 1):
            e = {**env, "AGENT_AUDIO_ROOT": str(r)}
            res = _run("pipeline/agent_index/gen_index.py", e)
            assert res.returncode == 0, (res.stderr or "")[-300:]
            rows = [json.loads(l) for l in
                    (r / "library_files.jsonl").read_text(
                        encoding="utf-8").splitlines() if l.strip()]
            assert len(rows) >= 10
            # the served file list must be THIS root's, not the cache's
            assert all(f"root{n}_beep_" in x["p"] for x in rows), \
                [x["p"] for x in rows[:3]]
        # cache carries the stamp of the LAST root; re-running root 1
        # must detect the mismatch and rescan, not serve root-2 paths
        res = _run("pipeline/agent_index/gen_index.py",
                   {**env, "AGENT_AUDIO_ROOT": str(roots[0])})
        assert res.returncode == 0
        assert "stale" in (res.stdout or ""), (res.stdout or "")[:200]


def test_native_worklist_is_lf_only():
    """The worklist was written in Windows text mode; Git Bash read -r
    kept the \\r and Blender silently failed on 'path\\r'."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        (lib / "Kits" / "K1").mkdir(parents=True)
        (lib / "Kits" / "K1" / "kit.blend").write_bytes(b"fake")
        # park any real scratch (gitignored, but never destroy owner data)
        native = REPO / "pipeline" / "native"
        parked = {}
        for name in ("native_manifest.json", "native_blends.txt"):
            real = native / name
            if real.exists():
                parked[name] = real.read_bytes()
        try:
            r = _run("pipeline/native/make_native_manifest2.py", {
                "PHAROS_CONFIG": str(tmp / "missing.json"),
                "PHAROS_LIBRARY_ROOT": str(lib)})
            assert r.returncode == 0, (r.stderr or "")[-300:]
            data = (native / "native_blends.txt").read_bytes()
            assert data and b"\r" not in data, data[:80]
            mdata = (native / "native_manifest.json").read_bytes()
            assert b"\r" not in mdata
        finally:
            for name in ("native_manifest.json", "native_blends.txt"):
                p = native / name
                if name in parked:
                    p.write_bytes(parked[name])
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass


def test_native_driver_fails_loudly():
    """A Blender crash used to read as success (pipeline-to-grep lost
    every exit code, driver always exited 0). Stub Blender: nonzero stub
    exit must surface as driver exit 9."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stub = tmp / "stub-blender.sh"
        stub.write_text('#!/usr/bin/env bash\n'
                        'echo "NATIVE_DONE 3 objects (stub)"\n'
                        'echo "record" >> "$AMNATIVE_OUT"\n'
                        'exit "${STUB_RC:-0}"\n', encoding="utf-8")
        stub.chmod(0o755)
        native = REPO / "pipeline" / "native"
        parked = {}
        for name in ("native_manifest.json", "native_blends.txt"):
            real = native / name
            if real.exists():
                parked[name] = real.read_bytes()
        try:
            (native / "native_manifest.json").write_text(json.dumps({
                "importable": [], "blend_files": [
                    {"path": "X:/nope/kit.blend", "section": "S"}]}),
                encoding="utf-8")
            (native / "native_blends.txt").write_text(
                "X:/nope/kit.blend\nS\n", encoding="utf-8")
            import os as _os
            base_env = {**_os.environ,
                        "BLENDER_EXE": str(stub),
                        "PHAROS_CONFIG": str(tmp / "missing.json")}
            r_bad = subprocess.run(
                ["bash", str(native / "index_native_all.sh")],
                capture_output=True, text=True, cwd=str(REPO),
                timeout=120, env={**base_env, "STUB_RC": "1"})
            assert r_bad.returncode == 9, \
                (r_bad.returncode, (r_bad.stdout or "")[-200:])
            r_ok = subprocess.run(
                ["bash", str(native / "index_native_all.sh")],
                capture_output=True, text=True, cwd=str(REPO),
                timeout=120, env=base_env)
            assert r_ok.returncode == 0, (r_ok.stdout or "")[-200:]
        finally:
            for name in ("native_manifest.json", "native_blends.txt"):
                p = native / name
                if name in parked:
                    p.write_bytes(parked[name])
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass
            for junk in native.glob("native_index_*.jsonl"):
                try:
                    junk.unlink()
                except OSError:
                    pass
            for junk in native.glob("native_blender_*.log"):
                try:
                    junk.unlink()
                except OSError:
                    pass


def test_indexer_prune_scoped_to_its_root():
    """Pack rows are root-relative; the prune pass used to delete EVERY
    row whose hero path was gone -- an offline second root was wiped the
    moment root A got re-indexed."""
    from asset_service import indexer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dbp = tmp / "reg.sqlite"
        roots = []
        for n in ("A", "B"):
            # DISTINCT relpaths per root: identical ones collide on the
            # same pack id (the documented multi-root limitation) and
            # would make this test assert the wrong thing
            r = tmp / n / "Animation" / f"Packs{n}"
            r.mkdir(parents=True)
            (r / f"pack_{n.lower()}.fbx").write_bytes(b"stubfbx")
            roots.append(tmp / n)
        indexer.index_root(roots[0], dbp, config={})
        indexer.index_root(roots[1], dbp, config={})
        import sqlite3
        conn = sqlite3.connect(str(dbp))
        n_before = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE canonical_root LIKE ?",
            (f"%/{roots[0].name}",)).fetchone()[0]
        conn.close()
        assert n_before >= 1, "root A pack not indexed"
        # root A goes offline (folder deleted); re-indexing root B must
        # NOT prune root A's rows
        import shutil
        shutil.rmtree(roots[0])
        indexer.index_root(roots[1], dbp, config={})
        conn = sqlite3.connect(str(dbp))
        n_after = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE canonical_root LIKE ?",
            (f"%/{roots[0].name}",)).fetchone()[0]
        conn.close()
        assert n_after == n_before, \
            f"root A rows wiped by root B's run: {n_before} -> {n_after}"
        # but a NEW run against (the now-empty) root A prunes its own
        roots[0].mkdir()
        indexer.index_root(roots[0], dbp, config={})
        conn = sqlite3.connect(str(dbp))
        n_final = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE canonical_root LIKE ?",
            (f"%/{roots[0].name}",)).fetchone()[0]
        conn.close()
        assert n_final == 0, "root A's own stale row not pruned"


def test_no_replace_checks_the_real_target():
    """--no-replace used to inspect <out-root>/<pack>/Exports while the
    real target with --out-root is <out-root>/<pack> -- the gate was
    vacuous exactly when redirecting output."""
    bash = _find_usable_bash()
    if bash is None:
        print("SKIP: no usable bash")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out_root = Path(tmp) / "redirected"
        (out_root / "FixturePack").mkdir(parents=True)   # the REAL target
        r = subprocess.run(
            [bash, str(REPO / "pipeline" / "conversion" / "convert_packs.sh"),
             "--pack", "FixturePack", "--out-root", str(out_root),
             "--no-replace"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
            env={**os.environ, "UE_EXE": sys.executable,
                 "ASSETS_ROOT": REPO.as_posix()})
        combined = (r.stdout or "") + (r.stderr or "")
        assert r.returncode == 5, (r.returncode, combined[-200:])
        assert "--no-replace" in combined and "already exists" in combined


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
