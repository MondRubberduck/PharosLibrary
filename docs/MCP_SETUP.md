# Pharos MCP Setup

Add to your MCP client (Claude Desktop, Cursor, etc.):

## Claude Desktop / claude_desktop_config.json

```json
{
  "mcpServers": {
    "pharos": {
      "command": "python",
      "args": ["-m", "service.pharos_mcp_server"],
      "cwd": "C:/path/to/pharos"
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
      "args": ["-m", "service.pharos_mcp_server", "--cwd", "C:/path/to/pharos"]
    }
  }
}
```

## Available tools

| Tool | What it answers |
|---|---|
| `pharos_stats` | How big is this library? (call first) |
| `pharos_search_meshes` | Find geometry by dimensions (metres), theme, budget |
| `pharos_search_textures` | Find PBR sets (brick, marble, wet asphalt) |
| `pharos_search_audio` | Find sounds by keyword, category, duration |
| `pharos_search_collection` | What do I own? What's downloaded vs not? |
| `pharos_list_packs` | Exact pack names (needed for pack= filters) |

## Tips for agents

1. Call `pharos_stats` first to calibrate expectations.
2. Use `pharos_list_packs` before filtering by `pack=` — names must match exactly.
3. Dimensions are in **metres**. A door is ~2m, a building 10-80m, a prop 0.5-3m.
4. `hero_textures` in mesh results contains the material recipe: albedo, normal,
   packed (ORM/RMA with channel maps). Use these to build materials without
   opening the manifest.
5. The collection tool shows `availability: local` (use now) vs
   `owned-not-downloaded` (ask the human to download).
