"""Leak scan: assert no personal data sits in TRACKED repo files.

Usage: python tools/leak_scan.py   (exit 1 with hits, 0 when clean)
Run before publishing, zipping, or archiving anything from this tree.
Checks the STAGED/TRACKED view (what ships), not the working tree.
"""
import re
import subprocess
import sys

SEP = "[/\\\\]+"
PATTERNS = [
    "Nils", "Gallist", "kondi",
    "D:" + SEP + "3D_Assets",
    "D:" + SEP + "Pipeline" + SEP + "kiosk",
    "C:" + SEP + "Users" + SEP + "kondi",
    "E:" + SEP + "Epic",
    "KitbashOrdner", "Kiosk_zCode", "Mens_V1",
    # library-content markers: specific pack/vendor names from the owner's
    # library that leaked into tracked sources once already. Specific
    # enough to never false-positive on generic prose.
    "Oriantel", "HorrorMansion", "Sonniss", "AlbertMansion",
]


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True,
        check=True).stdout.splitlines()   # split() breaks on filenames with spaces
    rx = re.compile("|".join(PATTERNS), re.I)
    hits = []
    for f in files:
        if f.replace("\\", "/") == "tools/leak_scan.py":
            continue          # this file legitimately contains the patterns
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:100]}")
    if hits:
        print("LEAKS FOUND in tracked files:")
        for h in hits[:50]:
            print(" ", h)
        return 1
    print(f"clean: {len(files)} tracked files, no personal data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
