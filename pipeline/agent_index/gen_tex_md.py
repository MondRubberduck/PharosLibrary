from pathlib import Path
import os, json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import section_root
ROOT = section_root("textures", "AGENT_TEX_ROOT")
idx = json.load(open(os.path.join(ROOT, "library_index.json"), encoding="utf-8"))
T = idx["totals"]

def gb(n): return f"{n/1e9:.2f} GB"
L = []
A = L.append
A("# Textures & Materials Library - Agent Index (READ ME FIRST)")
A("")
A("> Machine-readable companions in this folder: **`library_index.json`** (taxonomy/counts/keys), **`library_files.jsonl`** (one JSON per file), **`material_sets.jsonl`** (one JSON per PBR material set).")
A(f"> Generated {idx['generated']} | Root: `{idx['root']}`")
A("")
A("## What this is")
A(idx["purpose"])
A("")
A(f"- **{T['files']:,} files** across **{T['folders']:,} folders**, **{T['size_gb']} GB**")
A(f"- **{T['categories']} categories**, ~**{T['material_sets_est']:,} material sets** (heuristic)")
A("- Formats: " + ", ".join(f"`{k}`x{v}" for k, v in list(idx["extensions"].items())[:8]))
A("")
A("## How to use (for agents) - fastest path")
A("1. **`library_index.json`** - what exists, where, and the keyword routing.")
A("2. **`library_files.jsonl`** - grep a flat list (path, top, cat, map, ext, bytes, w, h) instead of walking 18k files.")
A("3. **`material_sets.jsonl`** - find complete PBR sets and which maps they contain.")
A("4. Only then open the concrete paths.")
A("")
A("```bash")
A('# a category / a map type / a resolution')
A('grep "\\"cat\\":\\"Wood\\"" library_files.jsonl | head')
A('grep "\\"map\\":\\"normal\\"" library_files.jsonl | wc -l')
A('grep "\\"w\\":2048" library_files.jsonl | head')
A('# full PBR sets that include a normal AND a roughness map')
A('grep -E "\\"maps\\":\\[[^]]*\\"normal\\"" material_sets.jsonl | grep "roughness"')
A("```")
A("")
A("## Top-level folders")
A("")
A("| Folder | Files | What it is |")
A("|---|---:|---|")
TOPDESC = {
    "4K_Textures_Gumroad": "Main PBR library: ~18 material classes (Wood, Rocks, Metals, Fabrics, Marble, Roads, Ground, Grass, Animals, Sci-Fi, Military, Medieval, Destruction, Organic, Walls, Roofs, Pavements, Flooring). Each material is a folder of maps.",
    "(root)": "Loose collection: Textures.com sets/previews, Poliigon-style PBR, single photos, overlays, skyboxes, HDR-ish images.",
    "CC0Textures": "CC0 PBR sets (Color/Normal/Roughness/Displacement) as PNG + matching .zip archives + USD (.usda/.usdc).",
    "Glitch_Art": "Glitch / datamosh art images (PNG).",
    "Pixel_Art": "Pixel-art tile sets (Bricks, Dungeon, etc.).",
    "Cloth": "Fabric/cloth wrinkles, creases and zBrush brushes (.psd/.zbp/.zip + JPG).",
    "HDRi": "HDRI environment maps (.exr/.hdr), incl. Manhattan Nights set.",
    "JSPlacememnt": "Seamless pattern/displacement images (DotGrid etc.).",
    "Wood_Floor": "Wood floor PBR sets.",
    "Bedroom_Textures": "Misc bedroom/prop reference images.",
    "Neuer Ordner": "A few misc PBR sets.",
    "Smudges": "Overlay/smudge/scuff textures.",
    "Blender_AddOns": "Blender addon (.zip) + EdgeWear .blend (not textures).",
}
for k, c in idx["top_level"].items():
    A(f"| `{k}` | {c['files']} | {TOPDESC.get(k,'')} |")
A("")
A("## Categories")
A("")
A("| Category | Files | Sets | Common maps | Description |")
A("|---|---:|---:|---|---|")
for c, d in idx["categories"].items():
    maps = ", ".join(d["common_maps"].keys())
    A(f"| **{c}** | {d['files']} | {d['material_sets']} | {maps} | {d['description']} |")
A("")
A("## Map types (what a texture file IS)")
A("")
A("| Map | Files |")
A("|---|---:|")
for k, v in idx["map_types"].items():
    A(f"| {k} | {v['files']} |")
A("")
A("## Resolutions")
A("")
A("| Bucket | Files |")
A("|---|---:|")
for k, v in idx["resolutions"]["by_bucket"].items():
    A(f"| {k} | {v} |")
A("")
A("## Naming conventions")
A("")
for k, v in idx["naming_conventions"].items():
    A(f"- **{k}**: {v}")
A("")
A("## Notes")
A("")
for k, v in idx["notes"].items():
    A(f"- **{k}**: {v}")
text = "\n".join(L)
for a, b in [("\u2014", "-"), ("\u00b7", "|"), ("\u00d7", "x"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2013", "-")]:
    text = text.replace(a, b)
open(os.path.join(ROOT, "AGENT_INDEX.md"), "w", encoding="utf-8").write(text)
print("wrote AGENT_INDEX.md", len(text), "chars")
