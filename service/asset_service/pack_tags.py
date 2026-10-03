"""Pack tags: the owner's own labels per pack, kept in one plain file.

    <library_root>/<agent_files>/pack_tags.csv

One row per pack folder. Columns:

    folder          the pack folder, relative to the library (the key)
    name            the pack's display name (written by Pharos, for reading)
    photorealistic  x when the pack looks photorealistic
    stylized        x when the pack looks stylized
    low_poly        x when the pack is low poly (combines with either look)
    domain          one of auto_classifier.DOMAINS, or empty
    tags            your own words, separated by commas

The file is the truth for these labels. It lives inside the library, so it
survives a registry rebuild and a move of the library to another drive.
Edit it on the dashboard's Tags page (/tags) or in any spreadsheet or text
editor (comma, semicolon or tab separated; x, 1, yes or a tick mark a box).
Pharos adds a row for every new pack and never removes one: a row whose
folder is gone stays, so a renamed folder loses nothing.

A model carries the tags of the deepest tagged pack folder that holds its
file. Model search uses the owner's tags only; the pack list falls back to
the classifier's guess while a pack has no tags yet.
"""

from __future__ import annotations

import csv
import io
import os
import threading
from pathlib import Path

from . import config
from . import textmatch as tm
from .auto_classifier import DOMAINS

PATH = config.AGENT_FILES / "pack_tags.csv"

# marker name -> CSV column
MARKERS = {"Photorealistic": "photorealistic", "Stylized": "stylized",
           "Low Poly": "low_poly"}
COLUMNS = ("folder", "name", "photorealistic", "stylized", "low_poly",
           "domain", "tags")
_YES = {"x", "1", "yes", "y", "true", "✓", "✔", "☑"}
_LOCK = threading.RLock()
_cache: dict = {"key": None, "rows": {}}


class TagFileBusy(OSError):
    """The file is open in another program (a spreadsheet locks it)."""


def _norm_folder(folder: str) -> str:
    return (folder or "").replace("\\", "/").strip().strip("/")


def marker(value: str) -> str:
    """'low poly', 'lowpoly', 'Low-Poly' -> 'Low Poly'; '' when unknown."""
    v = "".join(ch for ch in (value or "").lower() if ch.isalnum())
    for name in MARKERS:
        if v == name.lower().replace(" ", ""):
            return name
    return {"photoreal": "Photorealistic", "realistic": "Photorealistic",
            "stylised": "Stylized", "lowpolygon": "Low Poly"}.get(v, "")


def split_tags(text: str) -> list:
    out = []
    for t in (text or "").replace(";", ",").split(","):
        t = " ".join(t.split())
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def _row(folder: str, name: str = "", styles=(), domain: str = "",
         tags=()) -> dict:
    styles = [m for m in MARKERS if m in set(styles)]
    tags = list(tags)
    return {"folder": _norm_folder(folder), "name": name or "",
            "styles": styles, "domain": domain if domain in DOMAINS else "",
            "tags": tags,
            # the words model search matches: 'low poly' and 'lowpoly'
            "words": tm.stems(" ".join(styles + tags)
                              + (" lowpoly" if "Low Poly" in styles else ""))}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):     # a German Excel saves cp1252
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse(text: str) -> dict:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = None
    rows: dict = {}
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        if header is None:
            header = ["_".join(c.strip().lower().replace("-", " ").split())
                      for c in cells]
            continue
        rec = dict(zip(header, (c.strip() for c in cells)))
        folder = _norm_folder(rec.get("folder", ""))
        if not folder:
            continue
        styles = [m for m, col in MARKERS.items()
                  if rec.get(col, "").lower() in _YES]
        dom = rec.get("domain", "")
        dom = next((d for d in DOMAINS if d.lower() == dom.lower()), "")
        rows[folder.lower()] = _row(folder, rec.get("name", ""), styles,
                                    dom, split_tags(rec.get("tags", "")))
    return rows


def load(path=None) -> dict:
    """folder (lower case) -> row. Re-read only when the file changed."""
    p = Path(path or PATH)
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _LOCK:
        if _cache["key"] != key:
            try:
                _cache["rows"] = _parse(_decode(p.read_bytes()))
            except PermissionError as exc:
                raise TagFileBusy(f"{p} is open in another program") from exc
            _cache["key"] = key
        return _cache["rows"]


def save(rows: dict, path=None) -> None:
    p = Path(path or PATH)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
        w.writerow([r["folder"], r["name"]]
                   + ["x" if m in r["styles"] else "" for m in MARKERS]
                   + [r["domain"], ", ".join(r["tags"])])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(buf.getvalue().encode("utf-8-sig"))
    try:
        os.replace(tmp, p)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise TagFileBusy(f"{p} is open in another program: close it "
                          f"there, then try again") from exc


def sync(packs, seed=None, path=None) -> int:
    """Add a row for every pack folder the file does not list yet.
    `packs`: (folder, name) pairs. `seed`: folder -> (domain, style) the
    owner already set in the registry, copied in when the file is first
    made. Never removes a row. Returns the number of rows added."""
    p = Path(path or PATH)
    with _LOCK:
        first = not p.is_file()
        rows = dict(load(p))
        added = 0
        for folder, name in packs:
            folder = _norm_folder(folder)
            if not folder or folder.lower() in rows:
                continue
            dom, style = (seed or {}).get(folder, ("", "")) if first else ("", "")
            rows[folder.lower()] = _row(folder, name, [style] if style else [],
                                        dom)
            added += 1
        if added:
            save(rows, p)
        return added


def update(folder: str, styles=None, domain=None, tags=None,
           add_styles=(), path=None) -> dict:
    """Change one row (re-read first, so edits made in a spreadsheet in
    the meantime are kept). None leaves a field as it is."""
    p = Path(path or PATH)
    with _LOCK:
        rows = dict(load(p))
        k = _norm_folder(folder).lower()
        if k not in rows:
            raise KeyError(folder)
        r = rows[k]
        new_styles = list(r["styles"] if styles is None else styles)
        new_styles += [s for s in add_styles if s not in new_styles]
        rows[k] = _row(r["folder"], r["name"], new_styles,
                       r["domain"] if domain is None else domain,
                       r["tags"] if tags is None else tags)
        save(rows, p)
        return rows[k]


class Lookup:
    """Library-relative file path -> the row of the deepest TAGGED folder
    holding it (None when none does). A row added by hand for a parent
    folder tags every pack below it that has no tags of its own."""

    def __init__(self, rows: dict):
        self.rows = {k: r for k, r in rows.items() if r["styles"] or r["tags"]}
        self._dirs: dict = {}

    def __call__(self, rel_path: str):
        if not self.rows:
            return None
        d = _norm_folder(rel_path).lower().rsplit("/", 1)[0]
        if d in self._dirs:
            return self._dirs[d]
        hit, probe = None, d
        while probe:
            if probe in self.rows:
                hit = self.rows[probe]
                break
            probe = probe.rsplit("/", 1)[0] if "/" in probe else ""
        self._dirs[d] = hit
        return hit
