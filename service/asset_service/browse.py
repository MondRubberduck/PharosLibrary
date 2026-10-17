"""The Pharos server: the HTTP API agents search with and the pages a
human verifies with, over the SQLite registry.

  /                dashboard: the four sections with their counts
  /setup           pick the library, correct the survey, index
  /tags            the library map: kinds, decisions, the owner's labels
  /assets /textures /audio /animation   browse one section
  /viewer?pack=..  live three.js FBX/BVH playback with auto-orientation
  /api/...         the JSON API (the generated AGENT_API.md lists it)

    python pharos.py serve                 # http://127.0.0.1:8765
    python pharos.py serve --port 9000 --rebuild

Stdlib only; binds 127.0.0.1 by default (nothing leaves the workstation).
Reads the registry and the library; writes the registry, the previews
and the library map, nothing else.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote as url_quote, unquote, urlparse

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_service import config  # noqa: E402
from asset_service import db  # noqa: E402
from asset_service import collection_import  # noqa: E402
from asset_service import textures_import  # noqa: E402
from asset_service import audio_import  # noqa: E402
from asset_service import meshes_import  # noqa: E402
from asset_service import textmatch as tm  # noqa: E402
from asset_service import skeleton  # noqa: E402
from asset_service import library_map  # noqa: E402
from asset_service import setup_flow  # noqa: E402
from asset_service import clips_import, folders_import, sheets  # noqa: E402
from asset_service.library_map import DOMAINS  # noqa: E402

DEFAULT_DB = config.DB_PATH
CANONICAL_ROOTS = [config.LIBRARY_ROOT]
COLLECTION_ROOT = config.COLLECTION_ROOT
TEXTURES_ROOT = config.TEX_ROOT
TEX_DISPLAY_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_ROOT = config.AUDIO_ROOT
AUDIO_PLAYABLE = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}
AUDIO_CTYPE = {".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
               ".flac": "audio/flac", ".m4a": "audio/mp4",
               ".aif": "audio/aiff", ".aiff": "audio/aiff"}


def _audio_hero_uri() -> str:
    """Dark waveform SVG as the dashboard tile artwork."""
    import random
    from urllib.parse import quote as _uq
    rnd = random.Random(42)
    bars = []
    x = 40
    while x < 760:
        h = rnd.randint(14, 170)
        c = rnd.choice(["#2c5d8a", "#3a7ab0", "#25506f"])
        bars.append(
            f'<rect x="{x}" y="{225 - h // 2}" width="7" height="{h}" '
            f'rx="3" fill="{c}"/>')
        x += 13
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="800" '
           'height="450" viewBox="0 0 800 450">'
           '<rect width="800" height="450" fill="#0d1015"/>'
           + "".join(bars) + "</svg>")
    return "data:image/svg+xml;charset=utf-8," + _uq(svg)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# Word matching lives in textmatch.py: ONE implementation for the importers,
# the HTTP endpoints and the MCP tools (it used to exist five times with
# different rules -- 'benches' found nothing in a library with 38 benches).


class _Params:
    """Request parameters with honest feedback.

    A mistyped or unsupported filter used to be dropped silently and the
    UNFILTERED result came back looking authoritative (max_dur=3 returned
    the same sounds as no filter at all). Every list endpoint now reads
    its parameters through this class and reports what it could not use
    in `warnings`."""

    def __init__(self, params: dict):
        self.params = params or {}
        self.used: set = set()
        self.warnings: list = []

    def s(self, *names: str) -> str:
        """First present alias, as a string ('' when absent)."""
        self.used.update(names)
        for n in names:
            v = self.params.get(n)
            if v:
                return v[0]
        return ""

    def num(self, name: str, default: float) -> float:
        self.used.add(name)
        v = self.params.get(name)
        if not v or v[0] == "":
            return default
        try:
            return float(v[0])
        except ValueError:
            self.warn(f"{name}={v[0]!r} is not a number: filter ignored")
            return default

    def page_size(self, default: int) -> tuple:
        self.used.update(("page", "size"))
        out = []
        for name, dflt, hi in (("page", 1, 10 ** 9), ("size", default, 500)):
            v = self.params.get(name)
            try:
                n = int(v[0]) if v and v[0] != "" else dflt
            except ValueError:
                self.warn(f"{name}={v[0]!r} is not a number: using {dflt}")
                n = dflt
            out.append(min(hi, max(1, n)))
        return out[0], out[1]

    def flag(self, name: str) -> bool:
        """1 / true / yes / on -> True; anything else unknown is reported
        (textures=true used to be ignored and returned every mesh)."""
        v = self.s(name).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v not in ("", "0", "false", "no", "off"):
            self.warn(f"{name}={v!r} is not 1 or 0: ignored")
        return False

    def pick(self, label: str, value: str, have) -> str:
        """`value` matched case-insensitively against the values the
        section HAS. An unknown value still filters (to nothing) and is
        reported with the closest names: '0 results' must not read as
        'the library has none'."""
        v = (value or "").strip()
        if not v:
            return ""
        known = {h.lower(): h for h in have if h}
        if v.lower() in known:
            return known[v.lower()]
        near = difflib.get_close_matches(v, list(known.values()), n=3,
                                         cutoff=0.5)
        self.warn(f"no row has {label}={value!r}"
                  + (f"; did you mean {near}?" if near else ""))
        return v

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def done(self, resp: dict) -> dict:
        unknown = sorted(k for k in self.params if k not in self.used)
        if unknown:
            self.warn("ignored unknown parameter(s): " + ", ".join(unknown)
                      + " -- accepted here: " + ", ".join(sorted(self.used)))
        if self.warnings:
            resp["warnings"] = self.warnings
        return resp


def _thumb_path(r):
    """Thumbnail the UI should show: human override if the file still
    exists, else the crawler's first image."""
    if r.get("thumb_override") and Path(r["thumb_override"]).is_file():
        return r["thumb_override"]
    return r["first_image"]
STATUS_FILE = (Path(config.EXTENSION_STATUS_FILE).expanduser()
               if config.EXTENSION_STATUS_FILE else None)
STATIC_DIR = Path(__file__).resolve().parent / "static"   # vendored three.js
TPL_DIR = Path(__file__).resolve().parent / "templates"


def _load_tpl(name: str) -> str:
    return (TPL_DIR / name).read_text(encoding="utf-8")


ASSETS_PAGE = _load_tpl("assets.html")
TEXTURES_PAGE = _load_tpl("textures.html")
AUDIO_PAGE = _load_tpl("audio.html")
TAGS_PAGE = _load_tpl("tags.html")
THUMB_LIMIT = 8000
PREVIEW_EXTS = {".fbx", ".bvh"}          # animation clips: played by the viewer, filmed by /batchrender
VIEWER_EXTS = PREVIEW_EXTS | {".obj", ".glb", ".gltf"}    # what /file hands the 3D viewer
TEXTURE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
RESOURCE_EXTS = TEXTURE_EXTS | {".avif", ".mtl", ".bin"}   # what /res/ hands the viewer next to a model
PAGE_SIZE = 200            # pack-content page rows per request
PREVIEW_DIR = config.PREVIEW_DIR   # rendered clip loops (webm)
SETUP_PAGE = _load_tpl("setup.html")
MODELS_PAGE = _load_tpl("models.html")
# the purchases tile exists only when the library has a purchase list
_ASSETS_TILE = ('<a class="tile" href="/assets">\n <img src="{{ASSET_THUMB}}" alt="" '
                'onerror="this.style.opacity=0">\n <div class="edge"></div>\n <div class="label">'
                '<h1>Assets</h1><span>{{ASSET_PACKS}} purchases · galleries · store links</span>'
                '</div>\n</a>\n')
# SETUP MODE: the server runs without a library (no config yet, or the
# owner is re-pointing it); only the setup page and its API answer, the
# rest says so with a 503 until the first ingest ran
_SETUP_MODE = False
_SETUP_OPEN = ("/setup", "/api/setup/", "/api/fs/", "/static/", "/favicon.ico")


@config.on_reload
def _rebind_roots() -> None:
    """After config.reload(): every path this module derived from the
    roots follows the new config (setup mode changes it at run time)."""
    global DEFAULT_DB, COLLECTION_ROOT, TEXTURES_ROOT, AUDIO_ROOT, STATUS_FILE
    global PREVIEW_DIR, _DB_PATH
    DEFAULT_DB = config.DB_PATH
    CANONICAL_ROOTS[:] = [config.LIBRARY_ROOT]
    COLLECTION_ROOT = config.COLLECTION_ROOT
    TEXTURES_ROOT = config.TEX_ROOT
    AUDIO_ROOT = config.AUDIO_ROOT
    STATUS_FILE = (Path(config.EXTENSION_STATUS_FILE).expanduser()
                   if config.EXTENSION_STATUS_FILE else None)
    PREVIEW_DIR = config.PREVIEW_DIR
    _DB_PATH = config.DB_PATH

# Concept clusters for tiered ("meta") search: exact title match first, then
# related motions from the same cluster (search "talking" -> "arguing",
# "presenting", ...). Curated + offline on purpose.
ANIM_CLUSTERS = [
    {"talk", "speak", "chat", "converse", "conversation", "discuss", "argue",
     "argument", "present", "explain", "tell", "nag", "gossip", "complain",
     "whisper", "shout", "yell", "announce", "pitch", "speech"},
    {"listen", "hear", "agree", "nod", "disagree", "refuse", "deny", "accept",
     "approve", "attention", "attentive", "confirm"},
    {"greet", "hello", "wave", "welcome", "goodbye", "bye", "farewell", "handshake"},
    {"idle", "breathe", "breathing", "wait", "stand", "relax", "rest", "calm"},
    {"walk", "jog", "run", "sprint", "march", "stroll", "step", "pace", "strut"},
    {"dance", "groove", "party", "club", "rave", "jump", "skip", "hop", "climb"},
    {"fight", "punch", "kick", "attack", "block", "hit", "battle", "combat",
     "box", "sword", "stab", "slap"},
    {"sit", "seat", "chair", "crouch", "kneel", "lie"},
    {"angry", "anger", "sad", "cry", "laugh", "happy", "joy", "cheer", "excited",
     "agitated", "frustrated", "upset", "disgust", "surprised", "scared",
     "afraid", "fear", "smile", "grin", "smug"},
    {"carry", "hold", "lift", "pull", "push", "grab", "throw", "catch", "pick"},
    {"phone", "call", "telephone", "smartphone", "text"},
    {"clap", "applause", "applaud", "salute"},
    {"work", "type", "write", "office", "read", "book"},
    {"door", "open", "close", "knock", "enter", "exit"},
]


def _foreign_share(p: Path) -> bool:
    """A UNC path on a share none of the roots lives on. Resolving it would
    open the share: a web page's <img> can carry such a path, and an SMB
    connection hands out credentials. Refused before resolve()."""
    drive = p.drive
    if not drive.startswith(("\\\\", "//")):
        return False
    return not any(root.drive.lower() == drive.lower() for root in CANONICAL_ROOTS)


def _safe_asset_file(path_str: str, allowed: set) -> Optional[Path]:
    """Resolve a path (absolute or canonical-root-relative) and verify it is
    an existing file under a canonical root with an allowed extension."""
    p = Path(path_str)
    if _foreign_share(p):
        return None
    if not p.is_absolute():
        p = CANONICAL_ROOTS[0] / p
    try:
        p = p.resolve()
    except OSError:
        return None
    if p.suffix.lower() not in allowed:
        return None
    for root in CANONICAL_ROOTS:
        try:
            p.relative_to(root.resolve())
            break
        except ValueError:
            continue
    else:
        return None
    return p if p.is_file() else None


def _normalize_pack_id(raw: str) -> str:
    """Accept both 'pack::A/B' (db form) and 'pack::A::B' (older links)."""
    if not raw.startswith("pack::"):
        return raw
    return "pack::" + raw[len("pack::"):].replace("::", "/")


def _anim_prefix() -> str:
    """Pack-id prefix for the animation section, FROM THE CONFIG. The
    section name is configurable; this prefix was hardcoded 'Animation'
    in six queries, so any library with a different folder name read
    '0 clips' everywhere. substr-prefix matching (not LIKE) also makes
    the bare-section id 'pack::<name>' (clips directly in the section
    folder) count, which LIKE '.../%' never matched."""
    return "pack::" + (config.SECTIONS.get("animation") or "Animation")


# Retarget body for skeleton-only clips: one library's own asset, looked up
# at this layout UNDER THE CONFIGURED animation section. Libraries without
# it preview bones-only (the pages get '' from the server, never a literal).
_RIG_SUBPATH = "Actor/motion-dummy_male/Render_Dummy.fbx"


def _rig_rel(section: Optional[str] = None,
             root: Optional[Path] = None) -> str:
    """Library-relative rig path, or '' when that file does not exist."""
    sec = section or config.SECTIONS.get("animation") or "Animation"
    rel = f"{sec}/{_RIG_SUBPATH}"
    return rel if ((root or CANONICAL_ROOTS[0]) / rel).is_file() else ""


def _with_rig(page: str) -> str:
    """Serve a page with its {{RIG_REL}} placeholder (the body of a JS
    double-quoted string) filled from _rig_rel()."""
    return page.replace("{{RIG_REL}}",
                        json.dumps(_rig_rel())[1:-1].replace("<", "\\u003c"))


_DB_PATH = DEFAULT_DB


# ---------------------------------------------------------------------------
# API payload builders
# ---------------------------------------------------------------------------

def _preview_key(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]


def api_stats() -> dict:
    conn = db.connect(_DB_PATH)
    stats = db.db_stats(conn)
    sections = {}
    for tbl in ("meshes", "textures", "audio", "collection"):
        try:
            sections[tbl] = conn.execute(
                f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            sections[tbl] = 0      # table not created yet (importer skipped)
    try:
        # clips were the one section missing here: an agent reading the
        # live totals saw no animation at all
        sections["animations"] = db.anim_clip_count(
            conn, config.SECTIONS.get("animation"))
    except sqlite3.OperationalError:
        sections["animations"] = 0
    stats["last_write"] = db.get_meta(conn, "sources_stamp_at")
    conn.close()
    status = None
    try:
        if STATUS_FILE is not None and STATUS_FILE.is_file():
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))[:5]
    except (OSError, ValueError):
        status = None
    return {"stats": stats, "sections": sections, "extension_status": status,
            "domains": list(DOMAINS),
            "styles": [*library_map.MARKERS, "Neutral/Unclassified"]}




def _tag_rows() -> dict:
    """The library map's rows; {} while it cannot be read (a search must
    not fail because a spreadsheet holds the file)."""
    try:
        return library_map.load()
    except OSError:
        return {}


_survey_cache: dict = {"nodes": {}, "at": 0.0}


def _survey_nodes() -> dict:
    """folder (lower) -> the latest survey node, from the last refresh."""
    return _survey_cache["nodes"]


def _sync_pack_tags() -> int:
    """Survey the library and give every folder a row in the map, so new
    packs appear after an ingest (the 0.10 library_map.csv is carried over
    once). Returns the number of rows added."""
    import time
    if not config.is_configured():
        return 0        # setup mode: there is no library to survey yet
    try:
        res = library_map.refresh(config.LIBRARY_ROOT)
    except library_map.TagFileBusy:
        return 0        # a spreadsheet holds the file: serve what load() reads
    if "error" in res:
        return 0
    _survey_cache["nodes"] = {n["folder"].lower(): n for n in res.get("nodes") or []}
    _survey_cache["at"] = time.time()
    return res.get("map_added", 0)


def api_pack_tags() -> dict:
    """Every folder of the map with its kind, decision and the owner's
    tags, the survey's guess beside it, and the registry's counts."""
    if not config.is_configured():
        return {"file": "", "markers": list(library_map.MARKERS),
                "kinds": list(library_map.KINDS), "domains": list(DOMAINS),
                "pending": [], "packs": []}
    _sync_pack_tags()
    rows = _tag_rows()
    nodes = _survey_nodes()
    out = []
    for k, r in sorted(rows.items(), key=lambda kv: (kv[1]["folder"] != ".",
                                                     kv[1]["folder"].lower())):
        n = nodes.get(k)
        out.append({
            "folder": r["folder"], "name": r["name"], "kind": r["kind"],
            "state": r["state"], "include": r["include"], "action": r["action"],
            "confirmed": r["confirmed"],
            "options": library_map.decision_options(r["kind"], r["state"]),
            "effective_action": library_map.effective_action(r, rows),
            "styles": r["styles"], "domain": r["domain"], "tags": r["tags"],
            "note": r["note"],
            "found": bool(n and n.get("files")),
            "files": (n or {}).get("files") or 0,
            "survey": ({"kind": n["kind"], "confidence": n["confidence"],
                        "state": n.get("state", ""),
                        "evidence": n["evidence"]} if n else None),
            "guess": None, "thumb": None})
    return {"file": str(library_map.PATH), "markers": list(library_map.MARKERS),
            "kinds": list(library_map.KINDS), "domains": list(DOMAINS),
            "pending": [r["folder"] for r in library_map.pending(rows)],
            "packs": out}


def api_pack_tags_set(body: dict) -> tuple:
    """POST {folder, kind?, include?, action?, confirmed?, styles?, domain?,
    tags?, note?} -> (answer, HTTP status). A field left out stays as it
    is; "" / [] clears it."""
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        return {"error": "folder missing"}, 400
    kw: dict = {}
    if "kind" in body:
        kind = library_map.kind_of(str(body.get("kind") or ""))
        if not kind:
            return {"error": f"unknown kind {body.get('kind')!r}; use: "
                             f"{', '.join(library_map.KINDS)}"}, 400
        kw["kind"] = kind
    if "state" in body:
        kw["state"] = str(body.get("state") or "").lower()
    for flag in ("include", "confirmed"):
        if flag in body:
            kw[flag] = bool(body.get(flag))
    if "action" in body:
        kw["action"] = str(body.get("action") or "").lower()
    styles = body.get("styles")
    if styles is not None:
        if not isinstance(styles, list):
            return {"error": "styles must be a list"}, 400
        bad = [s for s in styles if not library_map.marker(str(s))]
        if bad:
            return {"error": f"unknown style(s) {bad}; use: "
                             f"{', '.join(library_map.MARKERS)}"}, 400
        kw["styles"] = [library_map.marker(str(s)) for s in styles]
    domain = body.get("domain")
    if domain not in (None, "") and domain not in DOMAINS:
        return {"error": f"unknown domain {domain!r}; use: "
                         f"{', '.join(DOMAINS)}"}, 400
    if domain is not None:
        kw["domain"] = domain
    tags = body.get("tags")
    if tags is not None:
        kw["tags"] = library_map.split_tags(
            ", ".join(map(str, tags)) if isinstance(tags, list) else str(tags))
    if "note" in body:
        kw["note"] = str(body.get("note") or "")
    # one lock around the row and the config mirror: two quick changes on
    # the page are two threads, and the config file is written whole
    with library_map._LOCK:
        try:
            row = library_map.update(folder, **kw)
        except KeyError:
            return {"error": "no such folder in the library map"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except library_map.TagFileBusy as exc:
            return {"error": str(exc)}, 409
        # the config mirrors the map (sections, scan folders, skip dirs)
        try:
            cfg = config.load()
            weights = {k: n.get("files", 0) for k, n in _survey_nodes().items()}
            library_map.apply_to_config(_tag_rows(), cfg, config.LIBRARY_ROOT,
                                        weights=weights)
            config.save(cfg)
        except OSError:
            pass
    keys = ("folder", "name", "kind", "state", "include", "action", "confirmed",
            "styles", "domain", "tags", "note")
    return {"ok": True, "pack": {k: row[k] for k in keys},
            "options": library_map.decision_options(row["kind"], row["state"])}, 200


def _jailed(t, root) -> bool:
    """True when resolved path t sits inside root (real containment, not
    a string-prefix match -- 'Audio_Assets_backup' must not pass a jail
    meant for 'Audio_Assets')."""
    try:
        t.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False

_RES_INDEX: dict = {}        # folder path -> {lower file name: [Path, ...]}
_RES_INDEX_CAP = 200_000     # files looked at per folder, at most
_NAMED_EXTS = TEXTURE_EXTS | {".avif"}


def _res_index(folder: Path) -> dict:
    """Every texture under `folder` by lower-cased file name, walked once
    per server run."""
    key = str(folder.resolve()).lower()
    index = _RES_INDEX.get(key)
    if index is None:
        index = {}
        seen = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "_Agent_Files"))]
            for fn in filenames:
                if Path(fn).suffix.lower() in _NAMED_EXTS:
                    index.setdefault(fn.lower(), []).append(Path(dirpath) / fn)
            seen += len(filenames)
            if seen > _RES_INDEX_CAP:
                break
        _RES_INDEX[key] = index
    return index


def _suffix_match(path: Path, want: list) -> int:
    """How many trailing folder names of `path` agree with `want` (the
    folders the file named): the 2k texture for a path that said 2k."""
    segs = [x.lower() for x in path.parent.parts]
    n = 0
    while n < len(want) and n < len(segs) and segs[-1 - n] == want[-1 - n]:
        n += 1
    return n


def _resource_by_name(rel: str) -> Optional[Path]:
    """A texture the model names by a path that is not there (OBJ kits
    keep the author's absolute paths, textures get moved): the same file
    name under the model's library-map folder, then the folders above it
    (a kit downloaded in parts keeps its textures in a sibling part), never
    a top-level folder or the root. Among same-named files the one whose
    folders agree with the path the file gave wins."""
    rel = rel.replace("\\", "/")
    parts = [x for x in rel.split("/") if x and x not in (".", "..")]
    name = parts[-1] if parts else ""
    if Path(name).suffix.lower() not in _NAMED_EXTS:
        return None
    root = CANONICAL_ROOTS[0]
    # the model's folder: the longest leading run of segments that is a
    # folder in the library (after it comes the path the file made up)
    model_dir = ""
    for n in range(1, len(parts)):
        cand = "/".join(parts[:n])
        if (root / cand).is_dir():
            model_dir = cand
        else:
            break
    if not model_dir:
        return None                      # no model folder: nothing to search
    want = [x.lower() for x in parts[len(model_dir.split("/")):-1]]
    node = library_map.Lookup(library_map.load()).node(model_dir + "/x")
    folder = node["folder"] if node and node["folder"] != "." else model_dir
    folders = [folder]
    while folder.count("/") >= 2 and len(folders) < 4:
        folder = folder.rsplit("/", 1)[0]
        folders.append(folder)
    for f in folders:
        top = root / f
        if not _jailed(top, root) or not top.is_dir():
            continue
        cands = _res_index(top).get(name.lower()) or []
        if cands:
            best = max(cands, key=lambda c: _suffix_match(c, want))
            if best.is_file() and _jailed(best, root):
                return best
    return None


def api_clips(params: dict) -> dict:
    """FBX/BVH files of one pack for the live 3D viewer."""
    pack_id = _normalize_pack_id((params.get("pack") or [""])[0])
    if not pack_id.startswith("pack::"):
        return {"files": [], "name": ""}
    conn = db.connect(_DB_PATH)
    folder = pack_id[len("pack::"):]
    files: list = []
    if clips_import.has_rows(conn):
        files = [r[0] for r in conn.execute(
            "SELECT rel FROM clips WHERE folder = ? ORDER BY rel LIMIT 400", (folder,))]
    conn.close()
    return {"name": _pretty_folder(folder), "files": files}





def _query(q_raw: str, P) -> "tm.Query":
    """Parse a search string; warn when it leaves no usable word (single
    letters and punctuation are ignored), so a 0 is not read as 'absent'."""
    query = tm.Query(q_raw)
    if query.empty:
        P.warn(f"q={q_raw!r} has no usable search word (single letters and "
               "punctuation are ignored)")
    return query


def _related(query, clusters) -> set:
    """Words of every concept cluster the query touches, minus the query
    itself: 'stucco' -> plaster, brick, facade ... (curated, offline)."""
    if not query:
        return set()
    qstems = query.stems
    out: set = set()
    for cl in clusters:
        cst: set = set()
        for w in cl:
            cst |= tm.variants(w)
        if qstems & cst:
            out |= cst
    return {w for w in out - qstems if len(w) >= 3}


def _match_rows(rows, query, name_of, context_of, clusters=(),
                glued: bool = False) -> tuple:
    """The one AND-first matcher behind every list endpoint.

    Returns (matched, mode): entries {'r', 'tier', 'score', '_hits'}.
      tier 1/2  every query word matches (1 = all as whole name words)
      tier 3    OR fallback -- served ONLY when no row matches every word;
                ranked by word rarity (documented for all sections, but
                only the mesh endpoint used to do it: the others listed
                fallback hits alphabetically)
      tier 3    related-cluster rows -- served only when no query word
                matches anything at all
    No query word -> every row, tier 0, minus '-word' exclusions."""
    if not query:
        return [{"r": r, "tier": 0, "score": 0, "_hits": []} for r in rows
                if not (query.exclude and tm.excluded(
                    query, name_of(r), context_of(r)))], "and"
    related = _related(query, clusters)
    matched, partial, adjacent = [], [], []
    for r in rows:
        name = name_of(r)
        ctx = context_of(r)
        if tm.excluded(query, name, ctx):
            continue
        hits = tm.hits(query, name, ctx, glued=glued)
        if all(hits):
            matched.append({"r": r, "tier": tm.tier(hits),
                            "score": sum(hits) + tm.bonus(query, name),
                            "_hits": hits})
        elif any(hits):
            partial.append({"r": r, "tier": 3, "score": sum(hits),
                            "_hits": hits})
        elif related and (related & tm.name_forms(name)[0] or related & ctx):
            adjacent.append({"r": r, "tier": 3, "score": 0, "_hits": hits})
    if matched:
        return matched, "and"
    if partial:
        tm.rarity_rank(partial, query)
        return partial, "or-fallback"
    return adjacent, ("or-fallback" if adjacent else "and")


def _suggest(resp: dict, query, rows, name_of, best_tier: int = 0) -> None:
    """Unless a whole-word name match exists, name the closest words the
    section DOES contain for query words it does not know ('barel' ->
    'barrel'): a thin or empty answer must not read as "not in the
    library"."""
    if not query or best_tier == 1:
        return
    vocab = set()
    for r in rows:
        vocab.update(tm.tokens(name_of(r)))
    # only for words the section does not contain at all
    missing = {g["word"] for g in query.groups if not (g["exact"] & vocab)}
    near = tm.did_you_mean(query, vocab, missing) if missing else {}
    if near:
        resp["did_you_mean"] = near


# ---------------------------------------------------------------------------
# collection (purchase crawler CSV) -- the Assets section ground truth
# ---------------------------------------------------------------------------

# concept clusters for tier-3 "adjacent" search over the collection
COLL_CLUSTERS = [
    {"scifi", "sci", "fi", "cyberpunk", "futuristic", "spaceship", "space",
     "starship", "mech", "robot", "technology", "lab"},
    {"medieval", "castle", "fortress", "kingdom", "knight", "fantasy", "magic"},
    {"horror", "haunted", "spooky", "creepy", "demonic", "demon", "hell",
     "crypt", "graveyard", "zombie"},
    {"nature", "forest", "jungle", "tree", "plant", "foliage", "rock",
     "stone", "mountain", "landscape", "grass"},
    {"city", "urban", "street", "town", "village", "building", "architecture",
     "skyscraper", "berlin"},
    {"winter", "snow", "ice", "arctic", "frozen"},
    {"desert", "sand", "dune", "canyon"},
    {"water", "aquatic", "underwater", "ocean", "sea", "island", "coast",
     "harbor", "docks"},
    {"interior", "room", "office", "apartment", "furniture", "house",
     "kitchen", "bathroom", "livingroom"},
    {"military", "gun", "weapon", "armor", "soldier", "war", "tank"},
    {"character", "human", "creature", "animal", "clothing", "outfit",
     "monk", "people"},
    {"vehicle", "car", "truck", "watercraft", "boat", "transportation"},
    {"ruins", "ruin", "destroyed", "apocalyptic", "post", "wreck", "abandoned",
     "derelict"},
    {"temple", "church", "religious", "shrine", "sacred"},
    {"japan", "japanese", "asian", "tokyo", "akairo"},
    {"victorian", "steampunk", "historical", "old", "ancient"},
    {"industrial", "factory", "machine", "warehouse", "industrial"},
]


def _coll_rows() -> list:
    conn = db.connect(_DB_PATH)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM collection ORDER BY store, name")]
    except sqlite3.OperationalError:
        rows = []       # table not created yet (importer skipped) -> empty
    finally:
        conn.close()
    for r in rows:
        try:
            r["taglist"] = json.loads(r["tags"] or "[]")
        except ValueError:
            r["taglist"] = []
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except (ValueError, TypeError):
            r["meta"] = {}
    return rows


def api_collection_tree() -> dict:
    rows = _coll_rows()
    stores: dict = {}
    groups: dict = {}
    cats: dict = {}
    local = sum(1 for r in rows if r.get("availability") == "local")
    for r in rows:
        stores[r["store"]] = stores.get(r["store"], 0) + 1
        groups[r["type_group"]] = groups.get(r["type_group"], 0) + 1
        if r["type_cat"]:
            cats[r["type_cat"]] = cats.get(r["type_cat"], 0) + 1
    return {
        "local": local, "notdown": len(rows) - local,
        "total": len(rows),
        "stores": [{"name": k, "n": v} for k, v in sorted(stores.items())],
        "groups": [{"name": k, "n": v} for k, v in sorted(groups.items())],
        "cats": [{"name": k, "n": v} for k, v in sorted(cats.items())],
    }


def _row_context(r: dict) -> set:
    """A row's searchable words besides its name: tags, themes, stems."""
    meta = r.get("meta") or {}
    return tm.searchable(r.get("taglist") if "taglist" in r else r.get("tags"),
                         meta.get("themes"), meta.get("stems"))


def api_collection_items(params: dict) -> dict:
    P = _Params(params)
    q_raw = P.s("q").strip()
    q = q_raw.lower()
    store = P.s("store")
    group = P.s("group")
    cat = P.s("cat")
    avail_f = P.s("avail")
    page, size = P.page_size(24)
    query = _query(q_raw, P)
    if query.empty:
        # every token was dropped (single letters / punctuation) -- such a
        # query must match nothing, never the whole library
        return P.done({"total": 0, "exact": 0, "mode": "and", "page": page,
                       "size": size, "q": q, "items": []})

    from urllib.parse import quote as _q
    all_rows = _coll_rows()
    store = P.pick("store", store, {r["store"] for r in all_rows})
    group = P.pick("group", group, {r["type_group"] for r in all_rows})
    cat = P.pick("cat", cat, {r["type_cat"] for r in all_rows})
    # items show availability 'owned-not-downloaded': accept that spelling
    avail_f = {"local": "local", "notdown": "notdown",
               "owned-not-downloaded": "notdown",
               "not-downloaded": "notdown"}.get(avail_f.strip().lower(),
                                                avail_f.strip())
    if avail_f not in ("", "local", "notdown"):
        P.warn(f"avail={avail_f!r} is not local or notdown: ignored")
        avail_f = ""
    rows = []
    for r in all_rows:
        if store and r["store"] != store:
            continue
        if group and r["type_group"] != group:
            continue
        if cat and r["type_cat"] != cat:
            continue
        if avail_f == "local" and r["availability"] != "local":
            continue
        if avail_f == "notdown" and r["availability"] == "local":
            continue
        rows.append(r)
    name_of = lambda r: r["name"] or ""                       # noqa: E731
    matched, mode = _match_rows(rows, query, name_of, _row_context,
                                COLL_CLUSTERS)
    if mode == "and":
        matched.sort(key=lambda e: (e["tier"], -e["score"], e["r"]["store"],
                                    (e["r"]["name"] or "").lower()))
    items = []
    lo = (page - 1) * size
    for e in matched[lo:lo + size]:
        r = e["r"]
        items.append({
            "id": r["id"], "name": r["name"], "store": r["store"],
            "group": r["type_group"], "cat": r["type_cat"], "sub": r["type_sub"],
            "seller": r["seller"], "price": r["price"], "url": r["url"],
            "image_count": r["image_count"],
            "availability": r.get("availability") or "",
            "thumb": "/cimg?path=" + _q(_thumb_path(r)) if _thumb_path(r) else "",
            "tier": e["tier"]})
    total = len(matched)
    resp = {"total": total, "exact": total if mode == "and" else 0,
            "mode": mode, "page": page, "size": size, "q": q, "items": items}
    _suggest(resp, query, rows, name_of,
             matched[0]["tier"] if matched else 0)
    return P.done(resp)


def api_collection_item(params: dict) -> dict:
    try:
        iid = int((params.get("id") or ["0"])[0])
    except ValueError:
        return {"error": "bad id"}
    from urllib.parse import quote as _q
    rows = _coll_rows()
    r = next((x for x in rows if x["id"] == iid), None)
    if r is None:
        return {"error": "not found"}
    images = []
    folder = r["folder"] or ""
    if folder and Path(folder).is_dir():
        try:
            images = sorted(p.as_posix() for p in Path(folder).iterdir()
                            if p.is_file() and p.suffix.lower() in IMG_EXTS)
        except OSError:
            images = []
    r["images"] = ["/cimg?path=" + _q(i) for i in images]
    r["image_paths"] = images
    if r.get("thumb_override") and not Path(r["thumb_override"]).is_file():
        r["thumb_override"] = None   # file vanished since the choice
    r["thumb_resolved"] = _thumb_path(r)
    r["availability"] = r.get("availability") or ""
    meta = r.get("meta") or {}
    r["themes"] = meta.get("themes") or []
    r["facets"] = meta.get("facets") or {}
    try:
        r["tags"] = json.loads(r["tags"] or "[]")
        if not isinstance(r["tags"], list):
            r["tags"] = []
    except ValueError:
        r["tags"] = []
    r.pop("taglist", None)
    r.pop("meta", None)
    return r


def api_collection_tag(params: dict, body: dict) -> dict:
    """Add or remove a tag on one collection item (human curation)."""
    try:
        iid = int(body.get("id") or 0)
    except (TypeError, ValueError):
        return {"error": "bad id"}
    tag = re.sub(r"[^a-z0-9 -]", "", (body.get("tag") or "").lower()).strip()
    mode = body.get("mode") or "add"
    if not tag or mode not in ("add", "remove"):
        return {"error": "bad tag"}
    conn = db.connect(_DB_PATH)
    row = conn.execute("SELECT tags FROM collection WHERE id=?", (iid,)).fetchone()
    if row is None:
        conn.close()
        return {"error": "not found"}
    try:
        tags = json.loads(row["tags"] or "[]")
    except ValueError:
        tags = []
    if mode == "add" and tag not in tags:
        tags.append(tag)
    if mode == "remove" and tag in tags:
        tags.remove(tag)
    conn.execute("UPDATE collection SET tags=? WHERE id=?",
                 (json.dumps(tags, ensure_ascii=False), iid))
    conn.commit()
    conn.close()
    return {"ok": True, "tags": tags}


# ---------------------------------------------------------------------------
# textures (Textures_Materials) -- third main section
# ---------------------------------------------------------------------------

TEX_CLUSTERS = [
    {"wall", "walls", "brick", "bricks", "plaster", "stucco", "facade",
     "concrete", "cement", "masonry", "bunker"},
    {"wood", "planks", "timber", "parquet", "bark", "shingles", "floor",
     "flooring"},
    {"metal", "steel", "rust", "rusted", "iron", "copper", "bronze",
     "galvanized", "treadplate"},
    {"ground", "dirt", "soil", "sand", "gravel", "desert", "terrain",
     "farmland", "petrified"},
    {"grass", "moss", "plant", "plants", "foliage", "leaves", "ivy",
     "hedge", "flower", "trees"},
    {"fabric", "cloth", "leather", "textile", "silk", "wool", "carpet",
     "wicker", "blanket", "jeans"},
    {"stone", "marble", "granite", "tiles", "cobblestone", "pavement",
     "slate"},
    {"water", "ice", "snow", "rain", "raindrops", "frost", "dune", "dunes"},
    {"destruction", "debris", "rubble", "damaged", "broken", "cracked",
     "bones", "scrapyard", "stain", "grunge", "smudge"},
    {"scifi", "sci", "panel", "futuristic", "military"},
    {"medieval", "ancient", "old"},
    {"sky", "skies", "cloud", "night", "skybox", "moon"},
    {"road", "roads", "asphalt", "street", "manhole", "city", "building",
     "highrise", "roof", "roofing", "doors", "windows", "shops"},
    {"glass", "backlit", "window"},
    {"decal", "decals", "overlay", "poster", "posters", "pixel", "art"},
]


def _tex_rows() -> list:
    conn = db.connect(_DB_PATH)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM textures ORDER BY grp, sub, name")]
    except sqlite3.OperationalError:
        rows = []       # table not created yet (importer skipped) -> empty
    finally:
        conn.close()
    for r in rows:
        for key in ("tags", "meta", "files", "images"):
            try:
                r[key] = json.loads(r.get(key) or "[]")
            except (ValueError, TypeError):
                r[key] = []
    return rows


def api_textures_tree() -> dict:
    rows = _tex_rows()
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["grp"], {"n": 0, "subs": {}})
        g["n"] += 1
        g["subs"][r["sub"]] = g["subs"].get(r["sub"], 0) + 1
    return {"total": len(rows),
            "groups": [{"name": k, "n": v["n"],
                        "subs": [{"name": s, "n": n}
                                 for s, n in sorted(v["subs"].items())]}
                       for k, v in sorted(groups.items())]}


def api_textures_items(params: dict) -> dict:
    P = _Params(params)
    q_raw = P.s("q").strip()
    q = q_raw.lower()
    grp = P.s("group")
    sub = P.s("sub")
    res = P.s("res", "resolution").strip().upper()
    if res.isdigit():                     # res=4096 means the 4K label
        res = textures_import._res_label(int(res))
    min_px = P.num("min_px", 0.0)
    has = [h.strip() for h in P.s("has").lower().split(",") if h.strip()]
    page, size = P.page_size(24)
    query = _query(q_raw, P)
    if query.empty:
        return P.done({"total": 0, "exact": 0, "mode": "and", "page": page,
                       "size": size, "q": q, "items": []})
    from urllib.parse import quote as _q
    all_rows = _tex_rows()
    grp = P.pick("group", grp, {r["grp"] for r in all_rows})
    sub = P.pick("sub", sub, {r["sub"] for r in all_rows})
    if res:
        res = P.pick("res", res, {(r["meta"] or {}).get("res") for r in all_rows
                                  if isinstance(r["meta"], dict)}).upper()
    if has:
        roles = set()
        for r in all_rows:
            if isinstance(r["meta"], dict):
                roles.update((r["meta"].get("maps") or {}).keys())
        for h in has:
            if h not in roles:
                P.warn(f"has={h!r}: no set has that map role; roles: "
                       + ", ".join(sorted(roles)))
    rows = []
    unmeasured = 0
    for r in all_rows:
        if grp and r["grp"] != grp:
            continue
        if sub and r["sub"] != sub:
            continue
        meta = r["meta"] if isinstance(r["meta"], dict) else {}
        if res and (meta.get("res") or "").upper() != res:
            continue
        if min_px:
            # a pixel filter is a MEASUREMENT query: unmeasured sets are
            # excluded and counted, never passed through
            px = meta.get("px")
            if not (isinstance(px, list) and px):
                unmeasured += 1
                continue
            if max(px) < min_px:
                continue
        if has and not all(h in (meta.get("maps") or {}) for h in has):
            continue
        rows.append(r)
    name_of = lambda r: r["name"] or ""                       # noqa: E731
    matched, mode = _match_rows(rows, query, name_of, _row_context,
                                TEX_CLUSTERS)
    if mode == "and":
        matched.sort(key=lambda e: (e["tier"], -e["score"], e["r"]["grp"],
                                    e["r"]["sub"],
                                    (e["r"]["name"] or "").lower()))
    items = []
    lo = (page - 1) * size
    for e in matched[lo:lo + size]:
        r = e["r"]
        meta = r["meta"] if isinstance(r["meta"], dict) else {}
        entry = {
            "id": r["id"], "name": r["name"], "grp": r["grp"], "sub": r["sub"],
            "image_count": len(r["images"]),
            "res": meta.get("res") or "",
            "thumb": "/timg?path=" + _q(r["thumb"]) if r["thumb"] else "",
            "tier": e["tier"]}
        if meta.get("px"):
            entry["px"] = meta["px"]              # measured, not the folder name
        if meta.get("maps"):
            entry["maps"] = sorted(meta["maps"])  # roles; paths: /api/textures/item
        # the set's folder: a scene manifest's textures[] needs it
        entry["folder"] = _lib_rel(r["folder"] or "")
        items.append(entry)
    total = len(matched)
    resp = {"total": total, "exact": total if mode == "and" else 0,
            "mode": mode, "page": page, "size": size, "q": q,
            # a set's folder is root + "/" + folder
            "root": CANONICAL_ROOTS[0].as_posix(), "items": items}
    if min_px:
        resp["unmeasured_excluded"] = unmeasured
    _suggest(resp, query, rows, name_of,
             matched[0]["tier"] if matched else 0)
    return P.done(resp)


def api_textures_item(params: dict) -> dict:
    try:
        tid = int((params.get("id") or ["0"])[0])
    except ValueError:
        return {"error": "bad id"}
    from urllib.parse import quote as _q
    rows = _tex_rows()
    r = next((x for x in rows if x["id"] == tid), None)
    if r is None:
        return {"error": "not found"}
    meta = r["meta"] if isinstance(r["meta"], dict) else {}
    r["images"] = ["/timg?path=" + _q(i) for i in r["images"]]
    total_mb = (r.get("bytes") or 0) / 1e6
    r["size_str"] = (f"{total_mb:.0f} MB" if total_mb >= 1
                     else f"{(r.get('bytes') or 0) / 1e3:.0f} KB")
    r["tags"] = [t for t in r["tags"] if t not in
                 ("loose", "files", "4k", "textures", "gumroad")][:16]
    r["res"] = meta.get("res") or ""
    r["px"] = meta.get("px")
    r["res_source"] = meta.get("res_source") or ""
    r["maps"] = meta.get("maps") or {}
    r["desc"] = meta.get("desc") or ""
    r["themes"] = meta.get("themes") or []
    return r


# ---------------------------------------------------------------------------
# audio (Audio_Assets) -- fourth main section
# ---------------------------------------------------------------------------

AUDIO_CLUSTERS = [
    {"explosion", "blast", "boom", "firework", "demolition", "destroy"},
    {"gunshot", "gun", "firearm", "pistol", "rifle", "shotgun", "shoot"},
    {"weapon", "reload", "melee", "sword", "bow", "knife", "blade"},
    {"footstep", "step", "walk", "run", "boot", "sprint"},
    {"ui", "button", "click", "notification", "terminal", "computer",
     "menu", "beep", "interface"},
    {"whoosh", "transition", "riser", "swoosh", "swish", "stinger"},
    {"ambiance", "ambient", "background", "bed", "atmosphere", "room",
     "street", "crowd"},
    {"rain", "thunder", "storm", "snow", "wind", "lightning", "weather"},
    {"water", "fire", "flame", "river", "ocean", "stream", "nature",
     "forest"},
    {"animal", "creature", "monster", "roar", "growl", "bird", "dog",
     "dragon", "beast", "screech"},
    {"voice", "scream", "grunt", "breath", "crowd", "human", "laugh",
     "pain"},
    {"vehicle", "car", "motorcycle", "boat", "aircraft", "train", "tank",
     "engine", "helicopter", "plane", "truck"},
    {"machine", "mechanical", "robot", "steampunk", "device", "motor",
     "servo"},
    {"music", "loop", "stem", "instrument", "guitar", "piano", "drum"},
    {"horror", "creepy", "gore", "magic", "dark", "scary", "haunt"},
    {"impact", "hit", "crash", "smash", "collision", "slam", "punch"},
    {"alarm", "siren", "warning", "buzzer", "emergency", "klaxon"},
    {"scifi", "sci", "drone", "robot", "energy", "laser", "tech"},
    {"foley", "door", "cloth", "paper", "kitchen", "tool", "prop"},
]


def _audio_rows() -> list:
    conn = db.connect(_DB_PATH)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audio ORDER BY cat, sub, name")]
    except sqlite3.OperationalError:
        rows = []       # table not created yet (importer skipped) -> empty
    finally:
        conn.close()
    for r in rows:
        for key in ("tags", "meta"):
            try:
                r[key] = json.loads(r.get(key) or "[]")
            except (ValueError, TypeError):
                r[key] = []
    return rows


def api_audio_tree() -> dict:
    rows = _audio_rows()
    cats: dict = {}
    for r in rows:
        c = cats.setdefault(r["cat"], {"n": 0, "desc": "", "subs": {}})
        c["n"] += 1
        if r.get("desc") and not c["desc"]:
            c["desc"] = r["desc"]
        c["subs"][r["sub"]] = c["subs"].get(r["sub"], 0) + 1
    return {"total": len(rows),
            "cats": [{"name": k, "n": v["n"], "desc": v["desc"],
                      "subs": [{"name": s, "n": n}
                               for s, n in sorted(v["subs"].items())]}
                     for k, v in sorted(cats.items())]}


def api_audio_items(params: dict) -> dict:
    P = _Params(params)
    q_raw = P.s("q").strip()
    q = q_raw.lower()
    cat = P.s("cat")
    sub = P.s("sub")
    min_dur = P.num("min_dur", 0.0) or P.num("min_duration", 0.0)
    max_dur = P.num("max_dur", 0.0) or P.num("max_duration", 0.0)
    page, size = P.page_size(24)
    query = _query(q_raw, P)
    if query.empty:
        return P.done({"total": 0, "exact": 0, "mode": "and", "page": page,
                       "size": size, "q": q, "items": []})
    all_rows = _audio_rows()
    cat = P.pick("cat", cat, {r["cat"] for r in all_rows})
    sub = P.pick("sub", sub, {r["sub"] for r in all_rows})
    rows = []
    for r in all_rows:
        if cat and r["cat"] != cat:
            continue
        if sub and r["sub"] != sub:
            continue
        if min_dur and (r["dur"] or 0) < min_dur:
            continue
        # an unknown duration (0 / NULL) is not "short": it fails max_dur
        if max_dur and not (0 < (r["dur"] or 0) <= max_dur):
            continue
        rows.append(r)
    name_of = lambda r: r["name"] or ""                       # noqa: E731
    matched, mode = _match_rows(rows, query, name_of, _row_context,
                                AUDIO_CLUSTERS)
    if mode == "and":
        matched.sort(key=lambda e: (e["tier"], -e["score"], e["r"]["cat"],
                                    e["r"]["sub"],
                                    (e["r"]["name"] or "").lower()))
    lo = (page - 1) * size
    items = [{"id": e["r"]["id"], "name": e["r"]["name"],
              "cat": e["r"]["cat"], "sub": e["r"]["sub"],
              "dur": e["r"]["dur"], "playable": e["r"]["playable"],
              "rel": e["r"]["rel"], "tier": e["tier"]}
             for e in matched[lo:lo + size]]
    total = len(matched)
    resp = {"total": total, "exact": total if mode == "and" else 0,
            "mode": mode, "page": page, "size": size, "q": q,
            # a sound's file is root + "/" + rel
            "root": AUDIO_ROOT.as_posix(), "items": items}
    _suggest(resp, query, rows, name_of,
             matched[0]["tier"] if matched else 0)
    return P.done(resp)


def api_audio_item(params: dict) -> dict:
    try:
        aid = int((params.get("id") or ["0"])[0])
    except ValueError:
        return {"error": "bad id"}
    rows = _audio_rows()
    r = next((x for x in rows if x["id"] == aid), None)
    if r is None:
        return {"error": "not found"}
    r["relfull"] = (AUDIO_ROOT / r["rel"]).as_posix()
    r["tags"] = [t for t in r["tags"]][:18]
    return r


# ---------------------------------------------------------------------------
# meshes (geometry layer) -- what agents need for scene building
# ---------------------------------------------------------------------------

_EMPTY_RECIPE = {"slots": [], "primary": {}, "resolved": False,
                 "primary_slot": None}

# what matching and a list item need; the heavy JSON columns (recipe,
# materials: ~40 MB on a 12k-mesh library) are read only for the rows that
# are actually returned
# 'SM_Crate_LOD1', 'Tree LOD2'; 'UCX_Wall', 'Rock_Collision'; 'Rock_col'
_LOD_RE = re.compile(r"(?:^|[^a-z0-9])lod[ _-]?(\d{1,2})(?![0-9])", re.I)
_COLLIDER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:ucx|ubx|ucp|usp|collider|collision)(?:[^a-z0-9]|$)",
    re.I)
# a bare 'col' only as a separate word of the RAW name: the prettified
# name splits camelCase, and a model named 'PaperCol' became a collider
_COL_RE = re.compile(r"(?:^|[_.\s-])col(?:[_.\s-]|\d|$)", re.I)
# an advertising billboard is a real 3D model; an impostor is a stand-in
_CARD_RE = re.compile(r"impost[eo]r", re.I)


def _mesh_flags(r: dict) -> None:
    """Rows an agent rarely wants when it asks for the lowest-poly or the
    biggest model: a lower LOD copy, a collision hull, a flat card (a
    2-triangle billboard or decal, an impostor). Read from the names and
    the box."""
    text = f"{r.get('name') or ''} {r.get('object_name') or ''}"
    raw = r.get("object_name") or Path(r.get("fbx") or "").stem
    m = _LOD_RE.search(text)
    r["_lod"] = int(m.group(1)) if m else 0
    # the same model without its LOD word: a LOD1 is a lower copy only
    # where its pack also holds the full one (_mesh_light_rows checks)
    r["_base"] = _LOD_RE.sub(" ", r.get("name") or "").strip().lower()
    r["_collider"] = bool(_COLLIDER_RE.search(text) or _COL_RE.search(raw))
    box = (r.get("bbox_x"), r.get("bbox_y"), r.get("bbox_z"))
    r["_card"] = bool(_CARD_RE.search(text)) or (
        r.get("triangles") is not None and r["triangles"] <= 12
        and None not in box and min(abs(b) for b in box) < 0.001)


_MESH_LIGHT = ("id,name,pack,source,kind,fbx,on_disk,triangles,bbox_x,"
               "bbox_y,bbox_z,max_dim,texture_count,tags,meta")
_MESH_FACT_KEYS = ("pivot", "units", "up_axis", "scale_suspect", "skin", "bones",
                   "category_hint", "lod_level", "lod_group", "variant_group")
_MESH_HEAVY = ("id", "vertices", "submeshes", "materials", "recipe",
               "bbox_min_x", "bbox_min_y", "bbox_min_z",
               "bbox_max_x", "bbox_max_y", "bbox_max_z")
_MESH_SORTS = ("relevance", "name", "max_dim", "-max_dim",
               "triangles", "-triangles")


def _mesh_light_rows() -> list:
    """Every mesh row WITHOUT its heavy JSON columns. Each request used to
    SELECT * and json-decode recipe + materials + texture_files of ALL rows
    (56 MB, ~0.7 s) before filtering -- for an answer of 8 items."""
    conn = db.connect(_DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
        if not cols:
            return []       # table not created yet (importer skipped)
        wired = ("(instr(recipe,'\"resolved\":true')"
                 "+instr(recipe,'\"resolved\": true'))>0"
                 if "recipe" in cols else "0")
        obj = "object_name" if "object_name" in cols else "NULL"
        facts = "facts" if "facts" in cols else "NULL"
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_MESH_LIGHT},{obj} AS object_name,{wired} AS wired,"
            f"{facts} AS facts FROM meshes ORDER BY pack, name")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    for r in rows:
        for key, kind in (("tags", list), ("meta", dict), ("facts", dict)):
            try:
                v = json.loads(r.get(key) or "null")
            except (ValueError, TypeError):
                v = None
            r[key] = v if isinstance(v, kind) else kind()
        # the object's own name is a name too: agents paste
        # 'KB3D_XYZ_PropFuseBox_A_grp' or 'SM_Wagon', and the prettified
        # name alone lacked their prefix and code words
        obj = r.get("object_name")
        if obj and obj != r["name"]:
            r["_mname"] = f"{r['name']} {obj}"
    _mesh_keys(rows)
    look = library_map.Lookup(_tag_rows())
    for r in rows:
        r["_ptag"] = look(r["_file"])
        _mesh_flags(r)
    full = {(r["pack"], r["_base"]) for r in rows if not r["_lod"]}
    for r in rows:
        if r["_lod"] and (r["pack"], r["_base"]) not in full:
            r["_lod"] = 0          # its only copy: it IS the model
    lods: dict = {}
    for r in rows:
        if r["_lod"]:
            k = (r["pack"], r["_base"])
            lods[k] = lods.get(k, 0) + 1
    for r in rows:
        r["_lods"] = 0 if r["_lod"] else lods.get((r["pack"], r["_base"]), 0)
        # a flat quad is flagged `card` but only an impostor is ranked
        # down: the same rule marks real 2-triangle floors and wall planes
        r["_junk"] = bool(r["_lod"] or r["_collider"] or _CARD_RE.search(
            f"{r.get('name') or ''} {r.get('object_name') or ''}"))
    return rows


def _mesh_heavy(ids: list) -> dict:
    """id -> heavy columns, for the handful of rows being returned."""
    out: dict = {}
    if not ids:
        return out
    conn = db.connect(_DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
        want = [c for c in _MESH_HEAVY if c in cols]
        for i in range(0, len(ids), 400):          # SQLite variable limit
            chunk = ids[i:i + 400]
            for r in conn.execute(
                    f"SELECT {','.join(want)} FROM meshes WHERE id IN "
                    f"({','.join('?' * len(chunk))})", chunk):
                out[r["id"]] = dict(r)
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    for h in out.values():
        try:
            h["materials"] = json.loads(h.get("materials") or "[]")
        except (ValueError, TypeError):
            h["materials"] = []
        try:
            h["recipe"] = json.loads(h.get("recipe") or "null")
        except (ValueError, TypeError):
            h["recipe"] = None
        if not isinstance(h["recipe"], dict):
            h["recipe"] = dict(_EMPTY_RECIPE)
    return out


def _lib_rel(path: str) -> str:
    """Library-relative spelling of a file path (forward slashes); the
    normalised absolute path for a file outside every library root. The
    registry holds three spellings of one root ('D:/Lib\\\\x', 'D:\\\\Lib\\\\x',
    'D:/Lib/x'), so nothing may compare raw paths."""
    p = (path or "").replace("\\", "/")
    low = p.lower()
    for root in CANONICAL_ROOTS:
        base = root.as_posix().rstrip("/") + "/"
        if low.startswith(base.lower()):
            return p[len(base):]
    return p


def _mesh_keys(rows: list) -> None:
    """Give every row its stable `key`: the library-relative file path,
    plus '#<object name>' when that file holds more than one indexed
    object (a .blend or a multi-object FBX). Ids are positional and change
    at every import, and (pack, name) is ambiguous for some rows; this
    pair is unique and survives rebuilds and a move of the library."""
    count: dict = {}
    for r in rows:
        r["_file"] = _lib_rel(r["fbx"])
        k = r["_file"].lower()
        count[k] = count.get(k, 0) + 1
    for r in rows:
        r["container"] = count[r["_file"].lower()] > 1
        r["key"] = (r["_file"] + "#" + (r.get("object_name") or r["name"] or "")
                    if r["container"] else r["_file"])


def _mesh_item(r: dict, tier: int) -> dict:
    """The SHORT list item: what an agent needs to choose.
    The material recipe is 60-80% of a full item and only matters for the
    few meshes that get picked -- ask for it with detail=1 or
    /api/meshes/item?key=...

    A third of the item used to repeat itself: `fbx` was `key` with the
    library root in front, `max_dim_m` and `height_m` are read off
    `bbox_m` (Z-up: z is the height), `id` changes at every import and
    `on_disk` was 1 on every row. The answer carries `root` once; full
    records carry `fbx` and `id`. About 370 bytes (~90 tokens)."""
    item = {
        "key": r["key"], "name": r["name"], "pack": r["pack"],
        "source": r["source"], "kind": r["kind"],
        "bbox_m": [r["bbox_x"], r["bbox_y"], r["bbox_z"]],   # Z-up
        "triangles": r["triangles"],
        "texture_count": r["texture_count"],
        "wired": bool(r.get("wired")),    # a resolved material recipe exists
        "themes": r["meta"].get("themes") or [],
        "tier": tier,
    }
    if r["_file"] and r["_file"] == (r["fbx"] or "").replace("\\", "/"):
        # outside every library root (a scanned outside folder): the key is
        # already the absolute path, so root + key does not apply
        item["fbx"] = r["_file"]
    if not r["on_disk"]:
        # the index lists the file, but it was not there when indexed
        item["missing_file"] = True
    if r.get("_lod"):
        item["lod"] = r["_lod"]           # a lower-detail copy (LOD0 = full)
    if r.get("_collider"):
        item["collider"] = True           # a collision hull, not a model
    if r.get("_card"):
        item["card"] = True               # flat: billboard, decal, floor quad
    if r.get("container"):
        # the file holds several objects: this one's exact name is needed
        # to import it (names above are prettified)
        item["container"] = True
        item["object_name"] = r.get("object_name") or r["name"]
    if (r["meta"].get("facets") or {}).get("dim_suspect"):
        item["dim_suspect"] = True
    f = r.get("facts") or {}
    if f.get("pivot"):
        item["pivot"] = f["pivot"]        # where the origin sits in the box
    if f.get("units"):
        item["units"] = f["units"]        # the source file's unit
    if f.get("up_axis"):
        item["up_axis"] = f.get("geo_up_axis") or f["up_axis"]
    if f.get("skin") or r["kind"] == "SkeletalMesh":
        item["skin"] = True               # a skinned character
        if f.get("bones"):
            item["bones"] = f["bones"]
    if f.get("scale_suspect"):
        item["scale_suspect"] = True      # over 60 m or under 1 cm
    if f.get("category_hint"):
        item["category"] = "/".join(f["category_hint"])
    if r.get("_lods"):
        item["lods"] = r["_lods"]         # lower-detail copies in the pack
    render = sheets.tile_url(r["key"])
    if render:
        item["render"] = render           # a 256 px picture of this model
    pt = r.get("_ptag")             # the owner's pack tags, when set
    if pt and pt["styles"]:
        item["styles"] = pt["styles"]
    if pt and pt["tags"]:
        item["pack_tags"] = pt["tags"]
    return item


def _mesh_full(r: dict, tier: int, heavy: dict) -> dict:
    """The full item: short item + recipe, material names, placement
    corners, the absolute `fbx`, the registry `id` and `view_url`."""
    h = heavy or {}
    item = _mesh_item(r, tier)
    mats = h.get("materials")
    fbx = (r["fbx"] or "").replace("\\", "/")     # one spelling, not three
    item.update({
        "id": r["id"], "fbx": fbx,
        "vertices": h.get("vertices"), "submeshes": h.get("submeshes"),
        "materials": mats if isinstance(mats, list) else [],
        "object_name": r.get("object_name") or None,
        "format": Path(r["fbx"] or "").suffix.lower().lstrip("."),
        "view_url": "/viewer?file=" + url_quote(fbx),
        **_mesh_extras({**r, **h}),
    })
    return item


def _mesh_extras(r: dict) -> dict:
    """Manifest-derived fields added 2026-09-16 (additive to the mesh item).

    `hero_textures` keeps its old shape (role -> absolute map path) but is now
    read out of the real per-slot wiring instead of guessed from the flat
    `texture_files` list, which gave every mesh of a pack the same recipe (e.g.
    one inspected mansion pack: its first eight meshes all resolved to the
    same debris texture). A mesh
    whose wiring did not resolve gets {} -- an absent recipe, not a wrong one.
    `recipe` is the full per-slot truth; `bbox_min`/`bbox_max` are world-space
    placement corners in metres, or None when the manifest carried no box.
    """
    recipe = r.get("recipe")
    if not isinstance(recipe, dict):
        recipe = dict(_EMPTY_RECIPE)
    hero = {}
    if recipe.get("resolved"):
        primary = recipe.get("primary") or {}
        for role, path in primary.items():
            if role in ("packed_channels", "other", "mask"):
                continue          # not a named material map
            hero[role] = path
        if "opacity" in hero:
            hero["alpha"] = hero["opacity"]      # legacy bucket name
        if "packed" in hero and primary.get("packed_channels"):
            hero["packed_channels"] = primary["packed_channels"]
    meta = r.get("meta") or {}
    return {
        "dim_suspect": bool((meta.get("facets") or {}).get("dim_suspect")),
        "recipe": recipe, "hero_textures": hero,
        "bbox_min": _bbox_triple(r, "bbox_min"),
        "bbox_max": _bbox_triple(r, "bbox_max")}


def _bbox_triple(r: dict, prefix: str):
    """[x, y, z] for bbox_min/bbox_max, or None if the manifest had no box."""
    vals = [r.get(f"{prefix}_{ax}") for ax in ("x", "y", "z")]
    return None if all(v is None for v in vals) else vals


def _spread_packs(entries: list) -> None:
    """Order AND matches by relevance, then deal them out pack by pack.

    Results used to be sorted by pack name: weak matches from an early
    pack came before exact ones, and for 'wall' 44 of the first 50 came
    from one pack out of 945 matches. Within a tier every pack now
    contributes its best match first, then its second best, and so on."""
    entries.sort(key=lambda e: (e["tier"], e["r"].get("_junk", False),
                                -e["score"], len(e["r"]["name"] or ""),
                                (e["r"]["name"] or "").lower(),
                                e["r"]["pack"] or ""))
    # lower LODs, collision hulls and flat cards are dealt out after
    # every real model of the tier, not as a pack's first turn
    seen: dict = {}
    for e in entries:
        junk = e["r"].get("_junk", False)
        k = (e["tier"], junk, e["r"]["pack"])
        e["turn"] = seen.get(k, 0)
        seen[k] = e["turn"] + 1
    entries.sort(key=lambda e: (e["tier"], e["r"].get("_junk", False),
                                e["turn"]))                 # stable


def api_meshes(params: dict) -> dict:
    """Geometry-aware mesh search.

    Filters: q (name words, stem-aware, '-word' excludes; the owner's pack
    tags count as words), pack, source, kind, theme, style (the owner's
    pack tags: Photorealistic, Stylized, Low Poly; a comma list needs
    all), min_dim/max_dim (metres, on the largest axis),
    min_h/max_h (bbox Z: the registry is Z-up), max_tri (triangle
    budget), textures=1 (only textured), folder (a library map folder:
    models below it), pivot (base-centre | corner |
    centre | top | outside | other: where the origin sits), skin=1|0
    (skinned characters only / none), lod=0 (no lower-detail copies),
    units (cm | m | in ...: the source file's unit), sort, page, size.
    Items are SHORT by default; detail=1 adds recipe, material names and
    corners. Size and triangle filters exclude rows without that
    measurement (unknown triangles are NULL) and disclose the count as
    `unmeasured_excluded`; pivot and units filters exclude unmeasured
    rows the same way.
    """
    P = _Params(params)
    q_raw = P.s("q").strip()
    q = q_raw.lower()
    pack = P.s("pack")
    source = P.s("source")
    kind = P.s("kind")
    theme = P.s("theme", "themes")
    want_styles = []
    for s in P.s("style", "styles").split(","):
        if s.strip():
            m = library_map.marker(s)
            if not m:
                P.warn(f"style={s.strip()!r} is not a style; use one of: "
                       f"{', '.join(library_map.MARKERS)} (0 results)")
            want_styles.append(m)
    min_dim, max_dim = P.num("min_dim", 0.0), P.num("max_dim", 1e9)
    min_h, max_h = P.num("min_h", 0.0), P.num("max_h", 1e9)
    max_tri = P.num("max_tri", 1e12)
    only_tex = P.flag("textures")
    want_folder = P.s("folder").replace("\\", "/").strip("/").lower()
    want_pivot = P.s("pivot").strip().lower()
    want_skin = P.s("skin").strip().lower()
    want_units = P.s("units").strip().lower()
    lod0_only = P.s("lod").strip() == "0"
    detail = P.flag("detail")
    sort = P.s("sort")
    page, size = P.page_size(20)
    if sort and sort not in _MESH_SORTS:
        P.warn(f"sort={sort!r} is not supported and was ignored; "
               f"use one of: {', '.join(_MESH_SORTS)}")
        sort = ""

    query = _query(q_raw, P)
    if query.empty:
        # every token was dropped -- match nothing, never the whole library
        return P.done({"total": 0, "exact": 0, "mode": "and", "page": page,
                       "size": size, "q": q,
                       "root": CANONICAL_ROOTS[0].as_posix(), "items": []})

    # a dimension/height/triangle filter is a MEASUREMENT query: rows
    # without that measurement must be EXCLUDED, never silently passed
    # through (unmeasured assets with max_dim 0 once won every size filter)
    dim_filter = (min_dim > 0 or max_dim < 1e9)
    h_filter = (min_h > 0 or max_h < 1e9)
    tri_filter = max_tri < 1e12
    unmeasured = 0

    rows = _mesh_light_rows()
    pack = P.pick("pack", pack, {r["pack"] for r in rows})
    kind = P.pick("kind", kind, {r["kind"] for r in rows})
    source = P.pick("source", source, {r["source"] for r in rows})
    pack_l, kind_l, source_l = pack.lower(), kind.lower(), source.lower()
    # theme=: one theme or a comma list (any of them); names as listed by
    # /api/meshes/themes, letter case ignored
    themes = set()
    if theme:
        have = {t for r in rows for t in (r["meta"].get("themes") or [])}
        themes = {P.pick("theme", t, have) for t in theme.split(",")
                  if t.strip()}

    def words_match(r) -> bool:
        """Every query word matches (and no '-word' excludes) -- for rows a
        filter removed before the words were checked."""
        meta, pt = r["meta"], r.get("_ptag")
        name = r.get("_mname") or r["name"] or ""
        ctx = tm.searchable(r["tags"], meta.get("stems"), meta.get("themes"))
        extra = tm.searchable(meta.get("ctx"), pt["words"] if pt else ())
        if tm.excluded(query, name, ctx, extra):
            return False
        return not query or all(tm.hits(query, name, ctx, extra))

    matched = []        # every query word matches
    partial = []        # only some do: served as OR fallback
    filtered = []       # removed by a filter: told about when nothing is left
    unknown = []        # a measurement filter cannot judge them (unmeasured)
    for r in rows:
        meta = r["meta"]
        pt = r.get("_ptag")
        out = bool(
            (pack and (r["pack"] or "").lower() != pack_l)
            or (source and (r["source"] or "").lower() != source_l)
            or (kind and (r["kind"] or "").lower() != kind_l)
            or (themes and not themes & set(meta.get("themes") or []))
            or (want_styles and not (pt and all(s in pt["styles"]
                                                for s in want_styles))))
        ptw = pt["words"] if pt else ()
        if not out and only_tex and not r["texture_count"]:
            out = True
        if not out and want_folder:
            f_low = (r.get("_file") or "").lower()
            if not (f_low == want_folder or f_low.startswith(want_folder + "/")):
                out = True
        facts = r.get("facts") or {}
        fact_unknown = False
        if not out and lod0_only and r.get("_lod"):
            out = True
        skinned = bool(facts.get("skin")) or (r["kind"] or "") == "SkeletalMesh"
        if not out and want_skin in ("1", "true", "yes") and not skinned:
            out = True
        if not out and want_skin in ("0", "false", "no") and skinned:
            out = True
        if not out and want_pivot:
            if not facts.get("pivot"):
                fact_unknown = True
            elif facts["pivot"].lower() != want_pivot:
                out = True
        if not out and want_units:
            if not facts.get("units"):
                fact_unknown = True
            elif facts["units"].lower() != want_units:
                out = True
        if fact_unknown and not out:
            unknown.append(r)
            continue
        if not out and tri_filter and r["triangles"] is not None \
                and r["triangles"] > max_tri:
            out = True
        unmeasured_row = False
        if not out and (dim_filter or h_filter):
            if not r["max_dim"]:
                unmeasured_row = True
            elif not (min_dim <= r["max_dim"] <= max_dim):
                out = True
            # height is Z; a measured zero height fails min_h (a bbox_y
            # guard here once let zero-height rows pass every filter)
            elif h_filter and not (min_h <= (r["bbox_z"] or 0) <= max_h):
                out = True
        if not out and not unmeasured_row and tri_filter:
            if r["triangles"] is None:        # unknown, never 0
                unmeasured_row = True
            elif r["triangles"] > max_tri:
                out = True
        if out or unmeasured_row:
            (filtered if out else unknown).append(r)
            continue
        name = r.get("_mname") or r["name"] or ""
        if not query:
            if not (query.exclude and tm.excluded(
                    query, name, tm.searchable(r["tags"], meta.get("stems"),
                                               meta.get("themes")),
                    tm.searchable(meta.get("ctx"), ptw))):
                matched.append({"r": r, "tier": 0, "score": 0, "_hits": []})
            continue
        ctx = tm.searchable(r["tags"], meta.get("stems"), meta.get("themes"))
        extra = tm.searchable(meta.get("ctx"), ptw)
        if tm.excluded(query, name, ctx, extra):
            continue
        hits = tm.hits(query, name, ctx, extra)
        if all(hits):
            matched.append({"r": r, "tier": tm.tier(hits),
                            "score": sum(hits) + tm.bonus(query, name),
                            "_hits": hits})
        elif any(hits):
            partial.append({"r": r, "tier": 3, "score": sum(hits),
                            "_hits": hits})

    # the unmeasured rows that MATCH: counting every unmeasured row of the
    # library reported hidden rows that had nothing to do with the query
    unmeasured = sum(1 for r in unknown if words_match(r))

    mode = "and"
    if query and not matched and filtered:
        # the words match, the filters removed it: a silent switch to the
        # partial matches below once put sci-fi chairs into a medieval room
        n_out = sum(1 for r in filtered if words_match(r))
        if n_out:
            used = ", ".join(
                f"{k}={v[0]}" for k, v in params.items()
                if k in _MESH_FILTERS and v and v[0]
                and v[0].strip().lower() not in ("0", "false", "no", "off"))
            P.warn(f"{n_out} model(s) match every query word but not your "
                   f"filters ({used}); "
                   + ("the items below match only SOME words"
                      if partial else "nothing passes them"))
    if query and not matched and partial:
        # AND found nothing: the query was a list, not a phrase ("lamp sign
        # hydrant"). Serve rows that hit SOME word, ranked by word rarity.
        matched = partial
        tm.rarity_rank(matched, query)
        mode = "or-fallback"

    # max_dim = largest first (the 0.x meaning), -max_dim = smallest first;
    # triangles = fewest first, -triangles = most first. A sort orders the
    # matches WITHIN each relevance tier (sorted by triangles alone, eleven
    # logs and loaves came before the first cart), lower LODs, collision
    # hulls and flat cards after real models; unmeasured rows come last.
    if sort in ("max_dim", "-max_dim"):
        sign = -1 if sort == "max_dim" else 1
        matched.sort(key=lambda e: (e["tier"], not e["r"]["max_dim"],
                                    e["r"]["_junk"],
                                    sign * (e["r"]["max_dim"] or 0)))
    elif sort in ("triangles", "-triangles"):
        sign = 1 if sort == "triangles" else -1
        matched.sort(key=lambda e: (e["tier"],
                                    e["r"]["triangles"] is None,
                                    e["r"]["_junk"],
                                    sign * (e["r"]["triangles"] or 0)))
    elif sort == "name" or not query:
        matched.sort(key=lambda e: (e["r"]["pack"] or "",
                                    (e["r"]["name"] or "").lower()))
    elif mode == "and":
        _spread_packs(matched)
    # (an OR fallback keeps its rarity order)

    total = len(matched)
    exact = total if mode == "and" else 0
    lo = (page - 1) * size
    window = matched[lo:lo + size]
    not_returned = []
    if detail:
        items, not_returned = _full_items(
            [(e["r"], e["tier"]) for e in window], P)
    else:
        items = [_mesh_item(e["r"], e["tier"]) for e in window]
    resp = {"total": total, "exact": exact, "mode": mode,
            "page": page, "size": size, "q": q,
            # a model's file is root + "/" + key (a container key minus
            # its last '#object'; a key outside the library is absolute)
            "root": CANONICAL_ROOTS[0].as_posix(), "items": items}
    if not_returned:
        resp["not_returned"] = not_returned
    if query and page == 1 and not pack:
        # one pack asked for: purchases elsewhere are not the question
        owned = _owned_not_downloaded(q_raw, want_styles)
        if owned is not None:
            resp["owned_not_downloaded"] = owned
    if query and total > len(items):
        # where the matches are: narrow with pack= instead of paging
        by_pack: dict = {}
        for e in matched:
            by_pack[e["r"]["pack"]] = by_pack.get(e["r"]["pack"], 0) + 1
        resp["packs"] = [{"pack": k, "n": v} for k, v in sorted(
            by_pack.items(), key=lambda kv: (-kv[1], kv[0] or ""))[:10]]
    _suggest(resp, query, rows, lambda r: r["name"] or "",
             matched[0]["tier"] if matched else 0)
    if dim_filter or h_filter or tri_filter or want_pivot or want_units:
        # disclose the hidden set: rows excluded because no measurement
        # exists (the agent should know it is filtering over measured
        # assets only, and how much it cannot see)
        resp["unmeasured_excluded"] = unmeasured
    return P.done(resp)


# purchase groups that are not 3D models an agent can place, or whose
# store listing is gone
_NOT_REQUESTABLE = ("tool", "print", "listing removed")


def _owned_not_downloaded(q_raw: str, styles=()):
    """Purchases the owner has but has not downloaded that match every
    query word and the search's style. Model search lists files on disk
    only, so this is where an agent learns that a match is one download
    away and can ask the owner for it instead of settling or modelling.
    `total: 0` means "checked, none"; None (no block) means the purchase
    list could not be read. Model themes and size filters do not apply:
    they are model facts a purchase does not have yet (most theme names,
    e.g. 'structure' or 'prop', are no words of any purchase)."""
    words = [q_raw]
    if "Stylized" in styles:
        words.append("stylized")
    if "Low Poly" in styles:
        words.append("low poly")
    if "Photorealistic" in styles:
        words.append("-stylized -low-poly")
    q_all = " ".join(w for w in words if w)
    try:
        c = api_collection_items({"q": [q_all], "avail": ["notdown"],
                                  "size": ["500"]})
    except Exception:  # noqa: BLE001 -- no purchase list must not break a model search
        return None
    if c.get("mode") != "and":
        return {"total": 0}
    hits = [i for i in c.get("items") or []
            if not any(w in (i.get("group") or "").lower()
                       for w in _NOT_REQUESTABLE)]
    if not hits:
        return {"total": 0}
    return {"total": len(hits),
            "items": [{"id": i["id"], "name": i["name"], "store": i["store"],
                       "group": i["group"], "image_count": i["image_count"],
                       "url": i["url"]} for i in hits[:3]],
            "more": "/api/collection/items?avail=notdown&q=" + url_quote(q_all)}


_MESH_FILTERS = ("pack", "source", "kind", "theme", "themes", "style",
                 "styles", "min_dim", "max_dim", "min_h", "max_h", "max_tri",
                 "textures", "pivot", "skin", "lod", "units", "folder")
_FULL_BUDGET = 80_000      # bytes of full records per answer (~20k tokens)
_FULL_MAX_KEYS = 50


def _full_items(entries: list, P=None) -> tuple:
    """Full records for (row, tier) pairs, kept under _FULL_BUDGET bytes.

    One building's recipe can be 100 KB; ten of them were a 240 KB answer
    that an agent's tool limit (about 25,000 tokens) turned into an error.
    Records past the budget are not dropped silently: their keys come
    back in `not_returned`, to ask for again."""
    heavy = _mesh_heavy([r["id"] for r, _ in entries])
    items, rest, used = [], [], 0
    for r, tier in entries:
        if rest:
            rest.append(r["key"])
            continue
        item = _mesh_full(r, tier, heavy.get(r["id"]))
        n = len(json.dumps(item, ensure_ascii=False))
        if items and used + n > _FULL_BUDGET:
            rest.append(r["key"])
            continue
        items.append(item)
        used += n
    if rest and P is not None:
        P.warn(f"{len(rest)} full record(s) not returned to keep this answer "
               f"under ~{_FULL_BUDGET // 4000}k tokens: ask again for the keys "
               "in not_returned")
    return items, rest


def api_meshes_item(params: dict) -> dict:
    """Full items (recipe, material names, placement corners) for the
    meshes an agent has CHOSEN: ?key=<key> (repeatable) or ?id=<id>.
    Search results stay short; this is where the recipe lives.

    A key is percent-encoded as a whole in a URL ('#' -> %23). A key that
    arrives cut at its '#' (a client dropped the fragment) names a
    multi-object file: the answer then lists that file's object keys as
    `candidates` instead of a bare 404. An absolute file path is accepted
    as a key."""
    P = _Params(params)
    keys = [k for k in (params.get("key") or []) if k]
    P.used.update(("key", "id"))
    ids = set()
    for v in params.get("id") or []:
        try:
            ids.add(int(v))
        except ValueError:
            P.warn(f"id={v!r} is not a number: ignored")
    if not keys and not ids:
        return {"error": "bad id", "detail": "pass key=<key from a search "
                                               "result> (repeatable) or id="}
    over = keys[_FULL_MAX_KEYS:]
    keys = keys[:_FULL_MAX_KEYS]
    rows = _mesh_light_rows()
    by_key = {r["key"].lower(): r for r in rows}
    by_file: dict = {}
    for r in rows:
        if r.get("container"):
            by_file.setdefault(r["_file"].lower(), []).append(r["key"])
    found, missing, candidates = [], [], {}
    for k in keys:
        norm = _lib_rel(k).lower()
        r = by_key.get(norm)
        if r is None:
            if norm in by_file:
                candidates[k] = by_file[norm][:50]
            missing.append(k)
        elif r not in found:
            found.append(r)
    for r in rows:
        if r["id"] in ids and r not in found:
            found.append(r)
    if not found:
        out = {"error": "not found", "missing": missing}
        if candidates:
            out["candidates"] = candidates
            out["detail"] = ("a key that names a multi-object file was cut at "
                             "its '#': percent-encode the whole key (%23)")
        return P.done(out)
    items, rest = _full_items([(r, 0) for r in found], P)
    out = {"items": items}
    if over:
        P.warn(f"at most {_FULL_MAX_KEYS} keys per call: {len(over)} not "
               "looked up (in not_returned)")
    if rest or over:
        out["not_returned"] = rest + over
    if missing:
        out["missing"] = missing
    if candidates:
        out["candidates"] = candidates
    return P.done(out)


def api_meshes_packs() -> dict:
    """Exact pack names + counts -- discover before filtering by pack.

    `slots_resolved` / `slots_total` / `wiring_ratio` are the pack's material
    wiring health as reported by its export manifest (Leartes) or derived from
    the `<material>_<suffix>` texture join (KitBash3D). `wiring_method` says
    which of those produced the number, and keeps "absent" apart from "present
    but not measured yet":
      "ue-param"     manifest present, with an engine-resolved `wiring` block
      "basename"     KitBash3D: derived from the `<material>_<suffix>` join
      "v1-manifest"  manifest file present, but it carries no `wiring` block
                     (a pre-relink export) -- ratio None, never fabricated
      "no-manifest"  no manifest file at all (CGTrader, TurboSquid, ...)
    The label is written by the importer (meshes_import), so a v1 manifest can
    no longer be mistaken for a pack that has no manifest on disk.

    `purchases` lists every purchase record whose folder holds files of the
    pack: its store images are the cheapest way to see what a pack looks
    like (/api/collection/item?id=...). Several purchases can share one
    folder, so it is a list, never a guess.
    """
    conn = db.connect(_DB_PATH)
    rows = [dict(r) for r in conn.execute(
        "SELECT pack, source, COUNT(*) AS n, MAX(max_dim) AS tallest "
        "FROM meshes GROUP BY pack, source ORDER BY pack")]
    # the distinct folders of each pack's files (one sample file named only
    # the first sub-pack of a pack that spans several purchases)
    folders: dict = {}
    for pk, fbx in conn.execute("SELECT DISTINCT pack, fbx FROM meshes"):
        d = (fbx or "").replace("\\", "/").rsplit("/", 1)[0].lower()
        folders.setdefault(pk, set()).add(d)
    wiring: dict = {}
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='mesh_pack_wiring'").fetchone():
        wiring = {r["pack"]: dict(r) for r in conn.execute(
            "SELECT pack, slots_total, slots_resolved, wiring_ratio, method "
            "FROM mesh_pack_wiring")}
    try:
        bought = [dict(r) for r in conn.execute(
            "SELECT id, name, image_count, asset_path FROM collection "
            "WHERE asset_path IS NOT NULL AND asset_path != ''")]
    except sqlite3.OperationalError:
        bought = []         # no purchases table / older schema
    conn.close()
    for r in rows:
        w = wiring.get(r["pack"]) or {}
        r["slots_total"] = w.get("slots_total")
        r["slots_resolved"] = w.get("slots_resolved")
        r["wiring_ratio"] = w.get("wiring_ratio")
        r["wiring_method"] = w.get("method")
    # merge same-name packs that appear once per source (a pack with both
    # 'blend' and 'native' rows is ONE pack to a pack= filter caller)
    merged: dict = {}
    for r in rows:
        m = merged.setdefault(r["pack"], {**r, "sources": [], "n": 0})
        m["sources"].append(r["source"])
        m["n"] += r["n"]
        if r.get("tallest") and (not m.get("tallest")
                                 or r["tallest"] > m["tallest"]):
            m["tallest"] = r["tallest"]
    look = library_map.Lookup(_tag_rows())
    for m in merged.values():
        m["source"] = "+".join(sorted(set(m.pop("sources")))) or None
        dirs = folders.get(m["pack"]) or set()
        styles, ptags = set(), []
        for d in dirs:
            pt = look(_lib_rel(d) + "/f")
            if pt:
                styles.update(pt["styles"])
                ptags += [x for x in pt["tags"] if x not in ptags]
        if styles:
            m["styles"] = [s for s in library_map.MARKERS if s in styles]
        if ptags:
            m["pack_tags"] = ptags
        hits = []
        for b in bought:
            base = (b["asset_path"] or "").replace("\\", "/").rstrip("/").lower()
            if base and any(d == base or d.startswith(base + "/")
                            for d in dirs):
                # the image files themselves: /api/collection/item?id=
                hits.append({"id": b["id"], "name": b["name"],
                             "image_count": b["image_count"]})
        if hits:
            m["purchases"] = hits
    return {"packs": sorted(merged.values(), key=lambda m: m["pack"] or "")}


def api_meshes_themes() -> dict:
    counts: dict = {}
    for r in _mesh_light_rows():
        for t in (r["meta"].get("themes") or []):
            counts[t] = counts.get(t, 0) + 1
    return {"themes": [{"name": k, "n": v}
                       for k, v in sorted(counts.items(),
                                          key=lambda kv: -kv[1])]}


_skel_cache: dict = {}          # (path, size, mtime) -> bone names


def _clip_rows() -> list:
    """Every clip of the animation section as a dict: the measured rows
    of `clips` (duration, fps, frames, root motion, skeleton); [] until
    `pharos.py measure --clips` (ingest runs it) wrote them."""
    prefix = _anim_prefix()
    section = prefix[len("pack::"):]
    conn = db.connect(_DB_PATH)
    rows: list = []
    try:
        if clips_import.has_rows(conn):
            # the configured section's clips only: rows of a renamed or a
            # second folder stay in the table but are not this section
            for r in conn.execute(
                    "SELECT rel, folder, name, bytes, frames, fps, duration_s, "
                    "root_motion, has_mesh, bones, skeleton, family, bone_names "
                    "FROM clips WHERE folder = ? OR substr(folder, 1, ?) = ? ORDER BY rel",
                    (section, len(section) + 1, section + "/")):
                d = dict(r)
                d["pack"] = "pack::" + (d["folder"] or "")
                d["measured"] = True
                rows.append(d)
        return rows
    finally:
        conn.close()


def _pretty_folder(folder: str) -> str:
    last = folder.rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", last).strip() or folder


def _stored_skeletons(rows: list, bodies: dict, root: Path) -> dict:
    """pack_skeletons from the clips table's stored bone names: no file is
    opened for a clip; a body without stored names is read once."""
    body_names = {b: (names if names is not None else skeleton._names(b, _skel_cache))
                  for b, names in bodies.items()}
    out: dict = {}
    for r in rows:
        try:
            names = json.loads(r.get("bone_names") or "[]")
        except ValueError:
            names = []
        if not names:
            continue
        folder = str(root / r["folder"])
        inner = r["rel"][len(r["folder"]) + 1:] if r["rel"].startswith(r["folder"] + "/") else r["rel"]
        sub = inner.split("/")[0] if "/" in inner else ""
        recs = out.setdefault(folder, {})
        sig = r.get("skeleton") or skeleton.signature(names)
        if sig not in recs:
            recs[sig] = {"family": r.get("family") or skeleton.family(names),
                         "bones": len(names), "signature": sig,
                         "subfolders": [], "clips": 0,
                         "bodies": [b for b, bn in body_names.items()
                                    if skeleton.overlap(names, bn) >= skeleton._MATCH
                                    and 4 * len(bn) >= len(names)]}
        if sub not in recs[sig]["subfolders"]:
            recs[sig]["subfolders"].append(sub)
        recs[sig]["clips"] += 1
    return {folder: list(recs.values()) for folder, recs in out.items()}


def _skeletons() -> dict:
    """pack id -> the skeleton(s) its clips use and the character files of
    this library whose bones match (from the stored bone names of the
    clips table; a few dozen file reads for an older index)."""
    rows = _clip_rows()
    if not rows:
        return {}
    conn = db.connect(_DB_PATH)
    try:
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(meshes)")}
            fcol = "facts" if "facts" in cols else "NULL"
            body_rows = conn.execute(
                f"SELECT fbx, {fcol} AS facts FROM meshes WHERE kind='SkeletalMesh' "
                "AND on_disk=1 AND fbx != ''").fetchall()
        except sqlite3.OperationalError:
            body_rows = []         # no mesh table yet
    finally:
        conn.close()
    root = CANONICAL_ROOTS[0]
    bodies: dict = {}
    for r in body_rows:
        try:
            names = (json.loads(r["facts"] or "{}") or {}).get("bone_names")
        except ValueError:
            names = None
        bodies[str(r["fbx"])] = names        # None = read the file once
    rig = _rig_rel()
    if rig:
        bodies.setdefault(str(root / rig), None)
    if all(r.get("measured") for r in rows):
        found = _stored_skeletons(rows, bodies, root)
    else:
        clips: dict = {}
        for r in rows:
            clips.setdefault(str(root / r["folder"]), []).append(str(root / r["rel"]))
        found = skeleton.pack_skeletons(clips, list(bodies), cache=_skel_cache)
    out = {}
    for folder, records in found.items():
        pack = "pack::" + _lib_rel(folder)
        out[pack] = [{"family": rec["family"], "bones": rec["bones"],
                      # a folder-per-clip layout lists hundreds of names
                      "subfolders": rec["subfolders"][:24],
                      "subfolders_total": len(rec["subfolders"]),
                      "clips": rec["clips"],
                      "bodies": sorted(_lib_rel(b) for b in rec["bodies"])}
                     for rec in records]
    return out


def api_anim_tree() -> dict:
    """Left-panel outliner: the Animation subfolders = packs, with clip counts."""
    rows = _clip_rows()
    by: dict = {}
    for r in rows:
        by[r["pack"]] = by.get(r["pack"], 0) + 1
    skel = _skeletons()
    folders = []
    for pack in sorted(by):
        folder = pack[len("pack::"):]
        f = {"pack": pack, "folder": folder, "name": _pretty_folder(folder),
             "clips": by[pack]}
        if pack in skel:
            f["skeletons"] = skel[pack]
        folders.append(f)
    return {"rig_body": _rig_rel() or None,
            "rig_note": "the preview body for Character-Creator-family "
                        "clips. It does NOT fit every clip: each folder "
                        "lists its own `skeletons` (family, bone count and "
                        "the character files in this library whose bones "
                        "match); null = no rig in this library",
            "measured": bool(rows) and all(r.get("measured") for r in rows),
            "folders": folders}


def api_anim_clips(params: dict) -> dict:
    """All animation clips, searched like every other section: every query
    word must match (tier 1 = all as whole words of the clip title, tier 2
    = via folder words, part of a longer word or an alias); only when no
    clip matches every word are single-word matches served (tier 3,
    mode 'or-fallback'). Clips of a RELATED motion ('walk' -> jog, run)
    follow at the end as tier 3 and are not counted in `exact`.

    Measured facts per clip (from `pharos.py measure`): duration_s, fps,
    frames, root_motion (true/false/null), family, bones, has_mesh.
    Filters: min_dur / max_dur (seconds; unmeasured clips are excluded
    and counted in unmeasured_excluded), root_motion=1|0, family=."""
    P = _Params(params)
    q_raw = P.s("q").strip()
    q = q_raw.lower()
    prefix = _anim_prefix()
    # `packs`: pack ids, pipe-separated; the folder spelling the tree also
    # shows ('<section>/Walks') is accepted. An entry that names no pack
    # was dropped silently and the UNFILTERED list came back.
    packs = []
    for p in (x.strip() for x in P.s("packs").split("|")):
        if p:
            packs.append(p if p.startswith("pack::") else "pack::" + p)
    page, size = P.page_size(24)
    min_dur, max_dur = P.num("min_dur", 0.0), P.num("max_dur", 1e9)
    dur_filter = min_dur > 0 or max_dur < 1e9
    want_rm = P.s("root_motion").strip().lower()
    want_family = P.s("family").strip().lower()
    rows = _clip_rows()
    if packs:
        known = {r["pack"] for r in rows}
        for p in packs:
            if p not in known:
                P.warn(f"packs: {p!r} is no animation pack (ids: /api/anim/tree)")
        # none left: match nothing (no pack has the empty id), never all
        packs = [p for p in packs if p in known] or [""]
        rows = [r for r in rows if r["pack"] in packs]
    unmeasured = 0
    if dur_filter or want_rm or want_family:
        kept = []
        for r in rows:
            if dur_filter:
                if r.get("duration_s") is None:
                    unmeasured += 1
                    continue
                if not (min_dur <= r["duration_s"] <= max_dur):
                    continue
            if want_rm in ("1", "true", "yes") and r.get("root_motion") != 1:
                continue
            if want_rm in ("0", "false", "no") and r.get("root_motion") != 0:
                continue
            if want_family and (r.get("family") or "").lower() != want_family:
                continue
            kept.append(r)
        rows = kept

    query = _query(q_raw, P)
    if query.empty:
        # every token was dropped (single letters / punctuation) -- such a
        # query must match nothing, never the whole library
        return P.done({"total": 0, "exact": 0, "mode": "and", "page": page,
                       "size": size, "q": q,
                       "root": CANONICAL_ROOTS[0].as_posix(), "clips": []})
    for r in rows:
        base = r["rel"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        r["folder"] = r["pack"][len(prefix):]
        r["title"] = " ".join(
            {"f": "female", "m": "male"}.get(t, t)
            for t in re.split(r"[-_]+", base))
        # the folder path between the section and the file carries meaning
        # the file name does not ('UN05/Root' = UE5 skeleton, root motion)
        path = r["rel"].rsplit("/", 1)[0] if "/" in r["rel"] else ""
        r["_ctx"] = tm.searchable(tm.stems(path), tm.tokens(path))
    name_of = lambda r: r["title"]                            # noqa: E731
    matched, mode = _match_rows(rows, query, name_of, lambda r: r["_ctx"],
                                glued=True)
    exact = len(matched) if (query and mode == "and") else 0
    if not query:
        exact = len(matched)
    if mode == "and":
        matched.sort(key=lambda e: (e["tier"], -e["score"], e["r"]["folder"],
                                    e["r"]["title"]))
    if query:
        # related motions from the same concept cluster, after the matches
        related = _related(query, ANIM_CLUSTERS)
        have = {id(e["r"]) for e in matched}
        tail = [{"r": r, "tier": 3} for r in rows if id(r) not in have
                and related & tm.name_forms(r["title"])[0]
                and not tm.excluded(query, r["title"], r["_ctx"])]
        tail.sort(key=lambda e: (e["r"]["folder"], e["r"]["title"]))
        matched = matched + tail
    items = []
    lo = (page - 1) * size
    for e in matched[lo:lo + size]:
        r = e["r"]
        key = _preview_key(r["rel"])
        pv = (PREVIEW_DIR / (key + ".webm")).is_file()
        rate = None
        meta = PREVIEW_DIR / (key + ".json")
        if meta.is_file():
            try:
                rate = json.loads(meta.read_text(encoding="utf-8")).get("rate")
            except (OSError, ValueError):
                rate = None
        item = {"rel": r["rel"], "pack": r["pack"], "folder": r["folder"],
                "title": r["title"],
                "name": r["title"],   # alias: every other section uses name
                "bytes": r["bytes"], "tier": e["tier"],
                "preview": pv, "rate": rate, "key": key}
        if r.get("measured"):
            item.update({
                "duration_s": r.get("duration_s"), "fps": r.get("fps"),
                "frames": r.get("frames"),
                "root_motion": (None if r.get("root_motion") is None
                                else bool(r["root_motion"])),
                "family": r.get("family"), "bones": r.get("bones"),
                "has_mesh": bool(r.get("has_mesh"))})
        items.append(item)
    total = len(matched)
    resp = {"total": total, "exact": exact,
            "mode": "or-fallback" if (query and exact == 0 and total) else "and",
            "page": page, "size": size, "q": q,
            # a clip's file is root + "/" + rel
            "root": CANONICAL_ROOTS[0].as_posix(), "clips": items}
    if items:
        # the character files a clip can play on, per folder on this page
        # (the same skeleton match /api/anim/tree lists): finding a body
        # used to take a second call and a guess
        skel = _skeletons()
        resp["bodies"] = {               # keyed like each clip's `pack`
            pk: [{"family": rec["family"], "subfolders": rec["subfolders"],
                  "bodies": rec["bodies"]} for rec in skel[pk]]
            for pk in dict.fromkeys(it["pack"] for it in items)
            if skel.get(pk)}
    if dur_filter:
        resp["unmeasured_excluded"] = unmeasured
    _suggest(resp, query, rows, name_of,
             matched[0]["tier"] if matched else 0)
    return P.done(resp)


def api_folders(params: dict) -> dict:
    """The library map with what was measured under each folder node:
    counts, units, up axis, pivot, LOD and variant groups, the vendor's
    own category words, license file, wiring health, Unity prefabs.
    `folder=` serves one folder with its `facts`."""
    folder = (params.get("folder") or [""])[0].strip()
    conn = db.connect(_DB_PATH)
    try:
        rows = folders_import.rows(conn, folder or None)
    finally:
        conn.close()
    for r in rows:
        r["sheet"] = sheets.sheet_url(r["folder"])
    if folder and rows:
        # the keys behind the sheet's tiles (reading order), one folder at
        # a time: the all-folders answer stays small
        index = sheets.load_index()
        rows[0]["sheet_keys"] = ((index.get(rows[0]["folder"]) or {}).get("keys") or []
                                 if rows[0]["sheet"] else [])
    if folder:
        if not rows:
            return {"error": "not found", "folder": folder}
        return {"folder": rows[0], "root": CANONICAL_ROOTS[0].as_posix()}
    return {"folders": rows, "total": len(rows), "root": CANONICAL_ROOTS[0].as_posix()}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

VIEWER_PAGE = _load_tpl("viewer.html")


ANIM_PAGE = _load_tpl("animation.html")


DASH_PAGE = _load_tpl("dashboard.html")


BATCH_PAGE = _load_tpl("batchrender.html")


class Handler(BaseHTTPRequestHandler):
    # DNS-rebinding guard: without a Host allow-list, any web page the
    # user visits while the server runs can rebind a hostname to
    # 127.0.0.1 and become same-origin -> full read+write of the
    # library. Only loopback names (plus an explicitly configured bind
    # host) are accepted.
    def _allowed_hosts(self) -> set:
        allowed = {"127.0.0.1", "localhost", "::1"}
        try:
            allowed.add(str(self.server.server_address[0]).lower())
        except Exception:
            pass
        return allowed

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        hp = host.rsplit(":", 1)
        name = hp[0].strip("[]")
        port = hp[1] if len(hp) == 2 else ""
        allowed = self._allowed_hosts()
        try:
            my_port = str(self.server.server_address[1])
        except Exception:
            my_port = ""
        return name in allowed and port in ("", my_port)

    def _origin_ok(self) -> bool:
        """CSRF hardening. The Host guard stops DNS-rebinding READS, but a
        web page the user is visiting can still SEND cross-site requests
        to the loopback server. Two browser signals close that:
        - Sec-Fetch-Site (all modern browsers, every request): the UI is
          same-origin by construction, so anything labelled cross-site
          was not initiated by our pages.
        - Origin (attached to every fetch/XHR POST): a foreign origin is
          rejected outright.
        Non-browser clients (curl, agents) send neither header and are
        unaffected."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "same-site", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            o = urlparse(origin)
            name = (o.hostname or "").strip().strip("[]").lower()
            port = str(o.port) if o.port else ""
        except ValueError:
            return False
        if name not in self._allowed_hosts():
            return False
        try:
            my_port = str(self.server.server_address[1])
        except Exception:
            my_port = ""
        return port in ("", my_port)

    def _json_body(self):
        """(body, err) for the JSON-mutating endpoints. Enforces an
        application/json content type: state-changing endpoints must not
        accept the opaque form posts a cross-site <form> can produce."""
        ctype = (self.headers.get("Content-Type") or
                 "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, "content-type must be application/json"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "bad content-length"
        try:
            return json.loads(self.rfile.read(length) or b"{}"), None
        except ValueError:
            return None, "body is not valid JSON"

    def log_message(self, fmt, *args):  # quiet
        pass

    # a client that declares more body than it sends must not park a
    # worker thread forever; and a dropped connection must not dump a
    # traceback per aborted request into the console
    timeout = 60

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _json_item(self, payload):
        """One-record answers: a missing record is a 404 and a bad id a
        400 -- they used to arrive as HTTP 200 with an error body, which
        a caller checking only the status read as success."""
        err = payload.get("error") if isinstance(payload, dict) else None
        self._json(payload, {"not found": 404, "bad id": 400}.get(err, 200))

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")   # live registry data
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")   # server restarts shouldn't strand tabs
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self._write(data)

    def _write(self, data: bytes):
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def do_HEAD(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        # link checkers and curl -I must not see 501 on previews/media
        self._head_only = True
        try:
            self.do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fs_ok(self) -> bool:
        try:
            return setup_flow.fs_allowed(str(self.server.server_address[0]))
        except Exception:                                 # noqa: BLE001
            return False

    def _setup_gate(self, path: str) -> bool:
        """In setup mode only the setup page and its API answer; `/` goes
        there, everything else gets a 503 that says so."""
        if not _SETUP_MODE or path.startswith(_SETUP_OPEN):
            return False
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        self._json({"error": "Pharos is not set up yet: no library indexed",
                    "setup": "/setup"}, 503)
        return True

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        url = urlparse(self.path)
        params = parse_qs(url.query)
        if self._setup_gate(url.path):
            return
        try:
            if url.path == "/setup":
                self._html(SETUP_PAGE.encode("utf-8"))
            elif url.path == "/api/setup/state":
                self._json(setup_flow.api_state("setup" if _SETUP_MODE else "serving"))
            elif url.path == "/api/setup/progress":
                self._json(setup_flow.progress())
            elif url.path == "/api/setup/map":
                self._json(api_pack_tags())
            elif url.path in ("/api/fs/drives", "/api/fs/list"):
                # the machine's folders: loopback binds only, and never for
                # a page from another site (an <img> can carry a UNC path)
                if not self._fs_ok():
                    return self._json({"error": "folder browsing is for the "
                                                "machine the server runs on"}, 403)
                if not self._origin_ok():
                    return self._json({"error": "cross-origin request rejected"}, 403)
                if url.path == "/api/fs/drives":
                    self._json(setup_flow.api_fs_drives())
                else:
                    answer, code = setup_flow.api_fs_list((params.get("path") or [""])[0])
                    self._json(answer, code)
            elif url.path == "/":
                _clips = _clip_rows()
                aclips = len(_clips)
                afold = len({c["pack"] for c in _clips})
                conn = db.connect(_DB_PATH)
                try:
                    apacks = conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
                except sqlite3.OperationalError:
                    apacks = 0
                try:
                    mcount = conn.execute("SELECT COUNT(*) FROM meshes").fetchone()[0]
                    mfolders = conn.execute(
                        "SELECT COUNT(*) FROM folders WHERE meshes > 0 AND include = 1").fetchone()[0]
                except sqlite3.OperationalError:
                    mcount = mfolders = 0
                first_sheet = next((p.name for p in sorted(sheets.sheets_dir().glob("*.png"))
                                    if not p.name.startswith("_")), None) \
                    if sheets.sheets_dir().is_dir() else None
                tcount = conn.execute("SELECT COUNT(*) FROM textures").fetchone()[0]
                acount = conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
                ahours = conn.execute(
                    "SELECT ROUND(SUM(dur)/3600.0, 1) FROM audio").fetchone()[0] or 0
                # hero images: config-curated picks first (pharos_config.json
                # "dashboard"), generic fallbacks (first available image)
                anim_pick = None
                hero = config.DASHBOARD.get("animation_hero") or ""
                if hero:
                    p = Path(hero)
                    anim_pick = p if p.is_file() else None
                asset_img = None
                for cand in config.DASHBOARD.get("asset_hero_names") or []:
                    row = conn.execute(
                        "SELECT first_image FROM collection WHERE name=?",
                        (cand,)).fetchone()
                    if row and row["first_image"]:
                        asset_img = row["first_image"]
                        break
                if not asset_img:
                    row = conn.execute(
                        "SELECT first_image FROM collection "
                        "WHERE first_image IS NOT NULL AND first_image != '' "
                        "ORDER BY id LIMIT 1").fetchone()
                    asset_img = row["first_image"] if row else None
                tex_img = None
                for pat in config.DASHBOARD.get("texture_hero_patterns") or []:
                    row = conn.execute(
                        "SELECT thumb FROM textures WHERE thumb LIKE ? "
                        "ORDER BY id LIMIT 1", (f"%{pat}%",)).fetchone()
                    if row and row["thumb"]:
                        tex_img = row["thumb"]
                        break
                if not tex_img:
                    row = conn.execute(
                        "SELECT thumb FROM textures WHERE thumb "
                        "IS NOT NULL AND thumb != '' ORDER BY id LIMIT 1"
                    ).fetchone()
                    tex_img = row["thumb"] if row else None
                conn.close()
                from urllib.parse import quote as _pq
                html = (DASH_PAGE
                        .replace("{{ASSETS_TILE}}", _ASSETS_TILE if apacks else "")
                        .replace("{{MODELS_THUMB}}",
                                 "/sheet/" + first_sheet if first_sheet
                                 else "/static/heroes/assets.svg")
                        .replace("{{MESH_COUNT}}", f"{mcount:,}")
                        .replace("{{MODEL_FOLDERS}}", str(mfolders))
                        .replace("{{ANIM_THUMB}}",
                                 "/cimg?path=" + _pq(anim_pick.as_posix())
                                 if anim_pick is not None
                                 else "/static/heroes/animation.svg")
                        .replace("{{ASSET_THUMB}}",
                                 "/cimg?path=" + _pq(asset_img)
                                 if asset_img
                                 else "/static/heroes/assets.svg")
                        .replace("{{TEX_THUMB}}",
                                 "/timg?path=" + _pq(tex_img)
                                 if tex_img
                                 else "/static/heroes/textures.svg")
                        .replace("{{ANIM_CLIPS}}", str(aclips))
                        .replace("{{ANIM_FOLDERS}}", str(afold))
                        .replace("{{ASSET_PACKS}}", str(apacks))
                        .replace("{{TEX_COUNT}}", str(tcount))
                        .replace("{{AUDIO_THUMB}}", _audio_hero_uri())
                        .replace("{{AUDIO_COUNT}}", str(acount))
                        .replace("{{AUDIO_HOURS}}", str(ahours)))
                self._html(html.encode("utf-8"))
            elif url.path == "/assets":
                self._html(ASSETS_PAGE.encode("utf-8"))
            elif url.path == "/textures":
                self._html(TEXTURES_PAGE.encode("utf-8"))
            elif url.path == "/audio":
                self._html(AUDIO_PAGE.encode("utf-8"))
            elif url.path == "/tags":
                self._html(TAGS_PAGE.encode("utf-8"))
            elif url.path == "/models":
                self._html(MODELS_PAGE.encode("utf-8"))
            elif url.path.startswith("/sheet/") or url.path.startswith("/render/"):
                name = url.path.rsplit("/", 1)[1]
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.png", name) or ".." in name:
                    return self._json({"error": "bad name"}, 400)
                base = sheets.sheets_dir() if url.path.startswith("/sheet/") else sheets.tiles_dir()
                pfile = base / name
                if not pfile.is_file():
                    return self._json({"error": "not rendered yet",
                                       "render": "python pharos.py previews --sheets"
                                       if url.path.startswith("/sheet/")
                                       else "python pharos.py previews --keys <key>"}, 404)
                stat = pfile.stat()
                etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = pfile.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")     # re-renders replace bytes
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if not getattr(self, "_head_only", False):
                    self.wfile.write(data)
            elif url.path == "/api/pack_tags":
                self._json(api_pack_tags())
            elif url.path == "/animation":
                self._html(_with_rig(ANIM_PAGE).encode("utf-8"))
            elif url.path == "/batchrender":
                self._html(_with_rig(BATCH_PAGE).encode("utf-8"))
            elif url.path == "/api/stats":
                self._json(api_stats())
            elif url.path == "/api/collection/tree":
                self._json(api_collection_tree())
            elif url.path == "/api/collection/items":
                self._json(api_collection_items(params))
            elif url.path == "/api/collection/item":
                self._json_item(api_collection_item(params))
            elif url.path == "/api/textures/tree":
                self._json(api_textures_tree())
            elif url.path == "/api/textures/items":
                self._json(api_textures_items(params))
            elif url.path == "/api/textures/item":
                self._json_item(api_textures_item(params))
            elif url.path == "/api/meshes":
                self._json(api_meshes(params))
            elif url.path == "/api/meshes/item":
                self._json_item(api_meshes_item(params))
            elif url.path == "/api/meshes/packs":
                self._json(api_meshes_packs())
            elif url.path == "/api/meshes/themes":
                self._json(api_meshes_themes())
            elif url.path == "/api/audio/tree":
                self._json(api_audio_tree())
            elif url.path == "/api/audio/items":
                self._json(api_audio_items(params))
            elif url.path == "/api/audio/item":
                self._json_item(api_audio_item(params))
            elif url.path.startswith("/audiofile"):
                # audio bytes, jailed to Audio_Assets; Range supported so the
                # in-page player can seek without downloading 25 MB wavs.
                # The page sends ROOT-RELATIVE paths (from the crawl jsonl).
                target = (params.get("path") or [""])[0]
                t = Path(target)
                if not t.is_absolute():
                    t = AUDIO_ROOT / t
                try:
                    t = t.resolve()
                except OSError:
                    t = None
                if (t is None or not _jailed(t, AUDIO_ROOT)
                        or t.suffix.lower() not in AUDIO_PLAYABLE
                        and t.suffix.lower() not in {".aif", ".aiff"}
                        or not t.is_file()):
                    return self._json({"error": "not found"}, 404)
                ctype = AUDIO_CTYPE.get(t.suffix.lower(), "application/octet-stream")
                size = t.stat().st_size
                rng = self.headers.get("Range")
                start, end = 0, size - 1
                code = 200
                if rng and rng.startswith("bytes="):
                    try:
                        sp = rng[6:].split("-")
                        if sp[0]:                     # "bytes=A-B" / "bytes=A-"
                            start = int(sp[0])
                            end = int(sp[1]) if len(sp) > 1 and sp[1] \
                                else size - 1
                        else:                         # "bytes=-N": LAST N bytes
                            n = int(sp[1]) if len(sp) > 1 and sp[1] else 0
                            start = max(0, size - n)
                            end = size - 1
                        start = max(0, min(start, size - 1))
                        end = max(start, min(end, size - 1))
                        code = 206
                    except ValueError:
                        start, end, code = 0, size - 1, 200
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if code == 206:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if self.command == "HEAD":
                    return
                with t.open("rb") as fh:
                    fh.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = fh.read(min(65536, remaining))
                        if not chunk:
                            break
                        self._write(chunk)
                        remaining -= len(chunk)
            elif url.path == "/api/open_explorer":
                # launch Windows Explorer for a folder (open) or file
                # (select) -- localhost UI convenience, jailed to asset roots.
                # Relative paths resolve against each asset root in turn.
                # State-changing GET: a cross-site page could embed it as
                # an <img> (no Origin header on those), so the Fetch
                # Metadata half of _origin_ok is the guard that matters.
                if not self._origin_ok():
                    return self._json({"error": "cross-site request rejected"}, 403)
                if self.command == "HEAD":
                    # HEAD is a probe (link checkers, curl -I) -- it must
                    # never trigger the side effect
                    return self._json(
                        {"error": "HEAD is not supported on this endpoint"}, 405)
                target = (params.get("path") or [""])[0]
                roots = [AUDIO_ROOT, TEXTURES_ROOT, COLLECTION_ROOT] + \
                    [r.resolve() for r in CANONICAL_ROOTS]
                t = Path(target)
                if not t.is_absolute():
                    for r in roots:
                        cand = (r / t).resolve()
                        if _jailed(cand, r) and cand.exists():
                            t = cand
                            break
                try:
                    t = t.resolve()
                except OSError:
                    t = None
                if t is None or not any(_jailed(t, r) for r in roots):
                    return self._json({"error": "path outside asset roots"}, 403)
                if not t.exists():
                    return self._json({"error": "path does not exist"}, 404)
                if (params.get("dry") or [""])[0] != "1":
                    # platform dispatch: os.startfile/explorer are
                    # Windows-only and used to raise -> HTTP 500 on
                    # macOS/Linux, killing every "show in Explorer" button
                    if sys.platform == "win32":
                        if t.is_dir():
                            os.startfile(str(t))
                        else:
                            subprocess.Popen(
                                ["explorer", "/select," + str(t)])
                    elif sys.platform == "darwin":
                        args = ["open"] + (["-R"] if not t.is_dir() else []) \
                            + [str(t)]
                        subprocess.Popen(args)
                    else:
                        subprocess.Popen(["xdg-open", str(t)])
                return self._json({"ok": True, "abs": t.as_posix()})
            elif url.path.startswith("/timg"):
                # texture previews/maps, jailed to Textures_Materials
                target = (params.get("path") or [""])[0]
                t = Path(target)
                try:
                    t = t.resolve()
                except OSError:
                    t = None
                if (t is None or not _jailed(t, TEXTURES_ROOT)
                        or t.suffix.lower() not in TEX_DISPLAY_EXT
                        or not t.is_file()):
                    return self._json({"error": "not found"}, 404)
                data = t.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 mimetypes.guess_type(str(t))[0] or "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self._write(data)
            elif url.path.startswith("/cimg"):
                # product images of the collection, jailed to Collected Files
                target = (params.get("path") or [""])[0]
                p = Path(target)
                try:
                    p = p.resolve()
                except OSError:
                    p = None
                if (p is None or not _jailed(p, COLLECTION_ROOT)
                        or p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                        or not p.is_file()):
                    return self._json({"error": "not found"}, 404)
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 mimetypes.guess_type(str(p))[0] or "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self._write(data)
            elif url.path == "/api/clips":
                self._json(api_clips(params))
            elif url.path in ("/api/folders", "/api/folders/item"):
                self._json_item(api_folders(params))
            elif url.path == "/api/anim/tree":
                self._json(api_anim_tree())
            elif url.path == "/api/anim/clips":
                self._json(api_anim_clips(params))
            elif url.path.startswith("/preview/"):
                name = url.path[len("/preview/"):]
                if not re.fullmatch(r"[0-9a-f]{20}\.webm", name):
                    return self._json({"error": "bad key"}, 400)
                pfile = PREVIEW_DIR / name
                if not pfile.is_file():
                    return self._json({"error": "not rendered yet"}, 404)
                stat = pfile.stat()
                etag = f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'
                inm = self.headers.get("If-None-Match")
                if inm and inm == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # NOT immutable: a re-render replaces bytes at the same key,
                # so clients must revalidate (304s keep it cheap)
                self.send_response(200)
                self.send_header("Content-Type", "video/webm")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(stat.st_size))
                self.end_headers()
                with pfile.open("rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        self._write(chunk)
            elif url.path == "/viewer":
                self._html(_with_rig(VIEWER_PAGE).encode("utf-8"))
            elif url.path.startswith("/static/"):
                rel = url.path[len("/static/"):]
                static_file = (STATIC_DIR / rel).resolve()
                if (not _jailed(static_file, STATIC_DIR.resolve())
                        or not static_file.is_file()):
                    return self._json({"error": "not found"}, 404)
                self._bytes(static_file.read_bytes(),
                            mimetypes.guess_type(str(static_file))[0]
                            or "application/octet-stream")
            elif url.path == "/file":
                # a model or clip for the 3D viewer (FBX, BVH, OBJ, GLB, glTF
                # under the canonical roots only)
                target = _safe_asset_file((params.get("path") or [""])[0],
                                          VIEWER_EXTS)
                if not target:
                    return self._json({"error": "not found"}, 404)
                self._bytes(target.read_bytes(), "application/octet-stream")
            elif url.path.startswith("/res/"):
                # what the viewer asks for next to a model: textures (FBX,
                # OBJ, glTF), the .mtl of an OBJ, the .bin buffers of a
                # .gltf. The rest of the URL is the library path, in real
                # segments (a browser resolves the file's '../' against them,
                # so a sibling folder works); the jail keeps it under the
                # canonical roots. A texture that is not where the file says
                # is looked up by its name under the model's map folder.
                rel = unquote(url.path[len("/res/"):]).lstrip("/")
                target = _safe_asset_file(rel, RESOURCE_EXTS) or _resource_by_name(rel)
                if not target:
                    return self._json({"error": "not found"}, 404)
                self._bytes(target.read_bytes(),
                            mimetypes.guess_type(str(target))[0]
                            or "application/octet-stream")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request rejected"}, 403)
        params = parse_qs(urlparse(self.path).query)
        try:
            path = urlparse(self.path).path
            if self._setup_gate(path):
                return
            if path in ("/api/setup/survey", "/api/setup/confirm", "/api/setup/map"):
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                if path == "/api/setup/map":
                    answer, code = api_pack_tags_set(body)
                    return self._json(answer, code)
                job = "survey" if path.endswith("survey") else "ingest"
                if not self._fs_ok():
                    # choosing and indexing a library is for the machine the
                    # server runs on, like browsing its folders
                    return self._json({"error": "setup is for the machine the "
                                                "server runs on"}, 403)
                answer, code = setup_flow.begin(job, body or {},
                                                on_configured=_after_survey,
                                                on_indexed=_after_ingest)
                return self._json(answer, code)
            if path == "/api/preview/store":
                # browser-rendered preview loop upload: body is a small webm
                qs = parse_qs(urlparse(self.path).query)
                rel = (qs.get("clip") or [""])[0]
                target = _safe_asset_file(rel, PREVIEW_EXTS)
                length = int(self.headers.get("Content-Length") or 0)
                if not target or length < 1024 or length > 12_000_000:
                    return self._json({"error": "bad request"}, 400)
                data = self.rfile.read(length)
                if not data.startswith(b"\x1a\x45\xdf\xa3"):   # EBML magic
                    return self._json({"error": "not a webm file"}, 400)
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                key = _preview_key(rel)
                (PREVIEW_DIR / (key + ".webm")).write_bytes(data)
                try:
                    rate = float((qs.get("rate") or ["0"])[0])
                except ValueError:
                    rate = 0.0
                if rate > 0:
                    (PREVIEW_DIR / (key + ".json")).write_text(
                        json.dumps({"rate": rate}), encoding="utf-8")
                return self._json({"ok": True, "key": key})
            if path == "/api/collection/thumb":
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                try:
                    iid = int(body.get("id") or 0)
                except (TypeError, ValueError):
                    return self._json({"error": "bad id"}, 400)
                image = (body.get("image") or "").strip()
                conn = db.connect(_DB_PATH)
                row = conn.execute("SELECT folder FROM collection WHERE id=?",
                                   (iid,)).fetchone()
                if row is None:
                    conn.close()
                    return self._json({"error": "not found"}, 404)
                if image:
                    try:
                        target = Path(image).resolve()
                    except OSError:
                        return self._json({"error": "bad path"}, 400)
                    folder = Path(row["folder"]).resolve()
                    if (not _jailed(target, folder)
                            or target.suffix.lower() not in IMG_EXTS
                            or not target.is_file()):
                        return self._json(
                            {"error": "image not in this asset's folder"}, 400)
                # the RESOLVED path: a relative string was stored as sent
                stored = str(target) if image else None
                conn.execute("UPDATE collection SET thumb_override=? WHERE id=?",
                             (stored, iid))
                conn.commit()
                conn.close()
                return self._json({"ok": True, "thumb": stored})
            if path == "/api/collection/tag":
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                return self._json(api_collection_tag(params, body))
            if path == "/api/pack_tags":
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                answer, code = api_pack_tags_set(body)
                return self._json(answer, code)
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def _loopback_warning(host: str) -> Optional[str]:
    """The loud non-loopback-bind warning SECURITY.md promises. None on a
    loopback bind; a plain-language warning paragraph otherwise."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return None
    return (
        f"WARNING: binding to {host} instead of a loopback address.\n"
        "  The Host allow-list accepts ONLY loopback names plus this\n"
        "  exact address: normal LAN browsers (whose Host is the machine's\n"
        "  LAN IP/hostname) will be REJECTED with 403. If you meant to\n"
        "  share Pharos over the network, add that name to the bind host\n"
        "  AND expect anyone who can reach it to read your library\n"
        "  metadata, filenames and thumbnails. When in doubt: 127.0.0.1.")


def _source_files() -> list:
    """The files whose change means the importers must run again."""
    af = config.AGENT_FILES
    files = [config._CONFIG_FILE, library_map.PATH, config.CSV_PATH,
             config.TEX_ROOT / "library_index.json", config.AUDIO_ROOT / "library_files.jsonl",
             config.AUDIO_ROOT / "library_index.json",
             af / "models.jsonl", af / "kb3d_models.jsonl", af / "native_models.jsonl",
             af / "availability.jsonl"]
    # the folders textures_import, audio_import and collection_import walk
    # themselves: a folder's mtime moves when a set or a file is added
    # directly in it, so the folders two levels down see new sets
    for root in (config.TEX_ROOT, config.AUDIO_ROOT, config.COLLECTION_ROOT):
        files += _dirs_below(root, 2)
    for root in config.MANIFEST_ROOTS:
        try:                   # the importer reads manifests at any depth
            files += list(Path(root).rglob("Exports/manifest.json"))
            files += list(Path(root).rglob("Exports/kit_manifest.json"))
        except OSError:
            pass
    return files


def _dirs_below(root, depth: int) -> list:
    """`root` and its sub-folders down to `depth` levels (folders only)."""
    out = []
    root = Path(root)
    if not root.is_dir():
        return out
    out.append(root)
    frontier = [root]
    for _ in range(depth):
        nxt = []
        for d in frontier:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "_")):
                            nxt.append(Path(e.path))
            except OSError:
                continue
        out += nxt
        frontier = nxt
        if len(out) > 5000:
            break
    return out


def _startup_imports(db_path: str, force: bool = False) -> dict:
    """What the server does before it answers: schema, every importer
    (each failing on its own), the folders table, the map, the registry
    stamp, the preview cache. Returns the per-section counts for the
    banner. A registry whose sources did not change since the last start
    (nothing ingested, scanned or measured; no index, map, CSV or config
    edit) is served as it is: a warm start takes well under a second
    instead of every importer again (`serve --rebuild` forces them)."""
    db.init_db(db_path)   # applies schema migrations
    counts = {"collection": 0, "textures": 0, "audio": 0, "meshes": 0}
    sig = db.sources_signature(db_path, _source_files())
    conn = db.connect(db_path)
    served = db.get_meta(conn, "served_signature")
    conn.close()
    if sig == served and not force:
        conn = db.connect(db_path)
        for tbl in ("collection", "textures", "audio", "meshes"):
            try:
                counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[tbl] = 0
        conn.close()
        print("registry up to date -- importers skipped (sources unchanged since "
              "the last start; python pharos.py serve --rebuild runs them again)", flush=True)
    else:
        failed = False
        steps = (("collection", lambda: collection_import.import_collection(db_path), "assets"),
                 ("textures", lambda: textures_import.import_textures(db_path), "sets"),
                 ("audio", lambda: audio_import.import_audio(db_path), "files"),
                 ("meshes", lambda: meshes_import.import_meshes(db_path), "records"))
        for name, fn, unit in steps:
            try:
                counts[name] = fn()
                print(f"{name} imported: {counts[name]} {unit}", flush=True)
            except Exception as exc:  # noqa: BLE001 -- a missing source must not kill the app
                print(f"{name} import skipped: {exc}", flush=True)
                # an absent purchase CSV is a state, not a failure
                if not (name == "collection" and isinstance(exc, FileNotFoundError)):
                    failed = True
        try:
            n_f = folders_import.build(db_path, config.LIBRARY_ROOT)
            print(f"folders: {n_f} node(s) with measured facts", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"folders skipped: {exc}", flush=True)
            failed = True
        try:
            conn = db.connect(db_path)
            # a pass with a failed step is not "served": the next start
            # runs the importers again (an empty signature matches nothing)
            db.set_meta(conn, "served_signature", "" if failed else sig)
            if not db.get_meta(conn, "sources_stamp_at"):
                import time as _time
                db.set_meta(conn, "sources_stamp_at",
                            _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime()))
            conn.close()
        except Exception:                                 # noqa: BLE001
            pass
    try:
        added = _sync_pack_tags()
        if added:
            print(f"library map: {added} new folder(s) listed in "
                  f"{library_map.PATH} (review them at /tags)", flush=True)
    except Exception as exc:  # noqa: BLE001 -- a locked file must not stop the app
        print(f"pack tags not updated: {exc}", flush=True)
    try:
        db.stamp_registry(db_path, config.LIBRARY_ROOT)   # this library's
    except Exception as exc:  # noqa: BLE001
        print(f"registry stamp skipped: {exc}", flush=True)
    # self-heal the preview cache: drop stub/empty recordings and orphaned
    # sidecars so a broken render can never linger as a black thumbnail
    if PREVIEW_DIR.is_dir():
        for junk in PREVIEW_DIR.glob("*.webm"):
            try:
                if junk.stat().st_size < 1024:
                    junk.unlink()
            except OSError:
                pass
        for orphan in PREVIEW_DIR.glob("*.json"):
            if not (PREVIEW_DIR / (orphan.stem + ".webm")).is_file():
                try:
                    orphan.unlink()
                except OSError:
                    pass
    try:
        _c = db.connect(db_path)
        counts["anim_packs"] = db.anim_clip_count(
            _c, config.SECTIONS.get("animation"), packs=True)
        _c.close()
    except Exception:                                     # noqa: BLE001
        counts["anim_packs"] = 0
    # explicit completion feedback: counts + folders nobody indexed, so a
    # human watching the console never wonders whether it worked
    print(f"INGESTION COMPLETE -- collection={counts['collection']}"
          f" textures={counts['textures']} audio={counts['audio']}"
          f" meshes={counts['meshes']} anim_packs={counts['anim_packs']}", flush=True)
    return counts


def _after_survey() -> dict:
    """The setup page surveyed a folder: `init` wrote the config and the
    first map; the roots follow it (config.reload ran) and the map is
    listed for the page."""
    global _SETUP_MODE
    _survey_cache["nodes"], _survey_cache["at"] = {}, 0.0
    # the registry must serve the roots: a new library (no registry yet, or
    # one that belongs to another library) puts the server back into setup
    # mode until its indexing ran; a re-survey of the same library stays live
    if not Path(_DB_PATH).is_file() or db.registry_foreign(_DB_PATH, config.LIBRARY_ROOT):
        _SETUP_MODE = True
    rows = library_map.load()
    return {"rows": len(rows), "pending": [r["folder"] for r in library_map.pending(rows)],
            "mode": "setup" if _SETUP_MODE else "serving"}


def _after_ingest() -> dict:
    """The setup page ran `ingest`: the server leaves setup mode, imports
    what ingest wrote, and reports the verdict and the chains the owner's
    decisions ask for."""
    global _SETUP_MODE
    owner = db.registry_foreign(_DB_PATH, config.LIBRARY_ROOT)
    if owner:
        raise RuntimeError(db.foreign_registry_message(_DB_PATH, owner, config.LIBRARY_ROOT))
    counts = _startup_imports(_DB_PATH)
    _SETUP_MODE = False
    out = setup_flow.doctor_verdict()
    out["counts"] = counts
    out["next"] = setup_flow.next_steps(library_map.load())
    return out


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    global _DB_PATH, _SETUP_MODE
    parser = argparse.ArgumentParser(description="Registry browser (localhost)")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=config.NETWORK["port"])
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--setup", action="store_true",
                        help="open the setup page (pick the library, correct the map, index)")
    parser.add_argument("--rebuild", action="store_true",
                        help="run every importer at start even when no source changed")
    parser.add_argument("--init", action="store_true",
                        help="first-run: detect folders and write pharos_config.json")
    args = parser.parse_args(argv)
    if args.init:
        from asset_service import init as pharos_init
        pharos_init.run_init()
        config.reload()
    host = config.NETWORK["host"]
    if not config.is_configured():
        # SETUP MODE: serve the setup page and nothing else until the
        # owner picked a library and the first ingest ran
        _SETUP_MODE = True
        print("SETUP MODE -- no library configured yet.\n"
              f"  open http://{host}:{args.port}/setup to pick your asset folder,\n"
              "  or run:  python pharos.py init <path-to-your-assets-folder>", flush=True)
    else:
        _DB_PATH = args.db
        # a registry left behind by ANOTHER library: refuse before the
        # importers write into it (they kept its rows, mixed with this one's)
        _owner = db.registry_foreign(_DB_PATH, config.LIBRARY_ROOT)
        if _owner:
            print(db.foreign_registry_message(_DB_PATH, _owner,
                                              config.LIBRARY_ROOT), flush=True)
            return 2
        _startup_imports(_DB_PATH, force=args.rebuild)
        warn = _loopback_warning(host)
        if warn:
            print("!" * 70 + "\n" + warn + "\n" + "!" * 70, flush=True)
        # the same rule as ingest's ASK block (sections, scan folders, skip
        # dirs, manifest/kit roots AND the registry's rows): listing indexed
        # scan folders here told agents to re-scan them -> duplicates
        from asset_service.init import _not_indexed_dirs
        strays = _not_indexed_dirs(config.LIBRARY_ROOT, config.load(), _DB_PATH)
        if strays:
            print(f"NOT INDEXED (no section claims these): "
                  f"{', '.join(strays)}", flush=True)
            print('  -> python pharos.py map set "<folder>" kind=<kind>  '
                  "(or include=no), then pharos.py ingest", flush=True)
        print(f"registry browser: http://{host}:{args.port}  (db={_DB_PATH})", flush=True)
    server = ThreadingHTTPServer((host, args.port), Handler)
    if not args.no_open or args.setup:
        page = "/setup" if (_SETUP_MODE or args.setup) else ""
        threading.Timer(0.4, lambda: webbrowser.open(
            f"http://{host}:{args.port}{page}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
