#!/usr/bin/env python3
"""Rapid SHA-256 hasher for library files.

Full hash by default; --partial hashes the first N KiB for fast dedup
pre-sorting (final dedup always uses full hashes).

Example:
    python tools/hash_files.py "X:/assets/pack/model.fbx"
    python tools/hash_files.py --partial 1024 file1.fbx file2.fbx
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def hash_file(path: Path, partial_kib: int | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        if partial_kib:
            h.update(fh.read(partial_kib * 1024))
        else:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    prefix = "partial:" if partial_kib else "sha256:"
    return prefix + h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--partial", type=int, metavar="KIB",
                        help="hash only the first KIB KiB")
    args = parser.parse_args()

    failures = 0
    for name in args.files:
        path = Path(name)
        try:
            print(f"{hash_file(path, args.partial)}  {path}")
        except OSError as exc:
            failures += 1
            print(f"ERROR {exc}  {path}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
