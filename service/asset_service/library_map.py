"""The library map: what every folder of a library is, and what the owner
decided about it. One plain file per library root:

    <library_root>/<agent_files>/library_map.csv

One row per folder the survey or the owner looked at. Columns:

    folder          path relative to the library root ('.' = the root)
    kind            3d-assets | unreal | unity | blender | textures | hdri |
                    animation | audio | purchases | archive | other | ignore
    state           what the survey saw: converted, raw, project, package,
                    kit, kit-exported, files (empty when it does not matter)
    include         x = index this folder (empty = leave it out)
    action          the owner's decision for the kind: unreal export|leave,
                    blender extract|leave, unity index-files|leave,
                    animation previews|no-previews (empty = not decided)
    confirmed       x once the owner confirmed the row (or the survey KNEW)
    name            display name
    photorealistic / stylized / low_poly   x = the pack's look
    domain          one of auto_classifier.DOMAINS, or empty
    tags            the owner's words, comma separated
    note            free text

Folders below a row inherit its kind, decision and tags unless they have
a row of their own. The file is the truth: ingest, the server and the
agent docs read it; `pharos_config.json` only mirrors the section picks
it implies (apply_to_config). It lives inside the library, so it survives
a registry rebuild and a move to another drive, and it opens in a
spreadsheet (comma, semicolon or tab; x, 1, yes or a tick mark a box).
Pharos adds rows for new folders and never removes one.

Grew out of pack_tags.csv (0.10): that file is read once and carried over.
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
from .survey import DECISIONS, KINDS, survey

FILE = "library_map.csv"
OLD_FILE = "pack_tags.csv"
PATH = config.AGENT_FILES / FILE


@config.on_reload
def _rebind() -> None:
    global PATH
    PATH = config.AGENT_FILES / FILE

# marker name -> CSV column
MARKERS = {"Photorealistic": "photorealistic", "Stylized": "stylized",
           "Low Poly": "low_poly"}
COLUMNS = ("folder", "kind", "state", "include", "action", "confirmed", "name",
           "photorealistic", "stylized", "low_poly", "domain", "tags", "note")
STATES = ("", "converted", "raw", "project", "package", "kit", "kit-exported",
          "files")
_YES = {"x", "1", "yes", "y", "true", "✓", "✔", "☑"}
_LOCK = threading.RLock()
_cache: dict = {"key": None, "rows": {}}


class TagFileBusy(OSError):
    """The file is open in another program (a spreadsheet locks it)."""


def _norm_folder(folder: str) -> str:
    f = (folder or "").replace("\\", "/").strip().strip("/")
    return f or "."


def marker(value: str) -> str:
    """'low poly', 'lowpoly', 'Low-Poly' -> 'Low Poly'; '' when unknown."""
    v = "".join(ch for ch in (value or "").lower() if ch.isalnum())
    for name in MARKERS:
        if v == name.lower().replace(" ", ""):
            return name
    return {"photoreal": "Photorealistic", "realistic": "Photorealistic",
            "stylised": "Stylized", "lowpolygon": "Low Poly"}.get(v, "")


def kind_of(value: str) -> str:
    """'3D Assets', 'Unreal Scene', 'textures' -> the map's kind, or ''."""
    v = "".join(ch for ch in (value or "").lower() if ch.isalnum())
    aliases = {"3dassets": "3d-assets", "3dasset": "3d-assets", "3d": "3d-assets",
               "assets": "3d-assets", "asset": "3d-assets", "models": "3d-assets",
               "model": "3d-assets", "meshes": "3d-assets", "unrealscene": "unreal",
               "unrealproject": "unreal", "unrealengine": "unreal", "ue": "unreal",
               "unityscene": "unity", "unityproject": "unity", "blenderfile": "blender",
               "blenderfiles": "blender", "blenderkit": "blender",
               "blend": "blender", "texture": "textures", "hdris": "hdri",
               "animations": "animation", "anim": "animation", "sounds": "audio",
               "sound": "audio", "purchase": "purchases", "collection": "purchases",
               "archives": "archive", "skip": "ignore", "ignored": "ignore"}
    for k in KINDS:
        if v == k.replace("-", ""):
            return k
    return aliases.get(v, "")


_CONTAINER_OPTIONS = {"unreal": ["export", "leave"], "blender": ["extract", "leave"],
                      "unity": ["index-files", "leave"],
                      "animation": ["previews", "no-previews"]}
def decision_options(kind: str, state: str) -> list:
    """The decisions a row may carry. A folder OF packs (state empty) takes
    its kind's options too, so one answer can cover every pack below it."""
    if (kind, state) in DECISIONS:
        return list(DECISIONS[(kind, state)])
    if not state and kind in _CONTAINER_OPTIONS:
        return list(_CONTAINER_OPTIONS[kind])
    return []


def needs_decision(r: dict) -> bool:
    """This folder itself is the thing the decision is about (a raw pack,
    a kit, loose .blend files, a Unity project, a clip folder) -- not a
    folder that merely contains such things."""
    return bool(DECISIONS.get((r["kind"], r["state"])))


def split_tags(text: str) -> list:
    out = []
    for t in (text or "").replace(";", ",").split(","):
        t = " ".join(t.split())
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def _row(folder: str, name: str = "", kind: str = "", state: str = "",
         include: bool = True, action: str = "", confirmed: bool = False,
         styles=(), domain: str = "", tags=(), note: str = "") -> dict:
    styles = [m for m in MARKERS if m in set(styles)]
    tags = list(tags)
    kind = kind if kind in KINDS else ""
    state = state if state in STATES else ""
    action = action if action in decision_options(kind, state) else ""
    return {"folder": _norm_folder(folder), "name": name or "",
            "kind": kind, "state": state,
            "include": bool(include) and kind != "ignore",
            "action": action, "confirmed": bool(confirmed),
            "styles": styles, "domain": domain if domain in DOMAINS else "",
            "tags": tags, "note": note or "",
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
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    # only the delimiter comes from the sample: a doubled quote further
    # down the file must still read as a quote (csv.excel rules)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
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
        if not rec.get("folder", "").strip():
            continue
        styles = [m for m, col in MARKERS.items()
                  if rec.get(col, "").lower() in _YES]
        dom = rec.get("domain", "")
        dom = next((d for d in DOMAINS if d.lower() == dom.lower()), "")
        # a pack_tags.csv (0.10) has no kind / include columns: every row
        # is included and the survey fills the kind in
        include = (rec.get("include", "x").lower() in _YES
                   if "include" in rec else True)
        rows[folder.lower()] = _row(
            folder, rec.get("name", ""), kind_of(rec.get("kind", "")),
            rec.get("state", "").lower(), include,
            rec.get("action", "").lower(),
            rec.get("confirmed", "").lower() in _YES,
            styles, dom, split_tags(rec.get("tags", "")), rec.get("note", ""))
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


def _depth(folder: str) -> int:
    return 0 if folder == "." else folder.count("/") + 1


def save(rows: dict, path=None) -> None:
    p = Path(path or PATH)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in sorted(rows.values(), key=lambda r: (r["folder"] != ".", r["folder"].lower())):
        w.writerow([r["folder"], r["kind"], r["state"],
                    "x" if r["include"] else "", r["action"],
                    "x" if r["confirmed"] else "", r["name"]]
                   + ["x" if m in r["styles"] else "" for m in MARKERS]
                   + [r["domain"], ", ".join(r["tags"]), r["note"]])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(buf.getvalue().encode("utf-8-sig"))
    try:
        os.replace(tmp, p)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise TagFileBusy(f"{p} is open in another program: close it "
                          f"there, then try again") from exc


def migrate(path=None) -> bool:
    """Carry a 0.10 pack_tags.csv over into the map (once). The old file
    stays where it is; it is not read again."""
    p = Path(path or PATH)
    old = p.with_name(OLD_FILE)
    if p.is_file() or not old.is_file():
        return False
    with _LOCK:
        rows = _parse(_decode(old.read_bytes()))
        save(rows, p)
    return True


def sync_survey(result: dict, path=None) -> tuple:
    """Add a row for every surveyed folder the map does not list, fill the
    kind of rows that have none. The owner's rows are never changed and no
    row is removed. Returns (added, filled)."""
    p = Path(path or PATH)
    with _LOCK:
        rows = dict(load(p))
        added = filled = 0
        for n in result.get("nodes") or []:
            folder = _norm_folder(n["folder"])
            k = folder.lower()
            know = n.get("confidence") == "know"
            if k not in rows:
                rows[k] = _row(folder, n.get("name") or folder.rsplit("/", 1)[-1],
                               n["kind"], n.get("state", ""),
                               include=n["kind"] != "ignore", confirmed=know)
                added += 1
            elif not rows[k]["kind"]:
                r = rows[k]
                rows[k] = _row(r["folder"], r["name"], n["kind"], n.get("state", ""),
                               r["include"], r["action"], know or r["confirmed"],
                               r["styles"], r["domain"], r["tags"], r["note"])
                filled += 1
            elif rows[k]["kind"] == n["kind"] and n.get("state", "") != rows[k]["state"] \
                    and (know or not rows[k]["state"]):
                # the state is the survey's: a pack converted since the
                # last survey moves raw -> converted (its spent decision
                # drops with it); the owner's columns stay as they are
                r = rows[k]
                rows[k] = _row(r["folder"], r["name"], r["kind"], n.get("state", ""),
                               r["include"], r["action"], r["confirmed"],
                               r["styles"], r["domain"], r["tags"], r["note"])
                filled += 1
        if added or filled:
            save(rows, p)
        return added, filled


def sync(packs, seed=None, path=None) -> int:
    """Add a row per (folder, name) pair the map lacks (kind left for the
    survey). `seed`: folder -> (domain, style) copied in on a first write."""
    p = Path(path or PATH)
    with _LOCK:
        first = not p.is_file()
        rows = dict(load(p))
        added = 0
        for folder, name in packs:
            folder = _norm_folder(folder)
            if folder == "." or folder.lower() in rows:
                continue
            dom, style = (seed or {}).get(folder, ("", "")) if first else ("", "")
            rows[folder.lower()] = _row(folder, name, styles=[style] if style else [],
                                        domain=dom)
            added += 1
        if added:
            save(rows, p)
        return added


def refresh(root=None, path=None) -> dict:
    """Survey the library and bring the map up to date; returns the survey
    result (with 'map_added' / 'map_filled' counts)."""
    root = Path(root or config.LIBRARY_ROOT)
    migrate(path)
    result = survey(root)
    if "error" not in result:
        added, filled = sync_survey(result, path)
        result["map_added"], result["map_filled"] = added, filled
    return result


def weights_of(result: dict) -> dict:
    """folder (lower) -> file count, from a survey result."""
    return {n["folder"].lower(): n.get("files", 0) for n in result.get("nodes") or []}


_UNSET = object()


def update(folder: str, kind=_UNSET, include=_UNSET, action=_UNSET,
           confirmed=_UNSET, styles=None, domain=None, tags=None, name=None,
           note=None, add_styles=(), state=_UNSET, path=None) -> dict:
    """Change one row (re-read first, so edits made in a spreadsheet in
    the meantime are kept). An argument left out stays as it is. A decision
    word that does not fit the row raises ValueError before anything is
    written (the decision already stored stays)."""
    p = Path(path or PATH)
    with _LOCK:
        rows = dict(load(p))
        k = _norm_folder(folder).lower()
        if k not in rows:
            raise KeyError(folder)
        r = rows[k]
        new_styles = list(r["styles"] if styles is None else styles)
        new_styles += [s for s in add_styles if s not in new_styles]
        new_kind = r["kind"] if kind is _UNSET else (kind_of(kind) or r["kind"])
        if state is _UNSET:
            state = r["state"] if new_kind == r["kind"] else ""
        elif state not in STATES:
            raise ValueError(f"unknown state {state!r}; use one of: "
                             f"{' | '.join(s for s in STATES if s)}")
        if action is not _UNSET and action and action not in decision_options(new_kind, state):
            options = decision_options(new_kind, state)
            raise ValueError(f"action {action!r} does not fit a {new_kind} {state} folder; "
                             f"options: {' | '.join(options) or 'none'}")
        rows[k] = _row(
            r["folder"], r["name"] if name is None else name, new_kind, state,
            r["include"] if include is _UNSET else bool(include),
            r["action"] if action is _UNSET else (action or ""),
            r["confirmed"] if confirmed is _UNSET else bool(confirmed),
            new_styles, r["domain"] if domain is None else domain,
            r["tags"] if tags is None else tags,
            r["note"] if note is None else note)
        save(rows, p)
        return rows[k]


class Lookup:
    """Library-relative file path -> the deepest row that holds it (any
    row with `node()`, a row that carries tags with `__call__`, as model
    search needs it). A row on a parent folder covers every folder below
    it that has no row of its own."""

    def __init__(self, rows: dict):
        self.all = dict(rows)
        self.rows = {k: r for k, r in rows.items() if r["styles"] or r["tags"]}
        self._dirs: dict = {}
        self._nodes: dict = {}

    def _find(self, table: dict, cache: dict, rel_path: str):
        if not table:
            return None
        d = _norm_folder(rel_path).lower()
        if "/" in d:
            d = d.rsplit("/", 1)[0]
        elif d != ".":
            d = "."
        if d in cache:
            return cache[d]
        hit, probe = None, d
        while True:
            if probe in table:
                hit = table[probe]
                break
            if probe == "." or not probe:
                break
            probe = probe.rsplit("/", 1)[0] if "/" in probe else "."
        cache[d] = hit
        return hit

    def __call__(self, rel_path: str):
        return self._find(self.rows, self._dirs, rel_path)

    def node(self, rel_path: str):
        return self._find(self.all, self._nodes, rel_path)


def sections(rows: dict) -> dict:
    """kind -> the included top-most folders of that kind (a folder below
    another folder of the same kind is part of it)."""
    out: dict = {}
    by_depth = sorted((r for r in rows.values() if included(r, rows) and r["kind"]
                       and r["folder"] != "."),
                      key=lambda r: (_depth(r["folder"]), r["folder"].lower()))
    for r in by_depth:
        parents = out.get(r["kind"], [])
        if any(r["folder"].lower().startswith(p.lower() + "/") for p in parents):
            continue
        out.setdefault(r["kind"], []).append(r["folder"])
    return out


def pending(rows: dict) -> list:
    """Included rows that need a decision the owner has not made, without a
    pending ancestor of the same kind (its answer covers the folders below;
    an answer on a container row covers them too)."""
    out = []
    for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
        if not (included(r, rows) and needs_decision(r) and r["folder"] != "."):
            continue
        if effective_action(r, rows):
            continue
        if any(a["kind"] == r["kind"] and needs_decision(a) and not a["action"]
               for a in _ancestors(r, rows)):
            continue
        out.append(r)
    return out


def included(r: dict, rows: dict) -> bool:
    """A row counts only while it and every folder above it are included:
    include=no on a store keeps its packs out too."""
    return bool(r["include"]) and all(a["include"] for a in _ancestors(r, rows))


def _ancestors(r: dict, rows: dict):
    probe = r["folder"].lower()
    while "/" in probe:
        probe = probe.rsplit("/", 1)[0]
        parent = rows.get(probe)
        if parent:
            yield parent


def effective_action(r: dict, rows: dict) -> str:
    """The row's own decision, else the nearest ancestor's of the same kind."""
    if r["action"]:
        return r["action"]
    for a in _ancestors(r, rows):
        if a["kind"] == r["kind"] and a["action"]:
            return a["action"]
    return ""


def apply_to_config(rows: dict, cfg: dict, root=None, weights=None) -> dict:
    """Mirror the map into the config keys the importers still read:
    sections (one folder per kind, the importers take one: the folder the
    config already names when it fits, else the heaviest by file count,
    else the first by name; a kind the map lacks has NO entry, so doctor
    stops asking for a default-named folder), scan_folders, manifest_roots,
    kitbash_root, indexer_skip_dirs, collection_csv."""
    root = Path(root or cfg.get("library_root") or config.LIBRARY_ROOT)
    weights = weights or {}
    sec = sections(rows)
    old = dict(cfg.get("sections") or {})
    cfg["sections"] = {}
    for key, kind in (("animation", "animation"), ("textures", "textures"),
                      ("audio", "audio"), ("collection", "purchases")):
        cands = sec.get(kind) or []
        if not cands:
            continue
        prev = (old.get(key) or "").replace("\\", "/").strip("/")
        if prev and prev.lower() in {c.lower() for c in cands}:
            cfg["sections"][key] = next(c for c in cands if c.lower() == prev.lower())
        else:
            cfg["sections"][key] = max(cands, key=lambda c: (
                weights.get(c.lower(), 0), -len(c), c.lower()))
    scan = list(sec.get("3d-assets", []))
    # every Unity project the owner wants indexed scans its Assets folder
    # (a decision on the folder of projects covers each project in it)
    for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
        if r["kind"] == "unity" and r["state"] == "project" \
                and included(r, rows) and effective_action(r, rows) == "index-files":
            assets = root / r["folder"] / "Assets"
            scan.append(f"{r['folder']}/Assets" if assets.is_dir() else r["folder"])
    cfg["scan_folders"] = scan
    roots = []
    for r in rows.values():
        if r["kind"] == "unreal" and r["state"] == "converted" and included(r, rows):
            parent = (root / r["folder"]).parent
            if str(parent) not in roots:
                roots.append(str(parent))
    cfg["manifest_roots"] = roots
    kits = sorted({str((root / r["folder"]).parent) for r in rows.values()
                   if r["kind"] == "blender" and r["state"] in ("kit", "kit-exported")
                   and included(r, rows)})
    if kits:
        cfg["kitbash_root"] = kits[0]
    else:
        cfg.pop("kitbash_root", None)
    skip = sorted({r["folder"] for r in rows.values()
                   if "/" not in r["folder"] and r["folder"] != "."
                   and (not r["include"] or r["kind"] in
                        ("ignore", "other", "archive", "purchases")
                        or (r["kind"] == "blender"
                            and effective_action(r, rows) == "leave"))})
    cfg["indexer_skip_dirs"] = skip
    coll = sec.get("purchases", [None])[0]
    if coll:
        from .survey import _purchase_csv
        csvs = sorted(f.name for f in (root / coll).glob("*.csv")
                      if _purchase_csv(f))
        if csvs:
            cfg["collection_csv"] = csvs[0]
    return cfg


def summary(rows: dict) -> list:
    """Top-level lines for the agent docs: folder, kind, decision, counts."""
    out = []
    pend = {r["folder"] for r in pending(rows)}
    for r in sorted(rows.values(), key=lambda r: r["folder"].lower()):
        if "/" in r["folder"] or r["folder"] == ".":
            continue
        line = f"{r['folder']}: {r['kind'] or 'unknown'}"
        if r["state"]:
            line += f" ({r['state']})"
        act = effective_action(r, rows)
        if act:
            line += f", decision: {act}"
        elif r["folder"] in pend:
            line += ", decision PENDING"
        else:
            below = [p for p in pend if p.lower().startswith(r["folder"].lower() + "/")]
            if below:
                line += f", {len(below)} decision(s) pending below"
        if not r["include"]:
            line += ", not indexed"
        if r["styles"] or r["tags"]:
            line += " -- " + ", ".join(r["styles"] + r["tags"])
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# CLI: pharos.py map show | set <folder> key=value ... | refresh | apply
# ---------------------------------------------------------------------------

def run_map(argv=None) -> int:
    import argparse
    import sys
    argv = argv if argv is not None else sys.argv[2:]
    ap = argparse.ArgumentParser(prog="pharos map",
                                 description="read or change the library map")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("show", help="print the map")
    sub.add_parser("refresh", help="survey the library and add new folders")
    sub.add_parser("apply", help="mirror the map into pharos_config.json")
    sub.add_parser("pending", help="decisions the owner has not made yet")
    s = sub.add_parser("set", help="change one folder's row")
    s.add_argument("folder")
    s.add_argument("fields", nargs="+", metavar="key=value",
                   help="kind=, state=, include=yes|no, action=, confirmed=yes|no, "
                        "domain=, tags=, note=")
    args = ap.parse_args(argv)
    if not config.is_configured():
        print("pharos_config.json is not configured yet -- run pharos.py init first")
        return 2
    if args.cmd in (None, "show"):
        rows = load()
        if not rows:
            print(f"no map yet at {PATH} -- run: pharos.py map refresh")
            return 0
        for r in sorted(rows.values(), key=lambda r: (r["folder"] != ".", r["folder"].lower())):
            flags = ("" if r["include"] else " [out]") + (" *" if r["confirmed"] else "")
            act = f" action={r['action']}" if r["action"] else (
                "  DECISION PENDING" if decision_options(r["kind"], r["state"]) else "")
            print(f"{r['folder']:<50} {r['kind']:<10} {r['state']:<12}{act}{flags}"
                  + (f"  {', '.join(r['styles'] + r['tags'])}" if r["styles"] or r["tags"] else ""))
        print(f"\n{len(rows)} rows in {PATH}  (* = confirmed, [out] = not indexed)")
        return 0
    if args.cmd == "refresh":
        res = refresh()
        if "error" in res:
            print(res["error"])
            return 1
        print(f"surveyed {res['files']:,} files in {res['seconds']} s: "
              f"{res['map_added']} folder(s) added, {res['map_filled']} filled in -> {PATH}")
        for r in pending(load()):
            print(f"  decision pending: {r['folder']} ({r['kind']} {r['state']}): "
                  f"{' | '.join(decision_options(r['kind'], r['state']))}")
        return 0
    if args.cmd == "pending":
        rows = load()
        for r in pending(rows):
            print(f"{r['folder']}: {r['kind']} {r['state']} -> "
                  f"{' | '.join(decision_options(r['kind'], r['state']))}")
        return 0
    if args.cmd == "apply":
        rows = load()
        cfg = config.load()
        apply_to_config(rows, cfg, weights=weights_of(survey(config.LIBRARY_ROOT)))
        config.save(cfg)
        print(f"pharos_config.json now mirrors {PATH}: sections={cfg['sections']} "
              f"scan_folders={cfg['scan_folders']}")
        return 0
    if args.cmd == "set":
        fields = {}
        for f in args.fields:
            if "=" not in f:
                print(f"not key=value: {f}")
                return 2
            k, v = f.split("=", 1)
            fields[k.strip().lower()] = v.strip()
        known = {"kind", "state", "include", "confirmed", "action", "domain",
                 "note", "name", "tags"}
        unknown = sorted(set(fields) - known)
        if unknown:
            print(f"unknown field(s): {', '.join(unknown)}; use: {', '.join(sorted(known))}")
            return 2
        kw: dict = {}
        if "kind" in fields:
            if not kind_of(fields["kind"]):
                print(f"unknown kind {fields['kind']!r}; use one of: {', '.join(KINDS)}")
                return 2
            kw["kind"] = fields["kind"]
        if "state" in fields:
            kw["state"] = fields["state"].lower()
        for flag in ("include", "confirmed"):
            if flag in fields:
                kw[flag] = fields[flag].lower() in _YES
        if "action" in fields:
            kw["action"] = fields["action"].lower()
        for text in ("domain", "note", "name"):
            if text in fields:
                kw[text] = fields[text]
        if "tags" in fields:
            kw["tags"] = split_tags(fields["tags"])
        try:
            r = update(args.folder, **kw)
        except KeyError:
            print(f"no row for {args.folder!r} -- run pharos.py map refresh, or check the spelling")
            return 2
        except ValueError as exc:
            print(exc)
            return 2
        print(f"{r['folder']}: kind={r['kind']} include={'yes' if r['include'] else 'no'} "
              f"action={r['action'] or '-'} confirmed={'yes' if r['confirmed'] else 'no'}")
        return 0
    return 2
