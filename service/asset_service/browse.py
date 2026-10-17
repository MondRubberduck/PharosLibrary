"""Local registry browser.

A tiny localhost web UI over the SQLite registry so the virtual taxonomy is
actually VISIBLE — the shared source of truth for the human AND the coding
agent ("know what's there and where to find it"):

  /                registry grid: search, filters, bulk human re-tagging
  /pack?id=...     pack CONTENT: every indexed file, filter/paginate,
                   per-file 3D play (FBX/BVH), copy-absolute-path buttons
  /viewer?pack=..  live three.js FBX/BVH playback with auto-orientation

    python service/asset_service/browse.py            # http://127.0.0.1:8765
    python service/asset_service/browse.py --port 9000 --no-open

Stdlib only; binds 127.0.0.1 exclusively (offline & license safety: nothing
leaves the workstation). Reads the registry and thumbnail caches;
writes nothing except its own access log output to stdout.
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
from asset_service import pack_tags  # noqa: E402
from asset_service.auto_classifier import DOMAINS, STYLES  # noqa: E402

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
PREVIEW_EXTS = {".fbx", ".bvh"}
TEXTURE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
PAGE_SIZE = 200            # pack-content page rows per request
PREVIEW_DIR = config.PREVIEW_DIR   # rendered clip loops (webm)

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


def _safe_asset_file(path_str: str, allowed: set) -> Optional[Path]:
    """Resolve a path (absolute or canonical-root-relative) and verify it is
    an existing file under a canonical root with an allowed extension."""
    p = Path(path_str)
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


_thumb_lock = threading.Lock()
_thumbs: list[dict] = []          # {"index": i, "abs": path, "key": mirrored rel}
_pack_thumb: dict[str, int] = {}  # pack id -> thumb index


# ---------------------------------------------------------------------------
# thumbnail index (external cache dirs from config, mirror-named after sources)
# ---------------------------------------------------------------------------

def build_thumb_index() -> None:
    with _thumb_lock:
        _thumbs.clear()
        _pack_thumb.clear()
        thumb_dirs: list[Path] = []
        for root in CANONICAL_ROOTS:
            if not root.is_dir():
                continue
            # host apps create thumbnail caches next to each added source
            # (usually depth 1-2 below the root); walk a few levels only
            for dirpath, dirnames, _ in os.walk(root):
                if dirpath.count(os.sep) - str(root).count(os.sep) > 3:
                    dirnames[:] = []
                    continue
                for name in list(dirnames):
                    if name in config.THUMB_CACHE_DIRS:
                        thumb_dirs.append(Path(dirpath) / name)
                        dirnames.remove(name)
                if len(thumb_dirs) > 50:
                    break
            for thumbs_dir in thumb_dirs:
                for p in thumbs_dir.rglob("*.jpg"):
                    if len(_thumbs) >= THUMB_LIMIT:
                        break
                    key = p.relative_to(thumbs_dir).as_posix()
                    _thumbs.append({"index": len(_thumbs), "abs": str(p), "key": key})
        # pack -> first thumbnail whose mirrored path lives under the pack
        # (caches mirror the source-relative layout, so also try the pack's
        # last path segment as fallback)
        for pack_id in [r["id"] for r in _all_pack_ids()]:
            rel = pack_id[len("pack::"):]
            candidates = [t for t in _thumbs
                          if t["key"].startswith(rel + "/") or t["key"].startswith(rel)]
            if not candidates:
                last = rel.rsplit("/", 1)[-1]
                candidates = [t for t in _thumbs
                              if t["key"].startswith(last + "/")]
            if candidates:
                _pack_thumb[pack_id] = candidates[0]["index"]


def _all_pack_ids() -> list[dict]:
    try:
        conn = db.connect(_DB_PATH)
        rows = conn.execute("SELECT id FROM assets").fetchall()
        conn.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


_DB_PATH = DEFAULT_DB


# ---------------------------------------------------------------------------
# API payload builders
# ---------------------------------------------------------------------------

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
    row = conn.execute(
        "SELECT MAX(updated_at) AS last, COUNT(*) AS n FROM assets").fetchone()
    conn.close()
    stats["last_write"] = row["last"] if row else None
    status = None
    try:
        if STATUS_FILE is not None and STATUS_FILE.is_file():
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))[:5]
    except (OSError, ValueError):
        status = None
    return {"stats": stats, "sections": sections, "extension_status": status,
            "domains": list(DOMAINS),
            "styles": [*pack_tags.MARKERS, "Neutral/Unclassified"]}


def api_packs(params: dict) -> list[dict]:
    q = (params.get("q") or [""])[0].strip()
    domain = (params.get("domain") or [""])[0].strip() or None
    style = (params.get("style") or [""])[0].strip() or None
    vendor = (params.get("vendor") or [""])[0].strip() or None
    validated = (params.get("validated") or [""])[0] in ("1", "true")
    conn = db.connect(_DB_PATH)
    # domain and style are filtered below, AFTER the owner's pack tags
    # replaced the classifier's guesses
    rows = db.search_assets(conn, q or None, vendor=vendor,
                            validated_only=validated, limit=500)
    tags = _tag_rows()
    want = pack_tags.marker(style) if style else ""
    out = []
    for r in rows:
        pt = tags.get(r["id"][len("pack::"):].lower())
        styles = (pt["styles"] if pt and pt["styles"] else
                  [r["style"]] if r["style"] in _LOOKS else [])
        dom = pt["domain"] if pt and pt["domain"] else r["domain"]
        if domain and dom != domain:
            continue
        if style and not (want in styles if want else
                          style == "Neutral/Unclassified"
                          and not set(styles) & set(_LOOKS)):
            continue
        detail = None
        if (params.get("detail") or [""])[0] == "1":
            detail = db.get_asset_details(conn, r["id"])
        counts = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(file_size),0) AS bytes "
            "FROM asset_files WHERE asset_id = ?", (r["id"],)).fetchone()
        thumb = _pack_thumb.get(r["id"])
        out.append({
            "id": r["id"], "name": r["name"], "domain": dom,
            "sub_category": r["sub_category"],
            "style": next((s for s in styles if s in _LOOKS),
                          "Neutral/Unclassified"),
            "styles": styles,
            "owner_tagged": bool(pt and (pt["styles"] or pt["domain"]
                                         or pt["tags"])),
            "pack_tags": pt["tags"] if pt else [],
            "confidence": r["confidence_score"],
            "validation": r["validation_status"], "status": r["status"],
            "vendor": r["vendor"], "formats": json.loads(r["formats"] or "[]"),
            "tags": json.loads(r["host_tags"] or "[]"),
            "hero": r["hero_file_path"],
            "files": counts["n"], "bytes": counts["bytes"],
            "updated_at": r["updated_at"],
            "thumb": f"/thumb/{thumb}" if thumb is not None else None,
            "detail": detail,
        })
    conn.close()
    return out


# ---- pack tags: the owner's labels, kept in <agent_files>/pack_tags.csv ----

_LOOKS = ("Photorealistic", "Stylized")


def _tag_rows() -> dict:
    """The pack-tag file's rows; {} while it cannot be read (a search must
    not fail because a spreadsheet holds the file)."""
    try:
        return pack_tags.load()
    except OSError:
        return {}


def _sync_pack_tags() -> int:
    """Give every pack folder a row in the tag file, so new packs appear
    after an ingest. The purchase section holds store images, not assets,
    and gets none. Labels the owner set in the registry are copied in when
    the file is first made."""
    conn = db.connect(_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, name, domain, style, validation_status FROM assets "
            "WHERE substr(id, 1, 6) = 'pack::'").fetchall()
    finally:
        conn.close()
    coll = (config.SECTIONS.get("collection") or "").strip("/").lower()
    packs, seed = [], {}
    for r in rows:
        folder = r["id"][len("pack::"):]
        low = folder.lower()
        if coll and (low == coll or low.startswith(coll + "/")):
            continue
        packs.append((folder, r["name"]))
        if r["validation_status"] == "human_verified":
            seed[folder] = (r["domain"],
                            r["style"] if r["style"] in _LOOKS else "")
    return pack_tags.sync(packs, seed)


def api_pack_tags() -> dict:
    """Every pack with the owner's tags, the classifier's guess beside
    them, and whether its folder is still indexed."""
    _sync_pack_tags()
    rows = _tag_rows()
    conn = db.connect(_DB_PATH)
    try:
        packs = {r["id"][len("pack::"):].lower(): dict(r) for r in conn.execute(
            "SELECT a.id AS id, a.domain AS domain, a.style AS style, "
            "COUNT(f.id) AS files, COALESCE(SUM(f.file_size), 0) AS bytes "
            "FROM assets a LEFT JOIN asset_files f ON f.asset_id = a.id "
            "WHERE substr(a.id, 1, 6) = 'pack::' GROUP BY a.id")}
    finally:
        conn.close()
    out = []
    for k, r in sorted(rows.items(), key=lambda kv: kv[1]["folder"].lower()):
        p = packs.get(k)
        thumb = _pack_thumb.get(p["id"]) if p else None
        out.append({
            "folder": r["folder"], "name": r["name"], "styles": r["styles"],
            "domain": r["domain"], "tags": r["tags"], "found": p is not None,
            "guess": ({"style": p["style"] if p["style"] in _LOOKS else "",
                       "domain": p["domain"]} if p else None),
            "files": p["files"] if p else 0, "bytes": p["bytes"] if p else 0,
            "thumb": f"/thumb/{thumb}" if thumb is not None else None})
    return {"file": str(pack_tags.PATH), "markers": list(pack_tags.MARKERS),
            "domains": list(DOMAINS), "packs": out}


def api_pack_tags_set(body: dict) -> tuple:
    """POST {folder, styles?, domain?, tags?} -> (answer, HTTP status).
    A field left out stays as it is; "" / [] clears it."""
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        return {"error": "folder missing"}, 400
    styles = body.get("styles")
    if styles is not None:
        if not isinstance(styles, list):
            return {"error": "styles must be a list"}, 400
        bad = [s for s in styles if not pack_tags.marker(str(s))]
        if bad:
            return {"error": f"unknown style(s) {bad}; use: "
                             f"{', '.join(pack_tags.MARKERS)}"}, 400
        styles = [pack_tags.marker(str(s)) for s in styles]
    domain = body.get("domain")
    if domain not in (None, "") and domain not in DOMAINS:
        return {"error": f"unknown domain {domain!r}; use: "
                         f"{', '.join(DOMAINS)}"}, 400
    tags = body.get("tags")
    if tags is not None:
        tags = pack_tags.split_tags(
            ", ".join(map(str, tags)) if isinstance(tags, list) else str(tags))
    try:
        row = pack_tags.update(folder, styles=styles, domain=domain, tags=tags)
    except KeyError:
        return {"error": "no such pack folder in the tag file"}, 404
    except pack_tags.TagFileBusy as exc:
        return {"error": str(exc)}, 409
    return {"ok": True, "pack": {k: row[k] for k in (
        "folder", "name", "styles", "domain", "tags")}}, 200


def _jailed(t, root) -> bool:
    """True when resolved path t sits inside root (real containment, not
    a string-prefix match -- 'Audio_Assets_backup' must not pass a jail
    meant for 'Audio_Assets')."""
    try:
        t.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False

def _safe_dir(encoded: str) -> Optional[str]:
    """Decode a URL-encoded directory that must live under a canonical root."""
    directory = Path(unquote(encoded))
    if not directory.is_absolute():
        directory = CANONICAL_ROOTS[0] / directory
    try:
        directory = directory.resolve()
    except OSError:
        return None
    for root in CANONICAL_ROOTS:
        try:
            directory.relative_to(root.resolve())
            return str(directory)
        except ValueError:
            continue
    return None


def api_clips(params: dict) -> dict:
    """FBX/BVH files of one pack for the live 3D viewer."""
    pack_id = _normalize_pack_id((params.get("pack") or [""])[0])
    if not pack_id.startswith("pack::"):
        return {"files": [], "name": ""}
    conn = db.connect(_DB_PATH)
    row = conn.execute("SELECT name FROM assets WHERE id = ?", (pack_id,)).fetchone()
    files = [dict(r) for r in conn.execute(
        "SELECT relative_path FROM asset_files WHERE asset_id = ? "
        "AND (lower(relative_path) LIKE '%.fbx' OR lower(relative_path) LIKE '%.bvh') "
        "ORDER BY relative_path LIMIT 400", (pack_id,))]
    conn.close()
    return {"name": row["name"] if row else pack_id,
            "files": [f["relative_path"] for f in files]}


def api_pack_files(params: dict) -> dict:
    """File-level contents of one pack (server-side pagination + filters)."""
    pack_id = _normalize_pack_id((params.get("id") or [""])[0])
    if not re.fullmatch(r"pack::[\w .()&!',#%/+-]+", pack_id):
        return {"error": "bad pack id"}
    q = (params.get("q") or [""])[0].strip()
    ext = re.sub(r"[^a-z0-9]", "", (params.get("ext") or [""])[0].lower())
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    conn = db.connect(_DB_PATH)
    asset = conn.execute(
        "SELECT name, domain, style, sub_category, vendor, hero_file_path, "
        "validation_status, source_url FROM assets WHERE id = ?", (pack_id,)).fetchone()
    if asset is None:
        conn.close()
        return {"error": "pack not found in registry"}
    where, args = "asset_id = ?", [pack_id]
    if q:
        where += " AND lower(relative_path) LIKE ? ESCAPE '/'"
        # % and _ are LIKE wildcards: a literal % in the query must not
        # match everything
        args.append("%" + q.lower().replace("/", "//")
                    .replace("%", "/%").replace("_", "/_") + "%")
    if ext:
        where += " AND lower(relative_path) LIKE ?"
        args.append(f"%.{ext}")
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM asset_files WHERE {where}", args).fetchone()["n"]
    rows = [dict(r) for r in conn.execute(
        f"SELECT relative_path, file_type, file_size FROM asset_files "
        f"WHERE {where} ORDER BY relative_path LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, (page - 1) * PAGE_SIZE])]
    counts: dict[str, int] = {}
    for r in conn.execute(
            "SELECT relative_path FROM asset_files WHERE asset_id = ?", (pack_id,)):
        base = r["relative_path"].rsplit("/", 1)[-1]
        extn = base.rsplit(".", 1)[-1].lower() if "." in base else "(no ext)"
        counts[extn] = counts.get(extn, 0) + 1
    conn.close()
    ext_counts = sorted(({"ext": k, "n": v} for k, v in counts.items()),
                        key=lambda e: -e["n"])[:24]
    return {"pack": dict(asset), "hero": asset["hero_file_path"],
            "total": total, "page": page, "size": PAGE_SIZE,
            "files": rows, "ext_counts": ext_counts,
            "root": CANONICAL_ROOTS[0].as_posix() + "/"}


# ---------------------------------------------------------------------------
# animation page: folder tree, tiered clip search, preview cache
# ---------------------------------------------------------------------------

def _preview_key(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]


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
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_MESH_LIGHT},{obj} AS object_name,{wired} AS wired "
            f"FROM meshes ORDER BY pack, name")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    for r in rows:
        for key, kind in (("tags", list), ("meta", dict)):
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
    look = pack_tags.Lookup(_tag_rows())
    for r in rows:
        r["_ptag"] = look(r["_file"])
        _mesh_flags(r)
    full = {(r["pack"], r["_base"]) for r in rows if not r["_lod"]}
    for r in rows:
        if r["_lod"] and (r["pack"], r["_base"]) not in full:
            r["_lod"] = 0          # its only copy: it IS the model
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
    budget), textures=1 (only textured), sort, page, size. Items are SHORT
    by default; detail=1 adds recipe, material names and corners. Size
    and triangle filters exclude rows without that measurement (unknown
    triangles are NULL) and disclose the count as `unmeasured_excluded`.
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
            m = pack_tags.marker(s)
            if not m:
                P.warn(f"style={s.strip()!r} is not a style; use one of: "
                       f"{', '.join(pack_tags.MARKERS)} (0 results)")
            want_styles.append(m)
    min_dim, max_dim = P.num("min_dim", 0.0), P.num("max_dim", 1e9)
    min_h, max_h = P.num("min_h", 0.0), P.num("max_h", 1e9)
    max_tri = P.num("max_tri", 1e12)
    only_tex = P.flag("textures")
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
    if dim_filter or h_filter or tri_filter:
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
                 "textures")
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
    look = pack_tags.Lookup(_tag_rows())
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
            m["styles"] = [s for s in pack_tags.MARKERS if s in styles]
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


def _skeletons() -> dict:
    """pack id -> the skeleton(s) its clips use and the character files of
    this library whose bones match.

    The tree used to tell every agent that all clips retarget onto ONE
    body. A library with clips for two rig families (Character Creator
    and the Unreal mannequin) had a third of its clips share no bone name
    with that body, while fitting characters sat unlinked in a model pack.
    A few dozen file reads on the first call, cached afterwards."""
    prefix = _anim_prefix()
    conn = db.connect(_DB_PATH)
    try:
        files = conn.execute(
            "SELECT a.id AS pack, f.relative_path AS rel FROM assets a "
            "JOIN asset_files f ON f.asset_id = a.id "
            "WHERE substr(a.id, 1, ?) = ? AND (lower(f.relative_path) LIKE "
            "'%.fbx' OR lower(f.relative_path) LIKE '%.bvh')",
            (len(prefix), prefix)).fetchall()
        try:
            bodies = [r[0] for r in conn.execute(
                "SELECT DISTINCT fbx FROM meshes WHERE kind='SkeletalMesh' "
                "AND on_disk=1 AND fbx != ''")]
        except sqlite3.OperationalError:
            bodies = []         # no mesh table yet
    finally:
        conn.close()
    if not files:
        return {}           # no clips: no reason to open a character file
    root = CANONICAL_ROOTS[0]
    # no answer cache: pack_skeletons re-checks size + mtime of the few
    # files it samples, so a replaced clip is read again instead of being
    # answered stale until the next restart
    clips: dict = {}
    for r in files:
        folder = str(root / r["pack"][len("pack::"):])
        clips.setdefault(folder, []).append(str(root / r["rel"]))
    rig = _rig_rel()
    if rig:
        bodies.append(str(root / rig))
    found = skeleton.pack_skeletons(clips, bodies, cache=_skel_cache)
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
    prefix = _anim_prefix()
    conn = db.connect(_DB_PATH)
    rows = conn.execute(
        "SELECT a.id AS id, a.name AS name, COUNT(f.rowid) AS n FROM assets a "
        "JOIN asset_files f ON f.asset_id = a.id "
        "WHERE substr(a.id, 1, ?) = ? AND "
        "(lower(f.relative_path) LIKE '%.fbx' OR lower(f.relative_path) LIKE '%.bvh') "
        "GROUP BY a.id ORDER BY a.id",
        (len(prefix), prefix)).fetchall()
    conn.close()
    skel = _skeletons()
    folders = []
    for r in rows:
        f = {"pack": r["id"], "folder": r["id"][len("pack::"):],
             "name": r["name"], "clips": r["n"]}
        if r["id"] in skel:
            f["skeletons"] = skel[r["id"]]
        folders.append(f)
    return {"rig_body": _rig_rel() or None,
            "rig_note": "the preview body for Character-Creator-family "
                        "clips. It does NOT fit every clip: each folder "
                        "lists its own `skeletons` (family, bone count and "
                        "the character files in this library whose bones "
                        "match); null = no rig in this library",
            "folders": folders}


def api_anim_clips(params: dict) -> dict:
    """All animation clips, searched like every other section: every query
    word must match (tier 1 = all as whole words of the clip title, tier 2
    = via folder words, part of a longer word or an alias); only when no
    clip matches every word are single-word matches served (tier 3,
    mode 'or-fallback'). Clips of a RELATED motion ('walk' -> jog, run)
    follow at the end as tier 3 and are not counted in `exact`."""
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
    conn = db.connect(_DB_PATH)
    if packs:
        known = {r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE substr(id, 1, ?) = ?",
            (len(prefix), prefix))}
        for p in packs:
            if p not in known:
                P.warn(f"packs: {p!r} is no animation pack (ids: /api/anim/tree)")
        # none left: match nothing (no pack has the empty id), never all
        packs = [p for p in packs if p in known] or [""]
    where = ("substr(a.id, 1, ?) = ? AND "
             "(lower(f.relative_path) LIKE '%.fbx' OR lower(f.relative_path) LIKE '%.bvh')")
    args: list = [len(prefix), prefix]
    if packs:
        where += " AND a.id IN (%s)" % ",".join("?" * len(packs))
        args += packs
    rows = [dict(r) for r in conn.execute(
        "SELECT f.relative_path AS rel, f.file_size AS bytes, a.id AS pack "
        "FROM asset_files f JOIN assets a ON a.id = f.asset_id "
        "WHERE " + where + " ORDER BY f.relative_path", args)]
    conn.close()

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
        items.append({"rel": r["rel"], "pack": r["pack"], "folder": r["folder"],
                      "title": r["title"],
                      "name": r["title"],   # alias: every other section uses name
                      "bytes": r["bytes"], "tier": e["tier"],
                      "preview": pv, "rate": rate, "key": key})
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
    _suggest(resp, query, rows, name_of,
             matched[0]["tier"] if matched else 0)
    return P.done(resp)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

VIEWER_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>3D Animation Viewer</title><style>
:root{--bg:#101216;--card:#1e2128;--line:#2c313a;--fg:#dfe3ea;--dim:#8b93a1}
body{margin:0;font:13px/1.4 system-ui;background:var(--bg);color:var(--fg);overflow:hidden}
#bar{position:fixed;top:0;left:0;right:0;display:flex;gap:8px;align-items:center;
padding:8px 14px;background:rgba(20,22,26,.94);border-bottom:1px solid var(--line);z-index:2;flex-wrap:wrap}
#bar a{color:#7cc4ff;text-decoration:none}
select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:4px 8px}
button{cursor:pointer}
button.r{min-width:36px}
#status{color:var(--dim);margin-left:auto;max-width:48vw;white-space:pre-wrap}
#status .err{color:#ff8a8a}
canvas{display:block}
</style>
<script type="importmap">
{"imports":{"three":"/static/three.module.js","three/addons/":"/static/jsm/"}}
</script>
<script>window.__errs=[];window.addEventListener('error',e=>window.__errs.push(String(e.message||e).slice(0,200)));window.addEventListener('unhandledrejection',e=>window.__errs.push('REJ: '+String(e.reason&&(e.reason.message||e.reason)).slice(0,200)));</script></head><body>
<div id="bar"><a href="/" title="home">⌂</a><strong id="pname"></strong>
<select id="file"></select><select id="clip"></select>
<button id="play">pause</button><button id="restart" title="restart clip from 0">&#9198;</button>
<span title="orientation (auto-detected — override here if the model lies on its side; your choice is remembered per file)">
<button class="r" data-r="x-">X&minus;</button><button class="r" data-r="x+">X+</button>
<button class="r" data-r="y-">Y&minus;</button><button class="r" data-r="y+">Y+</button>
<button class="r" data-r="z-">Z&minus;</button><button class="r" data-r="z+">Z+</button>
<button class="r" id="orientReset" title="clear saved orientation and re-run auto-detect">reset</button></span>
<a id="dl" href="#" title="download this FBX/BVH file" style="display:none;color:#7cc4ff;text-decoration:none">&#8681;</a>
<button id="vcp" title="copy the file's absolute path" style="display:none">path &#10697;</button>
<button id="vexp" title="show file in Explorer" style="display:none">&#128193;</button>
<span id="status">loading…</span></div>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {FBXLoader} from 'three/addons/loaders/FBXLoader.js';
import {BVHLoader} from 'three/addons/loaders/BVHLoader.js';

const fileSel=document.getElementById('file'),clipSel=document.getElementById('clip'),
      status=document.getElementById('status'),playBtn=document.getElementById('play');
const q=new URLSearchParams(location.search), pack=q.get('pack')||'';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
renderer.setSize(innerWidth,innerHeight);
document.body.appendChild(renderer.domElement);
const scene=new THREE.Scene();
scene.background=new THREE.Color(0x101216);
const camera=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.01,1e7);
const controls=new OrbitControls(camera,renderer.domElement);
async function fetchBuf(url,ms){
  const ctl=new AbortController();
  const t=setTimeout(()=>ctl.abort(),ms||30000);
  try{const r=await fetch(url,{signal:ctl.signal});
    if(!r.ok)throw new Error('HTTP '+r.status);
    return await r.arrayBuffer();}
  finally{clearTimeout(t);}}
scene.add(new THREE.HemisphereLight(0xdfe8ff,0x30281f,1.4));
const key=new THREE.DirectionalLight(0xffffff,2.2); key.position.set(3,6,4); scene.add(key);
const rim=new THREE.DirectionalLight(0x88aaff,0.8); rim.position.set(-4,3,-3); scene.add(rim);
let grid=null,mixer=null,playing=true,holder=null,clips=[],currentRel='',mode='fbx';

// skeleton-only clips all target the Actor motion-dummy rig
// ("skeletons should be the same") — show its body instead of naked bones
const RETARGET_REL="{{RIG_REL}}";  // from the server; '' = no rig in this library
let dummyObj=null,dummyPromise=null;
function getDummy(){
  if(!RETARGET_REL)return Promise.reject(new Error('no retarget rig'));
  if(!dummyPromise){
    const L=new FBXLoader();
    L.setResourcePath('/res/'+encodeURIComponent(RETARGET_REL.split('/').slice(0,-1).join('/'))+'/');
    dummyPromise=new Promise((res,rej)=>L.load('/file?path='+encodeURIComponent(RETARGET_REL),
      o=>{dummyObj=o;res(o);},undefined,rej));
  }
  return dummyPromise;}

function fail(msg){status.innerHTML='<span class="err">&#9888; '+esc(msg)+'</span>';console.error(msg);}
let watchdog=0;
function loading(msg){clearTimeout(watchdog);status.textContent=msg;
  watchdog=setTimeout(()=>{if(status.textContent===msg)
    status.textContent=msg+'\\nstill loading after 15s — very large file, or the server restarted (reload this page)';},15000);}
function loaded(msg){clearTimeout(watchdog);status.textContent=msg;}

function orientKey(){return 'orient2:'+pack+':'+currentRel;}
function saveOrient(){if(!holder)return;
  try{localStorage.setItem(orientKey(),JSON.stringify({x:holder.rotation.x,y:holder.rotation.y,z:holder.rotation.z}));}catch(e){}}
function loadOrient(){try{return JSON.parse(localStorage.getItem(orientKey()));}catch(e){return null;}}
function clearOrient(){try{localStorage.removeItem(orientKey());}catch(e){}}

function modelBox(){
  let b=new THREE.Box3().setFromObject(holder);
  let s=b.getSize(new THREE.Vector3());
  if(b.isEmpty()||s.lengthSq()===0){          // skin-only meshes: bind-pose
    try{b=new THREE.Box3().setFromObject(holder,true);}catch(e){}  // precise = skinned verts
    s=b.getSize(new THREE.Vector3());}
  if(b.isEmpty()||s.lengthSq()===0){          // skeleton-driven files: use bones
    const bb=new THREE.Box3();
    holder.traverse(o=>{if(o.isBone)bb.expandByPoint(o.getWorldPosition(new THREE.Vector3()));});
    if(!bb.isEmpty())b=bb;}
  return b;}
function autoOrientEuler(box){
  if(!box||box.isEmpty())return null;
  const s=box.getSize(new THREE.Vector3());
  const horiz=Math.max(s.x,s.z);
  if(!horiz||s.y>=horiz*0.4)return null;                 // upright already
  if(s.z>=s.x)return new THREE.Euler(-Math.PI/2,0,0);    // content up is +Z
  return new THREE.Euler(0,0,Math.PI/2);                 // content up is +X
}
function _findBone(re){let best=null;
  holder.traverse(o=>{if(o.isBone&&re.test(o.name)&&(!best||o.name.length<best.name.length))best=o;});
  return best;}
function boneAxisDelta(){
  // anatomy beats bounding boxes: the head bone must sit above the hips.
  // Returns the 90-degree step (absolute, from identity) that achieves that,
  // {x:0,z:0} when already upright, or null when no head/hip bones exist.
  const head=_findBone(/head/i),hip=_findBone(/pelvis|hips/i);
  if(!head||!hip)return null;
  const v=head.getWorldPosition(new THREE.Vector3()).sub(hip.getWorldPosition(new THREE.Vector3()));
  const ax=Math.abs(v.x),ay=Math.abs(v.y),az=Math.abs(v.z);
  if(v.y>0&&ay>=ax&&ay>=az)return{x:0,z:0};
  if(v.y<0&&ay>=ax&&ay>=az)return{x:0,z:Math.PI};   // 180 flipped (CC rigs)
  if(az>=ax)return v.z>0?{x:-Math.PI/2,z:0}:{x:Math.PI/2,z:0};
  return v.x>0?{x:0,z:Math.PI/2}:{x:0,z:-Math.PI/2};}
let bindBoxEmpty=false,bindAnatomyOk=false;
function applyOrient(){
  const saved=loadOrient();
  if(saved){holder.rotation.set(saved.x,saved.y,saved.z);bindBoxEmpty=false;bindAnatomyOk=true;return 'saved orientation';}
  const box0=modelBox();
  bindBoxEmpty=box0.isEmpty()||box0.getSize(new THREE.Vector3()).lengthSq()===0;
  if(!clips.length){bindAnatomyOk=true;return 'native orientation';}   // static props stay as-authored
  // clips are downloaded/exported from Blender = Z-up.
  // Baseline X-90 maps Z-up onto three.js Y-up, THEN anatomy verifies it.
  holder.rotation.set(-Math.PI/2,0,0);
  holder.updateMatrixWorld(true);
  const d=boneAxisDelta();
  bindAnatomyOk=!!d;
  if(d&&(d.x||d.z)){holder.rotation.x+=d.x;holder.rotation.z+=d.z;return 'auto-oriented (Z-up baseline)';}
  if(!d){const e=autoOrientEuler(modelBox());       // no head/hip bones → bbox check
    if(e){holder.rotation.x+=e.x;holder.rotation.z+=e.z;}}
  return 'auto-oriented (Z-up baseline)';}
function refit(){if(!holder)return;holder.updateMatrixWorld(true);fitCamera(modelBox());}
function scheduleDeferredOrient(){
  // ONLY for clips whose bind pose could not be verified anatomically:
  // authored motion (falls, dives, rolls) must never be "corrected" --
  // the run-fallover misfire rotated the scene 90 degrees mid-fall and
  // saved it. And auto-fixes never persist; only manual buttons save.
  if(bindAnatomyOk)return;
  setTimeout(()=>{if(!holder||loadOrient())return;
    let fix=false;
    if(clips.length){
      const d2=boneAxisDelta();
      if(d2&&(d2.x||d2.z)){holder.rotation.x+=d2.x;holder.rotation.z+=d2.z;fix=true;}
      else{const e2=autoOrientEuler(modelBox());
        if(e2){holder.rotation.x+=e2.x;holder.rotation.z+=e2.z;fix=true;}}}
    if(fix){refit();status.textContent+=' — auto-oriented once animation started';}
    else if(bindBoxEmpty)refit();},1400);}
function fitCamera(box){
  if(box.isEmpty())return;
  const size=box.getSize(new THREE.Vector3()),center=box.getCenter(new THREE.Vector3());
  const radius=Math.max(size.x,size.y,size.z)||1;
  camera.near=radius/100; camera.far=radius*50; camera.updateProjectionMatrix();
  camera.position.copy(center).add(new THREE.Vector3(radius*1.4,radius*0.9,radius*1.7));
  controls.target.copy(center); controls.update();
  if(grid)scene.remove(grid);
  grid=new THREE.GridHelper(radius*4,24,0x2c313a,0x1c2026); grid.position.set(center.x,box.min.y,center.z);
  scene.add(grid);
}
function fixMaterials(obj){
  obj.traverse(o=>{ if(!o.isMesh)return;
    const mats=Array.isArray(o.material)?o.material:[o.material];
    for(const m of mats){ if(!m.map&&(!m.color||m.color.getHex()===0))m.color=new THREE.Color(0xb8c0cc);
      if(m.map)m.color=new THREE.Color(0xffffff);}
    o.frustumCulled=false; });
}
function dispose(obj){obj.traverse(n=>{
  if(n.geometry)n.geometry.dispose();
  if(n.material)(Array.isArray(n.material)?n.material:[n.material]).forEach(m=>{
    for(const k of ['map','normalMap','bumpMap','roughnessMap','metalnessMap','emissiveMap','aoMap','alphaMap'])
      if(m[k])m[k].dispose();
    m.dispose();});});}
function clearModel(){
  if(mixer)mixer.stopAllAction(); mixer=null; clips=[]; clipSel.innerHTML='';
  if(holder){if(dummyObj&&dummyObj.parent===holder)holder.remove(dummyObj);  // keep the dummy for reuse
    scene.remove(holder);dispose(holder);holder=null;}
  if(grid){scene.remove(grid);grid=null;}}
function playClip(i){
  if(!mixer||!clips.length)return;
  mixer.stopAllAction();
  const action=mixer.clipAction(clips[i]); action.reset().play();
  clipSel.innerHTML=clips.map((c,j)=>`<option ${j===i?'selected':''}>${esc(c.name||j)}</option>`).join('');}

async function loadFile(rel){
  clearModel(); currentRel=rel;
  const dlA=document.getElementById('dl');
  dlA.href='/file?path='+encodeURIComponent(rel);
  dlA.setAttribute('download',rel.split('/').pop());
  dlA.style.display='';
  document.getElementById('vcp').style.display='';document.getElementById('vexp').style.display='';
  document.getElementById('vcp').onclick=()=>{fetch('/api/open_explorer?dry=1&path='+encodeURIComponent(rel))
    .then(r=>r.json()).then(j=>{if(j.abs&&navigator.clipboard)
      navigator.clipboard.writeText(j.abs).then(()=>{
        document.getElementById('vcp').textContent='copied ✓';
        setTimeout(()=>{document.getElementById('vcp').textContent='path ⧉';},1500);});});};
  document.getElementById('vexp').onclick=()=>{fetch('/api/open_explorer?path='+encodeURIComponent(rel))
    .then(r=>r.json()).then(j=>{if(!r.ok)alert('failed: '+(j.error||'?'));});};
  history.replaceState(null,'','/viewer?pack='+encodeURIComponent(pack)+'&file='+encodeURIComponent(rel));
  loading('loading '+rel.split('/').pop()+' …');
  const url='/file?path='+encodeURIComponent(rel);
  const dir=rel.split('/').slice(0,-1).join('/');
  // fetch with hard timeout + parse locally: a dead/restarted server must
  // surface as an error, never as an eternal "loading..."
  let buf;
  try{buf=await fetchBuf(url,30000);}
  catch(e){fail('load failed: '+((e&&e.message)||e)+' — if the server was just restarted, reload this page');return;}
  if(rel.toLowerCase().endsWith('.bvh')){
    mode='bvh';
    // BVHLoader.parse string-splits its input; a raw ArrayBuffer throws
    try{const res=new BVHLoader().parse(new TextDecoder('utf-8').decode(buf));
      try{
        holder=new THREE.Group();
        const root=res.skeleton.bones[0];
        holder.add(root);
        const helper=new THREE.SkeletonHelper(root);
        holder.add(helper);
        scene.add(holder);
        clips=[res.clip]; mixer=new THREE.AnimationMixer(root);
        const how=applyOrient(); refit(); playClip(0);
        loaded('playing skeleton ('+how+') — drag to orbit, wheel to zoom');
        scheduleDeferredOrient();
      }catch(e){fail('BVH error: '+((e&&e.message)||e));}
    }catch(err){fail('BVH load failed: '+((err&&err.message)||err));}
    return;
  }
  mode='fbx';
  window.__retarget={retargeted:false,common:0,clipBones:0};
  let obj;
  try{obj=new FBXLoader().setResourcePath('/res/'+encodeURIComponent(dir)+'/').parse(buf,'');}
  catch(err){
    const msg=(err&&err.message)||String(err);
    let extra=/ascii/i.test(msg)?' — ASCII FBX is not supported, binary only':'';
    fail('FBX load failed: '+String(msg).slice(0,200)+extra);
    return;}
  (async obj=>{
    try{
      holder=new THREE.Group(); holder.add(obj); scene.add(holder);
      clips=obj.animations||[];
      let hasMesh=false; obj.traverse(o=>{if(o.isMesh)hasMesh=true;});
      if(!hasMesh&&clips.length){
        // skeleton-only clip: drive the motion-dummy body with it, provided
        // the rigs actually share bone names
        try{
          const dummy=await getDummy();
          const dummyBones=new Set(); dummy.traverse(o=>{if(o.isBone)dummyBones.add(o.name);});
          const clipBones=new Set();
          for(const t of clips[0].tracks){const m=t.name.match(/bones\\[([^\\]]+)\\]/);
            clipBones.add(m?m[1]:t.name.split('.')[0]);}
          let common=0; for(const b of clipBones)if(dummyBones.has(b))common++;
          window.__retarget={retargeted:false,common,clipBones:clipBones.size};
          if(common>=6){
            holder.add(dummy); holder.remove(obj); dispose(obj);
            fixMaterials(dummy);
            hasMesh=true; window.__retarget.retargeted=true;
            mixer=new THREE.AnimationMixer(dummy); playClip(0);
          }
        }catch(e){/* dummy unavailable → fall back to bone view */}
      }
      if(!mixer&&clips.length){mixer=new THREE.AnimationMixer(obj);playClip(0);}
      if(!hasMesh)holder.add(new THREE.SkeletonHelper(obj));  // skeleton-only FBX
      const how=applyOrient(); refit();
      loaded((clips.length?('playing: '+(clips[0].name||'clip')):'static mesh — no animation in file')
        +(window.__retarget.retargeted?' on Render_Dummy body':(hasMesh?'':' — skeleton-only file, bones shown'))
        +' ('+how+') — drag to orbit, wheel to zoom');
      scheduleDeferredOrient();
    }catch(e){fail('FBX error: '+((e&&e.message)||e));}
  })(obj);
}

playBtn.onclick=()=>{playing=!playing;playBtn.textContent=playing?'pause':'play';};
document.getElementById('restart').onclick=()=>{
  if(mixer&&clips.length){mixer.stopAllAction();
    mixer.clipAction(clips[Math.max(0,clipSel.selectedIndex)]||clips[0]).reset().play();
    if(!playing){playing=true;playBtn.textContent='pause';}}};
document.querySelectorAll('button.r[data-r]').forEach(b=>b.onclick=()=>{
  if(!holder)return;
  const r=b.dataset.r, ax=r[0], dir=r[1]==='+'?1:-1;
  holder.rotation[ax]+=dir*Math.PI/2;
  saveOrient(); refit();});
document.getElementById('orientReset').onclick=()=>{
  if(!holder)return; clearOrient();
  holder.rotation.set(0,0,0);
  const how=applyOrient(); refit();
  loaded('orientation reset ('+how+')');};
clipSel.onchange=()=>playClip(clipSel.selectedIndex);
fileSel.onchange=()=>loadFile(fileSel.value);

// read-only debug probe so humans AND agents can verify viewer state
window.__viewerInfo=()=>{if(!holder)return{loaded:false};
  const s=modelBox().getSize(new THREE.Vector3());
  const probe={meshes:0,skinned:0,bones:0,verts:0};
  holder.traverse(o=>{if(o.isBone)probe.bones++;
    else if(o.isMesh){probe.meshes++;if(o.isSkinnedMesh)probe.skinned++;
      if(o.geometry&&o.geometry.attributes.position)probe.verts+=o.geometry.attributes.position.count;}});
  const head=_findBone(/head/i),hip=_findBone(/pelvis|hips/i);
  const headUp=(head&&hip)?+(head.getWorldPosition(new THREE.Vector3()).y
    -hip.getWorldPosition(new THREE.Vector3()).y).toFixed(1):null;
  return {loaded:true,mode,file:currentRel,clips:clips.length,probe,headUp,
    retarget:window.__retarget||null,
    rotation:{x:+holder.rotation.x.toFixed(3),y:+holder.rotation.y.toFixed(3),z:+holder.rotation.z.toFixed(3)},
    box:{x:+s.x.toFixed(3),y:+s.y.toFixed(3),z:+s.z.toFixed(3)},
    uprightRatio:+(s.y/(Math.max(s.x,s.z)||1)).toFixed(3)};};

(async()=>{
  try{
    const fileOnly=q.get('file');
    if(!pack&&fileOnly){
      document.getElementById('pname').textContent=fileOnly.split('/').pop();
      fileSel.innerHTML=`<option value="${esc(fileOnly)}">${esc(fileOnly.split('/').pop())}</option>`;
      loadFile(fileOnly);
      return;}
    const data=await(await fetch('/api/clips?pack='+encodeURIComponent(pack))).json();
    document.getElementById('pname').textContent=data.name||pack;
    if(!data.files.length){fail('no FBX/BVH files indexed for this pack');return;}
    fileSel.innerHTML=data.files.map(f=>`<option value="${esc(f)}">${esc(f.split('/').pop())}</option>`).join('');
    const want=q.get('file');
    let idx=want?data.files.indexOf(want):-1;
    if(idx<0)idx=0;
    fileSel.selectedIndex=idx;
    loadFile(data.files[idx]);
  }catch(e){fail('cannot load clip list: '+((e&&e.message)||e)+' — is the registry browser running? try reloading.');}
})();
const clock=new THREE.Clock();
(function loop(){requestAnimationFrame(loop);
  const dt=clock.getDelta();
  if(mixer&&playing)mixer.update(dt);
  controls.update(); renderer.render(scene,camera);})();
addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();
  renderer.setSize(innerWidth,innerHeight);});
</script></body></html>"""


PACK_PAGE = _load_tpl("pack.html")


ANIM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Animations — AI Asset Registry</title><style>
:root{--bg:#14161a;--card:#1e2128;--line:#2c313a;--fg:#dfe3ea;--dim:#8b93a1;--cols:6}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header a{color:#7cc4ff;text-decoration:none}
h1{font-size:15px;margin:0}
input,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px}
#wrap{flex:1;display:flex;min-height:0}
#side{width:250px;min-width:250px;border-right:1px solid var(--line);overflow-y:auto;padding:10px 8px}
#side h2{font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin:4px 8px 8px}
.fbtn{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;background:none;border:none;
color:var(--fg);padding:6px 10px;border-radius:6px;cursor:pointer;font-size:13px;word-break:break-all}
.fbtn:hover{background:#1c2028}
.fbtn.on{background:#243040;color:#7cc4ff}
.fbtn .n{color:var(--dim);white-space:nowrap}
#main{flex:1;overflow-y:auto;padding:14px}
#grid{display:grid;grid-template-columns:repeat(var(--cols),1fr);gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;position:relative}
.card:hover{border-color:#3a4250}
.media{aspect-ratio:16/9;background:#101216;display:flex;align-items:center;justify-content:center;position:relative}
.media video{width:100%;height:100%;object-fit:contain;display:block}
.ph{color:#4a5261;font-size:26px}
.ph.rendering{color:#7cc4ff;animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
.cap{padding:4px 8px;font-size:11.5px}
.cap .t{word-break:break-word;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.badge{position:absolute;top:6px;right:6px;background:#3d3220;color:#ffd479;border-radius:10px;
padding:1px 8px;font-size:10.5px;z-index:2}
#tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-left:auto}
#tools label{color:var(--dim);font-size:12px;display:flex;gap:6px;align-items:center}
#indicator{color:#7cc4ff;font-size:12px}
#pager{display:flex;gap:12px;align-items:center;justify-content:center;padding:16px 0;color:var(--dim)}
#pager button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 14px;cursor:pointer}
#pager button:disabled{opacity:.35;cursor:default}
#renderAll{background:#243040;color:#7cc4ff;border:1px solid #31506e;border-radius:6px;
padding:5px 12px;cursor:pointer;font-size:12.5px}
#renderAll:hover{background:#2b3b50}
#renderAll:disabled{opacity:.4;cursor:default}
input[type=range]{accent-color:#6ea8fe;width:130px}
</style>
<script type="importmap">
{"imports":{"three":"/static/three.module.js","three/addons/":"/static/jsm/"}}
</script>
<script>window.__errs=[];window.addEventListener('error',e=>window.__errs.push(String(e.message||e).slice(0,200)));window.addEventListener('unhandledrejection',e=>window.__errs.push('REJ: '+String(e.reason&&(e.reason.message||e.reason)).slice(0,200)));</script></head><body>
<header><a href="/" style="color:#8b93a1;text-decoration:none">⌂ home</a><h1>Animations</h1>
<input id="q" placeholder="search or meta-search (e.g. talking, fight, idle) …" size="36">
<div id="tools"><span id="indicator"></span>
<span id="count" style="color:var(--dim)"></span>
<button id="renderAll" title="render previews for every clip of the current filter. Keep this tab visible while it runs -- browsers pause rendering in hidden panels. Progress is cached; you can continue later.">render all previews</button>
<label>thumb size <input type="range" id="size" min="3" max="12" step="1"></label></div></header>
<div id="wrap"><div id="side"><h2>Subfolders</h2><div id="tree"></div></div>
<div id="main"><div id="grid"></div><div id="pager">
<button id="prev">&lsaquo; prev</button><span id="pg"></span><button id="next">next &rsaquo;</button></div></div></div>
<script type="module">
import * as THREE from 'three';
import {FBXLoader} from 'three/addons/loaders/FBXLoader.js';

const $=id=>document.getElementById(id);
const qBox=$('q'),sizeEl=$('size');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let folders=[],sel=null,q='',page=1,total=0;
let cols=parseInt(localStorage.getItem('animCols')||'6');sizeEl.value=cols;
document.documentElement.style.setProperty('--cols',cols);
const pageSize=()=>{                       // fill the viewport: rows that fit
 const gw=main.clientWidth-28;
 const cw=(gw-(cols-1)*12)/cols;
 const rowH=cw*9/16+34+12;                 // media + caption + gap
 const rows=Math.max(4,Math.floor((main.clientHeight-28)/rowH));
 return cols*rows;};
const PVV=2;   // bump to escape browsers that cached pre-fix preview bytes
async function previewKey(rel){
 const buf=await crypto.subtle.digest('SHA-1',new TextEncoder().encode(rel));
 return [...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join('').slice(0,20);}

async function loadTree(){
 const d=await(await fetch('/api/anim/tree')).json();folders=d.folders;
 tree.innerHTML=`<button class="fbtn${sel===null?' on':''}" data-f="">All animations <span class="n">${folders.reduce((s,f)=>s+f.clips,0)}</span></button>`+
  folders.map(f=>`<button class="fbtn${sel===f.pack?' on':''}" data-f="${esc(f.pack)}">${esc(f.folder)} <span class="n">${f.clips}</span></button>`).join('');
}
tree.onclick=e=>{const b=e.target.closest('.fbtn');if(!b)return;
 sel=b.dataset.f||null;page=1;loadTree();load();};

async function load(){
 const p=new URLSearchParams({q,page,size:pageSize()});
 if(sel)p.set('packs',sel);
 let d;try{d=await(await fetch('/api/anim/clips?'+p)).json();}catch(e){grid.innerHTML='';count.textContent='load failed — is the server up?';return;}
 total=d.total;page=d.page;
 count.textContent=`${d.total} clip(s)`;
 for(const c of d.clips)keyByRel.set(c.rel,c.key);
 grid.innerHTML=(d.total===0&&!d.q&&!sel)?`<div style="grid-column:1/-1;padding:56px 24px;text-align:center;color:#8b93a1;font-size:14px;line-height:1.7"><div style="font-size:17px;color:#dfe3ea;margin-bottom:10px">No animation packs indexed yet</div>Index your clip folders with <code>indexer.py --root &lt;library-root&gt;</code>,<br>then restart the server — previews render here automatically.</div>`:d.clips.map(c=>{
  const media=c.preview
   ?`<video src="/preview/${c.key}.webm?v=${PVV}" autoplay muted loop playsinline data-rate="${c.rate||''}"></video>`
   :`<div class="ph" data-rel="${esc(c.rel)}">&#9654;</div>`;
  const badge=d.q&&c.tier===3?`<span class="badge">related</span>`:'';
  return `<div class="card" data-rel="${esc(c.rel)}" data-pack="${esc(c.pack)}">${badge}
   <div class="media">${media}</div>
   <div class="cap" title="${esc(c.folder)} / ${esc(c.rel)}"><span class="t">${esc(c.title)}</span></div></div>`;}).join('');
 pg.textContent=`page ${d.page} / ${Math.max(1,Math.ceil(total/pageSize()))}`;
 prev.disabled=page<=1;next.disabled=page>=Math.ceil(total/pageSize());
 observer.disconnect();
 grid.querySelectorAll('.ph').forEach(el=>observer.observe(el));
 grid.querySelectorAll('video[data-rate]').forEach(v=>{
   const r=parseFloat(v.dataset.rate);
   if(r>0)v.addEventListener('loadedmetadata',()=>{v.playbackRate=Math.min(8,Math.max(.25,r));});});
 pump();
}
grid.onclick=e=>{const c=e.target.closest('.card');if(!c)return;
 location.href='/viewer?pack='+encodeURIComponent(c.dataset.pack)+'&file='+encodeURIComponent(c.dataset.rel);};
prev.onclick=()=>{if(page>1){page--;load();main.scrollTop=0;}};
next.onclick=()=>{if(page<Math.ceil(total/pageSize())){page++;load();main.scrollTop=0;}};
qBox.oninput=()=>{clearTimeout(window._t);window._t=setTimeout(()=>{q=qBox.value.trim();page=1;load();},250);};
sizeEl.oninput=()=>{cols=parseInt(sizeEl.value);localStorage.setItem('animCols',cols);
 document.documentElement.style.setProperty('--cols',cols);page=1;
 clearTimeout(window._s);window._s=setTimeout(load,250);};
addEventListener('resize',()=>{clearTimeout(window._r);window._r=setTimeout(load,300);});

// ---------------- preview rendering worker ----------------
// Renders each clip ONCE offscreen (three.js + MediaRecorder on a shared
// 480x270 canvas), uploads the webm to the server cache under
// (registry previews dir), and swaps in a looping <video>.
const PW=480,PH=270,FPS=30,MAXDUR=6;
let prenderer=null,pqueue=[],pactive=false;
const pdone=new Set();
const observer=new IntersectionObserver(es=>{
 for(const e of es){if(!e.isIntersecting)continue;
  const rel=e.target.dataset.rel;if(!rel||pdone.has(rel))continue;
  pdone.add(rel);if(!pqueue.includes(rel))pqueue.push(rel);}
 pump();},{root:main,rootMargin:'200px'});
const keyByRel=new Map();   // server is the source of truth for cache keys

let pdummy=null;
const RIG="{{RIG_REL}}";   // from the server; '' = no rig -> bones-only previews
function pgetRenderer(){if(!prenderer){
  prenderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
  prenderer.setSize(PW,PH);prenderer.setPixelRatio(1);
  // recorded previews came out very dark on some GPUs: explicit tone
  // mapping + exposure normalizes brightness across machines
  if(THREE.ACESFilmicToneMapping!==undefined){prenderer.toneMapping=THREE.ACESFilmicToneMapping;
   prenderer.toneMappingExposure=1.35;}}return prenderer;}
function pgetDummy(){
 if(!RIG)return Promise.reject(new Error('no retarget rig'));
 if(!pdummy){const L=new FBXLoader();
  L.setResourcePath('/res/'+encodeURIComponent(RIG.split('/').slice(0,-1).join('/'))+'/');
  pdummy=new Promise((res,rej)=>L.load('/file?path='+encodeURIComponent(RIG),
    res,undefined,e=>{pdummy=null;rej(e);}));}
 return pdummy;}
function pfit(cam,holder){
 holder.updateMatrixWorld(true);
 const box=new THREE.Box3().setFromObject(holder);
 if(box.isEmpty())return;
 const s=box.getSize(new THREE.Vector3()),c=box.getCenter(new THREE.Vector3());
 const r=Math.max(s.x,s.y,s.z)||1;
 cam.near=r/100;cam.far=r*40;cam.updateProjectionMatrix();
 cam.position.copy(c).add(new THREE.Vector3(r*1.1,r*0.8,r*1.9));
 cam.lookAt(c);}
async function renderPreview(rel){
 const renderer=pgetRenderer();
 const dir=rel.split('/').slice(0,-1).join('/');
 // fetch with timeout + local parse: one malformed file must never wedge
 // the page's main thread (hang bug 2026-09-15)
 const ctl=new AbortController();
 const kill=setTimeout(()=>ctl.abort(),45000);
 let buf;
 try{const r=await fetch('/file?path='+encodeURIComponent(rel),{signal:ctl.signal});
  if(!r.ok)throw new Error('HTTP '+r.status);
  buf=await r.arrayBuffer();}
 catch(e){clearTimeout(kill);throw new Error('fetch failed: '+((e&&e.message)||e));}
 const obj=new FBXLoader().setResourcePath('/res/'+encodeURIComponent(dir)+'/').parse(buf,'');
 clearTimeout(kill);
 const clips=obj.animations||[];
 if(!clips.length)throw new Error('no animation in file');
 let root=obj,hasMesh=false,holderHelper=null;obj.traverse(o=>{if(o.isMesh)hasMesh=true;});
 if(!hasMesh){try{
   const dummy=await pgetDummy();
   const db=new Set();dummy.traverse(o=>{if(o.isBone)db.add(o.name);});
   const cb=new Set();
   for(const t of clips[0].tracks){const m=t.name.match(/bones\\[([^\\]]+)\\]/);cb.add(m?m[1]:t.name.split('.')[0]);}
   let common=0;for(const b of cb)if(db.has(b))common++;
   if(common>=6)root=dummy;
   else holderHelper=obj;
 }catch(e){}}
 if(root===obj&&!hasMesh)holderHelper=obj;   // skeleton-only fallback: show bones
 root.traverse(o=>{                                     // skinned meshes get
  if(o.isMesh){o.frustumCulled=false;                   // culled without this
   const mats=Array.isArray(o.material)?o.material:[o.material];
   for(const m of mats){if(!m.map&&(!m.color||m.color.getHex()===0))m.color=new THREE.Color(0xb8c0cc);
     if(m.map)m.color=new THREE.Color(0xffffff);}}});
 const scene=new THREE.Scene();
 scene.background=new THREE.Color(0x101216);
 scene.add(new THREE.HemisphereLight(0xdfe8ff,0x30281f,1.9));
 const key=new THREE.DirectionalLight(0xffffff,2.8);key.position.set(3,6,4);scene.add(key);
 const fill=new THREE.DirectionalLight(0xcfe0ff,1.1);fill.position.set(-4,2,5);scene.add(fill);
 const holder=new THREE.Group();holder.add(root);scene.add(holder);
 if(holderHelper)holder.add(new THREE.SkeletonHelper(holderHelper));
 root.rotation.set(-Math.PI/2,0,0);   // Blender Z-up exports
 root.updateMatrixWorld(true);
 let head=null,hip=null;
 root.traverse(o=>{if(o.isBone){const n=o.name.toLowerCase();
   if(!head&&n.includes('head'))head=o;if(!hip&&/pelvis|hips/.test(n))hip=o;}});
 if(head&&hip){const v=head.getWorldPosition(new THREE.Vector3()).sub(hip.getWorldPosition(new THREE.Vector3()));
   const ax=Math.abs(v.x),ay=Math.abs(v.y),az=Math.abs(v.z);
   if(v.y<0&&ay>=ax&&ay>=az)holder.rotation.z+=Math.PI;              // 180 flipped (CC rig)
   else if(az>ay&&az>=ax)holder.rotation.x+=(v.z>0?-Math.PI/2:Math.PI/2);
   else if(ax>ay&&ax>=az)holder.rotation.z+=(v.x>0?Math.PI/2:-Math.PI/2);}
 const cam=new THREE.PerspectiveCamera(40,PW/PH,0.01,1e7);
 pfit(cam,holder);
 const clip=clips[0];
 const mixer=new THREE.AnimationMixer(root);
 mixer.clipAction(clip).play();
 const dur=Math.min(clip.duration||3,MAXDUR);
 const SPEED=2;                         // 2x recording, rate 0.5 playback = true speed
 const mime=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm']
   .find(m=>window.MediaRecorder&&MediaRecorder.isTypeSupported(m));
 if(!mime)throw new Error('MediaRecorder/webm unsupported');
 // real-time capture: captureStream(0)+requestFrame drops frames when the
 // recorder is stopped quickly (Chrome quirk), so record the wall clock
 const stream=renderer.domElement.captureStream(FPS);
 const rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:1500000});
 const chunks=[];rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
 const stopped=new Promise(res=>rec.onstop=res);
 rec.start(100);
 let mixT=0;const t0=performance.now();
 await new Promise(res=>{
  (function step(){
   const wallTotal=Math.max(1.0,dur);
   mixT=(performance.now()-t0)/1000;
   if(mixT>=wallTotal||mixT>wallTotal*6+2){res();return;}   // bail if rAF starved (occluded)
   mixer.setTime((mixT*SPEED)%dur);
   holder.updateMatrixWorld(true);
   renderer.render(scene,cam);
   requestAnimationFrame(step);})();
 });
 rec.stop();await stopped;
 const blob=new Blob(chunks,{type:'video/webm'});
 if(blob.size<1024)throw new Error('recording came out empty ('+blob.size+'B)');
 const rate=1/SPEED;
 const r=await fetch('/api/preview/store?clip='+encodeURIComponent(rel)+'&rate='+rate.toFixed(3),
   {method:'POST',body:blob});
 if(!r.ok)throw new Error('store failed: '+r.status);
 return {rate};}
let batchTotal=0,batchDone=0,batchRunning=false;
function updateIndicator(n){
 if(batchTotal>0)$('indicator').textContent=`rendering previews: ${batchDone} / ${batchTotal}`;
 else if(n)$('indicator').textContent='rendering previews: '+n+' queued';
 else $('indicator').textContent='';}
$('renderAll').onclick=async()=>{
 if(batchRunning)return;batchRunning=true;$('renderAll').disabled=true;
 try{
  const pages=Math.ceil(total/pageSize());
  for(let p2=1;p2<=pages;p2++){
   const q2=new URLSearchParams({q,page:p2,size:pageSize()});
   if(sel)q2.set('packs',sel);
   const d=await(await fetch('/api/anim/clips?'+q2)).json();
   for(const c of d.clips){keyByRel.set(c.rel,c.key);
    if(!c.preview&&!pdone.has(c.rel)&&!pqueue.includes(c.rel)){
     pdone.add(c.rel);pqueue.push(c.rel);}}}
  batchDone=0;batchTotal=pqueue.length;
  updateIndicator(batchTotal);
  pump();
 }finally{batchRunning=false;$('renderAll').disabled=false;}};
async function pump(){
 if(pactive)return;pactive=true;
 try{
  while(pqueue.length){
   const rel=pqueue.shift();
   updateIndicator(pqueue.length+1);
   const ph=cardFor(rel)?.querySelector('.ph');
   if(ph){ph.classList.add('rendering');ph.textContent='\\u23f3';}
   try{const {rate}=await renderPreview(rel);
    const card=cardFor(rel);
    if(card){const media=card.querySelector('.media');
     const k=keyByRel.get(rel)||await previewKey(rel);
     const v=document.createElement('video');
     v.src='/preview/'+k+'.webm?v='+PVV;
     v.autoplay=true;v.muted=true;v.loop=true;v.playsInline=true;
     v.addEventListener('loadedmetadata',()=>{v.playbackRate=Math.min(8,Math.max(.25,rate));});
     media.innerHTML='';media.appendChild(v);}
   }catch(e){console.warn('preview render failed:',rel,e);
    const p2=cardFor(rel)?.querySelector('.ph');
    if(p2){p2.classList.remove('rendering');p2.textContent='\\u26a0';}}
   if(batchTotal>0){batchDone++;updateIndicator(pqueue.length+1);}
  }
 }finally{pactive=false;if(batchTotal&&!pqueue.length)batchTotal=0;updateIndicator(0);}}
function cardFor(rel){return grid.querySelector(`.card[data-rel="${CSS.escape(rel)}"]`);}
loadTree();load();
if(new URLSearchParams(location.search).has('renderall')){
 const iv=setInterval(()=>{const b=$('renderAll');
  if(total>0&&b&&!b.disabled){clearInterval(iv);b.click();}},300);
 setTimeout(()=>clearInterval(iv),20000);}
</script></body></html>"""


DASH_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Library — Dashboard</title><style>
:root{--bg:#14161a;--fg:#dfe3ea;--dim:#8b93a1}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 system-ui;background:var(--bg);color:var(--fg);height:100vh;overflow:hidden;display:flex}
a.tile{position:relative;flex:1;overflow:hidden;display:block;text-decoration:none;color:var(--fg)}
a.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
filter:grayscale(.85) brightness(.5);transition:filter .3s ease,transform .3s ease;transform:scale(1.001)}
a.tile:hover img{filter:grayscale(0) brightness(1);transform:scale(1.06)}
.edge{position:absolute;inset:0;pointer-events:none;transition:opacity .3s}
.l .edge{background:linear-gradient(to right,var(--bg),transparent 14%),
 linear-gradient(to top,var(--bg),transparent 12%),linear-gradient(to bottom,var(--bg),transparent 12%)}
.m .edge{background:linear-gradient(to right,var(--bg),transparent 12%),
 linear-gradient(to left,var(--bg),transparent 12%),
 linear-gradient(to top,var(--bg),transparent 12%),linear-gradient(to bottom,var(--bg),transparent 12%)}
.r .edge{background:linear-gradient(to left,var(--bg),transparent 14%),
 linear-gradient(to top,var(--bg),transparent 12%),linear-gradient(to bottom,var(--bg),transparent 12%)}
.label{position:absolute;left:34px;bottom:30px;text-shadow:0 2px 14px #000}
.m .label{left:50%;transform:translateX(-50%);text-align:center}
.r .label{left:auto;right:34px;text-align:right}
.label h1{font-size:36px;margin:0 0 4px;letter-spacing:.02em}
.label span{color:var(--dim);font-size:14px}
.cut{position:absolute;top:0;bottom:0;width:2px;background:var(--bg);z-index:3}
</style></head><body>
<a class="tile l" href="/animation">
 <img src="{{ANIM_THUMB}}" alt="" onerror="this.style.opacity=0">
 <div class="edge"></div>
 <div class="label"><h1>Animations</h1><span>{{ANIM_CLIPS}} clips · {{ANIM_FOLDERS}} folders · live 3D preview</span></div>
</a>
<a class="tile m" href="/assets">
 <img src="{{ASSET_THUMB}}" alt="" onerror="this.style.opacity=0">
 <div class="edge"></div>
 <div class="label"><h1>Assets</h1><span>{{ASSET_PACKS}} purchases · galleries · store links</span></div>
</a>
<a class="tile m" href="/textures">
 <img src="{{TEX_THUMB}}" alt="" onerror="this.style.opacity=0">
 <div class="edge"></div>
 <div class="label"><h1>Textures</h1><span>{{TEX_COUNT}} texture sets · 4K maps · local files</span></div>
</a>
<a class="tile r" href="/audio">
 <img src="{{AUDIO_THUMB}}" alt="" onerror="this.style.opacity=0">
 <div class="edge"></div>
 <div class="label"><h1>Audio</h1><span>{{AUDIO_COUNT}} sounds · {{AUDIO_HOURS}}h · click to preview</span></div>
</a>
<div class="cut" style="left:25%"></div><div class="cut" style="left:50%"></div>
<div class="cut" style="left:75%"></div>
<a href="/tags" style="position:fixed;bottom:6px;right:66px;font-size:12px;letter-spacing:.04em;color:#8b93a1;text-decoration:none;z-index:9">pack tags</a>
<a href="/registry" style="position:fixed;bottom:6px;right:10px;font-size:10px;letter-spacing:.04em;color:#333a44;text-decoration:none;z-index:9">registry</a>
</body></html>"""


BATCH_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Batch preview renderer</title>
<style>body{background:#111;color:#eee;font:15px monospace;padding:24px}
#big{font-size:26px;color:#7cc4ff;margin-bottom:14px}
#bar{height:14px;background:#222;border-radius:7px;overflow:hidden;margin-bottom:14px;max-width:900px}
#bar i{display:block;height:100%;background:#6ea8fe;width:0%}
.err{color:#ff8a8a}.ok{color:#7dff9b}div.e{margin:2px 0}</style>
<script type="importmap">
{"imports":{"three":"/static/three.module.js","three/addons/":"/static/jsm/"}}
</script>
<script>window.__errs=[];window.addEventListener('error',e=>window.__errs.push(String(e.message||e).slice(0,200)));window.addEventListener('unhandledrejection',e=>window.__errs.push('REJ: '+String(e.reason&&(e.reason.message||e.reason)).slice(0,200)));</script></head><body>
<h2>batch preview renderer &mdash; 4 parallel lanes, 2x recording</h2>
<div id="big">starting&hellip;</div><div id="bar"><i></i></div><div id="log"></div>
<script type="module">
import * as THREE from 'three';
import {FBXLoader} from 'three/addons/loaders/FBXLoader.js';
const $=id=>document.getElementById(id);
const big=$('big'),bar=$('bar').firstElementChild,logEl=$('log');
const el=(t,cls)=>{const d=document.createElement('div');if(cls)d.className=cls;d.textContent=t;logEl.appendChild(d);if(logEl.children.length>12)logEl.firstChild.remove();return d;};
const PW=480,PH=270,FPS=30,MAXDUR=6,SPEED=2,DUMMY="{{RIG_REL}}";  // from the server; '' = no rig
const RATE=(1/SPEED).toFixed(3);
function loadF(rel,dir){
 const L=new FBXLoader();
 L.setResourcePath('/res/'+encodeURIComponent(dir)+"/");
 return new Promise((res,rej)=>L.load("/file?path="+encodeURIComponent(rel),res,undefined,rej));}
async function makeLane(i){
 if(!DUMMY)throw new Error("no retarget rig in this library");
 const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
 renderer.setSize(PW,PH);renderer.setPixelRatio(1);
 const scene=new THREE.Scene();
 scene.background=new THREE.Color(0x101216);
 scene.add(new THREE.HemisphereLight(0xdfe8ff,0x30281f,1.9));
 const key=new THREE.DirectionalLight(0xffffff,2.8);key.position.set(3,6,4);scene.add(key);
 const fill=new THREE.DirectionalLight(0xcfe0ff,1.1);fill.position.set(-4,2,5);scene.add(fill);
 const dummy=await loadF(DUMMY,DUMMY.split("/").slice(0,-1).join("/"));
 const bind=new Map();
 dummy.traverse(o=>{if(o.isBone||o===dummy)bind.set(o,[o.position.clone(),o.quaternion.clone(),o.scale.clone()]);});
 return {renderer,scene,dummy,bind};}
function pfit(cam,holder){
 holder.updateMatrixWorld(true);
 const box=new THREE.Box3().setFromObject(holder);
 if(box.isEmpty())return;
 const s=box.getSize(new THREE.Vector3()),c=box.getCenter(new THREE.Vector3());
 const r=Math.max(s.x,s.y,s.z)||1;
 cam.near=r/100;cam.far=r*40;cam.updateProjectionMatrix();
 cam.position.copy(c).add(new THREE.Vector3(r*1.1,r*0.8,r*1.9));
 cam.lookAt(c);}
async function renderPreview(rel,lane){
 const {renderer,dummy,bind}=lane;
 dummy.traverse(o=>{const b=bind.get(o);
  if(b){o.position.copy(b[0]);o.quaternion.copy(b[1]);o.scale.copy(b[2]);}});
 const obj=await loadF(rel,rel.split("/").slice(0,-1).join("/"));
 const clips=obj.animations||[];
 if(!clips.length)throw new Error("no animation in file");
 let root=obj,hasMesh=false,helper=null;
 obj.traverse(o=>{if(o.isMesh)hasMesh=true;});
 if(!hasMesh){
  const db=new Set();dummy.traverse(o=>{if(o.isBone)db.add(o.name);});
  const cb=new Set();
  for(const t of clips[0].tracks){const m=t.name.match(/bones\\[([^\\]]+)\\]/);cb.add(m?m[1]:t.name.split(".")[0]);}
  let common=0;for(const b of cb)if(db.has(b))common++;
  if(common>=6)root=dummy;else helper=obj;}
 if(root===obj&&!hasMesh)helper=obj;
 const fixMeshes=o=>{o.traverse(n=>{
  if(n.isMesh){n.frustumCulled=false;
   const mats=Array.isArray(n.material)?n.material:[n.material];
   for(const m of mats){if(!m.map&&(!m.color||m.color.getHex()===0))m.color=new THREE.Color(0xb8c0cc);
     if(m.map)m.color=new THREE.Color(0xffffff);}}});};
 fixMeshes(root);
 const scene=new THREE.Scene();
 scene.background=new THREE.Color(0x101216);
 scene.add(new THREE.HemisphereLight(0xdfe8ff,0x30281f,1.9));
 const key=new THREE.DirectionalLight(0xffffff,2.8);key.position.set(3,6,4);scene.add(key);
 const fill=new THREE.DirectionalLight(0xcfe0ff,1.1);fill.position.set(-4,2,5);scene.add(fill);
 const holder=new THREE.Group();holder.add(root);scene.add(holder);
 if(helper)holder.add(new THREE.SkeletonHelper(helper));
 root.rotation.set(-Math.PI/2,0,0);
 root.updateMatrixWorld(true);
 let head=null,hip=null;
 root.traverse(o=>{if(o.isBone){const n=o.name.toLowerCase();
   if(!head&&n.includes("head"))head=o;if(!hip&&/pelvis|hips/.test(n))hip=o;}});
 if(head&&hip){const v=head.getWorldPosition(new THREE.Vector3()).sub(hip.getWorldPosition(new THREE.Vector3()));
   const ax=Math.abs(v.x),ay=Math.abs(v.y),az=Math.abs(v.z);
   if(v.y<0&&ay>=ax&&ay>=az)holder.rotation.z+=Math.PI;
   else if(az>ay&&az>=ax)holder.rotation.x+=(v.z>0?-Math.PI/2:Math.PI/2);
   else if(ax>ay&&ax>=az)holder.rotation.z+=(v.x>0?Math.PI/2:-Math.PI/2);}
 const cam=new THREE.PerspectiveCamera(40,PW/PH,0.01,1e7);
 pfit(cam,holder);
 const clip=clips[0];
 const mixer=new THREE.AnimationMixer(root);
 mixer.clipAction(clip).play();
 const dur=Math.min(clip.duration||3,MAXDUR);
 const mime=["video/webm;codecs=vp9","video/webm;codecs=vp8","video/webm"]
   .find(m=>window.MediaRecorder&&MediaRecorder.isTypeSupported(m));
 if(!mime)throw new Error("MediaRecorder/webm unsupported");
 const stream=renderer.domElement.captureStream(FPS);
 const rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:1500000});
 const chunks=[];rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
 const stopped=new Promise(res=>rec.onstop=res);
 rec.start(100);
 let mixT=0;const t0=performance.now();
 await new Promise(res=>{
  (function step(){
   const wallTotal=Math.max(1.0,dur/SPEED);
   mixT=(performance.now()-t0)/1000;
   if(mixT>=wallTotal||mixT>wallTotal*6+2){res();return;}
   mixer.setTime((mixT*SPEED)%dur);
   holder.updateMatrixWorld(true);
   renderer.render(scene,cam);
   requestAnimationFrame(step);})();
 });
 rec.stop();await stopped;
 const blob=new Blob(chunks,{type:"video/webm"});
 if(blob.size<1024)throw new Error("recording empty ("+blob.size+"B)");
 const r=await fetch("/api/preview/store?clip="+encodeURIComponent(rel)+"&rate="+RATE,
   {method:"POST",body:blob});
 if(!r.ok)throw new Error("store failed: "+r.status);
 scene.remove(holder);}
async function main(){
 let todo=[];
 try{
  for(let p=1;;p++){
   const d=await(await fetch("/api/anim/clips?page="+p+"&size=120")).json();
   for(const c of d.clips)if(!c.preview)todo.push({rel:c.rel,title:c.title});
   if(p*120>=d.total||d.clips.length===0)break;}
 }catch(e){big.textContent="cannot list clips: "+(e&&e.message||e);return;}
 const total=todo.length;
 if(!total){big.textContent="nothing to do -- every clip already has a preview.";return;}
 big.textContent="0 / "+total;
 const lanes=(await Promise.all([0,1,2,3].map(i=>makeLane(i).catch(e=>{
   el("lane "+i+" unavailable: "+(e&&e.message||e),"err");return null;})))).filter(Boolean);
 if(!lanes.length){   // no lane = every clip failed, never "DONE, 0 failed"
  big.textContent="FAILED — 0 rendered, "+total+" failed (of "+total+"): no render lane started. "
   +"The /animation page renders bones-only previews without a rig.";return;}
 el(lanes.length+" parallel lanes running (2x recording speed)");
 let done=0,failed=0,cursor=0;
 async function worker(lane){
  while(cursor<todo.length){
   const item=todo[cursor++];
   try{await renderPreview(item.rel,lane);done++;
    if(done%5===0)el("ok: "+item.title,"ok");}
   catch(e){failed++;el("FAIL "+item.title+" -- "+(e&&e.message||e),"err");}
   big.textContent=done+" rendered · "+failed+" failed · "+total+" total";
   bar.style.width=Math.round((done+failed)/total*100)+"%";}}
 await Promise.all(lanes.map(l=>worker(l)));
 big.textContent="DONE — "+done+" rendered, "+failed+" failed (of "+total+"). You can close this tab.";
 el("all done — previews are cached on disk and show on the animation page","ok");}
main();
</script></body></html>"""










PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>AI Asset Registry</title><style>
:root{--bg:#14161a;--card:#1e2128;--line:#2c313a;--fg:#dfe3ea;--dim:#8b93a1}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;flex-wrap:wrap;align-items:center}
h1{font-size:16px;margin:0}input,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px}
#status{padding:8px 20px;border-bottom:1px solid var(--line);color:var(--dim);font-size:12.5px;white-space:pre-wrap}
main{padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.card img{width:100%;height:150px;object-fit:cover;display:block;background:#000}
.badge{height:150px;display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:700;color:#fff}
.card .body{padding:10px 12px}
.name{font-weight:600;margin-bottom:4px;word-break:break-word}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0}
.chip{background:#2a2f39;border-radius:20px;padding:1px 9px;font-size:11.5px;color:var(--dim)}
.chip.domain{color:#7cc4ff}.chip.style{color:#ffd479}.chip.human{color:#7dff9b}
.meta{color:var(--dim);font-size:12px}
.meta a{color:inherit}
.bar{height:4px;background:#2a2f39;border-radius:2px;margin-top:8px}
.bar i{display:block;height:100%;border-radius:2px;background:#6ea8fe}
.detail{padding:0 12px 12px;color:var(--dim);font-size:12px;word-break:break-all}
.card{position:relative;cursor:pointer}
.card:hover{border-color:#3a4250}
.card.selected{outline:2px solid #6ea8fe}
.selbox{position:absolute;top:8px;left:8px;width:18px;height:18px;accent-color:#6ea8fe;z-index:2}
#bar{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);
padding:10px 20px;display:none;gap:10px;align-items:center;flex-wrap:wrap}
#bar.show{display:flex}
#bar .meta{margin-right:4px}
#msg{padding:6px 20px;color:#7dff9b;font-size:12.5px;display:none}
.btn3d{background:#243040;color:#7cc4ff;border:1px solid #31506e;border-radius:6px;
padding:2px 10px;margin-top:7px;cursor:pointer;font-size:12px}
.btn3d:hover{background:#2b3b50}
</style></head><body>
<header><a href="/" style="color:#8b93a1;text-decoration:none;margin-right:2px">&larr; home</a><h1>AI Asset Registry</h1><a href="/animation" style="color:#7cc4ff;text-decoration:none">Animations</a><a href="/tags" style="color:#7cc4ff;text-decoration:none">Pack tags</a>
<input id="q" placeholder="search name / tags / path ..." size="34">
<select id="domain"><option value="">all domains</option></select>
<select id="style"><option value="">all styles</option></select>
<label class="meta"><input type="checkbox" id="selAll"> select all shown</label>
<span class="meta" id="count"></span></header>
<div id="status"></div><div id="msg"></div><main id="grid"></main>
<div id="bar"><span class="meta" id="selcount"></span>
<select id="setDomain"><option value="">set domain…</option></select>
<select id="setStyle"><option value="">set style…</option></select>
<button onclick="markValidated('validated_blender')">Mark Validated (Blender)</button>
<button onclick="markValidated('validated_unreal')">Mark Validated (Unreal)</button>
<button onclick="clearSel()">clear selection</button></div>
<script>
let packs=[];const sel=new Set();
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLORS={Environment:'#3f7d4e','Human Animations':'#7a5fb5',Archviz:'#b5823f',Props:'#b5455f',Characters:'#3f7db5','Textures_HDRI':'#8a6db0'};
const open=id=>location.href='/pack?id='+encodeURIComponent(id);
async function loadStats(){const r=await(await fetch('/api/stats')).json();
 for(const d of r.domains){domain.insertAdjacentHTML('beforeend',`<option>${esc(d)}</option>`);setDomain.insertAdjacentHTML('beforeend',`<option>${esc(d)}</option>`)}
 for(const s of r.styles){style.insertAdjacentHTML('beforeend',`<option>${esc(s)}</option>`);setStyle.insertAdjacentHTML('beforeend',`<option>${esc(s)}</option>`)}
 const st=r.extension_status&&r.extension_status[0];
 status.textContent=st?`last extension action: ${st.action} ${st.ok?'OK':'FAILED'} — ${st.target} — ${st.at}${st.detail?('\\n'+st.detail.replace(/\\n/g,' | ')):''}`:`no extension activity recorded yet`;
 status.style.color=st&&!st.ok?'#ff8a8a':'var(--dim)';}
async function load(){const p=new URLSearchParams({q:q.value,domain:domain.value,style:style.value});
 packs=await(await fetch('/api/packs?'+p)).json();count.textContent=packs.length+' packs';
 for(const id of[...sel])if(!packs.some(a=>a.id===id))sel.delete(id);
 // every interpolated value is esc()'d and ids travel in data-id
 // attributes read by the delegated listener below -- NEVER inside inline
 // onclick strings (pack ids are folder paths; ' is a legal filename
 // character on Windows and broke out of the old string splices)
 grid.innerHTML=packs.map((a)=>`<div class="card${sel.has(a.id)?' selected':''}" data-id="${esc(a.id)}">
 <input type="checkbox" class="selbox" ${sel.has(a.id)?'checked':''}>
 <div class="open" title="open file list">
 ${a.thumb?`<img loading="lazy" src="${esc(a.thumb)}">`:`<div class="badge" style="background:${COLORS[a.domain]||'#444'}">${esc((a.domain||'?')[0])}</div>`}
 <div class="body"><div class="name">${esc(a.name)}</div>
 <div class="chips">
 <span class="chip domain">${esc(a.domain)}</span><span class="chip">${esc(a.sub_category)}</span>
 <span class="chip style">${esc(a.style)}</span>${a.styles.includes('Low Poly')?'<span class="chip style">Low Poly</span>':''}${a.owner_tagged?'<span class="chip human">yours</span>':''}</div>
 <div class="meta">${esc(a.vendor||'—')} · <a href="/pack?id=${encodeURIComponent(a.id)}">${a.files} files</a> · ${(a.bytes/1e9).toFixed(1)} GB · ${esc(a.formats.slice(0,4).join(', '))}</div>
 <div class="bar"><i style="width:${Math.round((a.confidence||0)*100)}%"></i></div>
 ${a.formats.some(f=>f==='fbx'||f==='bvh')?`<button class="btn3d">3D &gt; play</button>`:''}</div></div>
 <div class="detail" style="display:none">${esc(a.hero)}<br>tags: ${esc(a.tags.slice(0,8).join(', '))}<br>confidence ${(a.confidence||0).toFixed(2)} · updated ${esc(a.updated_at)} · status ${esc(a.status||'')}</div></div>`).join('');
 grid.querySelectorAll('.card').forEach(card=>{
  card.querySelector('.selbox').addEventListener('click',e=>{e.stopPropagation();toggle(card.dataset.id);});
  const chips=card.querySelector('.chips');
  chips.addEventListener('click',e=>{e.stopPropagation();
   const d=card.querySelector('.detail');d.style.display=d.style.display==='block'?'none':'block';});
  const b3=card.querySelector('.btn3d');
  if(b3)b3.addEventListener('click',e=>{e.stopPropagation();
   location.href='/viewer?pack='+encodeURIComponent(card.dataset.id);});
  card.querySelector('.open').addEventListener('click',()=>open(card.dataset.id));});
 selAll.checked=packs.length>0&&packs.every(a=>sel.has(a.id));renderBar();}
function toggle(id){sel.has(id)?sel.delete(id):sel.add(id);
 const card=grid.querySelector(`.card:nth-child(${packs.findIndex(a=>a.id===id)+1})`);
 if(card){card.classList.toggle('selected',sel.has(id));card.querySelector('.selbox').checked=sel.has(id)}
 selAll.checked=packs.length>0&&packs.every(a=>sel.has(a.id));renderBar();}
selAll.onchange=()=>{if(selAll.checked)packs.forEach(a=>sel.add(a.id));else packs.forEach(a=>sel.delete(a.id));load();};
function clearSel(){sel.clear();load();}
function renderBar(){selcount.textContent=sel.size+' pack(s) selected';bar.classList.toggle('show',sel.size>0)}
async function retag(body){if(!sel.size)return;
 const r=await(await fetch('/api/retag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 msg.style.display='block';msg.textContent=`updated ${r.updated} pack(s) at ${new Date().toLocaleTimeString()}`;
 setDomain.value='';setStyle.value='';setTimeout(load,300);}
setDomain.onchange=()=>{if(setDomain.value)retag({ids:[...sel],set:{domain:setDomain.value}})};
setStyle.onchange=()=>{if(setStyle.value)retag({ids:[...sel],set:{style:setStyle.value}})};
function markValidated(status){retag({ids:[...sel],status})}
q.oninput=()=>{clearTimeout(window._t);window._t=setTimeout(load,250)};
domain.onchange=style.onchange=load;loadStats();load();
</script></body></html>"""


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

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        url = urlparse(self.path)
        params = parse_qs(url.query)
        try:
            if url.path == "/":
                _prefix = _anim_prefix()
                # the rig's pack thumbnail (under the configured section)
                anim = _pack_thumb.get(
                    _prefix + "/" + _RIG_SUBPATH.split("/")[0])
                conn = db.connect(_DB_PATH)
                aclips = conn.execute(
                    "SELECT COUNT(*) FROM asset_files f JOIN assets a ON a.id=f.asset_id "
                    "WHERE substr(a.id, 1, ?) = ? AND (lower(f.relative_path) "
                    "LIKE '%.fbx' OR lower(f.relative_path) LIKE '%.bvh')",
                    (len(_prefix), _prefix)).fetchone()[0]
                afold = conn.execute(
                    "SELECT COUNT(*) FROM assets WHERE substr(id, 1, ?) = ?",
                    (len(_prefix), _prefix)).fetchone()[0]
                apacks = conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
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
                        .replace("{{ANIM_THUMB}}",
                                 "/cimg?path=" + _pq(anim_pick.as_posix())
                                 if anim_pick is not None else
                                 ("/thumb/" + str(anim) if anim is not None
                                  else "/static/heroes/animation.svg"))
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
            elif url.path == "/registry":
                self._html(PAGE.encode("utf-8"))
            elif url.path == "/tags":
                self._html(TAGS_PAGE.encode("utf-8"))
            elif url.path == "/api/pack_tags":
                self._json(api_pack_tags())
            elif url.path == "/pack":
                self._html(PACK_PAGE.encode("utf-8"))
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
            elif url.path == "/api/packs":
                self._json(api_packs(params))
            elif url.path == "/api/pack_files":
                self._json(api_pack_files(params))
            elif url.path == "/api/clips":
                self._json(api_clips(params))
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
                # live 3D viewer source file (FBX/BVH under canonical roots only)
                target = _safe_asset_file((params.get("path") or [""])[0],
                                          PREVIEW_EXTS)
                if not target:
                    return self._json({"error": "not found"}, 404)
                self._bytes(target.read_bytes(), "application/octet-stream")
            elif url.path.startswith("/res/"):
                # texture resources for the FBX viewer: /res/<enc dir>/<name>.
                # Clips often reference shared textures that live elsewhere in
                # the pack; on miss, fall back to mirrored thumbnail copies
                # (same filenames, already cached on disk).
                remainder = url.path[len("/res/"):]
                enc_dir, _, name = remainder.partition("/")
                directory = _safe_dir(enc_dir)
                target = None
                if directory and name:
                    target = _safe_asset_file(
                        str(Path(directory) / unquote(name)), TEXTURE_EXTS)
                if target is None and name:
                    base = unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
                    with _thumb_lock:
                        entry = next((t for t in _thumbs
                                      if t["key"].rsplit("/", 1)[-1] == base), None)
                    if entry and Path(entry["abs"]).suffix.lower() in TEXTURE_EXTS:
                        target = Path(entry["abs"])
                if not target or not target.is_file():
                    return self._json({"error": "not found"}, 404)
                self._bytes(target.read_bytes(),
                            mimetypes.guess_type(str(target))[0]
                            or "application/octet-stream")
            elif url.path.startswith("/thumb/"):
                try:
                    index = int(url.path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad index"}, 400)
                with _thumb_lock:
                    entry = next((t for t in _thumbs if t["index"] == index), None)
                if not entry or not Path(entry["abs"]).is_file():
                    return self._json({"error": "not found"}, 404)
                self._bytes(Path(entry["abs"]).read_bytes(),
                            mimetypes.guess_type(entry["abs"])[0] or "image/jpeg")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request rejected"}, 403)
        """Bulk re-tagging: {"ids": [...], "set": {domain|style} | "status"}.
        Domain and style are written to the pack tag file; a status marks
        the pack 'human_verified' with confidence 1.0 in the registry."""
        params = parse_qs(urlparse(self.path).query)
        try:
            path = urlparse(self.path).path
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
            if path == "/api/pack_source":
                # link a pack to the store page it was bought from
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                pid = body.get("id") or ""
                surl = (body.get("url") or "").strip()
                if not re.fullmatch(r"pack::[\w .()&!',#%/+-]+", pid):
                    return self._json({"error": "bad pack id"}, 400)
                if surl and not (surl.startswith("http://") or surl.startswith("https://")) \
                        or len(surl) > 500:
                    return self._json({"error": "url must start with http(s)://"}, 400)
                conn = db.connect(_DB_PATH)
                cur = conn.execute("UPDATE assets SET source_url=? WHERE id=?",
                                   (surl or None, pid))
                conn.commit()
                conn.close()
                return self._json({"ok": True, "updated": cur.rowcount})
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
            if path != "/api/retag":
                return self._json({"error": "not found"}, 404)
            body, err = self._json_body()
            if err:
                return self._json({"error": err}, 415)
            ids = [i for i in body.get("ids", [])
                   if isinstance(i, str) and re.fullmatch(r"pack::[\w .()&!',#%/+-]+", i)]
            payload = body.get("set") or {}
            dom = payload.get("domain") if payload.get("domain") in DOMAINS else None
            sty = payload.get("style")
            sty = (pack_tags.marker(sty) or (sty if sty in STYLES else None)
                   if isinstance(sty, str) else None)
            status = body.get("status")
            if status not in ("validated_blender", "validated_unreal"):
                status = None
            if not ids or not (dom or sty or status):
                return self._json({"error": "nothing to do"}, 400)
            updated = 0
            if dom or sty:
                # domain and style are the owner's pack tags: they go to
                # the tag file, which survives a registry rebuild
                _sync_pack_tags()
                rows = _tag_rows()
                for i in ids:
                    r = rows.get(i[len("pack::"):].lower())
                    if r is None:
                        continue
                    keep = [s for s in r["styles"] if s not in _LOOKS]
                    styles = (None if not sty else
                              r["styles"] + ["Low Poly"] if sty == "Low Poly"
                              else [sty] + keep if sty in _LOOKS else keep)
                    try:
                        pack_tags.update(r["folder"], styles=styles,
                                         domain=dom)
                    except pack_tags.TagFileBusy as exc:
                        return self._json({"error": str(exc)}, 409)
                    updated += 1
            if status:
                conn = db.connect(_DB_PATH)
                cur = conn.execute(
                    f"UPDATE assets SET status=?, "
                    f"validation_status='human_verified', confidence_score=1.0, "
                    f"updated_at=CURRENT_TIMESTAMP "
                    f"WHERE id IN ({','.join('?' * len(ids))})",
                    [status] + ids)
                conn.commit()
                updated = max(updated, cur.rowcount)
                conn.close()
            self._json({"updated": updated})
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


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    global _DB_PATH
    parser = argparse.ArgumentParser(description="Registry browser (localhost)")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=config.NETWORK["port"])
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--init", action="store_true",
                        help="first-run: detect folders and write pharos_config.json")
    args = parser.parse_args(argv)
    if args.init:
        from asset_service import init as pharos_init
        pharos_init.run_init()
        if not config.is_configured():
            return 0
    if not config.is_configured():
        print("pharos_config.json has no library_root/registry_dir set.\n"
              "Run:  python -m asset_service init <path-to-your-assets-folder>\n"
              "(or copy pharos_config.example.json and edit it by hand)",
              flush=True)
        return 2
    _DB_PATH = args.db
    # a registry left behind by ANOTHER library: refuse before the
    # importers write into it (they kept its rows, mixed with this one's)
    _owner = db.registry_foreign(_DB_PATH, config.LIBRARY_ROOT)
    if _owner:
        print(db.foreign_registry_message(_DB_PATH, _owner,
                                          config.LIBRARY_ROOT), flush=True)
        return 2

    build_thumb_index()
    db.init_db(_DB_PATH)   # applies schema migrations (source_url etc.)
    c_n = t_n = a_n = m_n = 0
    try:
        c_n = collection_import.import_collection(_DB_PATH)
        print(f"collection imported: {c_n} assets", flush=True)
    except Exception as exc:  # noqa: BLE001 -- CSV missing/moved must not kill the app
        print(f"collection import skipped: {exc}", flush=True)
    try:
        t_n = textures_import.import_textures(_DB_PATH)
        print(f"textures imported: {t_n} sets", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"textures import skipped: {exc}", flush=True)
    try:
        a_n = audio_import.import_audio(_DB_PATH)
        print(f"audio imported: {a_n} files", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"audio import skipped: {exc}", flush=True)
    try:
        m_n = meshes_import.import_meshes(_DB_PATH)
        print(f"meshes imported: {m_n} records", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"meshes import skipped: {exc}", flush=True)
    try:
        added = _sync_pack_tags()
        if added:
            print(f"pack tags: {added} new pack(s) listed in "
                  f"{pack_tags.PATH} (tag them at /tags)", flush=True)
    except Exception as exc:  # noqa: BLE001 -- a locked file must not stop the app
        print(f"pack tags not updated: {exc}", flush=True)
    try:
        db.stamp_registry(_DB_PATH, config.LIBRARY_ROOT)   # this library's
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
    host = config.NETWORK["host"]
    warn = _loopback_warning(host)
    if warn:
        print("!" * 70 + "\n" + warn + "\n" + "!" * 70, flush=True)
    # explicit completion feedback: counts + folders nobody indexed, so a
    # human watching the console never wonders whether it worked
    try:
        _c = db.connect(_DB_PATH)
        _prefix = _anim_prefix()
        _afold = _c.execute(
            "SELECT COUNT(*) FROM assets WHERE substr(id, 1, ?) = ?",
            (len(_prefix), _prefix)
        ).fetchone()[0]
        _c.close()
    except Exception:                                     # noqa: BLE001
        _afold = 0
    print(f"INGESTION COMPLETE -- collection={c_n}"
          f" textures={t_n} audio={a_n} meshes={m_n}"
          f" anim_packs={_afold}", flush=True)
    if config.is_configured():
        # the same rule as ingest's ASK block (sections, scan folders, skip
        # dirs, manifest/kit roots AND the registry's rows): listing indexed
        # scan folders here told agents to re-scan them -> duplicates
        from asset_service.init import _not_indexed_dirs
        strays = _not_indexed_dirs(config.LIBRARY_ROOT, config.load(),
                                   _DB_PATH)
        if strays:
            print(f"NOT INDEXED (no section claims these): "
                  f"{', '.join(strays)}", flush=True)
            print("  -> scan with scanner.py <folder> or edit "
                  "pharos_config.json sections, then restart", flush=True)
    print(f"registry browser: http://{host}:{args.port}  "
          f"(db={_DB_PATH}, thumbs={len(_thumbs)})", flush=True)
    server = ThreadingHTTPServer((host, args.port), Handler)
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(
            f"http://{host}:{args.port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
