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
