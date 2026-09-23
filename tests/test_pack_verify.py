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
    the good file keeps REAL dims, the poison file gets a row whose counts
    are NULL = unknown (recorded, not guessed; S1: a 0 passed every
    triangle filter as "costs nothing")."""
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
        assert rows["poison"]["triangles"] is None, \
            f"poison must yield NULL (unknown), not 0 or fabricated numbers: {rows['poison']}"


def test_promote_native_never_reads_blends_file():
    """native_index_blends.jsonl sorts AFTER every timestamped run
    ('b' > '2'); a plain newest-wins glob fed blend records to this
    wholesale overwrite and erased every FBX/OBJ container record."""
    # run from a temp COPY of pipeline/: the script reads its inputs from
    # its own folder, and the real one may hold a user's chain-3 output
    tmp_native = _native_copy()
    native_dir = tmp_native / "pipeline" / "native"
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
            r = subprocess.run(
                [sys.executable, "-B", str(native_dir / "promote_native.py")],
                capture_output=True, text=True, timeout=300,
                env={**os.environ,
                     "PHAROS_CONFIG": str(Path(tmp) / "missing.json"),
                     "PHAROS_LIBRARY_ROOT": str(lib)})
            assert r.returncode == 0, (r.stderr or "")[-300:]
            out = lib / "_Agent_Files" / "native_models.jsonl"
            assert out.is_file(), "index not written"
            body = out.read_text(encoding="utf-8")
            assert "FROMBLENDS" not in body, "blend records leaked in"
            assert "ts_0" in body, "timestamped records missing"
        finally:
            shutil.rmtree(tmp_native, ignore_errors=True)


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
        # W0.2: CI installs mcp and sets PHAROS_REQUIRE_MCP=1 -- there a
        # load failure must FAIL, never print SKIP and count as a pass
        assert os.environ.get("PHAROS_REQUIRE_MCP") != "1", \
            f"PHAROS_REQUIRE_MCP=1 but the MCP server did not load: {exc}"
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


# ---- INFRA lane: W0.3 Blender-in-CI wrapper (W0.2's PHAROS_REQUIRE_MCP
# gate lives inside test_mcp_db_preflight_and_clip_scope) ----

def _blender_for_checks() -> str:
    """BLENDER_EXE first -- set but missing counts as NOT FOUND, never a
    silent fallback -- else doctor's finder (PATH + install dirs)."""
    env = os.environ.get("BLENDER_EXE", "")
    if env:
        return env if Path(env).is_file() else ""
    from asset_service.doctor import _find_blender
    return _find_blender()


def test_scene_builder_in_blender():
    """scene_builder checks that need Blender run in tests/
    blender_scene_checks.py (build, save, reopen headless, assert). SKIP
    without Blender, unless PHAROS_REQUIRE_BLENDER=1 makes that a FAIL."""
    exe = _blender_for_checks()
    if not exe:
        assert os.environ.get("PHAROS_REQUIRE_BLENDER") != "1", \
            "PHAROS_REQUIRE_BLENDER=1 but no Blender found " \
            "(BLENDER_EXE / PATH / install dirs)"
        print("SKIP: no Blender on this machine -- scene_builder checks "
              "not run")
        return
    try:
        r = subprocess.run(
            [exe, "--background", "--factory-startup",
             "--python-exit-code", "1",
             "--python", str(REPO / "tests" / "blender_scene_checks.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", cwd=str(REPO), timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"Blender run failed: {exc}") from exc
    assert r.returncode == 0, \
        f"blender_scene_checks rc={r.returncode}:\n{(r.stdout or '')[-1500:]}"


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


def _native_copy(dest=None) -> Path:
    """A throwaway copy of pipeline/_config.py + pipeline/native/: the
    chain-3 scripts read and write next to themselves, and the REAL
    pipeline/native/ may hold a user's chain-3 output -- tests must never
    write or delete there."""
    dest = Path(dest or tempfile.mkdtemp(prefix="pharos_native_"))
    (dest / "pipeline" / "native").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "pipeline" / "_config.py", dest / "pipeline")
    for f in (REPO / "pipeline" / "native").iterdir():
        if f.is_file() and f.suffix in (".py", ".sh"):
            shutil.copy2(f, dest / "pipeline" / "native" / f.name)
    return dest


def test_native_worklist_is_lf_only():
    """The worklist was written in Windows text mode; Git Bash read -r
    kept the \\r and Blender silently failed on 'path\\r'."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        (lib / "Kits" / "K1").mkdir(parents=True)
        (lib / "Kits" / "K1" / "kit.blend").write_bytes(b"fake")
        native = _native_copy(tmp / "copy") / "pipeline" / "native"
        r = subprocess.run(
            [sys.executable, "-B", str(native / "make_native_manifest2.py")],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "PHAROS_CONFIG": str(tmp / "missing.json"),
                 "PHAROS_LIBRARY_ROOT": str(lib)})
        assert r.returncode == 0, (r.stderr or "")[-300:]
        data = (native / "native_blends.txt").read_bytes()
        assert data and b"\r" not in data, data[:80]
        mdata = (native / "native_manifest.json").read_bytes()
        assert b"\r" not in mdata


def test_native_driver_fails_loudly():
    """A Blender crash used to read as success (pipeline-to-grep lost
    every exit code, driver always exited 0). Stub Blender: nonzero stub
    exit must surface as driver exit 9."""
    bash = _find_usable_bash()
    if bash is None:
        print("SKIP: no usable bash")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stub = tmp / "stub-blender.sh"
        stub.write_text('#!/usr/bin/env bash\n'
                        'echo "NATIVE_DONE 3 objects (stub)"\n'
                        'echo "record" >> "$AMNATIVE_OUT"\n'
                        'exit "${STUB_RC:-0}"\n', encoding="utf-8")
        stub.chmod(0o755)
        native = _native_copy(tmp / "copy") / "pipeline" / "native"
        (native / "native_manifest.json").write_text(json.dumps({
            "importable": [], "blend_files": [
                {"path": "X:/nope/kit.blend", "section": "S"}]}),
            encoding="utf-8")
        (native / "native_blends.txt").write_text(
            "X:/nope/kit.blend\nS\n", encoding="utf-8")
        base_env = {**os.environ,
                    "BLENDER_EXE": str(stub),
                    "PHAROS_CONFIG": str(tmp / "missing.json")}
        r_bad = subprocess.run(
            [bash, str(native / "index_native_all.sh")],
            capture_output=True, text=True, cwd=str(REPO),
            timeout=120, env={**base_env, "STUB_RC": "1"})
        assert r_bad.returncode == 9, \
            (r_bad.returncode, (r_bad.stdout or "")[-200:])
        r_ok = subprocess.run(
            [bash, str(native / "index_native_all.sh")],
            capture_output=True, text=True, cwd=str(REPO),
            timeout=120, env=base_env)
        assert r_ok.returncode == 0, (r_ok.stdout or "")[-200:]


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


def test_scan_keying_same_name_different_folders():
    """B2/B6: same-named assets in sibling folders must BOTH survive a
    scan (the old (name, pack) key collapsed them silently; audio's
    UNIQUE(rel) cross-scan overwrite had the same effect)."""
    import wave as _w
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parent = tmp / "MyPacks"
        for sub in ("PackA", "PackB"):
            (parent / sub).mkdir(parents=True)
            (parent / sub / "Chair.fbx").write_bytes(_min_fbx())
        db = tmp / "reg.sqlite"
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "service" / "asset_service" / "scanner.py"),
             str(parent), "--db", str(db)],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode == 0, (r.stderr or "")[-200:]
        import sqlite3
        conn = sqlite3.connect(str(db))
        n_mesh = conn.execute("SELECT COUNT(*) FROM meshes").fetchone()[0]
        conn.close()
        assert n_mesh == 2, \
            f"B2: same-named meshes collapsed: {n_mesh} rows (want 2)"

        # B6: two separately-scanned audio folders with same-named files
        aud_root = tmp / "audio"
        for sub in ("FolderA", "FolderB"):
            d = aud_root / sub
            d.mkdir(parents=True)
            with _w.open(str(d / "beep.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 4000)
        db2 = tmp / "aud.sqlite"
        for sub in ("FolderA", "FolderB"):
            r = subprocess.run(
                [sys.executable, "-B",
                 str(REPO / "service" / "asset_service" / "scanner.py"),
                 str(aud_root / sub), "--db", str(db2)],
                capture_output=True, text=True, cwd=str(REPO),
                timeout=120)
            assert r.returncode == 0, (r.stderr or "")[-200:]
        conn = sqlite3.connect(str(db2))
        n_aud = conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
        rels = [r_[0] for r_ in conn.execute("SELECT rel FROM audio")]
        conn.close()
        assert n_aud == 2, \
            f"B6: cross-scan audio overwrite: {n_aud} rows (want 2): {rels}"


# ---- INGEST lane: 1.1 1.2 1.3 2.1 2.2 (material roles, recipe flags,
# one-map-per-role collapse, packed channel letters, FBX triangle counts) ----

def test_material_role_aliases():
    """1.1: role inference was a substring match only, so exact UE aliases
    (Emmisive, Base Map, NRM, AO, ...) landed in `other`. The rules now
    live in one stdlib module shared by the relink step and the importer;
    aliases match EXACTLY, so *_Base layer params stay `other`."""
    import importlib.util as ilu
    assert ilu.find_spec("asset_service.material_roles"), \
        "asset_service.material_roles does not exist (1.1)"
    from asset_service import material_roles as mr
    cases = {"Emmisive": "emissive", "Emission Texture": "emissive",
             "Base Map": "albedo", "NRM": "normal", "AO": "ao",
             "Metalic": "metallic", "Base N": "normal",
             "Rust_Base": "other", "SecondBase": "other",
             "Paint_Base": "other", "BlendBase": "other",
             # substring rules and packed channels are unchanged
             "BaseColor": "albedo", "ORM": "packed"}
    for param, want in cases.items():
        got = mr.classify_param(param)[0]
        assert got == want, f"{param!r} -> {got!r}, want {want!r}"
    src = (REPO / "service" / "asset_service"
           / "material_roles.py").read_text(encoding="utf-8")
    assert "import unreal" not in src, "material_roles must not import unreal"
    relink = (REPO / "pipeline" / "conversion"
              / "relink_materials.py").read_text(encoding="utf-8")
    assert "ROLE_RULES = (" not in relink and "material_roles" in relink, \
        "relink_materials.py must import the shared rules, not keep a copy"


def _fx_leartes_mesh() -> dict:
    """Anonymised v2 manifest mesh: one real slot + one unresolved slot."""
    ovr = "instance_override:MI_Rope_Fixture"
    return {"name": "SM_FX_Rope", "asset_path": "/Game/FX/SM_FX_Rope",
            "materials": [
                {"slot": 0, "name": "MI_Rope_Fixture", "base": "M_FX_Master",
                 "textures": [
                     # manifest order puts a master default FIRST
                     {"param": "EmissiveTex", "role": "emissive",
                      "file": "Textures/TX_FX_Sheet_Diffuse.png",
                      "source": "material_default"},
                     {"param": "DiffuseTex", "role": "albedo",
                      "file": "Textures/T_FX_Rope_ALB.png", "source": ovr},
                     {"param": "Base Map", "role": "other",
                      "file": "Textures/T_FX_Rope_B.png", "source": ovr},
                     {"param": "AlphaTex", "role": "opacity",
                      "file": "Textures/T_Fill_FX_B.png",
                      "source": "material_default"},
                     {"param": "Emmisive", "role": "other",
                      "file": "Textures/T_Fill02_FX.png",
                      "source": "material_default"},
                     {"param": "Colour Mask", "role": "packed",
                      "channels": None,
                      "file": "Textures/T_FX_Rope_Mask.png",
                      "source": "material_default"},
                     # mask-like param wins over an ORM token in the file
                     {"param": "Grunge Mask", "role": "packed",
                      "channels": None,
                      "file": "Textures/T_FX_Grunge_ORM.png",
                      "source": "material_default"},
                     # channel-less pack whose FILE names the order
                     {"param": "RGBTex", "role": "packed", "channels": None,
                      "file": "Textures/T_FX_Rope_ORM.png", "source": ovr},
                 ]},
                {"slot": 1, "name": "MI_FX_Trim", "base": "M_FX_Trim",
                 "note": "unresolved",
                 "unresolved_reason": "material chain exposes no texture "
                                      "parameters (master M_FX_Trim)",
                 "textures": [
                     {"param": None, "asset_path": "/Game/FX/T_FX_Trim",
                      "file": "Textures/T_FX_Trim_D.png",
                      "source": "material_used_textures",
                      "note": "unresolved"}]},
            ]}


def test_recipe_flags():
    """1.2: the served recipe listed master defaults and TX_Fill
    placeholders as live maps (source was stripped), channel-less colour
    masks became ORM `packed`, and unresolved slots lost their marker.
    Flags only: every map stays served."""
    from asset_service import meshes_import as mi
    rec = mi._finish_recipe(mi._leartes_recipe(
        _fx_leartes_mesh(), Path("fx_lib") / "FX_Pack" / "Exports"))
    s0, s1 = rec["slots"]
    by = {Path(m["file"]).name: m for m in s0["maps"] + s1["maps"]}
    # guard (passes on the old code too): nothing is dropped
    assert len(s0["maps"]) == 8 and len(s1["maps"]) == 1, \
        f"maps dropped: {len(s0['maps'])}+{len(s1['maps'])} (want 8+1)"
    for m in s0["maps"] + s1["maps"]:
        assert m.get("source") in ("override", "default", "unnamed"), \
            f"{Path(m['file']).name}: source {m.get('source')!r}"
    assert s0["maps"][0]["source"] == "override", \
        "maps are not rank-sorted (a master default is served first)"
    assert by["T_Fill_FX_B.png"].get("placeholder") is True, \
        "T_Fill_FX_B not flagged placeholder"
    assert by["T_Fill02_FX.png"].get("placeholder") is True
    assert "placeholder" not in by["T_FX_Rope_ALB.png"], \
        "placeholder key must be absent on a real map"
    assert by["T_FX_Rope_B.png"]["role"] == "albedo", \
        f"'Base Map' role {by['T_FX_Rope_B.png']['role']!r} (want albedo)"
    assert by["T_Fill02_FX.png"]["role"] == "emissive"
    assert by["T_FX_Rope_Mask.png"]["role"] == "mask", \
        f"Colour Mask role {by['T_FX_Rope_Mask.png']['role']!r} (want mask)"
    assert by["T_FX_Grunge_ORM.png"]["role"] == "mask"
    orm = by["T_FX_Rope_ORM.png"]
    assert orm["role"] == "packed" and orm.get("channels") == \
        {"r": "ao", "g": "roughness", "b": "metallic"}, \
        f"RGBTex -> *_ORM channels {orm.get('channels')!r}"
    prim = rec["primary"]
    assert "emissive" not in prim and "opacity" not in prim, \
        f"primary serves a default emissive/opacity: {sorted(prim)}"
    assert Path(prim.get("albedo", "")).name == "T_FX_Rope_ALB.png", prim
    assert rec["resolved"] is True
    assert "resolved" not in s0, "resolved key added to a resolved slot"
    assert s1.get("resolved") is False and s1.get("unresolved_reason"), \
        f"unresolved slot lost its marker: {sorted(s1)}"
    from asset_service import scene_manifest as sm
    assert hasattr(sm, "collapse_slot_maps"), "collapse_slot_maps missing"
    assert prim == sm.collapse_slot_maps(s0), \
        "primary is not the builder's collapse_slot_maps pick"
    # a mesh whose only named maps are masks is not resolved
    only_mask = {"name": "SM_FX_Masked", "materials": [
        {"slot": 0, "name": "MI_FX_Masked", "textures": [
            {"param": "Colour Mask", "role": "packed", "channels": None,
             "file": "Textures/T_FX_Masked_Mask.png",
             "source": "instance_override"}]}]}
    assert mi._finish_recipe(mi._leartes_recipe(
        only_mask, Path("fx_lib")))["resolved"] is False, \
        "a mask-only mesh reads resolved:true"
    # guard: KitBash maps keep their suffix roles (refraction has no rule)
    kb = mi._finish_recipe(mi._kb3d_recipe(
        {"materials": ["M_FX_Glass"],
         "texture_files": ["M_FX_Glass_refraction.png",
                           "M_FX_Glass_basecolor.png"]}, Path("fx_kit")))
    assert sorted(m["role"] for m in kb["slots"][0]["maps"]) == \
        ["albedo", "refraction"], kb["slots"][0]["maps"]


def test_slot_map_precedence():
    """2.1: the builder collapsed a slot with {role: file}, so the LAST map
    per role won (TX_Fill placeholders listed after the real maps), and
    the packed file and its channels could come from different maps."""
    from asset_service import scene_manifest as sm
    assert hasattr(sm, "collapse_slot_maps"), \
        "scene_manifest.collapse_slot_maps does not exist (2.1)"
    f = lambda n: f"fx_lib/Textures/{n}.png"                  # noqa: E731
    rma = {"r": "roughness", "g": "metallic", "b": "ao"}
    slot = {"slot": 0, "material": "MI_FX_Wall", "maps": [
        {"role": "albedo", "file": f("T_FX_Wall_ALB"), "source": "override"},
        {"role": "normal", "file": f("T_FX_Wall_NRM"), "source": "override"},
        {"role": "packed", "file": f("T_FX_Wall_RMA"), "channels": rma,
         "source": "override"},
        {"role": "albedo", "file": f("TX_Fill_FX_ALB"), "source": "default",
         "placeholder": True},
        {"role": "normal", "file": f("TX_Fill_FX_NRM"), "source": "default",
         "placeholder": True},
        {"role": "roughness", "file": f("TX_Fill_FX_R"), "source": "default",
         "placeholder": True},
        {"role": "packed", "file": f("T_FX_Wall_Pack"), "channels": None,
         "source": "default"},
        {"role": "mask", "file": f("T_FX_Wall_Mask"), "channels": None,
         "source": "default"},
        {"role": "emissive", "file": f("TX_FX_Sheet_Diffuse"),
         "source": "default"},
        {"role": "opacity", "file": f("T_FX_Wall_Opacity"),
         "source": "default"},
    ]}
    got = sm.collapse_slot_maps(slot)
    assert got.get("albedo") == f("T_FX_Wall_ALB"), got
    assert got.get("normal") == f("T_FX_Wall_NRM"), got
    assert got.get("packed") == f("T_FX_Wall_RMA") and \
        got.get("packed_channels") == rma, \
        f"packed {got.get('packed')} / {got.get('packed_channels')}"
    assert "roughness" not in got, "a placeholder roughness blocks the ORM"
    assert "emissive" not in got and "opacity" not in got, sorted(got)
    # channel-less packs only: the FIRST one, and no invented channels
    bare = sm.collapse_slot_maps({"maps": [
        {"role": "packed", "file": f("T_FX_A_RGB"), "channels": None},
        {"role": "packed", "file": f("T_FX_B_RGB"), "channels": None}]})
    assert bare.get("packed") == f("T_FX_A_RGB") and \
        bare.get("packed_channels") is None, bare
    # ...but the first packed map WITH channels beats an earlier bare one
    mixed = sm.collapse_slot_maps({"maps": [
        {"role": "packed", "file": f("T_FX_C_RGB"), "channels": None},
        {"role": "packed", "file": f("T_FX_C_RMA"), "channels": rma}]})
    assert mixed.get("packed") == f("T_FX_C_RMA") and \
        mixed.get("packed_channels") == rma, mixed
    # a mask is never the ORM
    assert "packed" not in sm.collapse_slot_maps({"maps": [
        {"role": "mask", "file": f("T_FX_Only_Mask"), "channels": None}]})
    # a slot marked resolved:false keeps the FBX's own material
    assert sm.collapse_slot_maps({"resolved": False, "maps": [
        {"role": "albedo", "file": f("T_FX_Trim_D")}]}) == {}
    # source guard: the builder uses the same helper
    src = (REPO / "service" / "asset_service"
           / "scene_builder.py").read_text(encoding="utf-8")
    assert "collapse_slot_maps(" in src, \
        "scene_builder.py does not call collapse_slot_maps"


def test_packed_channel_letters():
    """2.2: channel letters went through {r,g,b}.get(letter, "Green"), so
    `a` (height, 3k+ maps) and any unknown letter silently read Green."""
    from asset_service import scene_manifest as sm
    assert hasattr(sm, "packed_channel_sockets"), \
        "scene_manifest.packed_channel_sockets does not exist (2.2)"
    got = sm.packed_channel_sockets(
        {"r": "ao", "g": "roughness", "b": "metallic", "a": "height"})
    assert got == [("Red", "ao"), ("Green", "roughness"),
                   ("Blue", "metallic"), ("Alpha", "height")], got
    assert sm.packed_channel_sockets({"x": "roughness", "b": "metallic"}) \
        == [("Blue", "metallic")], "an unknown letter was not skipped"
    assert sm.packed_channel_sockets(None) == []
    src = (REPO / "service" / "asset_service"
           / "scene_builder.py").read_text(encoding="utf-8")
    assert "packed_channel_sockets(" in src and \
        '.get(ch_letter, "Green")' not in src, \
        "scene_builder still maps unknown channel letters to Green"


def test_agent_index_triangles_from_fbx():
    """1.3 (D4, D9): models.jsonl copied the UE manifest's triangles
    (render LOD0; null for every SkeletalMesh). The exported FBX is the
    truth; the engine value is kept as `triangles_engine`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        for i in range(10):          # the 10-pack / 10-row near-empty guards
            ex = lib / f"FX_Pack_{i:02d}" / "Exports"
            (ex / "FBX").mkdir(parents=True)
            (ex / "FBX" / "m.fbx").write_bytes(_min_fbx())   # 1 tri, 3 verts
            meshes = [{"name": "m", "kind": "StaticMesh", "fbx": "FBX/m.fbx",
                       "triangles": 999 if i == 0 else 1, "vertices": 50,
                       "bbox_m": [1.0, 1.0, 0.0], "materials": []}]
            if i == 0:
                meshes.append({"name": "skm", "kind": "SkeletalMesh",
                               "fbx": "FBX/m.fbx", "triangles": None,
                               "vertices": None, "bbox_m": [1.0, 1.0, 0.0],
                               "materials": []})
                # unparseable FBX: the engine value must survive
                (ex / "FBX" / "bad.fbx").write_bytes(b"Kaydara FBX Binary  \x00")
                meshes.append({"name": "bad", "kind": "StaticMesh",
                               "fbx": "FBX/bad.fbx", "triangles": 7,
                               "vertices": 9, "bbox_m": [1.0, 1.0, 0.0],
                               "materials": []})
            (ex / "manifest.json").write_text(json.dumps({
                "schema": "pharos.pack.export/v2", "pack": f"FX_Pack_{i:02d}",
                "counts": {"fbx_written": len(meshes)}, "meshes": meshes}),
                encoding="utf-8")
        r = _run("pipeline/agent_index/build_agent_index.py", {
            "PHAROS_CONFIG": str(tmp / "missing_config.json"),
            "PHAROS_LIBRARY_ROOT": str(lib)})
        assert r.returncode == 0, (r.stderr or r.stdout or "")[-400:]
        out = lib / "_Agent_Files" / "models.jsonl"
        assert out.is_file(), "models.jsonl not written to the temp library"
        rows = {row["name"]: row for row in (
            json.loads(line) for line in
            out.read_text(encoding="utf-8").splitlines() if line.strip())
            if row["pack"] == "FX_Pack_00"}
        sm_, skm, bad = rows["m"], rows["skm"], rows["bad"]
        assert sm_["triangles"] == 1, \
            f"StaticMesh triangles {sm_['triangles']} (want 1 from the FBX)"
        assert skm["triangles"] == 1, \
            f"SkeletalMesh triangles {skm['triangles']} (want 1 from the FBX)"
        assert sm_.get("triangles_engine") == 999, sm_
        assert sm_["vertices"] == 3 and skm["vertices"] == 3, (sm_, skm)
        assert bad["triangles"] == 7 and bad["vertices"] == 9, bad


def test_unknown_triangles_stay_null():
    """1.3: the importer stored an unknown triangle count as 0, so the row
    passed every max_tri filter and cost nothing in a budget."""
    from asset_service import meshes_import as mi
    rec = mi._record({"name": "SM_FX_Unknown", "pack": "FX_Pack",
                      "triangles": None, "vertices": None},
                     dict(mi.EMPTY_RECIPE), None, None)
    assert rec["triangles"] is None, \
        f"unknown triangles stored as {rec['triangles']!r} (want NULL)"
    assert rec["vertices"] is None, \
        f"unknown vertices stored as {rec['vertices']!r} (want NULL)"
    known = mi._record({"name": "SM_FX_Known", "pack": "FX_Pack",
                        "triangles": 12, "vertices": 8},
                       dict(mi.EMPTY_RECIPE), None, None)
    assert known["triangles"] == 12 and known["vertices"] == 8


def test_recipe_primary_skips_mask_only_slot():
    """1.2 (review fix): `primary` came from the first slot whose collapse
    was non-empty, so a first slot holding only a mask made the mesh read
    resolved:false and its real later slots were never served."""
    from asset_service import meshes_import as mi
    mesh = {"name": "SM_FX_MaskFirst", "materials": [
        {"slot": 0, "name": "MI_FX_Decal", "textures": [
            {"param": "Colour Mask", "role": "packed", "channels": None,
             "file": "Textures/T_FX_Decal_Mask.png",
             "source": "instance_override"}]},
        {"slot": 1, "name": "MI_FX_Body", "textures": [
            {"param": "DiffuseTex", "role": "albedo",
             "file": "Textures/T_FX_Body_ALB.png",
             "source": "instance_override"}]}]}
    rec = mi._finish_recipe(mi._leartes_recipe(mesh, Path("fx_lib")))
    assert rec["resolved"] is True and \
        Path(rec["primary"].get("albedo", "")).name == "T_FX_Body_ALB.png", \
        f"mask-only slot 0 hid slot 1: resolved={rec['resolved']} " \
        f"primary={sorted(rec['primary'])}"
    assert rec["primary_slot"] == 1, rec["primary_slot"]


# ---- LEAD: 1.8 (ingest ASK block helpers) ----

def test_not_indexed_dirs_claims_absolute_sections():
    """1.8 (review fix): a section configured as an ABSOLUTE path inside
    the library (config accepts that) was listed as an unclaimed folder."""
    from asset_service.init import _not_indexed_dirs
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for d in ("Audio", "Stuff", "Models", "_Agent_Files"):
            (root / d).mkdir()
        cfg = {"sections": {"audio": str(root / "Audio")},
               "scan_folders": ["Models"]}
        got = _not_indexed_dirs(root, cfg)
        assert got == ["Stuff"], f"unclaimed folders {got} (want ['Stuff'])"
        # the root as the user typed it may be UNRESOLVED (macOS /var ->
        # /private/var, Windows 8.3 RUNNER~1) while config/DB paths are
        # resolved: a claimed folder must still count as claimed
        (root / "Kits").mkdir()
        cfg = {"kitbash_root": str((root / "Kits").resolve())}
        unresolved = root / "Stuff" / ".."
        got = _not_indexed_dirs(unresolved, cfg)
        assert "Kits" not in got, \
            f"resolved kitbash_root not matched against an unresolved " \
            f"root: {got}"


# ---- RELEASE: R3 (near-empty guard vs small libraries) ----

_GUARDED_WRITERS = ("pipeline/agent_index/build_agent_index.py",
                    "pipeline/agent_index/build_availability_catalog.py",
                    "pipeline/agent_index/gen_index.py",
                    "pipeline/agent_index/gen_tex_index.py",
                    "pipeline/kitbash/build_kb3d_index.py",
                    "pipeline/native/promote_blends.py",
                    "pipeline/native/promote_native.py")


def test_index_guard_allows_small_first_build():
    """R3: every index writer refused ANY result under 10 rows, so a
    library with 2 converted packs (or 1 kit, or 5 sounds) could never
    get an index at all. The guard exists to stop a wrong root from
    REPLACING a real live index -- it must refuse only that."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        for i in range(2):
            ex = lib / f"FX_Small_{i}" / "Exports"
            (ex / "FBX").mkdir(parents=True)
            (ex / "FBX" / "m.fbx").write_bytes(_min_fbx())
            (ex / "manifest.json").write_text(json.dumps({
                "schema": "pharos.pack.export/v2", "pack": f"FX_Small_{i}",
                "meshes": [{"name": "m", "kind": "StaticMesh",
                            "fbx": "FBX/m.fbx", "triangles": 1,
                            "vertices": 3, "bbox_m": [1.0, 1.0, 1.0],
                            "materials": []}]}), encoding="utf-8")
        env = {"PHAROS_CONFIG": str(tmp / "missing_config.json"),
               "PHAROS_LIBRARY_ROOT": str(lib)}
        r = _run("pipeline/agent_index/build_agent_index.py", env)
        assert r.returncode == 0 and \
            (lib / "_Agent_Files" / "models.jsonl").is_file(), \
            f"2-pack first build refused: {(r.stderr or r.stdout)[-160:]}"
        # ...but a wrong root must still not REPLACE a real live index
        live = lib / "_Agent_Files" / "models.jsonl"
        live.write_text("".join(json.dumps({"pack": "P", "name": f"n{k}"})
                                + "\n" for k in range(40)), encoding="utf-8")
        r = _run("pipeline/agent_index/build_agent_index.py", env)
        assert r.returncode != 0 and "FATAL" in (r.stderr + r.stdout), \
            "a near-empty scan replaced a 40-row live index"
        assert len(live.read_text(encoding="utf-8").splitlines()) == 40
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("pipeline_cfg",
                                       REPO / "pipeline" / "_config.py")
    cfg = ilu.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    assert hasattr(cfg, "refuse_near_empty"), "no shared guard helper"
    # a small library that LOSES a pack or two is not "near-empty":
    # 11 -> 9 must be written, 40 -> 2 (a wrong root) must not
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "idx.jsonl"
        live.write_text("{}\n" * 11, encoding="utf-8")
        try:
            cfg.refuse_near_empty(9, str(live))
        except SystemExit as exc:
            raise AssertionError(f"11 -> 9 rows refused: {exc}") from exc
        live.write_text("{}\n" * 40, encoding="utf-8")
        try:
            cfg.refuse_near_empty(2, str(live))
            raise AssertionError("40 -> 2 rows was NOT refused")
        except SystemExit:
            pass
    for w in _GUARDED_WRITERS:
        src = (REPO / w).read_text(encoding="utf-8")
        assert "refuse_near_empty(" in src and "< 10:" not in src, \
            f"{w} still carries its own unconditional <10 guard"


def test_agent_index_finds_nested_packs():
    """R2 follow-up: ingest detects converted packs at ANY depth, but the
    agent index only looked at <top>/Exports and <top>/<pack>/Exports, so
    a pack under <top>/<category>/<pack>/Exports got a recipe and no mesh
    row. It must also honour the configured agent_files folder."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        ex = lib / "FX_Unreal" / "FX_Envs" / "FX_Deep" / "Exports"
        (ex / "FBX").mkdir(parents=True)
        (ex / "FBX" / "m.fbx").write_bytes(_min_fbx())
        (ex / "manifest.json").write_text(json.dumps({
            "schema": "pharos.pack.export/v2", "pack": "FX_Deep",
            "meshes": [{"name": "deep_m", "kind": "StaticMesh",
                        "fbx": "FBX/m.fbx", "triangles": 1, "vertices": 3,
                        "bbox_m": [1.0, 1.0, 1.0], "materials": []}]}),
            encoding="utf-8")
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps({"agent_files": "FX_Index"}),
                       encoding="utf-8")
        r = _run("pipeline/agent_index/build_agent_index.py", {
            "PHAROS_CONFIG": str(cfg), "PHAROS_LIBRARY_ROOT": str(lib)})
        out = lib / "FX_Index" / "models.jsonl"
        assert r.returncode == 0 and out.is_file(), \
            f"index not written to the configured agent_files folder: " \
            f"rc={r.returncode} {(r.stderr or r.stdout)[-160:]}"
        names = [json.loads(x)["name"] for x in
                 out.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert names == ["deep_m"], f"nested pack rows: {names}"


def test_registry_stamp_and_message():
    """R1 (review): (a) re-running init one folder HIGHER -- the natural
    answer to "are these ALL your folders?" -- was refused as foreign
    although every row lies under the new root; narrowing to a subfolder
    must stay foreign. (b) The STOP message told users to delete the whole
    registry FOLDER, which also holds the rendered animation previews.
    (c) Registries were opened through file:// URIs, which SQLite cannot
    open on a network (UNC) path: serve crashed, the R1 check failed open."""
    from asset_service import db
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        (lib / "Sub" / "Deeper").mkdir(parents=True)
        reg = tmp / "reg" / "assets.sqlite"
        reg.parent.mkdir()
        db.stamp_registry(reg, lib / "Sub")
        assert db.registry_foreign(reg, lib / "Sub") == ""
        assert db.registry_foreign(reg, lib) == "", \
            "a registry stamped for a SUBFOLDER was refused for its parent"
        assert db.registry_foreign(reg, lib / "Sub" / "Deeper"), \
            "narrowing the library to a subfolder must stay foreign"
        assert db.registry_foreign(reg, tmp / "other"), "other root accepted"
        msg = db.foreign_registry_message(reg, "stamped for X", lib)
        assert "assets.sqlite" in msg and "delete the folder" not in msg, \
            f"STOP message must name the registry FILE: {msg[:200]}"
    for f in ("service/asset_service/db.py", "service/asset_service/init.py",
              "service/pharos_mcp_server.py",
              "pipeline/agent_index/build_availability_catalog.py"):
        src = (REPO / f).read_text(encoding="utf-8")
        assert "as_uri()" not in src and "?mode=r" not in src, \
            f"{f} opens the registry via a file: URI (fails on UNC paths)"


def test_pipeline_native_follows_config():
    """R4/S2 (review): chain 3 hard-coded the `Animation` folder (clip FBX
    in any other section became 'native models'), and promote_blends
    excluded the kitbash_root section as 'covered by kit exports' even
    when those kits were never exported (S2 now detects raw kits)."""
    src = (REPO / "pipeline" / "native" / "make_native_manifest2.py").read_text(
        encoding="utf-8")
    assert '{"Animation", "_Agent_Files"}' not in src, \
        "make_native_manifest2 still hard-codes the Animation section"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "pipeline" / "native").mkdir(parents=True)
        shutil.copy2(REPO / "pipeline" / "_config.py", tmp / "pipeline")
        shutil.copy2(REPO / "pipeline" / "native" / "promote_blends.py",
                     tmp / "pipeline" / "native")
        lib = tmp / "lib"
        kit = lib / "Kits" / "FX_Kit" / "kb3d_fxkit.blender.native"
        kit.mkdir(parents=True)
        (kit / "fxkit.blend").write_bytes(b"BLENDER-stub")
        (lib / "_Agent_Files").mkdir()
        (tmp / "pipeline" / "native" / "native_index_blends.jsonl").write_text(
            "".join(json.dumps({
                "section": "Kits", "object": f"FX_Bldg_{i}",
                "source": str(kit / "fxkit.blend"), "triangles": 100,
                "vertices": 60, "bbox_m": [1, 1, 1], "materials": []}) + "\n"
                for i in range(3)), encoding="utf-8")
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps({"library_root": str(lib),
                                   "kitbash_root": str(lib / "Kits")}),
                       encoding="utf-8")
        env = dict(os.environ, PHAROS_CONFIG=str(cfg),
                   PHAROS_LIBRARY_ROOT=str(lib))
        r = subprocess.run([sys.executable, "-B", str(
            tmp / "pipeline" / "native" / "promote_blends.py")],
            capture_output=True, text=True, env=env, timeout=120)
        out = lib / "_Agent_Files" / "native_models.jsonl"
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8")
                .splitlines() if x.strip()] if out.is_file() else []
        assert len(rows) == 3, \
            f"unexported kit blends dropped as 'covered': rc={r.returncode} " \
            f"rows={len(rows)} {(r.stdout + r.stderr)[-160:]}"


def test_init_force_keeps_outside_roots_on_same_library():
    """R2e (review): `init --force` now rebuilds detection-owned keys, but
    a manifest root / kit root the user set OUTSIDE the library (converted
    packs on another drive) can't be re-detected and was silently dropped;
    switching to ANOTHER library must still drop them all."""
    from asset_service import config, init
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib, outside = tmp / "lib", tmp / "elsewhere" / "Converted"
        lib.mkdir()
        outside.mkdir(parents=True)
        old = {"library_root": str(lib),
               "manifest_roots": [str(outside), str(lib / "Gone")],
               "kitbash_root": str(tmp / "elsewhere" / "Kits"),
               "sections": {}}
        real_load = config.load
        try:
            config.load = lambda: json.loads(json.dumps(old))
            rep = init.detect(lib)
            cfg = init.build_config(rep, tmp / "reg")
            assert cfg.get("manifest_roots") == [str(outside)] and \
                cfg.get("kitbash_root") == old["kitbash_root"], \
                f"same library: outside roots lost: {cfg.get('manifest_roots')}" \
                f" / {cfg.get('kitbash_root')}"
            other = tmp / "other_lib"
            other.mkdir()
            cfg2 = init.build_config(init.detect(other), tmp / "reg")
            assert not cfg2.get("manifest_roots") and \
                not cfg2.get("kitbash_root"), \
                f"another library inherited roots: {cfg2.get('manifest_roots')}"
        finally:
            config.load = real_load


def test_console_survives_non_latin_names():
    """Release check: a folder name outside the console's code page (CJK
    on a cp1252 Windows pipe -- how every agent reads output) killed init,
    ingest and the scanner mid-run with UnicodeEncodeError."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        models = tmp / "models"
        pack = models / "日本のパック" / "Exports"
        pack.mkdir(parents=True)
        (pack / "manifest.json").write_text(
            '{"schema": "pharos.pack.export/v2", "meshes": []}',
            encoding="utf-8")
        (models / "plate.obj").write_text(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONIOENCODING"] = "cp1252"   # the Windows pipe, on any OS
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "service" / "asset_service" / "scanner.py"),
             str(models), "--db", str(tmp / "reg.sqlite")],
            capture_output=True, env=env, timeout=120)
        err = (r.stderr or b"").decode("cp1252", "replace")
        assert r.returncode == 0 and "UnicodeEncodeError" not in err, \
            f"scanner died on a non-Latin folder name: rc={r.returncode} " \
            f"{err[-200:]}"


def test_config_with_bom_is_read():
    """Release check: Windows PowerShell 5.1 (Set-Content/Out-File -Encoding
    utf8) writes a UTF-8 BOM; such a pharos_config.json was silently read as
    'not configured' and doctor then advised init --force."""
    from asset_service import config
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("pipeline_cfg_bom",
                                       REPO / "pipeline" / "_config.py")
    pcfg = ilu.module_from_spec(spec)
    spec.loader.exec_module(pcfg)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "pharos_config.json"
        f.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"library_root": "X:/fx_lib", "registry_dir": "X:/fx_reg"}).encode())
        real = config._CONFIG_FILE
        try:
            config._CONFIG_FILE = f
            got = config.load().get("library_root")
        finally:
            config._CONFIG_FILE = real
        assert got == "X:/fx_lib", f"service config.load read {got!r}"
        old = os.environ.get("PHAROS_CONFIG")
        os.environ["PHAROS_CONFIG"] = str(f)
        try:
            got2 = pcfg.load().get("library_root")
        finally:
            if old is None:
                os.environ.pop("PHAROS_CONFIG", None)
            else:
                os.environ["PHAROS_CONFIG"] = old
        assert got2 == "X:/fx_lib", f"pipeline _config.load read {got2!r}"


def test_kit_without_metadata_pass_does_not_kill_import():
    """Release check: chain 2's export step writes a group's `materials`
    as a COUNT; only the metadata pass turns it into a list. A kit whose
    metadata pass never ran (or failed) raised TypeError and stopped the
    whole mesh import -- no converted mesh of ANY pack was imported."""
    from asset_service import meshes_import as mi
    try:
        rec = mi._finish_recipe(mi._kb3d_recipe(
            {"materials": 3, "texture_files": ["M_FX_basecolor.png"]},
            Path("fx_kit")))
    except TypeError as exc:
        raise AssertionError(f"kit group with materials=3 crashed: {exc}")
    assert rec["slots"] == [], rec


def test_scan_rows_of_a_later_converted_pack_are_removed():
    """Release check: ingest #1 scans a model folder holding a RAW pack
    (a vendor FBX inside it becomes a scan row); after the user converts
    the pack, the scanner skips it -- but the old scan row survived every
    rebuild, so the mesh showed up twice (one copy without its recipe)."""
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        models = tmp / "models"
        raw = models / "FX_Pack" / "Source"
        raw.mkdir(parents=True)
        (raw / "fx_crate.fbx").write_bytes(_min_fbx())
        (models / "loose.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                                          encoding="utf-8")
        db = tmp / "reg.sqlite"
        scan = lambda: subprocess.run(                          # noqa: E731
            [sys.executable, "-B",
             str(REPO / "service" / "asset_service" / "scanner.py"),
             str(models), "--db", str(db)],
            capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert scan().returncode == 0
        ex = models / "FX_Pack" / "Exports"
        ex.mkdir()
        (ex / "manifest.json").write_text(
            '{"schema": "pharos.pack.export/v2", "meshes": []}',
            encoding="utf-8")
        r = scan()
        conn = sqlite3.connect(str(db))
        names = sorted(n for (n,) in conn.execute(
            "SELECT name FROM meshes WHERE source='scan'"))
        conn.close()
        assert r.returncode == 0 and names == ["loose"], \
            f"scan rows after the pack was converted: {names}"


def test_tex_index_is_library_neutral():
    """Release check: the texture index wrote the AUTHOR's folder layout
    ("4K_Textures_Gumroad holds the bulk ...") into every user's library,
    sending their agent to a folder that does not exist."""
    src = (REPO / "pipeline" / "agent_index" / "gen_tex_index.py").read_text(
        encoding="utf-8")
    assert "Gumroad" not in src and "earlier downsize" not in src, \
        "gen_tex_index.py still ships the author's library layout"


def test_purchase_csv_headers_people_type():
    """Release check: the setup question asks for a purchase CSV with
    'Name/URL/Price columns', but the importer read only the exact
    spellings 'Name', 'Product URL', 'Price (USD)' -- a CSV made as told
    imported with no links and no prices (lowercase: no names either)."""
    import sqlite3
    from asset_service import collection_import as ci, config
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csvp = tmp / "purchases.csv"
        csvp.write_text("name,url,price\nFX Barrel Pack,https://example.com/b,"
                        "9.99\n", encoding="utf-8")
        saved = config.AVAILABILITY_JSONL
        try:
            config.AVAILABILITY_JSONL = tmp / "no_availability.jsonl"
            ci.import_collection(tmp / "reg.sqlite", csvp)
        finally:
            config.AVAILABILITY_JSONL = saved
        conn = sqlite3.connect(str(tmp / "reg.sqlite"))
        rows = conn.execute("SELECT name, url, price FROM collection").fetchall()
        conn.close()
        assert rows == [("FX Barrel Pack", "https://example.com/b", "9.99")], \
            f"imported {rows}"


def test_chain3_does_not_reindex_what_ingest_covers():
    """Release check (stranger E2E): chain 3 re-enumerated folders the
    scanner already indexes and the kit-export FBX that kb3d_models.jsonl
    already covers (a kit listed 3x), and its driver's second pass wrote
    .blend objects into the file promote_native publishes wholesale."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        for rel in ("Scanned/a.fbx", "Blends/b.fbx", "Blends/x.blend",
                    "Kits/K1/Exports/FBX/g.fbx", "Deep/Cat/P1/Exports/FBX/m.fbx"):
            (lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (lib / rel).write_bytes(b"stub")
        (lib / "Kits/K1/Exports/kit_manifest.json").write_text("{}")
        (lib / "Deep/Cat/P1/Exports/manifest.json").write_text("{}")
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps({"library_root": str(lib),
                                   "scan_folders": ["Scanned"]}),
                       encoding="utf-8")
        native = _native_copy(tmp / "copy") / "pipeline" / "native"
        r = subprocess.run(
            [sys.executable, "-B", str(native / "make_native_manifest2.py")],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "PHAROS_CONFIG": str(cfg),
                 "PHAROS_LIBRARY_ROOT": str(lib)})
        assert r.returncode == 0, (r.stderr or "")[-300:]
        man = json.loads((native / "native_manifest.json").read_text(
            encoding="utf-8"))
        got = sorted(Path(i["rel"]).as_posix() for i in man["importable"])
        assert got == ["Blends/b.fbx"], f"chain-3 worklist: {got}"
    drv = (REPO / "pipeline" / "native" / "index_native_all.sh").read_text(
        encoding="utf-8")
    assert "AMNATIVE_SECTION" not in drv, \
        "index_native_all.sh still writes .blend objects into the " \
        "promoted native index (run_blends.py owns that pass)"


def test_kit_index_covers_every_kit_root():
    """Release check: kits in TWO parent folders -- init keeps one
    kitbash_root, ingest records both as manifest roots, but the kit index
    only globbed kitbash_root: the second folder's kits had recipes and
    no mesh rows, silently."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        for parent, kit in (("KitsA", "FX_KitA"), ("KitsB", "FX_KitB")):
            ex = lib / parent / kit / "Exports"
            (ex / "FBX").mkdir(parents=True)
            (ex / "FBX" / "g.fbx").write_bytes(b"Kaydara FBX Binary  \x00")
            (ex / "kit_manifest.json").write_text(json.dumps({
                "schema": "pharos.kb3d.export/v1", "kit": kit,
                "groups": [{"group": f"{kit}_g", "fbx": "FBX/g.fbx",
                            "triangles": 10, "vertices": 5, "submeshes": 1,
                            "materials": ["M_1"], "texture_count": 0}]}),
                encoding="utf-8")
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps({
            "library_root": str(lib), "kitbash_root": str(lib / "KitsA"),
            "manifest_roots": [str(lib / "KitsA"), str(lib / "KitsB")]}),
            encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PHAROS_KB3D_ROOT"}
        r = subprocess.run(
            [sys.executable, "-B",
             str(REPO / "pipeline" / "kitbash" / "build_kb3d_index.py")],
            capture_output=True, text=True, timeout=300,
            env={**env, "PHAROS_CONFIG": str(cfg),
                 "PHAROS_LIBRARY_ROOT": str(lib)})
        out = lib / "_Agent_Files" / "kb3d_models.jsonl"
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8")
                .splitlines() if x.strip()] if out.is_file() else []
        packs = sorted({x.get("pack") for x in rows})
        assert r.returncode == 0 and packs == ["FX_KitA", "FX_KitB"], \
            f"kit index packs {packs} rc={r.returncode} " \
            f"{(r.stderr or r.stdout)[-160:]}"


# ---- RELEASE: R5 R6 (PIPE lane: UE verifier metrics, Blender finder) ----
# (PIPE lane inserts its tests directly below this line)

def test_verifier_derives_static_metrics_from_fbx():
    """R5: UE 5.5 has no StaticMesh.get_num_vertices, so the exporter
    wrote vertices: null for EVERY static mesh and the verifier failed
    the whole pack (relink exit 7) although every FBX was fine. A null
    static-mesh count is derived from the exported FBX (same rule as
    SkeletalMesh triangles); an FBX that does not parse still FAILS."""
    with tempfile.TemporaryDirectory() as tmp:
        exports = Path(tmp) / "SmPack" / "Exports"
        (exports / "FBX").mkdir(parents=True)
        (exports / "FBX" / "a.fbx").write_bytes(_min_fbx())
        (exports / "FBX" / "b.fbx").write_bytes(_min_fbx())
        man = {
            "schema": "pharos.pack.export/v2", "pack": "SmPack",
            "meshes": [
                {"name": "a", "kind": "StaticMesh", "fbx": "FBX/a.fbx",
                 "dimension_method": "engine",
                 "triangles": 1, "vertices": None,
                 "bbox_m": [1.0, 1.0, 0.5], "materials": []},
                {"name": "b", "kind": "StaticMesh", "fbx": "FBX/b.fbx",
                 "dimension_method": "engine",
                 "triangles": None, "vertices": None,
                 "bbox_m": [1.0, 1.0, 0.5], "materials": []},
            ],
            "textures": [],
            "counts": {"static_mesh": 2, "skeletal_mesh": 0,
                       "fbx_written": 2, "texture_files_written": 0,
                       "failures": 0},
            "wiring": {"method": "ue-param", "slots_total": 0,
                       "slots_resolved": 0, "slots_unresolved": 0},
        }
        (exports / "manifest.json").write_text(json.dumps(man),
                                               encoding="utf-8")
        r = _verify(exports)
        out = r.stdout or ""
        assert r.returncode == 0 and "null metrics=0" in out, \
            f"null static-mesh counts not derived from the FBX: " \
            f"rc={r.returncode} {out[-300:]}"
        # an FBX that does not parse leaves nothing to derive: still FAIL
        (exports / "FBX" / "b.fbx").write_bytes(_poison_fbx())
        r2 = _verify(exports)
        assert r2.returncode != 0 and "null metrics=2" in (r2.stdout or ""), \
            (r2.stdout or "")[-300:]


def test_export_pack_warns_on_missing_metric_method():
    """R5: static_mesh_metrics skipped a missing engine method SILENTLY,
    so a pack full of null vertex counts carried no hint why. The
    exporter only runs inside Unreal: test the function in isolation."""
    import ast
    src = (REPO / "pipeline" / "conversion" / "export_pack.py").read_text(
        encoding="utf-8")
    fn = next((n for n in ast.parse(src).body
               if isinstance(n, ast.FunctionDef)
               and n.name == "static_mesh_metrics"), None)
    assert fn is not None, "static_mesh_metrics not found in export_pack.py"
    warned = []
    ns = {"log_warn": warned.append}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 "export_pack.py", "exec"), ns)

    class _Mesh:       # UE 5.5 shape: triangle count yes, vertex method gone
        def get_name(self):
            return "SM_Fixture"

        def get_num_triangles(self, lod):
            return 12

        def get_num_lods(self):
            return 1

    out = ns["static_mesh_metrics"](_Mesh())
    assert out["triangles"] == 12 and out["vertices"] is None, out
    assert any("get_num_vertices" in w for w in warned), \
        f"missing engine method skipped silently (warnings: {warned})"


def _pipeline_config(name: str):
    import importlib.util as ilu
    spec = ilu.spec_from_file_location(name, REPO / "pipeline" / "_config.py")
    cfg = ilu.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def test_blender_exe_finder():
    """R6: pipeline scripts defaulted to exactly `Blender 5.1` and died
    on any other install. One finder: BLENDER_EXE (only when it is a
    file) > PATH > highest-versioned Program Files install (5.10 beats
    5.9) > macOS app bundle > ""."""
    cfg = _pipeline_config("pipeline_cfg_r6")
    assert hasattr(cfg, "blender_exe"), "pipeline/_config.py has no blender_exe()"
    assert hasattr(cfg, "BLENDER_WIN_ROOT") and hasattr(cfg, "BLENDER_MAC_APP"), \
        "install locations are not module constants"
    saved = {k: os.environ.get(k) for k in ("BLENDER_EXE", "PATH")}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            exe = tmp / "my-blender.exe"
            exe.write_bytes(b"")
            os.environ["BLENDER_EXE"] = str(exe)
            assert cfg.blender_exe() == str(exe), "an existing BLENDER_EXE must win"
            # below: nothing on PATH, a fake Program Files tree, no mac app
            (tmp / "empty").mkdir()
            os.environ["PATH"] = str(tmp / "empty")
            cfg.BLENDER_WIN_ROOT = str(tmp / "BF")
            cfg.BLENDER_MAC_APP = str(tmp / "no.app" / "Blender")
            os.environ["BLENDER_EXE"] = str(tmp / "gone.exe")
            got = cfg.blender_exe()
            assert got == "", f"a missing BLENDER_EXE must be ignored, got {got!r}"
            for ver in ("4.2", "5.9", "5.10"):
                d = tmp / "BF" / f"Blender {ver}"
                d.mkdir(parents=True)
                (d / "blender.exe").write_bytes(b"")
            got = cfg.blender_exe()
            assert got and Path(got).parent.name == "Blender 5.10", \
                f"picked {got!r}, want the highest version (5.10)"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


_BLENDER_SCRIPTS = ("pipeline/kitbash/export_all_kb3d.py",
                    "pipeline/kitbash/export_kb3d_rest.py",
                    "pipeline/kitbash/run_kb3d_metadata.py",
                    "pipeline/native/run_blends.py")


def test_pipeline_scripts_use_blender_finder():
    """R6: no pipeline script pins a versioned Blender install path; with
    no Blender found each Python driver stops with ONE line naming
    BLENDER_EXE (not a FileNotFoundError traceback). The drivers run from
    a temp copy, so no real kit or worklist can ever be touched."""
    import re
    pin = re.compile(r"Blender Foundation[\\/]+Blender \d")
    for f in sorted((REPO / "pipeline").rglob("*")):
        if f.suffix not in (".py", ".sh") or "__pycache__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]    # comments/examples may name one
            assert not pin.search(code), \
                f"{f.relative_to(REPO).as_posix()}:{n} pins a Blender install path"
    for rel in _BLENDER_SCRIPTS + ("pipeline/native/index_native_all.sh",
                                    "pipeline/conversion/convert_packs.sh"):
        assert "blender_exe" in (REPO / rel).read_text(encoding="utf-8"), \
            f"{rel} does not use _config.blender_exe()"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "pipeline").mkdir()
        shutil.copy2(REPO / "pipeline" / "_config.py", tmp / "pipeline")
        for rel in _BLENDER_SCRIPTS:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, tmp / rel)
        (tmp / "kits").mkdir()
        (tmp / "empty").mkdir()
        env = {**os.environ, "PATH": str(tmp / "empty"),
               "PHAROS_CONFIG": str(tmp / "missing.json"),
               "PHAROS_KB3D_ROOT": str(tmp / "kits")}
        env.pop("BLENDER_EXE", None)
        pre = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
               "import _config; _config.BLENDER_WIN_ROOT = sys.argv[3]; "
               "_config.BLENDER_MAC_APP = sys.argv[3]; "
               "runpy.run_path(sys.argv[2], run_name='__main__')")
        for rel in _BLENDER_SCRIPTS:
            r = subprocess.run(
                [sys.executable, "-B", "-c", pre, str(tmp / "pipeline"),
                 str(tmp / rel), str(tmp / "empty")],
                capture_output=True, text=True, cwd=str(tmp), timeout=120,
                env=env)
            said = (r.stdout or "") + (r.stderr or "")
            assert r.returncode != 0 and "BLENDER_EXE" in said \
                and "Traceback" not in said, \
                f"{rel}: rc={r.returncode} {said[-200:]}"


# ---- RELEASE: R4 C12 (ANIM lane: animation section, doctor Blender) ----
# (ANIM lane inserts its tests directly below this line)

def test_anim_counts_follow_configured_section():
    """R4: the animation section folder is configurable, but doctor, the
    agent docs and MCP counted only 'pack::Animation/%': a library whose
    clips live in e.g. 'Mocap' read 0 clips and doctor never said READY.
    All counts must use the configured section, and agree with the API."""
    import sqlite3
    from asset_service import agent_docs, browse, config, db, doctor
    assert hasattr(db, "anim_clip_count"), "no shared db.anim_clip_count"
    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "a.sqlite"
        conn = db.init_db(dbp)
        packs = {"pack::Mocap/Walks": ("Mocap/Walks/w1.fbx",
                                       "Mocap/Walks/w2.BVH",
                                       "Mocap/Walks/notes.txt"),
                 "pack::Mocap": ("Mocap/loose.fbx",),
                 "pack::Props/Crates": ("Props/Crates/crate.fbx",)}
        for pid, files in packs.items():
            conn.execute(
                "INSERT INTO assets (id, name, canonical_root, "
                "hero_file_path, domain, sub_category, content_hash) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, pid[6:], td, files[0], "Props", "Misc", pid))
            conn.executemany(
                "INSERT INTO asset_files (asset_id, relative_path, file_type)"
                " VALUES (?,?,?)", [(pid, f, "anim") for f in files])
        conn.execute("CREATE TABLE meshes (pack TEXT, max_dim REAL)")
        conn.executemany("INSERT INTO meshes VALUES (?,?)",
                         [("P", None), ("P", 0), ("P", 1.5)])
        conn.commit()
        try:
            assert db.anim_clip_count(conn, "Mocap") == 3, \
                db.anim_clip_count(conn, "Mocap")
            assert db.anim_clip_count(conn, "Mocap", packs=True) == 2
            assert db.anim_clip_count(conn, "Animation") == 0
        finally:
            conn.close()
        old_sec, old_db = config.SECTIONS.get("animation"), browse._DB_PATH
        config.SECTIONS["animation"] = "Mocap"
        browse._DB_PATH = str(dbp)
        try:
            counts, _ = doctor._section_counts(str(dbp))
            assert counts.get("anim_packs") == 2, \
                f"doctor anim_packs={counts.get('anim_packs')} (want 2)"
            c = agent_docs._counts(str(dbp))
            api = browse.api_anim_clips({})["total"]
            assert c["clips"] == api == 3, f"docs={c['clips']} api={api}"
            assert c.get("unmeasured") == 2, \
                f"docs unmeasured={c.get('unmeasured')} (want 2)"
            try:
                import importlib.util as ilu
                spec = ilu.spec_from_file_location(
                    "pharos_mcp_anim", str(REPO / "service" /
                                           "pharos_mcp_server.py"))
                srv = ilu.module_from_spec(spec)
                spec.loader.exec_module(srv)
            except Exception as exc:               # noqa: BLE001
                assert os.environ.get("PHAROS_REQUIRE_MCP") != "1", \
                    f"PHAROS_REQUIRE_MCP=1 but MCP did not load: {exc}"
                srv = None
            if srv is not None:
                mc = sqlite3.connect(str(dbp))
                try:
                    n = srv._animation_clip_count(mc)
                finally:
                    mc.close()
                assert n == 3, f"MCP animation_clips={n} (want 3)"
        finally:
            config.SECTIONS["animation"] = old_sec
            browse._DB_PATH = old_db


def test_no_hardcoded_animation_section():
    """R4 source guard: 'Animation' is only the DEFAULT section name; no
    count query and no served rig/clip path may spell it out."""
    import re
    svc = REPO / "service"
    for rel in ("asset_service/doctor.py", "asset_service/agent_docs.py",
                "pharos_mcp_server.py", "asset_service/browse.py"):
        src = (svc / rel).read_text(encoding="utf-8")
        assert "pack::Animation/" not in src, \
            f"{rel} still hard-codes the pack::Animation/ prefix"
    src = (svc / "asset_service" / "browse.py").read_text(encoding="utf-8")
    hits = re.findall(r"""['"]Animation/""", src)
    assert not hits, f"browse.py: {len(hits)} literal 'Animation/...' paths"


def test_rig_path_follows_section_and_exists():
    """R4: the retarget rig is one library's own asset, hard-coded under
    'Animation/': every other library 404'd on it and /batchrender said
    'DONE -- 0 rendered, 0 failed'. The rig path now derives from the
    configured section, is used only when the file exists, reaches the
    pages from the server, and a batch run with no lane counts failures."""
    from asset_service import browse
    assert hasattr(browse, "_rig_rel") and hasattr(browse, "_with_rig"), \
        "no rig-path resolver / page filler"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert browse._rig_rel("Mocap", root) == "", "absent rig not ''"
        rig = root / "Mocap" / browse._RIG_SUBPATH
        rig.parent.mkdir(parents=True)
        rig.write_bytes(b"x")
        assert browse._rig_rel("Mocap", root) == \
            "Mocap/" + browse._RIG_SUBPATH
        assert browse._rig_rel("Animation", root) == ""
    orig = browse._rig_rel
    try:
        browse._rig_rel = lambda *a, **k: ""
        for name in ("VIEWER_PAGE", "ANIM_PAGE", "GLTEST_PAGE", "BATCH_PAGE"):
            page = getattr(browse, name)
            assert "{{RIG_REL}}" in page, f"{name}: rig not server-filled"
            assert "{{RIG_REL}}" not in browse._with_rig(page), name
        assert 'DUMMY=""' in browse._with_rig(browse.BATCH_PAGE)
        browse._rig_rel = lambda *a, **k: 'M"x</script>'
        assert '"M\\"x\\u003c/script>"' in browse._with_rig(
            browse.BATCH_PAGE), "rig path not escaped for a JS string"
    finally:
        browse._rig_rel = orig
    assert "if(!lanes.length)" in browse.BATCH_PAGE, \
        "/batchrender with no render lane must count every clip as failed"


def test_doctor_honours_blender_exe():
    """C12: doctor's docstring says Blender is found via BLENDER_EXE first,
    but _find_blender() ignored it. An existing file wins; a set-but-
    missing path is ignored (falls through to PATH / install dirs). The
    install-dir pick is version-aware (lexical sort chose 5.9 over 5.10)."""
    from asset_service import doctor
    saved = {k: os.environ.get(k) for k in ("BLENDER_EXE", "PATH")}
    old_root = getattr(doctor, "BLENDER_WIN_ROOT", None)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        exe = tmp / "blender-custom.exe"
        exe.write_bytes(b"")
        missing = str(tmp / "missing" / "blender.exe")
        try:
            os.environ["BLENDER_EXE"] = str(exe)
            got = doctor._find_blender()
            assert got == str(exe), f"BLENDER_EXE ignored: got {got!r}"
            os.environ["BLENDER_EXE"] = missing
            assert doctor._find_blender() != missing, \
                "a set-but-missing BLENDER_EXE must be ignored"
            assert old_root is not None, "no BLENDER_WIN_ROOT constant"
            (tmp / "empty").mkdir()
            os.environ["PATH"] = str(tmp / "empty")   # nothing on PATH
            doctor.BLENDER_WIN_ROOT = str(tmp / "BF")
            for ver in ("4.2", "5.9", "5.10"):
                d = tmp / "BF" / f"Blender {ver}"
                d.mkdir(parents=True)
                (d / "blender.exe").write_bytes(b"")
            got = doctor._find_blender()
            assert got and Path(got).parent.name == "Blender 5.10", \
                f"picked {got!r}, want the highest version (5.10)"
        finally:
            if old_root is not None:
                doctor.BLENDER_WIN_ROOT = old_root
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


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
