"""Tests for the binary .uasset parser using synthetic packages.

Fixtures mirror the real package format verified against CUE4Parse and live
marketplace files: summary with version-conditional fields, name map as
FString+hashes, and import/export table FNames as (nameIndex, number) pairs.
"""

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from asset_service.uasset_parser import (  # noqa: E402
    UassetParseError,
    parse_uasset_bytes,
)

TAG = 0x9E2A83C1


def fstring(s: str) -> bytes:
    raw = s.encode("ascii") + b"\x00"
    return struct.pack("<i", len(raw)) + raw


def fname(idx: int, number: int = 0) -> bytes:
    return struct.pack("<ii", idx, number)


def engine_version_bytes(major=5, minor=4, patch=4) -> bytes:
    return (
        struct.pack("<HHH", major, minor, patch)
        + struct.pack("<I", 12345)
        + fstring("++release")
    )


def build_package(
    *,
    legacy: int,
    version_ue4: int,
    version_ue5: int = 0,
    licensee: int = 0,
    names,
    imports,
    first_export=None,
    package_flags: int = 0,
    layout_versions: tuple = None,
) -> bytes:
    """Assemble a minimal valid package.

    `imports` entries: (class_package_idx, class_name_idx, outer, object_name_idx)
    `first_export`: (class_index, object_name_idx)
    `layout_versions` overrides which version numbers drive the layout
    conditionals (use for unversioned files: fields written as 0, layout as
    the version the saving editor would have used).
    """

    if layout_versions is not None:
        layout_ue4, layout_ue5 = layout_versions
    else:
        layout_ue4, layout_ue5 = version_ue4, version_ue5

    names_blob = b"".join(fstring(n) + b"\x01\x00\x02\x00" for n in names)

    has_pkg_name = layout_ue4 >= 520 and not (package_flags & 0x80000000)
    has_optional = layout_ue5 >= 1003
    imports_blob = b"".join(
        fname(cp) + fname(cn) + struct.pack("<i", outer) + fname(on)
        + (fname(0) if has_pkg_name else b"")
        + (struct.pack("<i", 0) if has_optional else b"")
        for (cp, cn, outer, on) in imports
    )
    has_template = layout_ue4 >= 508
    export_blob = b""
    if first_export is not None:
        ci, on_idx = first_export
        if has_template:
            export_blob = struct.pack("<iiii", ci, 0, 0, 0) + fname(on_idx)
        else:
            export_blob = struct.pack("<iii", ci, 0, 0) + fname(on_idx)

    def head(name_off: int, imp_off: int, exp_off: int) -> bytes:
        h = struct.pack("<I", TAG) + struct.pack("<i", legacy)
        h += struct.pack("<i", 864)                 # versionUE3 (legacy != -4)
        h += struct.pack("<i", version_ue4)
        if legacy <= -8:
            h += struct.pack("<i", version_ue5)
        h += struct.pack("<i", licensee)            # int32
        h += struct.pack("<i", 0)                   # custom versions: empty
        if layout_ue5 >= 1016:
            h += b"\x00" * 20 + struct.pack("<i", 0)  # saved hash + header size
        else:
            h += struct.pack("<i", 0)               # totalHeaderSize
        h += fstring("None")                        # folderName
        h += struct.pack("<I", package_flags)
        h += struct.pack("<i", len(names)) + struct.pack("<i", name_off)
        if layout_ue5 >= 1008:                      # soft object path list
            h += struct.pack("<ii", 0, 0)
        if not (package_flags & 0x80000000) and layout_ue4 >= 516:
            h += fstring("")                        # localizationId
        if layout_ue4 >= 459:                       # gatherable text data
            h += struct.pack("<ii", 0, 0)
        h += struct.pack("<i", 1 if export_blob else 0) + struct.pack("<i", exp_off)
        h += struct.pack("<i", len(imports)) + struct.pack("<i", imp_off)
        if layout_ue5 >= 1015:                      # verse cells
            h += struct.pack("<iiii", 0, 0, 0, 0)
        if layout_ue5 >= 1014:                      # metadata offset
            h += struct.pack("<i", 0)
        h += struct.pack("<i", 0)                   # dependsOffset
        if layout_ue4 >= 384:                       # soft package references
            h += struct.pack("<ii", 0, 0)
        if layout_ue4 >= 510:                       # searchable names
            h += struct.pack("<i", 0)
        h += struct.pack("<i", 0)                   # thumbnailTableOffset
        if layout_ue5 >= 1018:                      # import type hierarchies
            h += struct.pack("<ii", 0, 0)
        if layout_ue5 < 1016:
            h += b"\x01" * 16                       # package guid
        if not (package_flags & 0x80000000):
            if layout_ue4 >= 518:
                h += b"\x02" * 16                   # persistentGuid
            if 518 <= layout_ue4 < 520:
                h += b"\x03" * 16                   # ownerPersistentGuid (4.26 only)
        h += struct.pack("<i", 1) + struct.pack("<ii", 1, len(names))  # generations
        h += engine_version_bytes() + engine_version_bytes()
        return h

    head_len = len(head(0, 0, 0))
    name_off = head_len
    imp_off = name_off + len(names_blob)
    exp_off = imp_off + len(imports_blob)
    return head(name_off, imp_off, exp_off) + names_blob + imports_blob + export_blob


NAMES_A = ["/Script/CoreUObject", "Class", "/Script/Engine", "StaticMesh",
           "SM_Rock", "MaterialInstanceConstant", "M_Rock"]
IMPORTS_A = [(0, 1, 0, 3), (2, 5, 0, 6)]  # /Script.CoreUObject Class StaticMesh ; /Script.Engine MaterialInstanceConstant M_Rock


def test_ue426_staticmesh():
    data = build_package(
        legacy=-7, version_ue4=518,
        names=NAMES_A, imports=IMPORTS_A, first_export=(-1, 4),
    )
    s = parse_uasset_bytes(data, "SM_Rock.uasset")
    assert s.asset_class == "StaticMesh"
    assert s.asset_class_source == "first_export"
    assert s.engine_version == "5.4.4"
    assert s.primary_export_name == "SM_Rock"
    assert "MaterialInstanceConstant" in s.import_classes
    assert s.version_ue5 is None and s.legacy_version == -7


def test_ue51_animsequence():
    names = ["/Script/CoreUObject", "Class", "AnimSequence", "AS_Walk_Fwd",
             "Skeleton", "SK_Mannequin", "/Game/Chars"]
    imports = [(0, 1, 0, 2), (6, 4, 0, 5)]
    data = build_package(
        legacy=-8, version_ue4=522, version_ue5=1008,
        names=names, imports=imports, first_export=(-1, 3),
    )
    s = parse_uasset_bytes(data)
    assert s.asset_class == "AnimSequence"
    assert s.asset_class_source == "first_export"
    assert s.engine_version == "5.4.4"
    assert s.version_ue5 == 1008
    assert s.primary_export_name == "AS_Walk_Fwd"


def test_unversioned_ue5_package():
    # all version fields zero: parser must assume candidate versions and
    # still resolve the class (builder lays the file out as 522/1008)
    names = ["/Script/CoreUObject", "Class", "StaticMesh", "SM_Chair"]
    imports = [(0, 1, 0, 2)]
    data = build_package(
        legacy=-8, version_ue4=0, version_ue5=0, licensee=0,
        names=names, imports=imports, first_export=(-1, 3),
        layout_versions=(522, 1008),
    )
    s = parse_uasset_bytes(data)
    assert s.unversioned is True
    assert s.asset_class == "StaticMesh"


def test_import_heuristic_fallback():
    # class_index == 0 (no class ref) -> heuristic scan of import candidates
    data = build_package(
        legacy=-7, version_ue4=518,
        names=NAMES_A, imports=IMPORTS_A, first_export=(0, 3),
    )
    s = parse_uasset_bytes(data)
    assert s.asset_class == "StaticMesh"
    assert s.asset_class_source == "import_heuristic"


def test_rejects_non_packages():
    for junk in (b"JUNKJUNKJUNK", b"", struct.pack("<I", 0xC1832A9E) + b"\x00" * 64):
        try:
            parse_uasset_bytes(junk)
        except UassetParseError:
            continue
        raise AssertionError(f"expected UassetParseError for {junk[:8]!r}")


def test_rejects_old_legacy():
    data = build_package(
        legacy=-3, version_ue4=500, names=["X"], imports=[(0, 0, 0, 0)],
    )
    try:
        parse_uasset_bytes(data)
    except UassetParseError:
        return
    raise AssertionError("expected legacy -3 to be rejected")


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
