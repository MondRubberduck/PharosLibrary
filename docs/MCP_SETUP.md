# Pharos MCP Setup

Optional: the HTTP API is the primary surface; MCP is a convenience for
clients that prefer native tools. **Blender needs NO MCP server, NO
plugin, nothing** — scene builds run headless via
`blender --background --factory-startup --python scene_builder.py`.
This config is only for YOUR agent client (Claude Desktop, Cursor, …).

Install the one extra package first (the HTTP API needs nothing):

```bash
python -m pip install "mcp<2"
```

The server resolves its own paths, so the working directory does not
matter: point your client at `service/pharos_mcp_server.py` by absolute
path. If plain `python` is not on PATH, use the absolute interpreter path.

## Claude Desktop / claude_desktop_config.json

```json
{
  "mcpServers": {
    "pharos": {
      "command": "python",
      "args": ["C:/path/to/PharosLibrary/service/pharos_mcp_server.py"]
    }
  }
}
```

## Cursor (.cursor/mcp.json)

```json
{
  "mcpServers": {
    "pharos": {
      "command": "python",
      "args": ["C:/path/to/PharosLibrary/service/pharos_mcp_server.py"]
    }
  }
}
```


## Windows notes

- In PowerShell, `curl` is an alias for `Invoke-WebRequest` and the
  documented `-s` flags hang — use `curl.exe` or the Python one-liner:
  `python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8765/api/stats').read()[:200])"`.

## Available tools

| Tool | What it answers |
|---|---|
| `pharos_stats` | How big is this library? (call first) |
| `pharos_tree` | Which themes / groups / categories / folders exist? |
| `pharos_list_packs` | Pack names, wiring health, linked purchase |
| `pharos_search_meshes` | Find geometry by words, dimensions (metres), theme, budget |
| `pharos_get_meshes` | Material recipe and details of the meshes you chose |
| `pharos_search_textures` | Find PBR sets (brick, marble, wet asphalt), by measured size |
| `pharos_get_texture` | One set's files and which map each one is |
| `pharos_search_audio` | Find sounds by keyword, category, duration |
| `pharos_search_animations` | Find animation clips |
| `pharos_search_collection` | What do I own? What's downloaded vs not? |

## Tips for agents

1. Call `pharos_stats` first to calibrate expectations.
2. Use `pharos_list_packs` / `pharos_tree` to see which pack, theme and
   category names exist before filtering by them.
3. Dimensions are in **metres**. A door is ~2m, a building 10-80m, a prop 0.5-3m.
4. Search results are short. For the meshes you pick, call
   `pharos_get_meshes(keys)`: `recipe.slots[]` is the per-slot material
   wiring and `hero_textures` the one-map-per-role summary (albedo,
   normal, packed ORM/RMA with channel maps).
5. The collection tool shows `availability: local` (use now) vs
   `owned-not-downloaded` (ask the human to download).
6. The MCP tools call the same search as the HTTP API
   (`http://127.0.0.1:8765`, see the generated `AGENT_API.md`): both
   routes give the same answer. Read `mode`, `warnings` and
   `did_you_mean` in every answer.
