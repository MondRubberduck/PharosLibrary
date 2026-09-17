from pathlib import Path
import os, re, json, collections, datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import section_root
ROOT = section_root("textures", "AGENT_TEX_ROOT")
TMP = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(ROOT):
    raise SystemExit("FATAL: texture root does not exist: %s" % ROOT)
SKIP_NAMES = {"library_index.json", "library_files.jsonl", "material_sets.jsonl", "AGENT_INDEX.md"}

meta = json.load(open(os.path.join(TMP, "tex_meta.json"), encoding="utf-8"))

def norm(s):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

CAT_RULES = [
    ("HDRi_Sky", ["hdri", "hdr", "exr", "skybox", "skies", "sky", "clouds", "sunset", "sunrise", "starry", "moon", "earth map", "night", "environment map", "panorama"]),
    ("Pixel_Art", []),
    ("Glitch_Art", ["glitch"]),
    ("Military", ["military", "camo", "camouflage", "grenade", "sandbag", "supply case", "tent", "fuel tank", "ammo", "ammunition", "bullet", "artillery", "armor", "armour", "helmet", "barrel"]),
    ("Medieval", ["medieval", "castle", "chain mail", "chainmail", "shield", "village", "reinforced wooden", "ornament", "royal"]),
    ("SciFi_Space", ["sci fi", "sci-fi", "space ship", "space station", "spaceship", "space", "solar panel", "crt display", "engine parts"]),
    ("Animal_Skin", ["fur", "feather", "hide", "armadillo", "bat", "cheetah", "cow", "crab", "crocodile", "elephant", "fish", "fox", "frog", "hedgehog", "horse", "lion", "lizard", "lobster", "octopus", "pig", "rhino", "salamander", "seal", "shark", "snake", "sphinx", "tiger", "tortoise", "turtle", "whale", "zebra"]),
    ("Organic_Creature", ["creature", "organic", "flesh", "guts", "gore", "blood", "dragon", "tentacle", "maggot", "insect", "brain", "tongue", "alien skin", "skin growth", "pores", "wrinkled skin", "bruised", "burnt skin", "zombie", "coral", "skull", "bone", "meat", "scales", "growths", "eyes"]),
    ("Destruction", ["rubble", "bullet hole", "explosion", "destroyed", "debris", "burnt", "collapse", "destruction"]),
    ("Doors_Windows", ["door", "window", "gate", "hatch", "shutter"]),
    ("Roofing", ["roof", "shingle", "gutter"]),
    ("Buildings", ["building", "highrise", "high rise", "house", "shops", "shop", "tower", "towerblocks", "facade", "venice", "slum", "city", "architecture", "neoclassical", "residential", "barracks", "bank", "office"]),
    ("Brick", ["brick"]),
    ("Concrete", ["concrete", "cement", "cinderblock", "bunker"]),
    ("Tiles", ["tile", "mosaic", "herringbone", "hexagon"]),
    ("Pavement_Road", ["pavement", "asphalt", "road", "street", "sidewalk", "curb", "kerb", "manhole", "stair", "cobblestone"]),
    ("Stone_Rock", ["rock", "stone", "cliff", "boulder", "marble", "granite", "slate", "cobble", "flagstone", "pebble", "gravel", "gem"]),
    ("Wood", ["wood", "plank", "timber", "bark", "log", "parquet", "beam", "plywood", "siding", "pallet", "floor", "chevron", "basket weave"]),
    ("Metal", ["metal", "steel", "iron", "copper", "bronze", "rust", "aluminium", "aluminum", "zinc", "wire", "duct", "treadplate", "gunmetal", "galvanized", "plate", "pipe", "foil", "grate", "chain"]),
    ("Wall_Plaster", ["plaster", "stucco", "drywall", "wallpaper", "wall"]),
    ("Fabric_Cloth", ["fabric", "cloth", "carpet", "wool", "linen", "silk", "blanket", "textile", "curtain", "knit", "denim", "jeans", "rug", "upholstery", "leather", "quilted", "braided", "mat"]),
    ("Ground_Soil", ["ground", "soil", "dirt", "earth", "sand", "mud", "farmland", "terrain", "landscape", "arid", "desert", "field", "forest", "roots", "roots", "leaves"]),
    ("Snow_Ice", ["snow", "ice", "frost", "winter", "glacier"]),
    ("Water", ["water", "ocean", "sea", "river", "beach", "shore", "wave", "lake", "rain", "raindrop", "puddle", "flood"]),
    ("Vegetation", ["grass", "leaf", "leaves", "tree", "hedge", "ivy", "moss", "plant", "flower", "foliage", "bush", "groundplants", "corn", "wheat", "fern", "clover", "daisy", "lawn", "pine", "needles"]),
    ("Decals_Overlays", ["decal", "stain", "overlay", "smudge", "scuff", "splatter", "grunge", "scratch", "leak", "spill", "spot", "sticker", "crumpled", "photo"]),
    ("Signs_Posters", ["poster", "sign", "banner", "graffiti", "label", "text", "typography", "letter"]),
    ("Paper_Cardboard", ["paper", "cardboard", "card", "newspaper", "page"]),
    ("Food", ["food", "grain", "bread", "fruit", "coffee", "kafee", "lemon", "meat", "vegetable"]),
    ("Reference", ["screenshot", "read me", "readme", "credits", "license", "ds store", "logo", "watermark", "unreal engine", "template", "hands", "koerpermerkmale"]),
]
CAT_DESC = {
    "HDRi_Sky": "HDRI environment maps and sky/skybox images (.exr/.hdr + sky JPG/PNG).",
    "Pixel_Art": "Hand-made pixel-art tile sets (Bricks, Dungeon, etc.).",
    "Glitch_Art": "Glitch / datamosh art images.",
    "Military": "Military: camo, bunkers, supply cases, grenades, sandbags, ammo, fuel tanks.",
    "Medieval": "Medieval: castle walls, chain mail, shields, ornaments, village materials.",
    "SciFi_Space": "Sci-fi / space: spaceship & space-station walls, floors, grates, solar panels, CRT screens.",
    "Animal_Skin": "Real animal skins/furs/scales (frog, snake, whale, tiger, fish...).",
    "Organic_Creature": "Fantasy/horror organic: creature skin, dragon scales, flesh, guts, bone, coral.",
    "Destruction": "Destruction: rubble, bullet holes, explosion craters, burnt walls.",
    "Doors_Windows": "Doors, gates, hatches and windows.",
    "Roofing": "Roof textures, shingles, roofing metals.",
    "Buildings": "Building facades, houses, towers, city blocks.",
    "Brick": "Brick walls and facades.",
    "Concrete": "Concrete, cement, cinderblock.",
    "Tiles": "Tile floors/walls, mosaic, herringbone, hexagon.",
    "Pavement_Road": "Pavement, asphalt, roads, streets, stairs, manholes.",
    "Stone_Rock": "Natural rock, stone, cliff, marble, gravel, pebbles.",
    "Wood": "Wood planks, bark, beams, parquet, floors, siding.",
    "Metal": "Metal, steel, rust, copper, ducts, plates, foil.",
    "Wall_Plaster": "Plaster, stucco, drywall, wallpaper, general walls.",
    "Fabric_Cloth": "Fabric, cloth, carpet, wool, linen, leather, quilts.",
    "Ground_Soil": "Ground, soil, dirt, sand, mud, fields, terrain.",
    "Snow_Ice": "Snow, ice, frost, winter.",
    "Water": "Water, ocean, waves, rivers, beach, rain, puddles.",
    "Vegetation": "Grass, leaves, trees, hedges, ivy, moss, plants, flowers.",
    "Decals_Overlays": "Decals, stains, overlays, smudges, scratches, grunge, photos.",
    "Signs_Posters": "Posters, signs, banners, graffiti, text.",
    "Paper_Cardboard": "Paper, cardboard, cards.",
    "Food": "Food and grain textures.",
    "Reference": "Screenshots, readme/license, logos, non-texture reference.",
    "Misc": "Uncategorised files.",
}
MAP_DEF = {
    "albedo": ["albedo", "basecolor", "base", "color", "colour", "diffuse", "diff", "dif", "col", "c"],
    "normal": ["normal", "nrm", "nor", "nrml", "n"],
    "roughness": ["roughness", "rough", "rgh", "gloss", "glossiness", "smoothness"],
    "ao": ["ao", "occlusion", "occ", "ambientocclusion"],
    "height": ["height", "disp", "displacement", "hgt"],
    "metallic": ["metallic", "metalness", "metal", "m"],
    "specular": ["specular", "spec", "refl", "reflection", "reflectivity"],
    "emissive": ["emissive", "emission", "emit"],
    "alpha": ["alpha", "opacity", "mask", "masked", "cutout"],
    "overlay": ["overlay"],
    "bump": ["bump"],
    "curvature": ["curvature", "cavity"],
    "translucent": ["translucency", "sss", "translucent"],
}
TOKEN2MAP = {}
for m, toks in MAP_DEF.items():
    for t in toks:
        TOKEN2MAP.setdefault(t, m)
MAPTOKENS = set(TOKEN2MAP)
RESTOK = {"512", "1024", "2048", "4096", "8192", "256", "2k", "1k", "4k", "8k", "16k"}
DROP = {"seamless", "masked", "mirror", "wm", "png", "jpg", "raw", "base"}
FILEEXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".psd", ".exr", ".hdr"}

def top_of(rel):
    seg = rel.split(os.sep)
    return seg[0] if len(seg) > 1 else "(root)"

def cat_of(rel, ext, top):
    if top == "Pixel_Art":
        return "Pixel_Art"
    if ext in (".exr", ".hdr"):
        return "HDRi_Sky"
    t = norm(rel)
    for cat, kws in CAT_RULES:
        for kw in kws:
            if re.search(r"\b" + re.escape(kw) + r"s?\b", t):
                return cat
    return "Misc"

def map_of(fname):
    toks = re.split(r"[^a-z0-9]+", os.path.splitext(fname)[0].lower())
    for tk in reversed(toks):
        if tk in TOKEN2MAP:
            return TOKEN2MAP[tk]
    return ""

def setkey_of(rel):
    d = os.path.dirname(rel).replace("\\", "/")
    base = os.path.splitext(os.path.basename(rel))[0]
    base = re.sub(r"[ _]\(\d+\)$", "", base)
    toks = re.split(r"[^a-z0-9]+", base.lower())
    keep = []
    for tk in toks:
        if not tk or tk in MAPTOKENS or tk in RESTOK or tk in DROP:
            continue
        if re.fullmatch(r"\d+(\.\d+)?x\d+(\.\d+)?", tk):
            continue
        keep.append(tk)
    return (d + " / " + " ".join(keep)).strip()

rows = []
for r in meta:
    rel = os.path.relpath(r["path"], ROOT)
    if os.path.basename(rel) in SKIP_NAMES:
        continue
    ext = r["ext"]
    top = top_of(rel)
    rows.append({"p": rel.replace("\\", "/"), "top": top, "cat": cat_of(rel, ext, top),
                 "map": map_of(os.path.basename(rel)),
                 "set": setkey_of(rel) if ext in FILEEXT else "",
                 "ext": ext, "bytes": r["bytes"], "w": r["w"], "h": r["h"]})

cat_c = collections.Counter(r["cat"] for r in rows)
cat_b = collections.Counter()
for r in rows:
    cat_b[r["cat"]] += r["bytes"] or 0
map_c = collections.Counter(r["map"] or "(none)" for r in rows)
top_c = collections.Counter(r["top"] for r in rows)
ext_c = collections.Counter(r["ext"] or "(none)" for r in rows)
resb = collections.Counter()
for r in rows:
    m = max(r["w"] or 0, r["h"] or 0)
    resb["unknown" if not m else ("<=512" if m <= 512 else "1K" if m <= 1024 else "2K" if m <= 2048 else "4K" if m <= 4096 else ">4K")] += 1
exact = collections.Counter((f"{r['w']}x{r['h']}" if r["w"] else "?") for r in rows)

sets = collections.defaultdict(list)
for r in rows:
    if r["set"]:
        sets[r["set"]].append(r)
set_rows = []
for k, v in sets.items():
    if not k:
        continue
    w = max((x["w"] or 0) for x in v)
    h = max((x["h"] or 0) for x in v)
    set_rows.append({"set": k, "cat": v[0]["cat"], "files": len(v),
                     "maps": sorted(set(x["map"] for x in v if x["map"])),
                     "res": f"{w}x{h}" if w else "?", "example": v[0]["p"]})
set_rows.sort(key=lambda x: (x["cat"], x["set"]))
setcat = collections.Counter(s["cat"] for s in set_rows)
sets_with = collections.Counter(tuple(s["maps"]) for s in set_rows)

# category examples
cat_examples = collections.defaultdict(list)
for r in rows:
    if len(cat_examples[r["cat"]]) < 3:
        cat_examples[r["cat"]].append(r["p"])
cat_maps = collections.defaultdict(collections.Counter)
for r in rows:
    if r["map"]:
        cat_maps[r["cat"]][r["map"]] += 1

TOT_B = sum(r["bytes"] or 0 for r in rows)
nfolders = 0
for dp, dn, fn in os.walk(ROOT):
    nfolders += 1

index = {
    "schema": "texture-library-index/1",
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "root": ROOT,
    "purpose": "Machine-readable index of a 3D texture/material library so agents can locate textures, PBR map sets and resolutions fast without scanning the tree.",
    "read_order": [
        "1. Read library_index.json (top folders, categories, map types, resolutions, naming conventions).",
        "2. Grep library_files.jsonl for files (path/top/cat/map/ext/bytes/w/h).",
        "3. Grep material_sets.jsonl to find full PBR material sets and which maps they contain.",
        "4. Only then open the concrete file paths.",
    ],
    "totals": {"files": len(rows), "folders": nfolders, "size_bytes": TOT_B, "size_gb": round(TOT_B / 1e9, 2),
               "categories": len(cat_c), "material_sets_est": len(set_rows)},
    "top_level": {t: {"files": top_c[t]} for t in sorted(top_c, key=lambda x: -top_c[x])},
    "extensions": dict(ext_c.most_common()),
    "categories": {c: {"files": n, "size_gb": round(cat_b[c] / 1e9, 2), "material_sets": setcat.get(c, 0),
                        "common_maps": dict(cat_maps[c].most_common(6)),
                        "description": CAT_DESC.get(c, ""), "example_files": cat_examples[c]}
                   for c, n in cat_c.most_common()},
    "map_types": {k: {"files": v} for k, v in map_c.most_common()},
    "resolutions": {"by_bucket": dict(resb.most_common()), "by_exact_top20": dict(exact.most_common(20))},
    "naming_conventions": {
        "PBR_suffixes": "Map type in the 'map' field: albedo/diffuse, normal, roughness/glossiness, ao, height/disp, metalness, reflection/specular, emissive, opacity/alpha/mask.",
        "Gumroad_lib": "4K_Textures_Gumroad/<Class>/<material>/<material>_<map>.jpg - each <material> folder is one PBR set (~8-9 maps incl. render preview).",
        "Textures_com": "TexturesCom_<Name>_<tiling>x<tiling>_<res>_<type>.tif ; trailing _S/_M/_L on preview JPGs are SIZE variants, not maps.",
        "Poliigon_like": "<Name>_<id>_<type> (e.g. _COLOR/_NRM/_DISP/_OCC/_SPEC/_GLOSS).",
    },
    "query_cookbook": {
        "by_category": 'grep \'"cat":"Wood"\' library_files.jsonl',
        "by_map_type": 'grep \'"map":"normal"\' library_files.jsonl',
        "by_resolution": 'grep \'"w":2048\' library_files.jsonl',
        "sets_with_normal_and_roughness": 'grep -E \'"maps":\\[[^]]*"(normal|roughness)"\' material_sets.jsonl',
        "sets_in_category": 'grep \'"cat":"Concrete"\' material_sets.jsonl',
    },
    "notes": {
        "layout": "Files are physically unchanged (NOT reorganised). This index provides virtual tags; texture/PBR sets must stay together for project references.",
        "biggest_folder": "4K_Textures_Gumroad holds the bulk (PBR sets, ~2K after an earlier downsize).",
        "heuristic": "Categories come from folder/filename keywords; 'set' grouping and 'maps' are heuristic.",
        "possible_duplicate": "Verified 2026-09-15: the suspected second copy at D:\\Assets_Blender\\Textures_Materials\\4K_Textures_Gumroad does NOT exist. No duplicate present.",
    },
}
json.dump(index, open(os.path.join(ROOT, "library_index.json"), "w", encoding="utf-8"), ensure_ascii=True, indent=1)

with open(os.path.join(ROOT, "library_files.jsonl"), "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps({"p": r["p"], "top": r["top"], "cat": r["cat"], "map": r["map"],
                             "ext": r["ext"], "bytes": r["bytes"], "w": r["w"], "h": r["h"]}, ensure_ascii=True) + "\n")
with open(os.path.join(ROOT, "material_sets.jsonl"), "w", encoding="utf-8") as fh:
    for s in set_rows:
        fh.write(json.dumps(s, ensure_ascii=True) + "\n")

print("files:", len(rows), "sets:", len(set_rows))
print("cats:", dict(cat_c.most_common()))
print("res:", dict(resb.most_common()))
print("top:", dict(top_c.most_common()))
print("set-map-profiles:", sets_with.most_common(5))
print("wrote library_index.json, library_files.jsonl, material_sets.jsonl")
