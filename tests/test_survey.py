"""The survey: every folder kind a library can hold, classified by what we
KNOW (structure) before what we GUESS (file shares), with the evidence.

Fails on the 0.10 detector, which looked at the root's children only,
let any mesh file win, and read a static export's animation markers as
clips. Run standalone: python -B tests/test_survey.py
"""

import json
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from asset_service import survey  # noqa: E402


def _png(w=4, h=4) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00" * 4)


def _hdr(w=1024, h=512) -> bytes:
    return (b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n"
            + f"-Y {h} +X {w}\n".encode() + b"\x00" * 16)


def _fbx(*markers: bytes) -> bytes:
    """A fake binary FBX: the header plus the type names the survey samples."""
    return b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400) \
        + b"...".join(markers) + b"\x00" * 64


STATIC = _fbx(b"Geometry", b"AnimationStack", b"Takes")       # a UE export
CLIP = _fbx(b"AnimationStack", b"AnimationCurveNode", b"LimbNode")
BODY = _fbx(b"Geometry", b"LimbNode", b"Deformer", b"Takes")   # rigged, still


def _files(folder: Path, spec: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in spec.items():
        (folder / name).write_bytes(data if isinstance(data, bytes) else b"x")


def build_library(root: Path) -> None:
    """One folder of every kind, in the shapes real stores deliver."""
    # a Unity project with engine folders
    _files(root / "Unity" / "MyGame" / "Assets" / "Props", {"crate.fbx": STATIC,
                                                            "crate.prefab": b"",
                                                            "crate.fbx.meta": b""})
    _files(root / "Unity" / "MyGame" / "ProjectSettings", {"ProjectVersion.txt": b""})
    _files(root / "Unity" / "MyGame" / "Library", {"cache.bin": b""})
    # a raw Unreal pack (Content/ with .uasset) and a converted one
    _files(root / "UE" / "RawPack" / "Content" / "Meshes", {"SM_A.uasset": b"", "SM_B.uasset": b""})
    _files(root / "UE" / "Converted" / "Content" / "Meshes", {"SM_C.uasset": b""})
    _files(root / "UE" / "Converted" / "Exports" / "FBX", {"SM_C.fbx": STATIC})
    (root / "UE" / "Converted" / "Exports" / "manifest.json").write_text(
        json.dumps({"schema": "pharos.pack.export/v2", "meshes": []}), encoding="utf-8")
    # a KitBash3D kit (new layout) and an exported one
    _files(root / "Kits" / "Alpha" / "kb3d_Alpha.blender.native", {"kb3d_Alpha.blend": b""})
    _files(root / "Kits" / "Beta" / "Exports", {"kit_manifest.json": b"{}"})
    (root / "Kits" / "Beta" / "Beta.blend").write_bytes(b"")
    # a Blender library (a .blend plus textures) and a store of loose FBX products
    _files(root / "Blend" / "Office", {"office.blend": b"", "office.blend1": b""})
    _files(root / "Blend" / "Office" / "textures", {"wall.png": _png(), "floor.png": _png()})
    _files(root / "Store" / "Rocks" / "Rocks_FBX", {f"rock_{i}.fbx": STATIC for i in range(5)})
    _files(root / "Store" / "Rocks" / "Rocks_Textures", {f"rock_{i}_albedo.png": _png() for i in range(5)})
    _files(root / "Store" / "Lamp", {"lamp.obj": b"", "lamp.mtl": b"", "preview.jpg": b"\xff\xd8"})
    # a product whose folder holds preview images, models in subfolders
    _files(root / "Store" / "Mech", {f"shot_{i}.jpg": b"\xff\xd8" for i in range(6)})
    _files(root / "Store" / "Mech" / "Fbx", {f"part_{i}.fbx": STATIC for i in range(8)})
    _files(root / "Store" / "Mech" / "Obj", {f"part_{i}.obj": b"" for i in range(8)})
    # clothing: one model folder plus per-item texture folders
    _files(root / "Clothes" / "OBJ", {f"item_{i}.obj": b"" for i in range(4)})
    for i in range(4):
        _files(root / "Clothes" / f"Item_{i}", {"d.jpg": b"\xff\xd8", "n.png": _png()})
    # a texture library: categories of PBR sets, a .blend preview per set,
    # a few USD material files, and an HDRI folder inside it
    for cat in ("Wood", "Stone"):
        for s in range(3):
            _files(root / "Textures" / cat / f"{cat}{s:02d}", {
                f"{cat}{s:02d}_albedo.png": _png(), f"{cat}{s:02d}_normal.png": _png(),
                f"{cat}{s:02d}_roughness.png": _png(), f"{cat}{s:02d}.blend": b""})
    _files(root / "Textures", {"loose_albedo.png": _png(), "loose_normal.png": _png(),
                               "material.usda": b"", "material.usdc": b""})
    _files(root / "Textures" / "HDRI", {f"sky_{i}.hdr": _hdr() for i in range(3)})
    # audio categories
    for cat in ("Rain", "Wind"):
        _files(root / "Audio" / cat, {f"{cat}_{i}.wav": b"RIFF" for i in range(5)})
    # animation: a clip pack, a nested clip pack, a body folder, a bvh folder
    _files(root / "Anim" / "Walks", {f"walk_{i}.fbx": CLIP for i in range(6)})
    _files(root / "Anim" / "Climb" / "UN5" / "Root", {f"climb_{i}.fbx": CLIP for i in range(4)})
    _files(root / "Anim" / "Climb" / "UN5" / "InPlace", {f"climb_ip_{i}.fbx": CLIP for i in range(4)})
    _files(root / "Anim" / "Body", {"dummy.fbx": BODY, "dummy.png": _png()})
    _files(root / "Anim" / "Mocap", {f"take_{i}.bvh": b"HIERARCHY" for i in range(3)})
    # purchases: a catalogue CSV with store-image galleries
    _files(root / "Purchases", {"orders.csv": b"Name,Product URL,Price (USD),Local Folder\nX,https://e/x,1,Purchases/X\n"})
    _files(root / "Purchases" / "X", {"01.jpg": b"\xff\xd8", "02.jpg": b"\xff\xd8"})
    # archives not unpacked, a code folder, hidden/underscore folders, root files
    _files(root / "Downloads" / "Set A", {"set_a.zip": b"PK"})
    _files(root / "Downloads" / "Set B", {"set_b.rar": b"Rar!"})
    _files(root / "Tools", {"run.py": b"", "notes.txt": b""})
    _files(root / "_Agent_Files", {"AGENT_START_HERE.md": b""})
    _files(root / ".cache", {"x.fbx": STATIC})
    (root / "stray.fbx").write_bytes(STATIC)


def _by_folder(result: dict) -> dict:
    return {n["folder"]: n for n in result["nodes"]}


def test_every_kind_is_recognised_with_its_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Lib"
        build_library(root)
        res = survey.survey(root)
        by = _by_folder(res)
        want = {
            "Unity/MyGame": ("unity", "know", "project"),
            "UE/RawPack": ("unreal", "know", "raw"),
            "UE/Converted": ("unreal", "know", "converted"),
            "Kits/Alpha": ("blender", "know", "kit"),
            "Kits/Beta": ("blender", "know", "kit-exported"),
            "Blend/Office": ("blender", "guess", "files"),
            "Store/Rocks": ("3d-assets", "guess", ""),
            "Store/Lamp": ("3d-assets", "guess", ""),
            "Store/Mech": ("3d-assets", "guess", ""),
            "Store": ("3d-assets", "guess", ""),
            "Clothes": ("3d-assets", "guess", ""),
            "Textures": ("textures", "guess", ""),
            "Textures/HDRI": ("hdri", "guess", ""),
            "Audio": ("audio", "guess", ""),
            "Anim": ("animation", "guess", ""),
            "Anim/Walks": ("animation", "guess", ""),
            "Anim/Climb": ("animation", "guess", ""),
            "Anim/Body": ("3d-assets", "guess", ""),
            "Anim/Mocap": ("animation", "guess", ""),
            "Purchases": ("purchases", "know", ""),
            "Downloads": ("archive", "guess", ""),
            "Tools": ("other", "none", ""),
        }
        for folder, (kind, conf, state) in want.items():
            n = by.get(folder)
            assert n, f"{folder}: no node ({sorted(by)})"
            assert (n["kind"], n["confidence"], n.get("state", "")) == (kind, conf, state), \
                f"{folder}: {n['kind']}/{n['confidence']}/{n.get('state', '')} " \
                f"want {kind}/{conf}/{state} -- {n['evidence']}"
            assert n["evidence"], f"{folder}: no evidence"
        # what the walk must NOT list
        assert "Textures/Wood" not in by and "Textures/Wood/Wood00" not in by, \
            "texture categories and sets are the importer's business"
        assert "Audio/Rain" not in by, "audio categories collapsed into the library"
        assert "Unity/MyGame/Assets" not in by, "a project is one node"
        assert "_Agent_Files" not in by and ".cache" not in by
        # engine folders inside the Unity project are marked to be ignored
        assert by["Unity/MyGame/Library"]["kind"] == "ignore"
        # a root file is reported, never silently dropped
        assert res["root_files"] == {".fbx": 1}, res.get("root_files")
        # decisions: one per kind that needs the user before anything slow runs
        dec = {d["folder"]: d["options"] for d in res["decisions"]}
        assert dec["UE/RawPack"] == ["export", "leave"]
        assert dec["Kits/Alpha"] == ["extract", "leave"]
        assert dec["Blend/Office"] == ["extract", "leave"]
        assert dec["Unity/MyGame"] == ["index-files", "leave"]
        assert dec["Anim/Walks"] == ["previews", "no-previews"]
        assert "UE/Converted" not in dec and "Store/Rocks" not in dec
        # evidence names the structure, not a guess
        assert "manifest.json" in by["UE/Converted"]["evidence"]
        assert "Content/" in by["UE/RawPack"]["evidence"]
        assert "previews" in by["Store/Mech"]["evidence"]


def test_a_lazy_root_and_a_shallow_depth_still_classify_right():
    """One parent folder for everything, surveyed two levels deep: clip
    packs stay animation (motion is sampled before the model share), and
    a product that is mostly Unreal content says so."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Everything"
        build_library(root)
        _files(root / "Store" / "MultiEngine" / "part1", {"kit.blend": b""})
        _files(root / "Store" / "MultiEngine" / "part2" / "ue", {f"a{i}.uasset": b"" for i in range(40)})
        _files(root / "Store" / "MultiEngine" / "part3", {f"t{i}.jpg": b"\xff\xd8" for i in range(30)})
        res = survey.survey(root, max_depth=2)
        by = _by_folder(res)
        assert by["Anim/Climb"]["kind"] == "animation", by["Anim/Climb"]
        assert by["Store/MultiEngine"]["kind"] == "unreal" \
            and by["Store/MultiEngine"]["state"] == "raw", by["Store/MultiEngine"]
        assert by["."]["kind"] == "other" and "mixed" in by["."]["evidence"]
        assert res["seconds"] < 30


def test_cli_prints_tree_and_json():
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Lib"
        build_library(root)
        r = subprocess.run([sys.executable, "-B", str(REPO / "pharos.py"), "survey",
                            str(root), "--json"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=str(REPO))
        assert r.returncode == 0, r.stderr[-400:]
        data = json.loads(r.stdout)
        assert data["files"] > 50 and data["nodes"][0]["folder"] == "."
        r = subprocess.run([sys.executable, "-B", str(REPO / "pharos.py"), "survey",
                            str(root)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=str(REPO))
        assert r.returncode == 0 and "= known from structure" in r.stdout, r.stdout[-300:]
        assert "[export|leave]" in r.stdout and "purchase catalogue" in r.stdout


def _map_env(tmp: Path):
    """Point the map, the config file and the library root at a temp
    library (nothing here reads or writes the real config); returns the
    restore callable."""
    from asset_service import config, library_map
    lib = Path(tmp) / "Lib"
    saved = (library_map.PATH, config.LIBRARY_ROOT, config._CONFIG_FILE)
    library_map.PATH = lib / "_Agent_Files" / "library_map.csv"
    config.LIBRARY_ROOT = lib
    config._CONFIG_FILE = Path(tmp) / "pharos_config.json"
    config.save({**config._DEFAULTS, "library_root": str(lib),
                 "registry_dir": str(Path(tmp) / "reg")})

    def restore():
        library_map.PATH, config.LIBRARY_ROOT, config._CONFIG_FILE = saved
    return lib, restore


def test_library_map_holds_kinds_decisions_and_tags():
    """The map is the contract: the survey fills it, the owner's rows are
    never changed, a folder below a folder of the same kind inherits, a
    pack_tags.csv (0.10) is carried over once, and the config mirrors
    the picks (heaviest folder per kind, no entry for a kind the library
    does not have)."""
    from asset_service import library_map as lm
    with tempfile.TemporaryDirectory() as tmp:
        lib, restore = _map_env(tmp)
        try:
            build_library(lib)
            (lib / "_Agent_Files").mkdir(exist_ok=True)
            # a 0.10 tag file: carried over, kinds filled by the survey
            (lib / "_Agent_Files" / "pack_tags.csv").write_text(
                "folder,name,photorealistic,stylized,low_poly,domain,tags\n"
                "Store/Rocks,Rocks,x,,,Environment,\"cliff, grey\"\n", encoding="utf-8")
            res = lm.refresh(lib)
            rows = lm.load()
            assert res["map_added"] > 10 and res["map_filled"] == 1, (res["map_added"], res["map_filled"])
            rocks = rows["store/rocks"]
            assert rocks["kind"] == "3d-assets" and rocks["styles"] == ["Photorealistic"] \
                and rocks["tags"] == ["cliff", "grey"] and rocks["domain"] == "Environment", rocks
            assert rows["ue/converted"]["confirmed"] and not rows["store/lamp"]["confirmed"]
            assert rows["unity/mygame/library"]["kind"] == "ignore" \
                and not rows["unity/mygame/library"]["include"]
            # decisions: only the folders that are the thing itself pend,
            # once per cluster; an answer on a parent covers its children
            pend = {r["folder"] for r in lm.pending(rows)}
            assert "UE/RawPack" in pend and "UE/Converted" not in pend, pend
            assert "Anim" in pend and "Anim/Walks" not in pend, pend
            assert "Kits/Alpha" in pend and "Kits/Beta" not in pend, pend
            lm.update("Anim", action="previews")
            lm.update("UE", action="export")           # the folder of packs
            rows = lm.load()
            pend = {r["folder"] for r in lm.pending(rows)}
            assert "Anim" not in pend and "UE/RawPack" not in pend, pend
            assert lm.effective_action(rows["ue/rawpack"], rows) == "export"
            assert lm.effective_action(rows["anim/walks"], rows) == "previews"
            # the owner's correction stands; a re-survey never overwrites it
            lm.update("Store/Lamp", kind="textures", confirmed=True)
            lm.refresh(lib)
            assert lm.load()["store/lamp"]["kind"] == "textures"
            # a decision word that does not fit is refused, nothing is stored
            try:
                lm.update("Audio", action="export")
                assert False, "accepted a decision for a kind that has none"
            except ValueError:
                pass
            assert lm.load()["audio"]["action"] == ""
            # the lookup: a file deep below a tagged folder finds that folder
            look = lm.Lookup(lm.load())
            assert look("Store/Rocks/Rocks_FBX/rock_1.fbx")["folder"] == "Store/Rocks"
            assert look.node("Textures/Wood/Wood00/x.png")["kind"] == "textures"
            # the config mirror
            cfg = {"library_root": str(lib), "sections": {}}
            lm.apply_to_config(lm.load(), cfg, lib, lm.weights_of(res))
            assert cfg["sections"] == {"animation": "Anim", "textures": "Textures",
                                       "audio": "Audio", "collection": "Purchases"}, cfg["sections"]
            assert set(cfg["scan_folders"]) >= {"Store", "Clothes"} \
                and "Blend" not in cfg["scan_folders"], cfg["scan_folders"]
            assert cfg["manifest_roots"] == [str(lib / "UE")], cfg["manifest_roots"]
            assert cfg["kitbash_root"] == str(lib / "Kits"), cfg.get("kitbash_root")
            assert {"Purchases", "Downloads", "Tools"} <= set(cfg["indexer_skip_dirs"]), cfg["indexer_skip_dirs"]
            assert cfg["collection_csv"] == "orders.csv"
            # a Unity project the owner wants indexed scans its Assets folder
            lm.update("Unity/MyGame", action="index-files")
            lm.apply_to_config(lm.load(), cfg, lib)
            assert "Unity/MyGame/Assets" in cfg["scan_folders"], cfg["scan_folders"]
            # several audio folders: the heaviest is the section
            (lib / "Audio2").mkdir()
            for i in range(30):
                (lib / "Audio2" / f"a{i}.wav").write_bytes(b"RIFF")
            res = lm.refresh(lib)
            lm.apply_to_config(lm.load(), cfg, lib, lm.weights_of(res))
            assert cfg["sections"]["audio"] == "Audio", "the folder a config already names stays"
            fresh = {"library_root": str(lib), "sections": {}}
            lm.apply_to_config(lm.load(), fresh, lib, lm.weights_of(res))
            assert fresh["sections"]["audio"] == "Audio2", fresh["sections"]
        finally:
            restore()


def test_engine_chains_run_only_on_the_maps_decisions():
    """convert / extract / previews refuse without the owner's decision and
    name the command that gives it; with the decision they run the
    documented scripts (dry-run shows them)."""
    import io
    from contextlib import redirect_stdout
    from asset_service import chains, library_map as lm
    with tempfile.TemporaryDirectory() as tmp:
        lib, restore = _map_env(tmp)
        try:
            build_library(lib)
            lm.refresh(lib)

            def run(fn, *argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = fn(list(argv))
                return rc, buf.getvalue()
            rc, out = run(chains.convert, "--dry-run")
            assert rc == 2 and 'map set "UE/RawPack" action=export' in out, out
            lm.update("UE/RawPack", action="export")
            rc, out = run(chains.convert, "--dry-run")
            assert rc == 0 and "convert_packs.sh" in out and "RawPack" in out \
                and "Converted" not in out.split("converting")[1], out
            rc, out = run(chains.extract, "--dry-run")
            assert rc == 2 and 'map set "Kits/Alpha" action=extract' in out, out
            lm.update("Kits", action="extract")
            rc, out = run(chains.extract, "--dry-run")
            assert rc == 0 and "export_all_kb3d.py" in out and "make_native_manifest2" not in out, out
            lm.update("Blend/Office", action="extract")
            rc, out = run(chains.extract, "--dry-run")
            assert "make_native_manifest2.py" in out and "promote_blends.py" in out, out
            rc, out = run(chains.previews, "--dry-run")
            assert rc == 2 and 'map set "Anim" action=previews' in out, out
            lm.update("Anim", action="previews")
            rc, out = run(chains.previews, "--dry-run")
            assert rc == 0 and "/batchrender" in out, out
        finally:
            restore()


def test_the_review_cases_of_the_map():
    """What a second look found: a marker in the root never swallows the
    library; include=no on a parent keeps its packs out; the survey moves
    a pack's state when it is converted; a wrong decision word changes
    nothing; the owner can set the state the survey could not see; owner
    text with quotes survives a long file; images beside engine packs are
    previews."""
    import csv
    from asset_service import library_map as lm, survey, config
    with tempfile.TemporaryDirectory() as tmp:
        lib, restore = _map_env(tmp)
        try:
            build_library(lib)
            # a purchase CSV and a Content/ folder dropped in the root
            (lib / "orders.csv").write_text(
                "Name,Product URL,Price (USD)\nA,https://e/a,1\n", encoding="utf-8")
            (lib / "Content").mkdir()
            (lib / "Content" / "SM_X.uasset").write_bytes(b"stub")
            res = survey.survey(lib)
            by = {n["folder"]: n for n in res["nodes"]}
            assert "." not in by or by["."]["kind"] not in ("purchases", "unreal"), by.get(".")
            assert {"Store", "Anim", "UE/RawPack", "Content"} <= set(by), sorted(by)
            # images beside two raw Unreal packs are previews of a folder of packs
            mega = lib / "Mega"
            (mega / "PackA" / "Content").mkdir(parents=True)
            (mega / "PackB" / "Content").mkdir(parents=True)
            for i in range(6):
                (mega / "PackA" / "Content" / f"a{i}.uasset").write_bytes(b"stub")
                (mega / "PackB" / "Content" / f"b{i}.uasset").write_bytes(b"stub")
            (mega / "screenshot.jpg").write_bytes(b"\xff\xd8")
            by = {n["folder"]: n for n in survey.survey(lib)["nodes"]}
            assert by["Mega"]["kind"] == "unreal" and by["Mega/PackA"]["state"] == "raw", \
                (by["Mega"], by.get("Mega/PackA"))
            # include=no on the store keeps its packs out of the scan list
            # and out of the questions
            lm.refresh(lib)
            lm.update("Store", include=False)
            cfg = {"library_root": str(lib), "sections": {}}
            lm.apply_to_config(lm.load(), cfg, lib)
            assert not [f for f in cfg["scan_folders"] if f.split("/")[0] == "Store"], cfg["scan_folders"]
            lm.update("UE", include=False)
            assert "UE/RawPack" not in {r["folder"] for r in lm.pending(lm.load())}
            lm.update("UE", include=True)
            # the pack gets converted: the survey moves raw -> converted,
            # its spent decision drops, the owner's tags stay
            lm.update("UE/RawPack", action="export", tags=["crates"])
            (lib / "UE" / "RawPack" / "Exports").mkdir()
            (lib / "UE" / "RawPack" / "Exports" / "manifest.json").write_text("{}", encoding="utf-8")
            lm.refresh(lib)
            row = lm.load()["ue/rawpack"]
            assert row["state"] == "converted" and row["action"] == "" \
                and row["tags"] == ["crates"], row
            lm.apply_to_config(lm.load(), cfg, lib)
            assert str(lib / "UE") in cfg["manifest_roots"], cfg["manifest_roots"]
            # the converted pack's Exports folder joins the scan list (its
            # exported FBX get measured; the importer parks those rows)
            assert "UE/RawPack/Exports" in cfg["scan_folders"], cfg["scan_folders"]
            # a decision word that does not fit is refused before the write
            lm.update("Kits", action="extract")
            try:
                lm.update("Kits", action="exctract")
                assert False, "accepted a wrong decision word"
            except ValueError as exc:
                assert "extract" in str(exc)
            assert lm.load()["kits"]["action"] == "extract"
            # a kind the owner corrects gets its state from the owner too
            lm.update("Store/Lamp", kind="blender", state="files", action="extract")
            lamp = lm.load()["store/lamp"]
            assert lamp["state"] == "files" and lamp["action"] == "extract", lamp
            try:
                lm.update("Store/Lamp", state="loose")
                assert False, "accepted an unknown state"
            except ValueError:
                pass
            # quotes in owner text, far past the sniffer's sample
            for i in range(80):
                lm.sync([(f"Filler/F{i:03d}", f"F{i:03d}")])
            lm.update("Store/Rocks", tags=['6" pipe', "steel"], note='he said "hi"; twice')
            assert lm.PATH.stat().st_size > 4096
            rocks = lm.load()["store/rocks"]
            assert rocks["tags"] == ['6" pipe', "steel"] and rocks["note"] == 'he said "hi"; twice', rocks
            # the CLI refuses a field it does not know
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lm.run_map(["set", "Store/Rocks", "colour=red"])
            assert rc == 2 and "unknown field" in buf.getvalue(), buf.getvalue()
        finally:
            restore()


def test_extract_hands_the_scripts_only_the_approved_folders():
    """A kit the owner leaves alone is never opened: the chain passes the
    approved folders to the pipeline scripts, and the scripts honour them."""
    import io
    import os
    from contextlib import redirect_stdout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
    import _config as pcfg
    from asset_service import chains, library_map as lm
    with tempfile.TemporaryDirectory() as tmp:
        lib, restore = _map_env(tmp)
        try:
            build_library(lib)
            # a second unexported kit the owner leaves alone
            (lib / "Kits" / "Gamma" / "kb3d_Gamma.blender.native").mkdir(parents=True)
            (lib / "Kits" / "Gamma" / "kb3d_Gamma.blender.native" / "kb3d_Gamma.blend").write_bytes(b"")
            lm.refresh(lib)
            lm.update("Kits/Alpha", action="extract")
            lm.update("Kits/Gamma", action="leave")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = chains.extract(["--dry-run"])
            out = buf.getvalue()
            assert rc == 0 and "PHAROS_MAP_FOLDERS=Kits/Alpha" in out and "Gamma" not in out, out
            old = os.environ.get("PHAROS_MAP_FOLDERS")
            os.environ["PHAROS_MAP_FOLDERS"] = "Kits/Alpha"
            try:
                allow = pcfg.map_folders()
                assert allow == {"kits/alpha"}, allow
                assert pcfg.in_map_folders(str(lib / "Kits" / "Alpha" / "kb3d_Alpha.blender.native"), allow, str(lib))
                assert not pcfg.in_map_folders(str(lib / "Kits" / "Gamma" / "kb3d_Gamma.blender.native"), allow, str(lib))
            finally:
                if old is None:
                    del os.environ["PHAROS_MAP_FOLDERS"]
                else:
                    os.environ["PHAROS_MAP_FOLDERS"] = old
        finally:
            restore()


def test_init_keeps_the_owners_previews_folder_for_the_same_library():
    """A re-survey of the same library (the setup page's 'Survey again')
    runs init --force: the previews folder the owner set stays; another
    library gets the default beside its registry."""
    from asset_service import config, init as pharos_init
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "Lib"
        lib.mkdir()
        old_cf = config._CONFIG_FILE
        config._CONFIG_FILE = Path(tmp) / "pharos_config.json"
        try:
            config.save({**config._DEFAULTS, "library_root": str(lib),
                         "registry_dir": str(Path(tmp) / "reg"),
                         "previews_dir": str(Path(tmp) / "my_previews")})
            report = {"root": str(lib), "sections": {}, "mesh_folders": [],
                      "catalog_csvs": [], "manifest_roots": [], "folder_counts": {}}
            cfg = pharos_init.build_config(report, Path(tmp) / "reg")
            assert cfg["previews_dir"] == str(Path(tmp) / "my_previews"), cfg["previews_dir"]
            other = Path(tmp) / "Other"
            other.mkdir()
            cfg = pharos_init.build_config({**report, "root": str(other)}, Path(tmp) / "reg2")
            assert cfg["previews_dir"] == str(Path(tmp) / "reg2" / "previews"), cfg["previews_dir"]
        finally:
            config._CONFIG_FILE = old_cf


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:       # noqa: BLE001
                failures += 1
                kind = "" if isinstance(exc, AssertionError) else f"{type(exc).__name__}: "
                print(f"FAIL {name}: {kind}{exc}")
    raise SystemExit(1 if failures else 0)
