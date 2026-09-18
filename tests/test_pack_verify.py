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


def test_drivers_print_sandbox_remediation():
    """A virgin machine without a sandbox must get the exact regeneration
    command, not a bare 'missing' error (the sandbox is generated locally
    on purpose, so the error message IS the setup documentation)."""
    # the test needs a machine WITHOUT a sandbox; a generated one may
    # legitimately exist in the repo tree -- move it aside, restore after
    # (same backup/restore pattern as the fresh-install suite's config)
    real_sandbox = REPO / "pipeline" / "conversion" / "sandbox"
    parked = None
    if real_sandbox.is_dir():
        parked = real_sandbox.with_name("sandbox.parked-for-test")
        real_sandbox.rename(parked)
    try:
        for driver in ("convert_packs.sh", "relink_pack.sh"):
            r = subprocess.run(
                ["bash", str(REPO / "pipeline" / "conversion" / driver),
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
