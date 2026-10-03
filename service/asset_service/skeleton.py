"""Which skeleton does a clip or a character use -- stdlib only.

A clip plays on a character only when both have the same bones, and one
library can hold clips on several unrelated skeletons (an engine
mannequin, a Character-Creator body, Mixamo ...). This module reads the
bone NAMES straight from the files and compares them: same names = same
skeleton. No registry, no config: paths in, plain data out.

Format notes (verified on 1,900+ clips and 20 character files, binary FBX
7200-7700): every object name is stored uncompressed as
"<name>\\x00\\x01<class>" in a string property, followed by a string
property with the node type ("LimbNode", "Mesh" ...). Scanning for that
byte pattern needs no record walk, so the record header width (32-bit
below 7500, 64-bit from 7500 on) does not matter. The length field in
front of the name is checked: a damaged name is skipped, not returned.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path

_BINARY = b"Kaydara FBX Binary"
_MODEL = b"\x00\x01ModelS"     # end of a Model's name + tag of the type string
_JOINT = ("LimbNode", "Limb", "Root")       # "Limb" is the FBX 6.x spelling
_ASCII_MODEL = re.compile(
    rb'Model:\s*(?:-?\d+\s*,\s*)?"Model::([^"]+)"\s*,\s*"(\w+)"')
_BVH_JOINT = re.compile(rb"^[ \t]*(?:ROOT|JOINT)[ \t]+(\S+)", re.M | re.I)

# (family, name prefix, exact bone names): the first row hit by at least
# _FAMILY_HITS bones names the family. Four, because a prop or an animal
# can own a lone "pelvis" too.
_FAMILIES = (
    ("cc", "cc_base_", ()),
    ("mixamo", "mixamorig", ()),
    ("ue-mannequin", None,
     ("pelvis", "spine_01", "clavicle_l", "thigh_l", "calf_l")),
)
_FAMILY_HITS = 4
# overlap from which a body counts as fitting a clip. 0.7, not higher: a
# lean and an extended variant of ONE skeleton (68 vs 91 bones, the extra
# ones twist/IK helpers) share their whole core and still score 0.75-0.8
_MATCH = 0.7


def _binary_models(data: bytes) -> list[tuple[str, str]]:
    """(name, type) of every Model node in a binary FBX, in file order."""
    out = []
    hit = data.find(_MODEL)
    while hit != -1:
        # a name holds no NUL, so the NUL in front of it is the top byte
        # of its own length field: 'S' <u32 len> <name> NUL 0x01 "Model"
        nul = data.rfind(b"\x00", max(0, hit - 300), hit)
        tlen = hit + len(_MODEL)
        if (nul >= 4 and hit - nul > 1 and tlen + 4 <= len(data)
                and data[nul - 4:nul - 3] == b"S"
                and struct.unpack_from("<I", data, nul - 3)[0]
                == hit - nul + 6):
            n = struct.unpack_from("<I", data, tlen)[0]
            out.append((data[nul + 1:hit].decode("utf-8", "replace"),
                        data[tlen + 4:tlen + 4 + min(n, 16)]
                        .decode("latin-1")))
        hit = data.find(_MODEL, hit + 1)
    return out


def bone_names(path, limit_bytes: int | None = None) -> list[str]:
    """Skeleton node names of an FBX (binary or ASCII) or BVH file, in
    file order, without repeats. [] for anything unreadable; never raises.

    Only Model nodes typed LimbNode or Root count: a character file lists
    its mesh, camera and light objects as Model nodes too, and a mesh name
    is not a bone. Null nodes count ONLY when the file has no LimbNode or
    Root at all -- some exporters write a whole skeleton as plain helper
    nodes, but next to real joints a Null is the armature container or a
    placeholder named after the asset, and counting it would give two
    exports of one skeleton different signatures.

    limit_bytes caps how much of the file is read (the nodes sit behind
    any geometry, so a cap can cut the list short); None reads it all.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(-1 if limit_bytes is None else limit_bytes)
        if str(path).lower().endswith(".bvh"):
            # joints sit in the HIERARCHY block; the MOTION block behind
            # it is megabytes of numbers
            return list(dict.fromkeys(
                n.decode("utf-8", "replace") for n in
                _BVH_JOINT.findall(data.split(b"MOTION", 1)[0])))
        if data.startswith(_BINARY):
            nodes = _binary_models(data)
        else:
            nodes = [(n.decode("utf-8", "replace"), t.decode("latin-1"))
                     for n, t in _ASCII_MODEL.findall(data)]
        names = ([n for n, t in nodes if t in _JOINT]
                 or [n for n, t in nodes if t == "Null"])
        return list(dict.fromkeys(names))
    except Exception:       # a library file can be anything: [] = unknown
        return []


def _name_set(names) -> set[str]:
    return {str(n).lower() for n in names}


def signature(names) -> str:
    """Short stable id of a skeleton: the same set of bone names (any
    order, any letter case) always gives the same 12 hex digits."""
    blob = "\n".join(sorted(_name_set(names))).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def family(names) -> str:
    """Rig family by its marker bones (see _FAMILIES): "cc", "mixamo",
    "ue-mannequin" or "unknown"."""
    low = _name_set(names)
    for fam, prefix, exact in _FAMILIES:
        hits = sum(1 for n in low
                   if n in exact or (prefix and n.startswith(prefix)))
        if hits >= _FAMILY_HITS:
            return fam
    return "unknown"


def overlap(a, b) -> float:
    """Share (0..1) of the SMALLER name set found in the other, ignoring
    letter case. The smaller set, because one skeleton often ships lean
    and extended (extra twist or IK bones) and both still fit each other."""
    a, b = _name_set(a), _name_set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def describe(path) -> dict | None:
    """{"bones": n, "family": ..., "signature": ...}; None when the file
    holds no readable skeleton."""
    names = bone_names(path)
    return {"bones": len(names), "family": family(names),
            "signature": signature(names)} if names else None


def _names(path, cache) -> list[str]:
    """bone_names through the optional cache. Size and mtime are part of
    the key, so a changed file is read again instead of answered stale."""
    if cache is None:
        return bone_names(path)
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return []
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in cache:
        cache[key] = bone_names(path)
    return cache[key]


def _subfolder(folder, clip) -> str:
    """First folder level below `folder` on the way to the clip; "" when
    the clip sits directly in it (or not below it at all)."""
    try:
        parts = Path(clip).relative_to(folder).parts
    except ValueError:
        return ""
    return parts[0] if len(parts) > 1 else ""


def pack_skeletons(clips_by_folder, bodies, cache=None) -> dict:
    """Skeleton(s) of each animation folder and the characters they fit.

    clips_by_folder: {folder path: [clip paths below it]}; bodies: paths
    of candidate character files. Returns {folder: [record, ...]}, one
    record per distinct skeleton, [] when no clip is readable:
        {"family", "bones", "signature",
         "subfolders": direct sub-folders it was found in ("" = the
                       folder itself),
         "clips": clip count of those (counted, not each one opened),
         "bodies": the given bodies sharing >= 70 % of the bone names}

    At most two clips (first and last by name) are opened per direct
    sub-folder: packs keep one skeleton per sub-folder in practice, so
    two variants in two sub-folders show up as two records while a
    2,000-clip library costs a few dozen file reads.

    cache: optional dict the caller may persist; filled and used here,
    keyed (path, size, mtime_ns) -> list of bone names.
    """
    body_names = {str(b): _names(b, cache) for b in bodies}
    out = {}
    for folder, clips in clips_by_folder.items():
        by_sub: dict[str, list[str]] = {}
        # sub-folder looked up once per directory, not per clip: pathlib
        # on every clip was 5/6 of a cached run over 1,900 clips
        sub_of: dict[str, str] = {}
        for clip in sorted(str(c) for c in clips):
            d = os.path.dirname(clip)
            if d not in sub_of:
                sub_of[d] = _subfolder(folder, clip)
            by_sub.setdefault(sub_of[d], []).append(clip)
        records: dict[str, dict] = {}
        for sub in sorted(by_sub):
            group = by_sub[sub]
            for clip in dict.fromkeys((group[0], group[-1])):
                names = _names(clip, cache)
                if not names:
                    continue
                sig = signature(names)
                if sig not in records:
                    records[sig] = {
                        "family": family(names), "bones": len(names),
                        "signature": sig, "subfolders": [], "clips": 0,
                        # overlap alone lets a one-bone prop that owns a
                        # "root" match every rig with a "root": a body
                        # also needs a quarter of the clip's bone count
                        # (a quarter, so a finger-less lean rig still fits)
                        "bodies": [b for b, bn in body_names.items()
                                   if overlap(names, bn) >= _MATCH
                                   and 4 * len(bn) >= len(names)]}
                if sub not in records[sig]["subfolders"]:
                    records[sig]["subfolders"].append(sub)
                    records[sig]["clips"] += len(group)
        out[folder] = list(records.values())
    return out
