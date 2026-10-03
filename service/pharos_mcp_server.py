"""Pharos MCP server — exposes the asset library as native agent tools.

Run standalone (stdio transport):
    python -m service.pharos_mcp_server

Or add to any MCP client config:
    { "mcpServers": { "pharos": {
        "command": "python",
        "args": ["-m", "service.pharos_mcp_server"] } } }

Tools exposed:
    pharos_stats()                — library overview
    pharos_tree(section)          — the valid filter values of a section
    pharos_list_packs()           — exact pack names for filtering
    pharos_search_meshes()        — geometry search (dims, themes, budget)
    pharos_get_meshes(keys)       — recipe + materials of chosen meshes
    pharos_search_textures()      — PBR texture sets
    pharos_get_texture(id)        — one set's files and map roles
    pharos_search_audio()         — sounds by keyword/category/length
    pharos_search_animations()    — animation clips
    pharos_search_collection()    — owned purchases (local + not-downloaded)

Every search tool calls the SAME function as the HTTP API (browse.api_*):
one search implementation, one answer. This server used to carry its own
matcher (raw substring over the stored JSON), which disagreed with HTTP
on most queries -- 'crates' found 0 here and 63 there, and the word
'themes' matched every mesh because it is a key in the stored metadata.

Reads the same SQLite registry as the HTTP server; safe to run alongside it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))   # .../service
from asset_service import browse, config, db  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pharos")


def _db() -> sqlite3.Connection:
    """Open the registry read-write WITHOUT ever creating it.

    An unconfigured start used to sqlite3-connect the default path in
    whatever CWD the MCP client launched from -- creating a stray empty
    assets.sqlite there -- and then fail with raw sqlite errors on every
    tool call. Preflight (config + file exists) instead: clear errors,
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
    # plain path: the file exists (checked above), so nothing is created;
    # a file: URI fails on UNC paths and truncates at a '#' in the path
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def _api(fn, **params) -> dict:
    """Run one HTTP-API function against the configured registry."""
    _db().close()                       # preflight: clear error, creates nothing
    query = {}
    for k, v in params.items():
        if v is None or v == "" or v is False:
            continue
        query[k] = ([str(x) for x in v] if isinstance(v, (list, tuple))
                    else ["1" if v is True else str(v)])
    old = browse._DB_PATH
    browse._DB_PATH = str(config.DB_PATH)
    try:
        return fn(query) if fn.__code__.co_argcount else fn()
    finally:
        browse._DB_PATH = old


def _page(limit: int) -> int:
    return min(100, max(1, int(limit or 20)))


def _animation_clip_count(conn: sqlite3.Connection) -> int:
    """Clips = FBX/BVH under the ANIMATION packs only (mesh packs contain
    FBX too; counting everything inflated this number on mixed libs).
    The section folder name comes from the config, never a literal."""
    return db.anim_clip_count(conn, config.SECTIONS.get("animation"))


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
def pharos_tree(section: str) -> str:
    """The valid filter values of one section, with counts -- look here
    before filtering, the names are specific to this library.

    Args:
        section: 'meshes' (themes), 'textures' (groups/subs), 'audio'
                 (categories/subs), 'animations' (folders, skeletons),
                 'collection' (stores/groups/categories)
    """
    fn = {"meshes": browse.api_meshes_themes,
          "textures": browse.api_textures_tree,
          "audio": browse.api_audio_tree,
          "animations": browse.api_anim_tree,
          "collection": browse.api_collection_tree}.get(
              (section or "").strip().lower())
    if fn is None:
        return json.dumps({"error": "unknown section", "sections": [
            "meshes", "textures", "audio", "animations", "collection"]})
    return json.dumps(_api(fn), ensure_ascii=False)


@mcp.tool()
def pharos_list_packs() -> str:
    """List exact mesh pack names with counts, material-wiring health and,
    where the library has one, the linked purchase record (its store
    images show what the pack looks like). Use the names with pack=."""
    return json.dumps(_api(browse.api_meshes_packs), ensure_ascii=False)


@mcp.tool()
def pharos_search_meshes(
    query: Optional[str] = None,
    pack: Optional[str] = None,
    theme: Optional[str] = None,
    style: Optional[str] = None,
    kind: Optional[str] = None,
    min_dim: Optional[float] = None,
    max_dim: Optional[float] = None,
    min_height: Optional[float] = None,
    max_height: Optional[float] = None,
    max_triangles: Optional[int] = None,
    with_textures: bool = False,
    sort: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """Search 3D meshes with real-world dimensions in metres.

    Args:
        query: name words; plurals and aliases are understood, '-word'
               excludes. Every word must match; if no mesh matches all
               words the answer falls back to any word and says so
               (mode 'or-fallback', tier 3)
        pack: pack name (pharos_list_packs)
        theme: one of the library's themes (pharos_tree('meshes'))
        style: the owner's pack tags: Photorealistic, Stylized, Low Poly
               (a comma list needs all, e.g. 'Stylized,Low Poly'); packs
               the owner has not tagged match no style
        kind: e.g. StaticMesh, SkeletalMesh, blend-object
        min_dim/max_dim: filter on the largest bbox axis in METRES
        min_height/max_height: filter on bbox Z (height; the registry is
               Z-up) in METRES
        max_triangles: budget filter
        with_textures: only meshes with texture links
        sort: relevance (default with a query), name, max_dim (largest
              first), -max_dim (smallest first), triangles (fewest
              first), -triangles (most first); a sort orders within each
              relevance tier, lower LODs / collision hulls / flat cards
              after real models, unmeasured rows last
        limit: results per page (1-100); page: 1-based

    Returns JSON {total, exact, mode, root, items, warnings?,
    owned_not_downloaded?}. Items are SHORT: key, name, pack, bbox_m
    (metres, Z-up), triangles, wired (a material recipe exists), themes,
    tier, and the owner's `styles` / `pack_tags` when the pack is tagged;
    `lod` / `collider` / `card` mark lower-LOD copies, collision hulls and
    flat pieces (billboards, decals, plain floor or wall quads). When models match every word but the filters remove
    them all, `warnings` says how many.
    A mesh's file is root + '/' + key (for a container item drop the
    last '#object' part; an item outside the library carries its own
    absolute `fbx`). `owned_not_downloaded` (page 1, with a query) lists
    purchases the owner has bought but NOT downloaded that match the
    query and style (themes and sizes do not apply): offer them as a
    requisition before settling. The full list: pharos_search_collection
    with availability="notdown" and the q shown in its `more` link. Fetch the recipe of
    the meshes you pick with pharos_get_meshes(keys).
    """
    return json.dumps(_api(
        browse.api_meshes, q=query, pack=pack, theme=theme, style=style,
        kind=kind,
        min_dim=min_dim, max_dim=max_dim, min_h=min_height,
        max_h=max_height, max_tri=max_triangles, textures=with_textures,
        sort=sort, size=_page(limit), page=page), ensure_ascii=False)


@mcp.tool()
def pharos_get_meshes(keys: list[str]) -> str:
    """Full records for meshes you have CHOSEN: per-slot material recipe
    (`recipe.slots[]`), hero_textures, material names, placement corners
    (bbox_min/bbox_max), object_name for objects inside a .blend /
    multi-object file. Ask for 5-10 at a time (large buildings one at a
    time): an answer stops at about 20k tokens and lists the keys it had
    no room for in `not_returned`.

    Args:
        keys: the `key` values from pharos_search_meshes results
    """
    return json.dumps(_api(browse.api_meshes_item, key=list(keys or [])),
                      ensure_ascii=False)


@mcp.tool()
def pharos_search_textures(
    query: Optional[str] = None,
    group: Optional[str] = None,
    sub: Optional[str] = None,
    resolution: Optional[str] = None,
    min_px: Optional[int] = None,
    has: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """Search PBR texture sets.

    Args:
        query: name words (e.g. 'brick', 'wet asphalt', 'marble')
        group / sub: values from pharos_tree('textures')
        resolution: '8K', '4K', '2K', '1K' -- MEASURED from the files
        min_px: minimum pixel size of the larger side (e.g. 4096)
        has: comma-separated map roles that must exist (e.g. 'height')
        limit: results per page (1-100); page: 1-based

    Items: id, name, res, px (measured), maps (roles present), tier.
    File paths per role: pharos_get_texture(id).
    """
    return json.dumps(_api(
        browse.api_textures_items, q=query, group=group, sub=sub,
        res=resolution, min_px=min_px, has=has, size=_page(limit),
        page=page), ensure_ascii=False)


@mcp.tool()
def pharos_get_texture(id: int) -> str:
    """One texture set in full: every file path, the map role of each
    file (`maps`), measured pixel size, folder."""
    return json.dumps(_api(browse.api_textures_item, id=id),
                      ensure_ascii=False)


@mcp.tool()
def pharos_search_audio(
    query: Optional[str] = None,
    category: Optional[str] = None,
    sub: Optional[str] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """Search audio files (SFX, ambience, music, foley).

    Args:
        query: name words (e.g. 'rain', 'explosion', 'crowd')
        category / sub: values from pharos_tree('audio')
        min_duration / max_duration: length in seconds
        limit: results per page (1-100); page: 1-based

    Items: id, name, cat, sub, dur (seconds), path (absolute), tier.
    """
    out = _api(browse.api_audio_items, q=query, cat=category, sub=sub,
               min_dur=min_duration, max_dur=max_duration,
               size=_page(limit), page=page)
    for i in out.get("items") or []:
        i["path"] = str(config.AUDIO_ROOT / (i.get("rel") or ""))
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def pharos_search_animations(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """Search animation clips (FBX/BVH) by title words; folder names
    count too ('root', a pack name). pharos_tree('animations') lists the
    folders and which skeleton each uses.

    Items: title, rel (path under the library), path (absolute), tier.
    Clips of a related motion follow the matches as tier 3.
    """
    out = _api(browse.api_anim_clips, q=query, size=_page(limit), page=page)
    for c in out.get("clips") or []:
        c["path"] = str(config.LIBRARY_ROOT / (c.get("rel") or ""))
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def pharos_search_collection(
    query: Optional[str] = None,
    store: Optional[str] = None,
    availability: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """Search owned purchases (the vault). Shows what's on disk vs
    owned-but-not-downloaded.

    Args:
        query: name words
        store: a store name from pharos_tree('collection')
        availability: 'local' (on disk) or 'notdown' (owned, needs download)
        limit: results per page (1-100); page: 1-based
    """
    return json.dumps(_api(
        browse.api_collection_items, q=query, store=store,
        avail=availability, size=_page(limit), page=page),
        ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
