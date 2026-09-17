"""Lightweight binary parser for Unreal Engine .uasset/.umap package headers.

Reads the PackageFileSummary, name map, import table and the first export
entry directly from disk to recover the primary AssetClass (StaticMesh,
AnimSequence, MaterialInstanceConstant, ...) WITHOUT Unreal Engine running.
Read-only: files are opened 'rb' and never modified.

Format verified against Epic's serialization as implemented by CUE4Parse
(FPackageFileSummary / ObjectResource / FAssetArchive.ReadFName) and against
real marketplace packages (UE 4.25 - 5.x). Key facts:

  * Summary head: tag, legacyVersion, [versionUE3 if legacy != -4],
    versionUE4, [versionUE5 if legacy <= -8], licenseeVersion (int32),
    custom versions (count-based container for legacy <= -6).
  * Table FNames (imports/exports) are (nameIndex, number) index PAIRS
    referencing the name map -- never inline strings in package files.
  * FObjectImport gains a PackageName pair at VER_UE4_NON_OUTER_PACKAGE_IMPORT
    (520, saved unfiltered) and a 4-byte ImportOptional bool at
    VER_UE5_OPTIONAL_RESOURCES (1003).
  * The name map is FString entries + 2x uint16 hashes from
    VER_UE4_NAME_HASHES_SERIALIZED (504, UE 4.23+).
  * Several summary fields (SoftObjectPaths 1008, LocalizationId 516,
    PersistentGuid 518, VERSE_CELLS 1015, PACKAGE_SAVED_HASH 1016, ...) are
    version/flag-conditional; PKG_FilterEditorOnly (0x80000000) removes
    LocalizationId and PersistentGuid.
  * Unversioned packages (all three version fields zero) are decoded with
    candidate version sets and validated against the table offsets.

Supported: legacy -6..-8 (UE 4.20 through UE 5.x). Older/newer or damaged
packages raise UassetParseError or degrade to warnings, never crash a scan.

CLI:
    python uasset_parser.py FILE [FILE ...]     # prints JSON summaries
"""

from __future__ import annotations

import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

PACKAGE_FILE_TAG = 0x9E2A83C1
PACKAGE_FILE_TAG_SWAPPED = 0xC1832A9E
PKG_FilterEditorOnly = 0x80000000

# --- version thresholds (EUnrealEngineObjectUE4Version / UE5Version) -------
VER_UE4_ADD_STRING_ASSET_REFERENCES_MAP = 384
VER_UE4_SERIALIZE_TEXT_IN_PACKAGES = 459
VER_UE4_NAME_HASHES_SERIALIZED = 504
VER_UE4_PRELOAD_DEPENDENCIES_IN_COOKED_EXPORTS = 507
VER_UE4_TEMPLATEINDEX_IN_COOKED_EXPORTS = 508
VER_UE4_ADD_SEARCHABLE_NAMES = 510
VER_UE4_ADDED_PACKAGE_SUMMARY_LOCALIZATION_ID = 516
VER_UE4_ADDED_PACKAGE_OWNER = 518
VER_UE4_NON_OUTER_PACKAGE_IMPORT = 520
VER_UE5_INITIAL_VERSION = 1000
VER_UE5_OPTIONAL_RESOURCES = 1003
VER_UE5_ADD_SOFTOBJECTPATH_LIST = 1008
VER_UE5_METADATA_SERIALIZATION_OFFSET = 1014
VER_UE5_VERSE_CELLS = 1015
VER_UE5_PACKAGE_SAVED_HASH = 1016
VER_UE5_IMPORT_TYPE_HIERARCHIES = 1018

MAX_READ_AHEAD = 32 * 1024 * 1024
MAX_NAME_COUNT = 2_000_000
MAX_TABLE_COUNT = 200_000

# Fallback priority when the first-export class index is undecodable: lower
# wins. A package's *own* class beats classes it merely references (a
# StaticMesh pack imports MaterialInstanceConstant, an AnimSequence pack
# imports Skeleton, ...).
_CLASS_PRIORITY = {
    "AnimSequence": 0, "AnimMontage": 1, "AnimComposite": 2,
    "BlendSpace": 3, "BlendSpace1D": 4,
    "SkeletalMesh": 5, "StaticMesh": 6,
    "PhysicsAsset": 7, "Skeleton": 8,
    "MaterialInstanceConstant": 9, "Material": 10, "MaterialFunction": 11,
    "TextureCube": 12, "Texture2D": 13,
    "Blueprint": 14, "WidgetBlueprint": 15, "LevelSequence": 16,
    "SoundWave": 17, "SoundCue": 18,
    "World": 19, "Level": 20,
}


class UassetParseError(Exception):
    """Hard failure: the file is not a supported Unreal package."""


@dataclass
class UassetSummary:
    file_name: str
    file_size: int
    legacy_version: Optional[int] = None
    version_ue4: Optional[int] = None
    version_ue5: Optional[int] = None
    engine_version: Optional[str] = None      # e.g. "5.4.4" (best-effort)
    unversioned: bool = False
    package_flags: Optional[int] = None
    total_header_size: Optional[int] = None
    name_count: int = 0
    import_count: int = 0
    export_count: int = 0
    primary_export_name: Optional[str] = None
    asset_class: Optional[str] = None         # StaticMesh, AnimSequence, ...
    asset_class_source: str = "unknown"       # first_export | import_heuristic | unknown
    import_classes: list[str] = field(default_factory=list)
    names_sample: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class _Reader:
    """Little-endian primitive reader with strict bounds checking."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, n: int) -> bytes:
        end = self.pos + n
        if n < 0 or end > len(self.data):
            raise UassetParseError("unexpected end of header data")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def bool32(self) -> bool:
        return self.i32() != 0

    def fstring(self) -> str:
        """Unreal FString: i32 length incl. null; negative length = UTF-16LE."""
        n = self.i32()
        if n == 0:
            return ""
        if abs(n) > 65536:
            raise UassetParseError(f"implausible FString length {n}")
        if n > 0:
            raw = self.read(n)
            if raw[-1:] != b"\x00":
                raise UassetParseError("FString missing null terminator")
            return raw[:-1].decode("latin-1", "replace")
        raw = self.read(-n * 2)
        if raw[-2:] != b"\x00\x00":
            raise UassetParseError("FString missing null terminator")
        return raw[:-2].decode("utf-16-le", "replace")

    def fname(self) -> tuple[int, int]:
        """FName in package tables: (nameMapIndex, number) index pair."""
        return self.i32(), self.i32()


def _printable(s: str) -> bool:
    return bool(s) and all(31 < ord(c) < 127 for c in s)


def _read_engine_version(r: _Reader) -> str:
    major, minor, patch = struct.unpack("<HHH", r.read(6))
    changelist = r.u32()
    branch = r.fstring()
    if not (3 <= major <= 6 and minor <= 50 and patch <= 100 and len(branch) <= 64):
        raise UassetParseError(f"implausible engine version {major}.{minor}.{patch}")
    return f"{major}.{minor}.{patch}"


class _Tables:
    """Parsed name map + import table + first export head."""

    __slots__ = ("names", "imports", "export_class_index", "export_name_idx")

    def __init__(self):
        self.names: list[str] = []
        self.imports: list[tuple[int, int, int, int]] = []  # (cp, cn, outer, on) name indices
        self.export_class_index: Optional[int] = None
        self.export_name_idx: Optional[int] = None


def _read_names(data: bytes, offset: int, count: int, with_hashes: bool) -> list[str]:
    r = _Reader(data, offset)
    names: list[str] = []
    for _ in range(count):
        name = r.fstring()
        if not _printable(name):
            raise UassetParseError(f"garbage in name map: {name!r:.60}")
        names.append(name)
        if with_hashes:
            r.read(4)
    return names


def _read_imports(
    r: _Reader, count: int, name_count: int,
    has_package_name: bool, has_import_optional: bool,
) -> list[tuple[int, int, int, int]]:
    imports: list[tuple[int, int, int, int]] = []
    for _ in range(count):
        cp, _ = r.fname()
        cn, _ = r.fname()
        r.i32()  # outerIndex
        on, _ = r.fname()
        if has_package_name:
            r.fname()
        if has_import_optional:
            r.bool32()
        for idx in (cp, cn, on):
            if not 0 <= idx < name_count:
                raise UassetParseError(f"import name index {idx} outside name map ({name_count})")
        imports.append((cp, cn, 0, on))
    return imports


def _read_first_export_head(
    r: _Reader, name_count: int, has_template_index: bool
) -> tuple[int, int]:
    class_index = r.i32()
    r.i32()  # superIndex
    if has_template_index:
        r.i32()  # templateIndex
    r.i32()  # outerIndex
    name_idx, _ = r.fname()
    if not 0 <= name_idx < name_count:
        raise UassetParseError(f"export name index {name_idx} outside name map ({name_count})")
    return class_index, name_idx


@dataclass
class _Head:
    """Everything needed to locate the tables, plus version info."""
    legacy: int
    version_ue4: int
    version_ue5: int
    licensee: int
    unversioned: bool
    engine_version: Optional[str]
    package_flags: int
    total_header_size: int
    name_count: int
    name_offset: int
    export_count: int
    export_offset: int
    import_count: int
    import_offset: int


def _parse_head(data: bytes, assume_ue5: int) -> _Head:
    """Parse the summary through importOffset (tables located by offset)."""
    r = _Reader(data)
    tag = r.u32()
    if tag == PACKAGE_FILE_TAG_SWAPPED:
        raise UassetParseError("byte-swapped (console) package, not supported")
    if tag != PACKAGE_FILE_TAG:
        raise UassetParseError(f"not an Unreal package (tag=0x{tag:08X})")
    legacy = r.i32()
    if not -9 <= legacy <= -6:
        raise UassetParseError(
            f"unsupported package legacy version {legacy} (supported: -6..-8)")
    version_ue3 = 864
    if legacy != -4:
        version_ue3 = r.i32()  # legacyUE3Version
    version_ue4 = r.i32()
    version_ue5 = r.i32() if legacy <= -8 else 0
    licensee = r.i32()

    unversioned = version_ue4 == 0 and version_ue5 == 0 and licensee == 0
    if unversioned:
        # cooked/unversioned save: decode against candidate current versions
        version_ue4 = 522
        version_ue5 = assume_ue5 if legacy <= -8 else 0

    # custom versions: count-based container for everything we support
    custom_count = r.i32()
    if not 0 <= custom_count <= 4096:
        raise UassetParseError(f"implausible custom version count {custom_count}")
    for _ in range(custom_count):
        r.read(16)
        r.i32()

    if version_ue5 >= VER_UE5_PACKAGE_SAVED_HASH:
        r.read(20)  # FIoHash PackageSavedHash
        total_header_size = r.i32()
    else:
        total_header_size = r.i32()

    r.fstring()  # folderName
    package_flags = r.u32()

    name_count = r.i32()
    name_offset = r.i32()
    if version_ue5 >= VER_UE5_ADD_SOFTOBJECTPATH_LIST:
        r.i32(); r.i32()  # softObjectPathsCount/Offset
    filtered = bool(package_flags & PKG_FilterEditorOnly)
    if not filtered and version_ue4 >= VER_UE4_ADDED_PACKAGE_SUMMARY_LOCALIZATION_ID:
        r.fstring()  # localizationId
    if version_ue4 >= VER_UE4_SERIALIZE_TEXT_IN_PACKAGES:
        r.i32(); r.i32()  # gatherableTextDataCount/Offset

    export_count = r.i32()
    export_offset = r.i32()
    import_count = r.i32()
    import_offset = r.i32()

    if not (0 <= name_count <= MAX_NAME_COUNT
            and 0 <= import_count <= MAX_TABLE_COUNT
            and 0 <= export_count <= MAX_TABLE_COUNT):
        raise UassetParseError(
            f"implausible table counts (names={name_count}, "
            f"imports={import_count}, exports={export_count})")
    if name_offset > len(data) or import_offset > len(data) or (
        export_count and export_offset > len(data
    )):
        raise UassetParseError("table offset beyond header read-ahead")

    # ---- decorative tail: engine version (never let it fail the parse) ----
    engine_version: Optional[str] = None
    try:
        if version_ue5 >= VER_UE5_VERSE_CELLS:
            for _ in range(2):
                r.i32(); r.i32()  # cellExports/imports
        if version_ue5 >= VER_UE5_METADATA_SERIALIZATION_OFFSET:
            r.i32()  # metaDataOffset
        r.i32()  # dependsOffset
        if version_ue4 >= VER_UE4_ADD_STRING_ASSET_REFERENCES_MAP:
            r.i32(); r.i32()
        if version_ue4 >= VER_UE4_ADD_SEARCHABLE_NAMES:
            r.i32()
        r.i32()  # thumbnailTableOffset
        if version_ue5 >= VER_UE5_IMPORT_TYPE_HIERARCHIES:
            r.i32(); r.i32()
        if version_ue5 < VER_UE5_PACKAGE_SAVED_HASH:
            r.read(16)  # package guid
        if not filtered:
            if version_ue4 >= VER_UE4_ADDED_PACKAGE_OWNER:
                r.read(16)  # persistentGuid
            # owner persistent guid existed only for exactly 518
            if VER_UE4_ADDED_PACKAGE_OWNER <= version_ue4 < VER_UE4_NON_OUTER_PACKAGE_IMPORT:
                r.read(16)
        generations = r.i32()
        if not 0 <= generations <= 1024:
            raise UassetParseError("implausible generation count")
        r.read(8 * generations)
        engine_version = _read_engine_version(r)
        _read_engine_version(r)  # compatibleWithEngineVersion
    except (UassetParseError, struct.error):
        engine_version = None

    return _Head(
        legacy=legacy, version_ue4=version_ue4, version_ue5=version_ue5,
        licensee=licensee, unversioned=unversioned, engine_version=engine_version,
        package_flags=package_flags, total_header_size=total_header_size,
        name_count=name_count, name_offset=name_offset,
        export_count=export_count, export_offset=export_offset,
        import_count=import_count, import_offset=import_offset,
    )


def _parse_tables(data: bytes, head: _Head, warnings: list[str]) -> _Tables:
    tables = _Tables()

    # name map: strings, with 2x uint16 hashes from UE 4.23 (self-correcting)
    guess_hashes = (
        head.version_ue5 > 0
        or head.version_ue4 >= VER_UE4_NAME_HASHES_SERIALIZED
    )
    names = None
    for with_hashes in (guess_hashes, not guess_hashes):
        try:
            names = _read_names(data, head.name_offset, head.name_count, with_hashes)
            break
        except UassetParseError:
            continue
    if names is None:
        raise UassetParseError("name map unreadable (hash layout unknown)")
    tables.names = names

    filtered = bool(head.package_flags & PKG_FilterEditorOnly)
    has_package_name = (
        head.version_ue4 >= VER_UE4_NON_OUTER_PACKAGE_IMPORT and not filtered
    )
    has_import_optional = head.version_ue5 >= VER_UE5_OPTIONAL_RESOURCES

    try:
        tables.imports = _read_imports(
            _Reader(data, head.import_offset), head.import_count,
            head.name_count, has_package_name, has_import_optional,
        )
    except UassetParseError as exc:
        warnings.append(f"import table unreadable: {exc}")

    if head.export_count:
        try:
            tables.export_class_index, tables.export_name_idx = _read_first_export_head(
                _Reader(data, head.export_offset), head.name_count,
                head.version_ue4 >= VER_UE4_TEMPLATEINDEX_IN_COOKED_EXPORTS,
            )
        except UassetParseError as exc:
            warnings.append(f"first export unreadable: {exc}")
    return tables


def _resolve(head: _Head, tables: _Tables, summary: UassetSummary) -> None:
    def candidate(imp: tuple[int, int, int, int]) -> Optional[str]:
        cp, cn, _, on = imp
        class_name = tables.names[cn] if cn < len(tables.names) else ""
        object_name = tables.names[on] if on < len(tables.names) else ""
        # native UClass imports: ClassName="Class", real class in ObjectName
        return object_name if class_name == "Class" else class_name

    summary.import_classes = sorted(set(
        c for c in (candidate(i) for i in tables.imports) if c
    ))[:30]

    if tables.export_name_idx is not None and tables.export_name_idx < len(tables.names):
        summary.primary_export_name = tables.names[tables.export_name_idx]

    ci = tables.export_class_index
    first_export_class: Optional[str] = None
    if ci is not None and ci < 0:
        idx = -ci - 1
        if idx < len(tables.imports):
            first_export_class = candidate(tables.imports[idx]) or None
    if ci is not None and ci > 0:
        summary.warnings.append("first export class is an export reference (e.g. Blueprint subobject)")

    # The first export is usually the main asset, but subobjects (BodySetup,
    # NavCollision, thumbnails, import data) can be serialized first -- only
    # trust it directly for known primary classes.
    if first_export_class and first_export_class in _CLASS_PRIORITY:
        summary.asset_class = first_export_class
        summary.asset_class_source = "first_export"
        return

    known = [c for c in summary.import_classes if c in _CLASS_PRIORITY]
    if known:
        summary.asset_class = min(known, key=_CLASS_PRIORITY.get)
        summary.asset_class_source = "import_heuristic"
        summary.warnings.append(
            f"class from import heuristic (first export was {first_export_class or 'unresolvable'})")
    elif first_export_class:
        # unknown-to-us class from the first export (e.g.
        # MaterialParameterCollection) -- still the most trustworthy signal
        summary.asset_class = first_export_class
        summary.asset_class_source = "first_export"


def parse_uasset_bytes(data: bytes, file_name: str = "<bytes>") -> UassetSummary:
    # unversioned files need version candidates validated end-to-end
    for assume_ue5 in (VER_UE5_ADD_SOFTOBJECTPATH_LIST, VER_UE5_INITIAL_VERSION):
        head = _parse_head(data, assume_ue5)
        if not head.unversioned:
            break
        probe = UassetSummary(file_name=file_name, file_size=len(data))
        try:
            _parse_tables(data, head, probe.warnings)
            break
        except UassetParseError:
            continue

    summary = UassetSummary(
        file_name=file_name, file_size=len(data),
        legacy_version=head.legacy, version_ue4=head.version_ue4,
        version_ue5=head.version_ue5 or None, unversioned=head.unversioned,
        engine_version=head.engine_version, package_flags=head.package_flags,
        total_header_size=head.total_header_size,
        name_count=head.name_count, import_count=head.import_count,
        export_count=head.export_count,
    )
    if head.unversioned:
        summary.warnings.append("unversioned package; layout assumed from candidate versions")
    tables = _parse_tables(data, head, summary.warnings)
    summary.names_sample = tables.names[:512]
    _resolve(head, tables, summary)
    return summary


def parse_uasset(source: Union[str, Path, bytes, bytearray]) -> UassetSummary:
    """Parse a .uasset/.umap from a path or raw bytes. Raises
    UassetParseError for anything that is not a supported package."""
    if isinstance(source, (bytes, bytearray)):
        return parse_uasset_bytes(bytes(source))
    path = Path(source)
    file_size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(MAX_READ_AHEAD)
    summary = parse_uasset_bytes(data, file_name=path.name)
    summary.file_size = file_size
    if len(data) < file_size:
        summary.warnings.append(
            f"header read-ahead truncated at {MAX_READ_AHEAD // 1024 // 1024} MiB")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: uasset_parser.py FILE [FILE ...]", file=sys.stderr)
        return 2
    failures = 0
    for arg in argv:
        try:
            print(json.dumps(parse_uasset(arg).to_dict(), ensure_ascii=False, indent=2))
        except (OSError, UassetParseError) as exc:
            failures += 1
            print(json.dumps({"file": arg, "error": str(exc)}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
