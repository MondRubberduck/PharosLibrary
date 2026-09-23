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


def _fold(s: str) -> str:
    """Accent-fold for search: 'vâse' <-> 'vase'. Names in non-English
    libraries are full of umlauts/accents; folding BOTH sides of the
    comparison keeps plain-keyboard queries working."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


def _name_hit(tok: str, name_l: str) -> bool:
    """Short tokens match on word boundaries only ('lamp' != 'clamp');
    longer tokens may substring ('hologram' in 'holograms02'). Both
    sides are accent-folded so 'vase' finds 'Vâse'."""
    name_l = _fold(name_l)
    tok = _fold(tok)
    if len(tok) >= 5:
        return tok in name_l
    return re.search(r"\b" + re.escape(tok), name_l) is not None


SYNONYMS = {
    "ac": ["acunit", "airconditioner", "hvac", "air"],
    "tv": ["television", "monitor", "screen"],
    "car": ["vehicle", "automobile", "sedan", "suv"],
    "bike": ["motorcycle", "motorbike", "scooter"],
    "shack": ["favela", "slum", "shanty", "cabin", "shed"],
    "trash": ["garbage", "dumpster", "rubble", "debris"],
    "sofa": ["couch", "settee"],
    "gun": ["firearm", "pistol", "rifle", "weapon"],
    "phone": ["smartphone", "mobile", "telephone"],
}


def _synonyms(token: str) -> set:
    """Return synonym stems for known abbreviations/aliases."""
    out = set()
    for base, syns in SYNONYMS.items():
        if token == base or token in syns:
            out.add(base)
            out.update(_stems(" ".join(syns)))
    if out:
        out.add(token)
        out |= _stems(token)
    return out


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
            "domains": list(DOMAINS), "styles": list(STYLES)}


def api_packs(params: dict) -> list[dict]:
    q = (params.get("q") or [""])[0].strip()
    domain = (params.get("domain") or [""])[0].strip() or None
    style = (params.get("style") or [""])[0].strip() or None
    vendor = (params.get("vendor") or [""])[0].strip() or None
    validated = (params.get("validated") or [""])[0] in ("1", "true")
    conn = db.connect(_DB_PATH)
    rows = db.search_assets(conn, q or None, style=style, domain=domain,
                            vendor=vendor, validated_only=validated, limit=500)
    out = []
    for r in rows:
        detail = None
        if (params.get("detail") or [""])[0] == "1":
            detail = db.get_asset_details(conn, r["id"])
        counts = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(file_size),0) AS bytes "
            "FROM asset_files WHERE asset_id = ?", (r["id"],)).fetchone()
        thumb = _pack_thumb.get(r["id"])
        out.append({
            "id": r["id"], "name": r["name"], "domain": r["domain"],
            "sub_category": r["sub_category"], "style": r["style"],
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
    if not re.fullmatch(r"pack::[\w .()&!',#%-]+", pack_id):
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


def _stems(word: str) -> set:
    w = word.lower()
    out = {w}
    for suf, cut in (("ing", 3), ("ed", 2), ("s", 1), ("er", 2)):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            out.add(w[:-cut])
    for s in list(out):
        if len(s) > 3 and s[-1] == s[-2] and s[-1] not in "lsz":
            out.add(s[:-1])          # running -> runn -> run
    return out


def _word_hits(words: list, name_l: str, searchable: set) -> list:
    """Per query word: 2 = a variant hits the name, 1 = a variant is in the
    row's tags/themes/stems, 0 = no hit. `words` holds ONE stem-variant
    group per query word ('crates' -> {'crates', 'crate'}); a word matches
    when ANY of its variants does. Shared by the four item endpoints:
    merging every word's variants into one set and requiring ALL of them
    made plural queries fail AND matching."""
    return [2 if any(_name_hit(x, name_l) for x in g)
            else 1 if g & searchable else 0 for g in words]


def _cluster_stems() -> list[set]:
    out = []
    for cl in ANIM_CLUSTERS:
        st = set()
        for w in cl:
            st |= _stems(w)
        out.append(st)
    return out


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


def api_collection_items(params: dict) -> dict:
    q = (params.get("q") or [""])[0].strip().lower()
    store = (params.get("store") or [""])[0]
    group = (params.get("group") or [""])[0]
    cat = (params.get("cat") or [""])[0]
    avail_f = (params.get("avail") or [""])[0]
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    try:
        size = min(500, max(8, int((params.get("size") or ["24"])[0])))
    except ValueError:
        size = 24

    words = [{s for s in _stems(t) if len(s) >= 2}   # drop 1-char tokens
             for t in re.findall(r"[^\W_]+", q, re.UNICODE)]
    words = [w for w in words if w]
    qstems: set = set().union(*words)
    if q and not qstems:
        # every token was dropped (single letters / punctuation) -- such a
        # query must match nothing, never the whole library
        return {"total": 0, "exact": 0, "mode": "and", "page": page,
                "size": size, "q": q, "items": []}
    related: set = set()
    if qstems:
        for cl in COLL_CLUSTERS:
            cst: set = set()
            for w in cl:
                cst |= _stems(w)
            if qstems & cst:
                related |= cst
        related -= qstems

    # 1-2 letter stems ('fi' from 'sci fi') would substring-match 'file' etc.
    qsub = {t for t in qstems if len(t) >= 3}
    rsub = {t for t in related if len(t) >= 3}

    from urllib.parse import quote as _q
    items = []
    or_items = []
    rel_items = []
    for r in _coll_rows():
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
        meta = r.get("meta") or {}
        searchable = set(r["taglist"]) | set(meta.get("themes") or []) \
            | set(meta.get("stems") or [])
        name_l = (r["name"] or "").lower()
        if qstems:
            # AND over query words; single-word matches are reserved as a
            # fallback tier for list-style queries ("lamp sign hydrant"),
            # and the related cluster only answers when NO word matches
            hits = _word_hits(words, name_l, searchable)
            if not any(hits) and not (any(s in name_l for s in rsub)
                                      or any(s in searchable for s in rsub)):
                continue
            tier = (1 if min(hits) == 2 else 2) if all(hits) else 3
        else:
            tier = 0
        entry = {
            "id": r["id"], "name": r["name"], "store": r["store"],
            "group": r["type_group"], "cat": r["type_cat"], "sub": r["type_sub"],
            "seller": r["seller"], "price": r["price"], "url": r["url"],
            "image_count": r["image_count"],
            "availability": r.get("availability") or "",
            "thumb": "/cimg?path=" + _q(_thumb_path(r)) if _thumb_path(r) else "",
            "tier": tier}
        if tier < 3:
            items.append(entry)
        else:
            (or_items if any(hits) else rel_items).append(entry)
    if not items:
        items = or_items or rel_items
    items.sort(key=lambda x: (x["tier"], x["store"], x["name"].lower()))
    total = len(items)
    exact = sum(1 for e in items if e["tier"] < 3)
    lo = (page - 1) * size
    return {"total": total, "exact": exact if qstems else total,
            "mode": ("or-fallback" if qstems and exact == 0 and total
                     else "and"),
            "page": page, "size": size, "q": q,
            "items": items[lo:lo + size]}


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
    out = dict(r); out["item"] = r; return out


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
    q = (params.get("q") or [""])[0].strip().lower()
    grp = (params.get("group") or [""])[0]
    sub = (params.get("sub") or [""])[0]
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    try:
        size = min(500, max(8, int((params.get("size") or ["24"])[0])))
    except ValueError:
        size = 24
    words = [{s for s in _stems(t) if len(s) >= 2}   # drop 1-char tokens
             for t in re.findall(r"[^\W_]+", q, re.UNICODE)]
    words = [w for w in words if w]
    qstems: set = set().union(*words)
    if q and not qstems:
        return {"total": 0, "exact": 0, "mode": "and", "page": page,
                "size": size, "q": q, "items": []}
    related: set = set()
    if qstems:
        for cl in TEX_CLUSTERS:
            cst: set = set()
            for w in cl:
                cst |= _stems(w)
            if qstems & cst:
                related |= cst
        related -= qstems
    qsub = {t for t in qstems if len(t) >= 3}
    rsub = {t for t in related if len(t) >= 3}
    from urllib.parse import quote as _q
    items = []
    or_items = []
    rel_items = []
    for r in _tex_rows():
        if grp and r["grp"] != grp:
            continue
        if sub and r["sub"] != sub:
            continue
        searchable = (set(r["tags"]) | set(r["meta"].get("themes") or [])
                      | set(r["meta"].get("stems") or []))
        name_l = (r["name"] or "").lower()
        if qstems:
            # AND first; single-word matches reserved as fallback tier; the
            # related cluster only answers when NO query word matches
            hits = _word_hits(words, name_l, searchable)
            if not any(hits) and not (any(s in name_l for s in rsub)
                                      or any(s in searchable for s in rsub)):
                continue
            tier = (1 if min(hits) == 2 else 2) if all(hits) else 3
        else:
            tier = 0
        entry = {
            "id": r["id"], "name": r["name"], "grp": r["grp"], "sub": r["sub"],
            "image_count": len(r["images"]),
            "res": (r.get("meta") or {}).get("res") or "",
            "thumb": "/timg?path=" + _q(r["thumb"]) if r["thumb"] else "",
            "tier": tier}
        if tier < 3:
            items.append(entry)
        else:
            (or_items if any(hits) else rel_items).append(entry)
    if not items:
        items = or_items or rel_items
    items.sort(key=lambda x: (x["tier"], x["grp"], x["sub"], x["name"].lower()))
    total = len(items)
    exact = sum(1 for e in items if e["tier"] < 3)
    lo = (page - 1) * size
    return {"total": total, "exact": exact if qstems else total,
            "mode": ("or-fallback" if qstems and exact == 0 and total
                     else "and"),
            "page": page, "size": size, "q": q,
            "items": items[lo:lo + size]}


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
    r["images"] = ["/timg?path=" + _q(i) for i in r["images"]]
    total_mb = (r.get("bytes") or 0) / 1e6
    r["size_str"] = (f"{total_mb:.0f} MB" if total_mb >= 1
                     else f"{(r.get('bytes') or 0) / 1e3:.0f} KB")
    r["tags"] = [t for t in r["tags"] if t not in
                 ("loose", "files", "4k", "textures", "gumroad")][:16]
    r["res"] = (r.get("meta") or {}).get("res") or ""
    r["desc"] = (r.get("meta") or {}).get("desc") or ""
    r["themes"] = (r.get("meta") or {}).get("themes") or []
    out = dict(r); out["item"] = r; return out


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
    q = (params.get("q") or [""])[0].strip().lower()
    cat = (params.get("cat") or [""])[0]
    sub = (params.get("sub") or [""])[0]
    min_dur = None
    try:
        min_dur = float((params.get("min_dur") or
                         params.get("min_duration") or ["0"])[0])
    except ValueError:
        min_dur = None
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    try:
        size = min(500, max(8, int((params.get("size") or ["24"])[0])))
    except ValueError:
        size = 24
    words = [{s for s in _stems(t) if len(s) >= 2}   # drop 1-char tokens
             for t in re.findall(r"[^\W_]+", q, re.UNICODE)]
    words = [w for w in words if w]
    qstems: set = set().union(*words)
    if q and not qstems:
        return {"total": 0, "exact": 0, "mode": "and", "page": page,
                "size": size, "q": q, "items": []}
    related: set = set()
    if qstems:
        for cl in AUDIO_CLUSTERS:
            cst: set = set()
            for w in cl:
                cst |= _stems(w)
            if qstems & cst:
                related |= cst
        related -= qstems
    qsub = {t for t in qstems if len(t) >= 3}
    rsub = {t for t in related if len(t) >= 3}
    items = []
    or_items = []
    rel_items = []
    for r in _audio_rows():
        if cat and r["cat"] != cat:
            continue
        if sub and r["sub"] != sub:
            continue
        if min_dur is not None and (r["dur"] or 0) < min_dur:
            continue
        searchable = set(r["tags"]) | set(r["meta"].get("themes") or []) \
            | set(r["meta"].get("stems") or [])
        name_l = (r["name"] or "").lower()
        if qstems:
            # AND first; single-word matches reserved as fallback tier; the
            # related cluster only answers when NO query word matches
            hits = _word_hits(words, name_l, searchable)
            if not any(hits) and not (any(s in name_l for s in rsub)
                                      or any(s in searchable for s in rsub)):
                continue
            tier = (1 if min(hits) == 2 else 2) if all(hits) else 3
        else:
            tier = 0
        entry = {
            "id": r["id"], "name": r["name"], "cat": r["cat"], "sub": r["sub"],
            "dur": r["dur"], "playable": r["playable"], "rel": r["rel"],
            "tier": tier}
        if tier < 3:
            items.append(entry)
        else:
            (or_items if any(hits) else rel_items).append(entry)
    if not items:
        items = or_items or rel_items
    items.sort(key=lambda x: (x["tier"], x["cat"], x["sub"],
                              x["name"].lower()))
    total = len(items)
    exact = sum(1 for e in items if e["tier"] < 3)
    lo = (page - 1) * size
    return {"total": total, "exact": exact if qstems else total,
            "mode": ("or-fallback" if qstems and exact == 0 and total
                     else "and"),
            "page": page, "size": size, "q": q,
            "items": items[lo:lo + size]}


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
    out = dict(r); out["item"] = r; return out


# ---------------------------------------------------------------------------
# meshes (geometry layer) -- what agents need for scene building
# ---------------------------------------------------------------------------

_EMPTY_RECIPE = {"slots": [], "primary": {}, "resolved": False,
                 "primary_slot": None}


def _mesh_rows() -> list:
    conn = db.connect(_DB_PATH)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM meshes ORDER BY pack, name")]
    except sqlite3.OperationalError:
        rows = []       # table not created yet (importer skipped) -> empty
    finally:
        conn.close()
    for r in rows:
        for key in ("tags", "meta", "materials", "texture_files"):
            try:
                r[key] = json.loads(r.get(key) or "[]")
            except (ValueError, TypeError):
                r[key] = []
        try:
            r["recipe"] = json.loads(r.get("recipe") or "null")
        except (ValueError, TypeError):
            r["recipe"] = None
        if not isinstance(r["recipe"], dict):
            r["recipe"] = dict(_EMPTY_RECIPE)
    return rows


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
        "dim_suspect": bool((meta.get("facets") or {}).get("dim_suspect")),"recipe": recipe, "hero_textures": hero,
            "bbox_min": _bbox_triple(r, "bbox_min"),
            "bbox_max": _bbox_triple(r, "bbox_max")}


def _bbox_triple(r: dict, prefix: str):
    """[x, y, z] for bbox_min/bbox_max, or None if the manifest had no box."""
    vals = [r.get(f"{prefix}_{ax}") for ax in ("x", "y", "z")]
    return None if all(v is None for v in vals) else vals


def api_meshes(params: dict) -> dict:
    """Geometry-aware mesh search.

    Filters: q (name/tags/themes, stem-aware), pack, source, theme,
    min_dim/max_dim (metres, on the largest axis), min_h/max_h (bbox Z:
    the registry is Z-up), max_tri (triangle budget), textures=1 (only
    textured), sort. Size and triangle filters exclude rows without that
    measurement (unknown triangles are NULL) and disclose the count as
    `unmeasured_excluded`.
    """
    q = (params.get("q") or [""])[0].strip().lower()
    pack = (params.get("pack") or [""])[0]
    source = (params.get("source") or [""])[0]
    theme = (params.get("theme") or params.get("themes") or [""])[0]

    def _f(name: str, default: float = 0.0) -> float:
        try:
            return float((params.get(name) or [str(default)])[0])
        except ValueError:
            return default

    min_dim, max_dim = _f("min_dim"), _f("max_dim", 1e9)
    min_h, max_h = _f("min_h"), _f("max_h", 1e9)
    max_tri = _f("max_tri", 1e12)
    only_tex = (params.get("textures") or [""])[0] == "1"
    sort = (params.get("sort") or ["name"])[0]
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    try:
        size = min(500, max(8, int((params.get("size") or ["50"])[0])))
    except ValueError:
        size = 50

    words = [{s for s in _stems(t) if len(s) >= 2}   # drop 1-char tokens
             for t in re.findall(r"[^\W_]+", q, re.UNICODE)]
    words = [w for w in words if w]
    qstems: set = set().union(*words)
    if q and not qstems:
        # every token was dropped -- match nothing, never the whole library
        return {"total": 0, "exact": 0, "mode": "and", "page": page,
                "size": size, "q": q, "items": []}

    # a dimension/height/triangle filter is a MEASUREMENT query: rows
    # without that measurement must be EXCLUDED, never silently passed
    # through (unmeasured assets with max_dim 0 once won every size filter)
    dim_filter = (min_dim > 0 or max_dim < 1e9)
    h_filter = (min_h > 0 or max_h < 1e9)
    tri_filter = max_tri < 1e12
    unmeasured = 0

    def _mesh_item(r, tier):
        return {
            "id": r["id"], "name": r["name"], "pack": r["pack"],
            "source": r["source"], "kind": r["kind"],
            "fbx": r["fbx"], "fbx_path": r["fbx"],
            "on_disk": r["on_disk"], "triangles": r["triangles"],
            "vertices": r["vertices"], "submeshes": r["submeshes"],
            "bbox_m": [r["bbox_x"], r["bbox_y"], r["bbox_z"]],
            "max_dim_m": r["max_dim"], "max_dim": r["max_dim"],
            "height_m": r["bbox_z"],          # the registry is Z-up
            "materials": r["materials"] if isinstance(r["materials"], list)
                         else [],
            "texture_count": r["texture_count"],
            "view_url": "/viewer?file=" + url_quote(r["fbx"]),
            "themes": r["meta"].get("themes") or [],
            **_mesh_extras(r),
            "tier": tier,
        }

    items = []
    or_items = []
    for r in _mesh_rows():
        if pack and r["pack"] != pack:
            continue
        if source and r["source"] != source:
            continue
        if theme and theme not in (r["meta"].get("themes") or []):
            continue
        if dim_filter or h_filter:
            if not r["max_dim"]:
                unmeasured += 1
                continue
            if r["max_dim"] and not (min_dim <= r["max_dim"] <= max_dim):
                continue
            # height is Z; a measured zero height fails min_h (a bbox_y
            # guard here once let zero-height rows pass every filter)
            if h_filter and not (min_h <= (r["bbox_z"] or 0) <= max_h):
                continue
        if tri_filter:
            if r["triangles"] is None:        # unknown, never 0
                unmeasured += 1
                continue
            if r["triangles"] > max_tri:
                continue
        if only_tex and not r["texture_count"]:
            continue
        name_l = (r["name"] or "").lower()
        searchable = set(r["tags"]) | set(r["meta"].get("stems") or []) \
            | set(r["meta"].get("themes") or [])
        if qstems:
            # AND first ("cobblestone medieval"); if the whole query then
            # yields nothing, fall back to OR tiers ("lamp sign hydrant"
            # is a list, not a phrase). OR rows are reserved for fallback.
            hits = _word_hits(words, name_l, searchable)
            if not all(hits):
                if not any(hits):
                    continue
                or_items.append(r)
                continue
            q_tier = 1 if min(hits) == 2 else 2
        items.append(_mesh_item(r, q_tier if qstems else 0))
    fallback_ranked = False
    if not items and or_items and qstems:
        # OR fallback, ranked by token RARITY (inverse frequency among the
        # fallback rows): a row hitting the selective token ('cobblestone',
        # ~24 rows) outranks one hitting only the broad token ('medieval',
        # ~1400 rows). Name hits count double.
        for r in or_items:
            toks = {x for x in qstems
                    if _name_hit(x, (r["name"] or "").lower())
                    or x in (set(r["tags"])
                             | set(r["meta"].get("stems") or [])
                             | set(r["meta"].get("themes") or []))}
            e = _mesh_item(r, 3)
            e["_toks"] = toks
            items.append(e)
        freq: dict = {}
        for e in items:
            for t in e["_toks"]:
                freq[t] = freq.get(t, 0) + 1
        items.sort(key=lambda e: -sum(
            (2.0 if _name_hit(t, e["name"].lower()) else 1.0) / freq[t]
            for t in e["_toks"]))
        for e in items:
            e.pop("_toks", None)
        fallback_ranked = True
    if sort == "max_dim":
        items.sort(key=lambda x: -(x["max_dim_m"] or 0))
    elif sort == "triangles":                 # unknown (NULL) counts last
        items.sort(key=lambda x: (x["triangles"] is None,
                                  x["triangles"] or 0))
    elif not fallback_ranked:
        # default alphabetical order; after a rarity-ranked OR fallback it
        # would destroy the ranking we just computed
        items.sort(key=lambda x: (x["pack"], x["name"].lower()))
    total = len(items)
    exact = sum(1 for e in items if e.get("tier", 0) < 3)
    lo = (page - 1) * size
    resp = {"total": total,
            "exact": exact if qstems else total,
            "mode": ("or-fallback" if qstems and exact == 0 and total
                     else "and"),
            "page": page, "size": size, "q": q,
            "items": items[lo:lo + size]}
    if dim_filter or h_filter or tri_filter:
        # disclose the hidden set: rows excluded because no measurement
        # exists (the agent should know it is filtering over measured
        # assets only, and how much it cannot see)
        resp["unmeasured_excluded"] = unmeasured
    return resp


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
    """
    conn = db.connect(_DB_PATH)
    rows = [dict(r) for r in conn.execute(
        "SELECT pack, source, COUNT(*) AS n, MAX(max_dim) AS tallest "
        "FROM meshes GROUP BY pack, source ORDER BY pack")]
    wiring: dict = {}
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='mesh_pack_wiring'").fetchone():
        wiring = {r["pack"]: dict(r) for r in conn.execute(
            "SELECT pack, slots_total, slots_resolved, wiring_ratio, method "
            "FROM mesh_pack_wiring")}
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
    for m in merged.values():
        m["source"] = "+".join(sorted(set(m.pop("sources")))) or None
    return {"packs": sorted(merged.values(), key=lambda m: m["pack"] or "")}


def api_meshes_themes() -> dict:
    counts: dict = {}
    for r in _mesh_rows():
        for t in (r["meta"].get("themes") or []):
            counts[t] = counts.get(t, 0) + 1
    return {"themes": [{"name": k, "n": v}
                       for k, v in sorted(counts.items(),
                                          key=lambda kv: -kv[1])]}


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
    return {"rig_body": "Animation/Actor/motion-dummy_male/Render_Dummy.fbx",
            "rig_note": "skeleton-only clips retarget onto this body "
                        "(bone-name match, CC rig family)",
            "folders": [
        {"pack": r["id"],
         "folder": r["id"][len("pack::"):],
         "name": r["name"], "clips": r["n"]}
        for r in rows]}


def api_anim_clips(params: dict) -> dict:
    """All animation clips with tiered search:
    tier 1 = query in title, tier 2 = related concept in title,
    tier 3 = query in folder name; no query -> everything, folder-sorted."""
    q = (params.get("q") or [""])[0].strip().lower()
    prefix = _anim_prefix()
    packs = [p for p in ((params.get("packs") or [""])[0]).split("|")
             if p.startswith(prefix)]
    try:
        page = max(1, int((params.get("page") or ["1"])[0]))
    except ValueError:
        page = 1
    try:
        size = min(500, max(8, int((params.get("size") or ["24"])[0])))
    except ValueError:
        size = 24
    conn = db.connect(_DB_PATH)
    where = ("substr(a.id, 1, ?) = ? AND "
             "(lower(f.relative_path) LIKE '%.fbx' OR lower(f.relative_path) LIKE '%.bvh')")
    args: list = [len(prefix), prefix]
    if packs:
        where += " AND a.id IN (%s)" % ",".join("?" * len(packs))
        args += packs
    rows = conn.execute(
        "SELECT f.relative_path AS rel, f.file_size AS bytes, a.id AS pack "
        "FROM asset_files f JOIN assets a ON a.id = f.asset_id "
        "WHERE " + where + " ORDER BY f.relative_path", args).fetchall()
    conn.close()

    qtokens = re.findall(r"[^\W_]+", q, re.UNICODE)
    qstems: set = set()
    for t in qtokens:
        qstems |= _stems(t) | _synonyms(t)
    if q and not qstems:
        # every token was dropped (single letters / punctuation) -- such a
        # query must match nothing, never the whole library. The guard every
        # other item endpoint has; the animation section lacked it.
        return {"total": 0, "page": page, "size": size, "q": q, "clips": []}
    related: set = set()
    if qstems:
        for cst in _cluster_stems():
            if qstems & cst:
                related |= cst
        related -= qstems

    items = []
    for r in rows:
        base = r["rel"].rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].lower()
        folder = r["pack"][len(prefix):]
        if qstems:
            if any(s in stem for s in qstems):
                tier = 1
            elif any(s in stem for s in related):
                tier = 2
            elif any(s in folder for s in qstems):
                tier = 3
            else:
                continue
        else:
            tier = 0
        key = _preview_key(r["rel"])
        pv = (PREVIEW_DIR / (key + ".webm")).is_file()
        rate = None
        meta = PREVIEW_DIR / (key + ".json")
        if meta.is_file():
            try:
                rate = json.loads(meta.read_text(encoding="utf-8")).get("rate")
            except (OSError, ValueError):
                rate = None
        title_tokens = [t if t not in ("f", "m") else ("female" if t == "f" else "male")
                        for t in re.split(r"[-_]+", base.rsplit(".", 1)[0])]
        title = " ".join(title_tokens)
        items.append({"rel": r["rel"], "pack": r["pack"], "folder": folder,
                      "title": title,
                      "name": title,   # alias: every other section uses name
                      "bytes": r["bytes"], "tier": tier,
                      "preview": pv, "rate": rate, "key": key})
    items.sort(key=lambda x: (x["tier"], x["folder"], x["title"]))
    total = len(items)
    lo = (page - 1) * size
    return {"total": total, "page": page, "size": size, "q": q,
            "clips": items[lo:lo + size]}


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
const RETARGET_REL='Animation/Actor/motion-dummy_male/Render_Dummy.fbx';  // clean re-export rig
let dummyObj=null,dummyPromise=null;
function getDummy(){
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
  const badge=d.q&&c.tier===2?`<span class="badge">related</span>`:'';
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
function pgetRenderer(){if(!prenderer){
  prenderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
  prenderer.setSize(PW,PH);prenderer.setPixelRatio(1);
  // recorded previews came out very dark on some GPUs: explicit tone
  // mapping + exposure normalizes brightness across machines
  if(THREE.ACESFilmicToneMapping!==undefined){prenderer.toneMapping=THREE.ACESFilmicToneMapping;
   prenderer.toneMappingExposure=1.35;}}return prenderer;}
function pgetDummy(){
 if(!pdummy){const L=new FBXLoader();
  L.setResourcePath('/res/'+encodeURIComponent('Animation/Actor/motion-dummy_male')+'/');
  pdummy=new Promise((res,rej)=>L.load('/file?path='+encodeURIComponent('Animation/Actor/motion-dummy_male/Render_Dummy.fbx'),
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
<a href="/registry" style="position:fixed;bottom:6px;right:10px;font-size:10px;letter-spacing:.04em;color:#333a44;text-decoration:none;z-index:9">registry</a>
</body></html>"""


GLTEST_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>GL test</title>
<style>body{background:#111;color:#eee;font:16px monospace;padding:20px}div{margin:8px 0;padding:6px 10px;border:1px solid #333}
.ok{color:#7dff9b}.fail{color:#ff8a8a}</style></head><body><h2>capability self-test</h2><div id=log>running…</div>
<script type="module">
import * as THREE from '/static/three.module.js';
const log=document.getElementById('log');
const line=(txt,ok)=>{const d=document.createElement('div');d.className=ok?'ok':'fail';d.textContent=txt;log.appendChild(d);};
try{
 const r=new THREE.WebGLRenderer({antialias:true});
 line('WebGL renderer: OK ('+r.domElement.width+'x'+r.domElement.height+')',true);
 const sc=new THREE.Scene();sc.background=new THREE.Color(0x2060c0);
 const cam=new THREE.PerspectiveCamera(40,2,.1,10);cam.position.z=4;
 sc.add(new THREE.Mesh(new THREE.BoxGeometry(1,1,1),new THREE.MeshNormalMaterial()));
 r.render(sc,cam);
 // captureStream + MediaRecorder round trip
 const stream=r.domElement.captureStream(30);
 const mime=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'].find(m=>window.MediaRecorder&&MediaRecorder.isTypeSupported(m));
 if(!mime)throw new Error('no webm MediaRecorder mime');
 const rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:1000000});
 const chunks=[];rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
 const stopped=new Promise(res=>rec.onstop=res);
 rec.start(100);
 const t0=performance.now();
 await new Promise(res=>{(function f(){r.render(sc,cam);if(performance.now()-t0<700)requestAnimationFrame(f);else res();})();});
 rec.stop();await stopped;
 const blob=new Blob(chunks,{type:'video/webm'});
 line('MediaRecorder webm: OK, '+blob.size+' bytes via '+mime,true);
 // decode check
 const v=document.createElement('video');v.muted=true;
 const dec=await new Promise(res=>{const to=setTimeout(()=>res('decode timeout'),4000);
  v.onloadedmetadata=()=>{clearTimeout(to);res('decode OK '+v.videoWidth+'x'+v.videoHeight);};
  v.onerror=()=>{clearTimeout(to);res('decode ERR '+String(v.error&&v.error.code));};
  v.src=URL.createObjectURL(blob);});
 line('round-trip decode: '+dec,dec.startsWith('decode OK'));
 // fetch + upload path
 const fr=await fetch('/file?path='+encodeURIComponent('Animation/Actor/motion-dummy_male/Render_Dummy.fbx'));
 const fb=await fr.arrayBuffer();
 line('FBX fetch: '+fr.status+', '+fb.byteLength+' bytes',fr.ok&&fb.byteLength>1000000);
 // store route reachable + validating (400 expected: fake clip does not exist)
 const up=await fetch('/api/preview/store?clip='+encodeURIComponent('Animation/__selftest.fbx'),
   {method:'POST',body:new Uint8Array(4096)});
 const upj=await up.json();
 line('store route: HTTP '+up.status+' '+JSON.stringify(upj)+' (400 expected)',up.status===400);
}catch(e){line('FAILED: '+(e&&e.message||e),false);}
line('self-test complete',true);
</script></body></html>"""


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
const PW=480,PH=270,FPS=30,MAXDUR=6,SPEED=2,DUMMY='Animation/Actor/motion-dummy_male/Render_Dummy.fbx';
const RATE=(1/SPEED).toFixed(3);
function loadF(rel,dir){
 const L=new FBXLoader();
 L.setResourcePath('/res/'+encodeURIComponent(dir)+"/");
 return new Promise((res,rej)=>L.load("/file?path="+encodeURIComponent(rel),res,undefined,rej));}
async function makeLane(i){
 const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
 renderer.setSize(PW,PH);renderer.setPixelRatio(1);
 const scene=new THREE.Scene();
 scene.background=new THREE.Color(0x101216);
 scene.add(new THREE.HemisphereLight(0xdfe8ff,0x30281f,1.9));
 const key=new THREE.DirectionalLight(0xffffff,2.8);key.position.set(3,6,4);scene.add(key);
 const fill=new THREE.DirectionalLight(0xcfe0ff,1.1);fill.position.set(-4,2,5);scene.add(fill);
 const dummy=await loadF(DUMMY,"Animation/Actor/motion-dummy_male");
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
<header><a href="/" style="color:#8b93a1;text-decoration:none;margin-right:2px">&larr; home</a><h1>AI Asset Registry</h1><a href="/animation" style="color:#7cc4ff;text-decoration:none">Animations</a>
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
 <span class="chip style">${esc(a.style)}</span>${a.validation==='human_verified'?'<span class="chip human">human</span>':''}</div>
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
                anim = _pack_thumb.get("pack::Animation/Actor")
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
            elif url.path == "/pack":
                self._html(PACK_PAGE.encode("utf-8"))
            elif url.path == "/animation":
                self._html(ANIM_PAGE.encode("utf-8"))
            elif url.path == "/gltest":
                self._html(GLTEST_PAGE.encode("utf-8"))
            elif url.path == "/batchrender":
                self._html(BATCH_PAGE.encode("utf-8"))
            elif url.path == "/api/stats":
                self._json(api_stats())
            elif url.path == "/api/collection/tree":
                self._json(api_collection_tree())
            elif url.path == "/api/collection/items":
                self._json(api_collection_items(params))
            elif url.path == "/api/collection/item":
                self._json(api_collection_item(params))
            elif url.path == "/api/textures/tree":
                self._json(api_textures_tree())
            elif url.path == "/api/textures/items":
                self._json(api_textures_items(params))
            elif url.path == "/api/textures/item":
                self._json(api_textures_item(params))
            elif url.path == "/api/meshes":
                self._json(api_meshes(params))
            elif url.path == "/api/meshes/packs":
                self._json(api_meshes_packs())
            elif url.path == "/api/meshes/themes":
                self._json(api_meshes_themes())
            elif url.path == "/api/audio/tree":
                self._json(api_audio_tree())
            elif url.path == "/api/audio/items":
                self._json(api_audio_items(params))
            elif url.path == "/api/audio/item":
                self._json(api_audio_item(params))
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
                self._html(VIEWER_PAGE.encode("utf-8"))
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
        """Bulk human re-tagging: {"ids": [...], "set": {domain|style} | "status"}.
        Same semantics as the host-app extension: validation_status becomes
        'human_verified' and confidence 1.0."""
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
                if not re.fullmatch(r"pack::[\w .()&!',#%-]+", pid):
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
                conn.execute("UPDATE collection SET thumb_override=? WHERE id=?",
                             (image or None, iid))
                conn.commit()
                conn.close()
                return self._json({"ok": True, "thumb": image or None})
            if path == "/api/collection/tag":
                body, err = self._json_body()
                if err:
                    return self._json({"error": err}, 415)
                return self._json(api_collection_tag(params, body))
            if path != "/api/retag":
                return self._json({"error": "not found"}, 404)
            body, err = self._json_body()
            if err:
                return self._json({"error": err}, 415)
            ids = [i for i in body.get("ids", [])
                   if isinstance(i, str) and re.fullmatch(r"pack::[\w .()&!',#%-]+", i)]
            sets, params = [], []
            payload = body.get("set") or {}
            if payload.get("domain") in DOMAINS:
                sets.append("domain=?")
                params.append(payload["domain"])
            if payload.get("style") in STYLES:
                sets.append("style=?")
                params.append(payload["style"])
            status = body.get("status")
            if status in ("validated_blender", "validated_unreal"):
                sets.append("status=?")
                params.append(status)
            if not ids or not sets:
                return self._json({"error": "nothing to do"}, 400)
            conn = db.connect(_DB_PATH)
            cur = conn.execute(
                f"UPDATE assets SET {','.join(sets)}, "
                f"validation_status='human_verified', confidence_score=1.0, "
                f"updated_at=CURRENT_TIMESTAMP "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                params + ids)
            conn.commit()
            updated = cur.rowcount
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
        covered = {config.ANIM_ROOT.name, config.TEX_ROOT.name,
                   config.AUDIO_ROOT.name, config.COLLECTION_ROOT.name,
                   config.AGENT_FILES.name}
        covered.update(p.name for p in config.MANIFEST_ROOTS)
        try:
            strays = [d.name for d in config.LIBRARY_ROOT.iterdir()
                      if d.is_dir() and not d.name.startswith((".", "_"))
                      and d.name not in covered]
        except OSError:
            strays = []
        if strays:
            print(f"NOT INDEXED (no section claims these): "
                  f"{', '.join(sorted(strays))}", flush=True)
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
