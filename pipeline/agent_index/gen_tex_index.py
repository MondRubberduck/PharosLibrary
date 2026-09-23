from pathlib import Path
import os, re, json, collections, datetime, struct, sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import refuse_near_empty, section_root
ROOT = section_root("textures", "AGENT_TEX_ROOT")
TMP = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(ROOT):
    raise SystemExit("FATAL: texture root does not exist: %s" % ROOT)
SKIP_NAMES = {"library_index.json", "library_files.jsonl", "material_sets.jsonl", "AGENT_INDEX.md"}

# ---------------------------------------------------------------------------
# tex_meta cache (path/ext/bytes/w/h per image). The cache is BUILT HERE when
# missing (it used to be a hand-made leftover that no script produced, which
# made this chain author-machine-only). AGENT_TEX_META env overrides the
# cache path; --rescan forces a rebuild. Dims are read from file headers by a
# stdlib sniffer; anything unparseable gets w/h None and shows as "unknown"
# resolution -- never a crash, never a guess.
# ---------------------------------------------------------------------------
RES_CANON = ("--rescan" in sys.argv)
META_PATH = os.environ.get("AGENT_TEX_META") or os.path.join(TMP, "tex_meta.json")
FILEEXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif",
           ".psd", ".exr", ".hdr"}

def _image_dims(path, head):
    """(w, h) from file headers, or (None, None). Header-level only."""
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:2] == b"\xff\xd8":                                   # JPEG
            data = head
            pos = 2
            while pos + 4 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                    pos += 2
                    continue
                seglen = struct.unpack(">H", data[pos + 2:pos + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                    return (w, h)
                pos += 2 + seglen
            return (None, None)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:2] == b"BM":
            w, h = struct.unpack("<ii", head[18:26])
            return (w, abs(h))
        if head[:4] in (b"II*\x00", b"MM\x00\x00"):                   # TIFF
            e = "<" if head[:2] == b"II" else ">"
            ifd = struct.unpack(e + "I", head[4:8])[0]
            with open(path, "rb") as fh:
                fh.seek(ifd)
                body = fh.read(2 + 12 * 64)
            n = struct.unpack(e + "H", body[:2])[0]
            dims = {}
            for i in range(min(n, 64)):
                off = 2 + 12 * i
                tag, typ = struct.unpack_from(e + "HH", body, off)
                if tag in (256, 257):
                    val = struct.unpack_from(e + ("H" if typ == 3 else "I"),
                                             body, off + 8)[0]
                    dims[tag] = val
            return (dims.get(256), dims.get(257))
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":             # WEBP
            with open(path, "rb") as fh:
                data = fh.read(64)
            o = 12
            while o + 8 <= len(data):
                fourcc = data[o:o + 4]
                size = struct.unpack("<I", data[o + 4:o + 8])[0]
                payload = data[o + 8:o + 8 + min(size, 32)]
                if fourcc == b"VP8X" and len(payload) >= 10:
                    w = int.from_bytes(payload[4:7], "little") + 1
                    h = int.from_bytes(payload[7:10], "little") + 1
                    return (w, h)
                if fourcc == b"VP8L" and len(payload) >= 6 and payload[0] == 0x2F:
                    bits = struct.unpack("<I", payload[1:5])[0]
                    return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                if fourcc == b"VP8 " and payload[3:6] == b"\x9d\x01\x2a":
                    w = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
                    h = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
                    return (w, h)
                o += 8 + size + (size & 1)
            return (None, None)
        if head[:4] == b"\x76\x2f\x31\x01":                           # OpenEXR
            with open(path, "rb") as fh:
                fh.seek(8)
                buf = fh.read(1 << 16)
            i = 0
            while i < len(buf):
                e = buf.find(b"\x00", i)
                if e < 0 or e == i:
                    break
                name = buf[i:e]
                i = e + 1
                e = buf.find(b"\x00", i)
                if e < 0:
                    break
                i = e + 1
                size = struct.unpack("<I", buf[i:i + 4])[0]
                i += 4
                if name == b"dataWindow" and size == 16:
                    x0, y0, x1, y1 = struct.unpack("<iiii", buf[i:i + 16])
                    return (x1 - x0 + 1, y1 - y0 + 1)
                i += size
        if head[:11] in (b"#?RADIANCE\n", b"#?\n"):                   # Radiance HDR
            with open(path, "rb") as fh:
                for _ in range(64):
                    line = fh.readline(512)
                    if not line:
                        break
                    if line.startswith(b"-Y"):
                        parts = line.split()
                        h, w = int(parts[1]), int(parts[3])
                        return (w, h)
        if head[:4] == b"8BPS":                                       # PSD
            h, w = struct.unpack(">II", head[14:22])
            return (w, h)
    except (OSError, struct.error, IndexError):
        pass
    return (None, None)

def build_meta():
    rows = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d != "_Agent_Files"]
        for f in fn:
            ext = os.path.splitext(f)[1].lower()
            if ext not in FILEEXT:
                continue
            p = os.path.join(dp, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            w = h = None
            try:
                with open(p, "rb") as fh:
                    head = fh.read(64)
                w, h = _image_dims(p, head)
            except OSError:
                pass
            rows.append({"path": p.replace("\\", "/"), "ext": ext,
                         "bytes": size, "w": w, "h": h})
    return rows

if RES_CANON or not os.path.isfile(META_PATH):
    if RES_CANON:
        print("tex_meta cache: RESCAN requested -- rebuilding", flush=True)
    else:
        print("tex_meta cache missing (%s) -- building from the texture "
              "root (one-time; use --rescan to refresh later)" % META_PATH,
              flush=True)
    meta = build_meta()
    with open(META_PATH, "w", encoding="utf-8") as _mf:
        json.dump(meta, _mf)
    print("tex_meta cache: %d images written to %s" % (len(meta), META_PATH),
          flush=True)
meta = json.load(open(META_PATH, encoding="utf-8"))

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
        "per_set_folders": "<Class>/<material>/<material>_<map>.jpg - a folder whose files share one <material> stem is one PBR set.",
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
        "biggest_folder": ("%s holds %d of %d texture files" % (
            collections.Counter(r["top"] for r in rows).most_common(1)[0]
            + (len(rows),)) if rows else "no texture files found"),
        "heuristic": "Categories come from folder/filename keywords; 'set' grouping and 'maps' are heuristic.",
    },
}
# LOUD target + refusal: a wrong root once overwrote live index files
print("WRITE TARGET: %s (library_index/files/material_sets)" % ROOT)
refuse_near_empty(len(rows), os.path.join(ROOT, "library_files.jsonl"),
                  "texture files")
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
