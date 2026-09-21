"""Pharos MCP server — exposes the asset library as native agent tools.

Run standalone (stdio transport):
    python -m service.pharos_mcp_server

Or add to any MCP client config:
    { "mcpServers": { "pharos": {
        "command": "python",
        "args": ["-m", "service.pharos_mcp_server"] } } }

Tools exposed:
    pharos_stats()                — library overview
    pharos_search_meshes()        — geometry search (dims, themes, budget)
    pharos_search_textures()      — PBR texture sets
    pharos_search_audio()         — sounds by keyword/category
    pharos_search_collection()    — owned purchases (local + not-downloaded)
    pharos_list_packs()           — exact pack names for filtering

Reads the same SQLite registry as the HTTP server; safe to run alongside it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))   # .../service
from asset_service import config  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pharos")


def _db() -> sqlite3.Connection:
    """Open the registry read-write WITHOUT ever creating it.

    An unconfigured start used to sqlite3-connect the default path in
    whatever CWD the MCP client launched from -- creating a stray empty
    assets.sqlite there -- and then fail with raw sqlite errors on every
    tool call. Preflight + mode=rw (no create) instead: clear errors,
    zero side effects."""
    if not config.is_configured():
        raise FileNotFoundError(
            "pharos_config.json has no library_root/registry_dir -- run "
            "`python pharos.py init <assets-root>` first (this MCP server "
            "refuses to invent a registry location)")
    p = Path(config.DB_PATH)
    if not p.is_file():
        raise FileNotFoundError(
            f"registry not found at {p} -- start the server once "
            f"(`python pharos.py serve`) or run an import, then retry")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _animation_clip_count(conn: sqlite3.Connection) -> int:
    """Clips = FBX/BVH under the ANIMATION packs only (mesh packs contain
    FBX too; counting everything inflated this number on mixed libs)."""
    return conn.execute(
        "SELECT COUNT(*) FROM asset_files f JOIN assets a "
        "ON a.id = f.asset_id WHERE a.id LIKE 'pack::Animation/%' "
        "AND (lower(f.relative_path) LIKE '%.fbx' "
        "OR lower(f.relative_path) LIKE '%.bvh')").fetchone()[0]


@mcp.tool()
def pharos_stats() -> str:
    """Get a library overview: total counts per section (meshes, textures,
    audio, collection, animations). Call this FIRST to understand scale.
    Doctrine for everything you find: prefer assets already in the
    library; owned-not-downloaded -> ask the user (requisition list,
    never silent substitution); absent -> model it yourself and SAY SO
    in your report."""
    conn = _db()
    try:
        packs = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        meshes = conn.execute("SELECT COUNT(*) FROM meshes").fetchone()[0]
        textures = conn.execute("SELECT COUNT(*) FROM textures").fetchone()[0]
        audio = conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
        collection = conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
        local = conn.execute(
            "SELECT COUNT(*) FROM collection WHERE availability='local'"
        ).fetchone()[0]
        clips = _animation_clip_count(conn)
        return json.dumps({
            "packs": packs, "meshes": meshes, "texture_sets": textures,
            "audio_files": audio, "collection_items": collection,
            "collection_local": local, "animation_clips": clips,
        })
    finally:
        conn.close()


@mcp.tool()
def pharos_search_meshes(
    query: Optional[str] = None,
    pack: Optional[str] = None,
    theme: Optional[str] = None,
    min_dim: Optional[float] = None,
    max_dim: Optional[float] = None,
    min_height: Optional[float] = None,
    max_height: Optional[float] = None,
    max_triangles: Optional[int] = None,
    with_textures: bool = False,
    limit: int = 20,
) -> str:
    """Search 3D meshes with real-world dimensions in metres.

    Args:
        query: name/tag search (stem-aware, synonyms active: car→vehicle etc.)
        pack: exact pack name (get names from pharos_list_packs)
        theme: controlled vocabulary (building, door, prop, street, scifi,
               medieval, fantasy, nature, wall, light, destruction, ...)
        min_dim/max_dim: filter on largest bbox axis in METRES
        min_height/max_height: filter on bbox Y in METRES
        max_triangles: budget filter
        with_textures: only meshes with texture links
        limit: max results (1-100)

    Returns JSON: name, pack, fbx_path (absolute), bbox_m, triangles,
    material recipe (hero_textures), themes, tier.
    """
    conn = _db()
    try:
        where, args = [], []
        if pack:
            where.append("pack = ?")
            args.append(pack)
        if theme:
            where.append("meta LIKE ?")
            args.append(f'%"{theme}"%')
        if min_dim is not None:
            where.append("max_dim >= ?")
            args.append(min_dim)
        if max_dim is not None:
            where.append("max_dim <= ?")
            args.append(max_dim)
        if min_height is not None:
            where.append("bbox_y >= ?")
            args.append(min_height)
        if max_height is not None:
            where.append("bbox_y <= ?")
            args.append(max_height)
        if max_triangles is not None:
            where.append("triangles <= ?")
            args.append(max_triangles)
        if with_textures:
            where.append("texture_count > 0")
        w = (" WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM meshes{w} ORDER BY pack, name",
            args)]
        # text filter first, THEN limit (SQL LIMIT would sample the wrong
        # alphabetical window before the query filter runs)
        if query:
            ql = query.lower()
            tokens = ql.split()
            def hit(r):
                nl = (r["name"] or "").lower() + " " + (r["pack"] or "").lower()
                tags = (r.get("tags") or "") + (r.get("meta") or "")
                return all(t in nl or t in tags.lower() for t in tokens)
            rows = [r for r in rows if hit(r)]
        # total is the TRUE match count; items are the limited window
        total = len(rows)
        rows = rows[:min(100, max(1, limit))]
        out = []
        for r in rows:
            meta = json.loads(r.get("meta") or "{}")
            recipe = json.loads(r.get("recipe") or "null") if r.get("recipe") else None
            out.append({
                "name": r["name"], "pack": r["pack"], "source": r["source"],
                "fbx_path": r["fbx"], "triangles": r["triangles"],
                "bbox_m": [r["bbox_x"], r["bbox_y"], r["bbox_z"]],
                "max_dim_m": r["max_dim"],
                "texture_count": r["texture_count"],
                "hero_textures": recipe.get("primary") if recipe else None,
                "themes": meta.get("themes", []),
            })
        return json.dumps({"total": total, "items": out})
    finally:
        conn.close()


@mcp.tool()
def pharos_search_textures(
    query: Optional[str] = None,
    group: Optional[str] = None,
    sub: Optional[str] = None,
    resolution: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search PBR texture sets (albedo/normal/roughness maps).

    Args:
        query: name search (e.g. 'brick', 'wet asphalt', 'marble')
        group: '4K Textures Gumroad' or 'Misc'
        sub: subcategory (4K_Physical_Walls, 4K_Physical_Wood, ...)
        resolution: '4K', '2K', '1K', '512'
        limit: max results
    """
    conn = _db()
    try:
        where, args = [], []
        if group:
            where.append("grp = ?")
            args.append(group)
        if sub:
            where.append("sub = ?")
            args.append(sub)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM textures{w} ORDER BY grp, sub, name",
            args)]
        if query:
            ql = query.lower()
            tokens = ql.split()
            def hit(r):
                nl = (r["name"] or "").lower()
                tags = (r.get("tags") or "") + (r.get("meta") or "")
                return all(t in nl or t in tags.lower() for t in tokens)
            rows = [r for r in rows if hit(r)]
        # filter BEFORE the limit (the mesh tool documents this exact
        # rule): truncating first made resolution filters return false
        # "no 4K sets" answers on libraries over the limit. The meta blob
        # is matched CASE-INSENSITIVELY: '"res": "4K"' must match "4k".
        if resolution:
            rows = [r for r in rows
                    if resolution.lower() in (r.get("meta") or "").lower()]
        total = len(rows)
        rows = rows[:min(100, max(1, limit))]
        out = [{
            "name": r["name"], "group": r["grp"], "sub": r["sub"],
            "file_count": r["file_count"],
            "bytes": r["bytes"], "folder": r["folder"],
        } for r in rows]
        return json.dumps({"total": total, "items": out})
    finally:
        conn.close()


@mcp.tool()
def pharos_search_audio(
    query: Optional[str] = None,
    category: Optional[str] = None,
    sub: Optional[str] = None,
    min_duration: Optional[float] = None,
    limit: int = 20,
) -> str:
    """Search audio files (SFX, ambience, music, foley).

    Args:
        query: name search (e.g. 'rain', 'explosion', 'crowd')
        category: Alarms, Ambiance, Animals, Explosions, Foley, Footsteps,
                  Gunshots, Horror, Human, Impacts, Machines, Music, Nature,
                  SciFi, Transitions, UI, Vehicles, Weapons, Weather
        min_duration: minimum length in seconds (e.g. 30 for looping beds)
        limit: max results
    """
    conn = _db()
    try:
        where, args = [], []
        if category:
            where.append("cat = ?")
            args.append(category)
        if sub:
            where.append("sub = ?")
            args.append(sub)
        if min_duration is not None:
            where.append("dur >= ?")
            args.append(min_duration)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM audio{w} ORDER BY cat, name",
            args)]
        if query:
            ql = query.lower()
            tokens = ql.split()
            def hit(r):
                nl = (r["name"] or "").lower()
                tags = (r.get("tags") or "") + (r.get("meta") or "")
                return all(t in nl or t in tags.lower() for t in tokens)
            rows = [r for r in rows if hit(r)]
        total = len(rows)
        rows = rows[:min(100, max(1, limit))]
        out = [{
            "name": r["name"], "category": r["cat"], "sub": r["sub"],
            "duration_s": r["dur"], "sample_rate": r["sr"],
            "channels": r["ch"],
            "path": str(config.AUDIO_ROOT / (r["rel"] or "")),
        } for r in rows]
        return json.dumps({"total": total, "items": out})
    finally:
        conn.close()


@mcp.tool()
def pharos_search_collection(
    query: Optional[str] = None,
    store: Optional[str] = None,
    availability: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search owned purchases (the vault). Shows what's on disk vs
    owned-but-not-downloaded.

    Args:
        query: name search
        store: FAB, CGTrader, Leartes, TurboSquid, KitBash3D
        availability: 'local' (on disk) or 'notdown' (owned, needs download)
        limit: max results
    """
    conn = _db()
    try:
        where, args = [], []
        if store:
            where.append("store = ?")
            args.append(store)
        if availability == "local":
            where.append("availability = 'local'")
        elif availability == "notdown":
            where.append("availability != 'local'")
        w = (" WHERE " + " AND ".join(where)) if where else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM collection{w} ORDER BY name",
            args)]
        if query:
            ql = query.lower()
            tokens = ql.split()
            def hit(r):
                nl = (r["name"] or "").lower()
                tags = (r.get("tags") or "") + (r.get("meta") or "")
                return all(t in nl or t in tags.lower() for t in tokens)
            rows = [r for r in rows if hit(r)]
        total = len(rows)
        rows = rows[:min(100, max(1, limit))]
        out = [{
            "name": r["name"], "store": r["store"],
            "type": r["type_raw"], "seller": r["seller"],
            "availability": r["availability"],
            "asset_path": r["asset_path"], "product_url": r["url"],
        } for r in rows]
        return json.dumps({"total": total, "items": out})
    finally:
        conn.close()


@mcp.tool()
def pharos_list_packs() -> str:
    """List exact pack names with mesh counts (wiring ratios: HTTP only).
    Use this before filtering search_meshes by pack= (names must match)."""
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT pack, source, COUNT(*) AS n, MAX(max_dim) AS tallest "
            "FROM meshes GROUP BY pack, source ORDER BY pack")]
        return json.dumps({"packs": rows})
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
