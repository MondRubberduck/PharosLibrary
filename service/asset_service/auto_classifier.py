"""3-tier non-destructive auto-categorizer.

Assigns every logical asset pack a virtual taxonomy:
    style        Photorealistic | Stylized | Neutral/Unclassified
    domain       Human Animations | Archviz | Props | Environment |
                 Characters | Textures_HDRI
    sub_category per-domain refinement (Locomotion, Furniture, Modular, ...)
    confidence   0.0-1.0 with agreement/conflict rules from the coding plan

Tiers:
    Layer 1  token & manifest heuristics over folder paths, filenames and
             bundled .json/.txt sidecar text (camelCase-aware), corrected by
             PACK COMPOSITION (a texture-only pack is Textures_HDRI no
             matter what its folder is called; texture tokens are dampened
             in packs that contain geometry) and vendor domain hints.
    Layer 2  binary inspection: .uasset AssetClass via uasset_parser, FBX
             marker scan (pure animation takes vs embedded anims vs skinned
             character meshes).
    Layer 3  OPTIONAL local vision (Ollama endpoint via ai.vision_endpoint
             in library.yaml). Disabled by default -- nothing ever leaves
             the workstation (license safety rule).

All input is read from disk 'rb'; the classifier itself never touches a file.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

DOMAINS = (
    "Human Animations", "Archviz", "Props", "Environment",
    "Characters", "Textures_HDRI",
)

STYLES = ("Photorealistic", "Stylized", "Neutral/Unclassified")

# ---------------------------------------------------------------------------
# Layer 1: token heuristics
# ---------------------------------------------------------------------------

DOMAIN_TOKENS: dict[str, list[str]] = {
    "Human Animations": [
        "anim", "animation", "walk", "run", "idle", "bvh", "mocap", "motion",
        "blendspace", "rootmotion", "cmu", "mixamo", "locomotion", "combat",
        "fight", "attack", "punch", "kick", "sword", "dance", "chat", "talk",
        "conversation", "gesture", "daily", "activities", "sitting", "lying",
        "climb", "jump", "wave",
    ],
    "Archviz": [
        "archviz", "interior", "chair", "table", "sofa", "couch", "decor",
        "villa", "kitchen", "facade", "corbusier", "furniture", "mansion",
        "apartment", "livingroom", "bedroom", "bathroom", "lamp", "door",
        "window", "stairs", "architecture", "building", "house", "office",
        "vase", "rug", "shelf", "cottage",
    ],
    "Environment": [
        "environment", "landscape", "terrain", "forest", "nature", "rock",
        "cliff", "foliage", "tree", "plant", "grass", "desert", "biome",
        "megascan", "scan", "modular", "kitbash", "kit", "alley", "street",
        "city", "urban", "cave", "mountain", "water", "river", "bridge",
        "village", "countryside", "prison", "tower", "subway", "station",
        "province", "planet",
    ],
    "Characters": [
        "character", "char", "mannequin", "humanoid", "dummy", "male",
        "female", "skin", "outfit", "clothing", "costume", "portrait",
        "rigged", "skeleton", "presets", "mens", "womens",
    ],
    "Textures_HDRI": [
        "texture", "material", "pbr", "atlas", "tile", "seamless", "hdri",
        "hdr", "sky", "8k", "4k", "albedo", "normal", "roughness", "metallic",
        "mask", "decal", "trim", "substance", "smart", "fabric", "denim",
        "cotton", "canvas", "corduroy", "nylon", "leather", "basecolor",
        "diffuse", "diff", "nrm", "col", "rough", "metal", "mtlx",
    ],
    "Props": [
        "prop", "props", "weapon", "gun", "shield", "barrel", "crate",
        "box", "tool", "horn", "bone", "skull", "artifact", "vehicle",
        "car", "cart", "sign", "banner", "torch", "pot", "books", "wildwest",
        "western", "cowboy", "warzone",
    ],
}

STYLE_TOKENS: dict[str, list[str]] = {
    "Stylized": [
        "stylized", "handpainted", "hand-painted", "lowpoly", "low-poly",
        "toon", "anime", "cartoon", "celshade", "cel-shade", "flat", "painterly",
    ],
    "Photorealistic": [
        "photoreal", "photorealistic", "realistic", "megascans", "quixel",
        "photoscan", "photogrammetry", "scan",
    ],
}

VENDOR_TOKENS: dict[str, list[str]] = {
    "Quixel": ["quixel", "megascans"],
    "Fab": ["fab"],                      # path segment only (see SEGMENT_ONLY)
    "Gumroad": ["gumroad"],
    "CGTrader": ["cgtrader"],
    "TurboSquid": ["turbosquid"],
    "Reallusion": ["reallusion", "actorcore", "actor-core"],
    "Mixamo": ["mixamo"],
    "Sketchfab": ["sketchfab", "uploads_files"],
    "KitBash3D": ["kitbash3d", "kb3d"],
    "PolyHaven": ["polyhaven", "poly haven"],
    "Leartes": ["leartes"],
}
SEGMENT_ONLY_VENDOR_TOKENS = {"fab"}

# Factual domain/style bias for known marketplace vendors. Applied as a
# Layer-1 score bump (+VENDOR_HINT_WEIGHT) AFTER explicit path tokens, so a
# pack whose names clearly say otherwise still wins.
VENDOR_DOMAIN_HINT: dict[str, str] = {
    "Quixel": "Environment",
    "KitBash3D": "Environment",
    "Leartes": "Environment",
    "Mixamo": "Human Animations",
    "Reallusion": "Characters",
    "PolyHaven": "Textures_HDRI",
}
VENDOR_STYLE_HINT: dict[str, str] = {
    "Quixel": "Photorealistic",
    "KitBash3D": "Photorealistic",
    "Leartes": "Photorealistic",
    "PolyHaven": "Photorealistic",
}
VENDOR_HINT_WEIGHT = 8.0

# Sketchfab archive naming: uploads_files_<id>_<name>
_SKETCHFAB_RE = re.compile(r"uploads_files_\d+")

STOPWORDS = {
    "the", "and", "for", "with", "final", "pack", "v1", "v2", "vol", "set",
    "files", "file", "assets", "asset", "content", "source", "data", "new",
    "folder", "export", "exports", "objs", "game", "engine", "general",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> set[str]:
    text = _CAMEL_RE.sub(" ", text).lower()          # PackName -> pack name
    return {t for t in _SPLIT_RE.split(text) if len(t) > 2 and t not in STOPWORDS}


def _segment_tokens(rel_path: str) -> set[str]:
    segments: set[str] = set()
    for seg in rel_path.lower().replace("\\", "/").split("/"):
        segments |= _tokens(seg)
    return segments


# ---------------------------------------------------------------------------
# Input structures (built by the indexer)
# ---------------------------------------------------------------------------

# geometry file types for composition analysis (.usd can be a material file,
# so it does not count as geometry here)
GEOMETRY_TYPES = {"mesh", "uasset", "blend"}
USD_EXTS = {".usd", ".usda", ".usdc", ".usdz"}


@dataclass
class PackInput:
    """Everything the classifier may look at. Pure data, no live file handles."""
    name: str
    rel_path: str                                   # relative to canonical root
    files: list[dict[str, Any]] = field(default_factory=list)
    # files entries: {"relpath", "ext", "size", "file_type"}
    uasset_classes: list[str] = field(default_factory=list)   # Layer 2 probe
    fbx_signals: list[dict[str, bool]] = field(default_factory=list)
    sidecar_text: str = ""                          # bundled json/txt, capped


@dataclass
class Classification:
    domain: str = "Props"
    style: str = "Neutral/Unclassified"
    sub_category: str = "General"
    confidence: float = 0.0
    validation_status: str = "auto_tagged"
    vendor: Optional[str] = None
    host_category: str = "Assets"
    host_tags: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Layer 2 helpers
# ---------------------------------------------------------------------------

_UASSET_CLASS_DOMAINS: dict[str, str] = {
    "AnimSequence": "Human Animations",
    "AnimMontage": "Human Animations",
    "AnimComposite": "Human Animations",
    "BlendSpace": "Human Animations",
    "BlendSpace1D": "Human Animations",
    "AnimBlueprint": "Human Animations",
    "LevelSequence": "Human Animations",
    "SkeletalMesh": "Characters",
    "Skeleton": "Characters",
    "PhysicsAsset": "Characters",
    "Texture2D": "Textures_HDRI",
    "TextureCube": "Textures_HDRI",
    "Material": "Textures_HDRI",
    "MaterialInstanceConstant": "Textures_HDRI",
    "MaterialFunction": "Textures_HDRI",
    "World": "Environment",
    "Level": "Environment",
}

_FBX_MARKERS = {
    b"AnimationStack": "anim",
    b"Takes": "takes",
    b"LimbNode": "bones",
    b"Geometry": "mesh",
    b"Deformer": "skin",
}


def probe_fbx_markers(blob: bytes) -> dict[str, bool]:
    """Layer-2 FBX evidence from raw bytes (works for binary and ASCII FBX)."""
    hits = {label: pattern in blob for pattern, label in _FBX_MARKERS.items()}
    return {
        "has_anim": hits["anim"] or hits["takes"],
        "has_bones": hits["bones"],
        "has_mesh": hits["mesh"],
        "has_skin": hits["skin"],
    }


def _uasset_domain_votes(classes: list[str]) -> Counter:
    votes = Counter()
    for cls in classes:
        domain = _UASSET_CLASS_DOMAINS.get(cls)
        if domain:
            votes[domain] += 1
        # StaticMesh stays ambiguous (Props/Archviz/Environment): Layer 1 decides
    return votes


def _fbx_domain_votes(signals: list[dict[str, bool]]) -> Counter:
    """Weight FBX evidence by what the file actually is:

    - pure animation take (anim, no mesh)          -> strong Human Animations
    - skinned character mesh (mesh+bones+skin+anim) -> strong Characters
    - animation EMBEDDED in a mesh file (anim+mesh,
      no skin: doors, flags, demo rigs in env packs)-> weak Human Animations
    """
    votes: Counter = Counter()
    for sig in signals:
        anim, mesh = sig.get("has_anim"), sig.get("has_mesh")
        bones, skin = sig.get("has_bones"), sig.get("has_skin")
        if anim and not mesh:
            votes["Human Animations"] += 2.0
        elif anim and mesh and skin:
            votes["Characters"] += 2.0
            votes["Human Animations"] += 0.5
        elif anim and mesh:
            votes["Human Animations"] += 0.5
        if mesh and skin:
            votes["Characters"] += 1.0
    return votes


# ---------------------------------------------------------------------------
# Sub-categories
# ---------------------------------------------------------------------------

_SUBCATEGORY_TOKENS: dict[str, list[tuple[str, list[str]]]] = {
    "Human Animations": [
        ("Daily Life", ["daily", "activities", "sitting", "eating", "lying"]),
        ("Dance", ["dance"]),
        ("Dialogue", ["chat", "talk", "conversation", "dialogue"]),
        ("Idle", ["idle", "stand"]),
        ("Locomotion", ["walk", "run", "jog", "sprint", "jump", "climb", "locomotion"]),
        ("Combat", ["combat", "fight", "attack", "punch", "kick", "sword", "battle"]),
    ],
    "Archviz": [
        ("Furniture", ["chair", "sofa", "couch", "table", "shelf", "bed", "furniture"]),
        ("Kitchen", ["kitchen", "counter", "cabinet"]),
        ("Interior", ["interior", "room", "livingroom", "bedroom", "bathroom", "decor", "lamp", "vase", "rug"]),
        ("Buildings", ["mansion", "villa", "house", "facade", "building", "apartment", "architecture", "office", "cottage"]),
    ],
    "Environment": [
        ("Nature", ["forest", "rock", "cliff", "tree", "foliage", "plant", "grass", "desert", "nature", "mountain", "water", "planet", "countryside"]),
        ("Urban", ["street", "city", "urban", "alley", "facade", "shop", "subway", "station", "prison"]),
        ("Modular Kits", ["modular", "kitbash", "kit", "tile", "village"]),
    ],
    "Characters": [
        ("Rigged Humans", ["male", "female", "mannequin", "humanoid", "dummy", "character", "rigged"]),
        ("Parts & Presets", ["presets", "skin", "outfit", "clothing"]),
    ],
    "Textures_HDRI": [
        ("HDRI", ["hdri", "hdr", "sky"]),
        ("PBR Sets", ["pbr", "material", "albedo", "roughness", "atlas", "tile", "seamless", "fabric", "denim", "cotton", "canvas", "corduroy", "nylon"]),
        ("Decals", ["decal", "mask", "trim"]),
    ],
    "Props": [
        ("Weapons", ["weapon", "sword", "gun", "shield", "axe", "bow"]),
        ("Furniture", ["chair", "table", "sofa", "shelf"]),
        ("Decor", ["decor", "vase", "lamp", "pot", "books", "banner", "torch"]),
        ("Creatures & Oddities", ["horn", "bone", "skull", "dino", "creature"]),
        ("Structures", ["warzone", "western", "wildwest", "barrel", "crate"]),
    ],
}


def _sub_category(domain: str, tokens: set[str]) -> str:
    for name, sub_tokens in _SUBCATEGORY_TOKENS.get(domain, []):
        if tokens & set(sub_tokens):
            return name
    return "General"


# ---------------------------------------------------------------------------
# Layer 3: optional local vision (Ollama). Disabled unless configured.
# ---------------------------------------------------------------------------

class OllamaVision:
    """Queries a LOCAL Ollama endpoint for style/domain from a preview image.
    Never sends asset files anywhere off-machine; disabled unless
    ai.vision_endpoint is set in config."""

    PROMPT = (
        "Classify this 3D asset preview. Answer ONLY with JSON: "
        '{"style": "Photorealistic" or "Stylized", '
        '"domain": one of "Human Animations", "Archviz", "Props", '
        '"Environment", "Characters", "Textures_HDRI"}'
    )

    def __init__(self, endpoint: str, model: str):
        self.endpoint = endpoint.rstrip("/")
        self.model = model

    def classify_preview(self, image_path: str) -> Optional[dict[str, str]]:
        import base64
        try:
            with open(image_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            payload = json.dumps({
                "model": self.model, "prompt": self.PROMPT, "stream": False,
                "images": [b64],
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.endpoint}/api/generate", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                answer = json.loads(resp.read().decode("utf-8")).get("response", "")
            match = re.search(r"\{.*\}", answer, re.S)
            if not match:
                return None
            result = json.loads(match.group(0))
            if result.get("style") in STYLE_TOKENS and result.get("domain") in DOMAINS:
                return result
        except Exception:
            return None
        return None


def get_vision_classifier(config: dict) -> Optional[OllamaVision]:
    ai = (config or {}).get("ai") or {}
    endpoint, model = ai.get("vision_endpoint"), ai.get("vision_model")
    if not endpoint or not model:
        return None
    return OllamaVision(endpoint, model)


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def _basename(relpath: str) -> str:
    return relpath.rsplit("/", 1)[-1]


def _layer1_scores(pack: PackInput) -> tuple[dict[str, float], set[str]]:
    rel = pack.rel_path.replace("\\", "/")
    path_tokens = _tokens(rel.replace("/", " ") + " " + pack.name)
    seg_tokens = _segment_tokens(rel)
    name_tokens: Counter = Counter()
    for f in pack.files:
        name_tokens.update(_tokens(_basename(f["relpath"])))
    sidecar_tokens = _tokens(pack.sidecar_text[:4000])

    path_blob = rel.lower()
    file_blob = " ".join(_basename(f["relpath"]) for f in pack.files).lower()
    sidecar_blob = pack.sidecar_text[:4000].lower()
    matched: set[str] = set()
    scores: dict[str, float] = {}
    for domain, domain_tokens in DOMAIN_TOKENS.items():
        score = 0.0
        for tok in domain_tokens:
            if tok in path_tokens or tok in seg_tokens:
                score += 2.0
            if name_tokens.get(tok):
                score += 1.0 * min(name_tokens[tok], 5)
            if tok in path_blob or tok in file_blob:
                score += 1.5       # substring: catches glued words (HornsVolume2)
                matched.add(tok)
            if tok in sidecar_blob:
                score += 0.5
        scores[domain] = score
    return scores, path_tokens | seg_tokens | sidecar_tokens | matched


MARKETPLACE_VENDORS = {"Gumroad", "Sketchfab", "Fab", "CGTrader", "TurboSquid"}


def _vendor(pack: PackInput) -> tuple[Optional[str], bool]:
    """(vendor, is_scan_source). Marketplace names (where it was bought) lose
    to creator names (what it is) when both appear in the path."""
    low = pack.rel_path.lower().replace("\\", "/")
    segments = _segment_tokens(pack.rel_path)
    creators: list[str] = []
    marketplace: list[str] = []
    for vendor, vendor_tokens in VENDOR_TOKENS.items():
        for tok in vendor_tokens:
            hit = (tok in segments if tok in SEGMENT_ONLY_VENDOR_TOKENS
                   else tok in low)
            if not hit:
                continue
            (marketplace if vendor in MARKETPLACE_VENDORS else creators).append(vendor)
            break
    if _SKETCHFAB_RE.search(low) and "Sketchfab" not in marketplace:
        marketplace.append("Sketchfab")
    vendor = (creators or marketplace or [None])[0]
    return vendor, vendor == "Quixel"


def _composition(pack: PackInput) -> dict[str, Any]:
    counts = Counter(f["file_type"] for f in pack.files)
    geometry = sum(counts.get(t, 0) for t in GEOMETRY_TYPES)
    # .usd files can be MaterialX/material payloads rather than meshes --
    # exclude them from the geometry count
    usd_files = sum(1 for f in pack.files if f["ext"] in USD_EXTS)
    geometry = max(0, geometry - usd_files)
    textures = counts.get("texture", 0)
    anim = counts.get("anim", 0)
    return {
        "counts": dict(counts),
        "geometry": geometry,
        "textures": textures,
        "anim": anim,
        "texture_only": geometry == 0 and anim == 0 and textures >= 3,
    }


def classify_pack(pack: PackInput, vision: Optional[OllamaVision] = None,
                  preview_path: Optional[str] = None) -> Classification:
    result = Classification()
    exts = sorted({f["ext"].lstrip(".") for f in pack.files if f["ext"]})
    result.formats = exts
    comp = _composition(pack)

    unity_pack = bool({"prefab", "meta", "cs"} & set(exts))
    if unity_pack:
        result.evidence["flavor"] = "unity package (index-only per human plan)"
    else:
        targets = []
        if any(e in exts for e in ("uasset", "umap")):
            targets.append("unreal")
        if any(e in exts for e in ("blend", "fbx", "obj", "gltf", "glb", "bvh")):
            targets.append("blender")
        result.targets = targets or ["blender"]

    l1_scores, tokens = _layer1_scores(pack)
    result.vendor, scan_source = _vendor(pack)

    # composition corrections BEFORE ranking
    if comp["geometry"] >= 3:
        # textures are everywhere in 3D packs; they only define the pack
        # when there is (almost) nothing else
        l1_scores["Textures_HDRI"] *= 0.5
    if result.vendor and result.vendor in VENDOR_DOMAIN_HINT:
        l1_scores[VENDOR_DOMAIN_HINT[result.vendor]] += VENDOR_HINT_WEIGHT

    l1_top = max(l1_scores, key=l1_scores.get)          # type: ignore[arg-type]
    runner_up = sorted(l1_scores.values(), reverse=True)
    # margin = top vs its closest competitor, not share-of-total (a broad
    # token vocabulary must not drown out a clear winner)
    l1_margin = (l1_scores[l1_top] / (l1_scores[l1_top] + runner_up[1])
                 if len(runner_up) > 1 and runner_up[1] > 0 else 1.0)

    u_votes = _uasset_domain_votes(pack.uasset_classes)
    f_votes = _fbx_domain_votes(pack.fbx_signals)
    l2_votes = u_votes + f_votes
    l2_top, l2_n = (l2_votes.most_common(1)[0] if l2_votes else (None, 0))

    # --- domain fusion (coding plan section 4 rules) -----------------------
    texture_only = comp["texture_only"]
    l1_meaningful = l1_scores[l1_top] >= 1.5

    if texture_only:
        result.domain = "Textures_HDRI"
        result.confidence = 0.80
        result.evidence["rule"] = "texture-only pack (no geometry files)"
    elif l2_n >= 2 and l1_meaningful:
        if l2_top == l1_top:
            result.domain, result.confidence = l1_top, 0.95
        else:
            winner = l2_top if l2_n >= 3 else l1_top
            result.domain, result.confidence = winner, 0.50
            result.validation_status = "needs_review"
            result.evidence["conflict"] = {"layer1": l1_top, "layer2": l2_top}
    elif l2_n >= 2:
        result.domain, result.confidence = l2_top, 0.75  # type: ignore[assignment]
    elif l1_meaningful:
        result.domain = l1_top
        result.confidence = 0.70 if l1_margin > 0.5 else 0.45
        if result.confidence < 0.5:
            result.validation_status = "needs_review"
    else:
        if set(exts) <= {"hdr", "exr"} and exts:
            result.domain, result.confidence = "Textures_HDRI", 0.6
        elif pack.files and all(f["file_type"] == "anim" for f in pack.files):
            result.domain, result.confidence = "Human Animations", 0.6
        else:
            result.domain, result.confidence = "Props", 0.3
            result.validation_status = "needs_review"

    # plan rule: Quixel / Megascans scans are photoreal environment/archviz
    if scan_source:
        result.style = "Photorealistic"
        if result.domain not in ("Environment", "Archviz"):
            result.domain = "Environment"
        result.confidence = max(result.confidence, 0.9)

    # --- style --------------------------------------------------------------
    if result.style == "Neutral/Unclassified":
        for style, style_tokens in STYLE_TOKENS.items():
            if tokens & set(style_tokens):
                result.style = style
                break
        else:
            if result.vendor in VENDOR_STYLE_HINT:
                result.style = VENDOR_STYLE_HINT[result.vendor]

    # --- Layer 3 (optional, local-only) -------------------------------------
    if vision and preview_path:
        vision_result = vision.classify_preview(preview_path)
        if vision_result:
            result.evidence["vision"] = vision_result
            if vision_result["domain"] == result.domain and (
                    vision_result["style"] == result.style
                    or result.style == "Neutral/Unclassified"):
                result.confidence = 0.95
                if result.style == "Neutral/Unclassified":
                    result.style = vision_result["style"]
            else:
                result.confidence = 0.50
                result.validation_status = "needs_review"

    result.sub_category = _sub_category(result.domain, tokens)

    # --- host-app projection ------------------------------------------------
    result.host_category = ("HDRI" if result.domain == "Textures_HDRI"
                             and result.sub_category == "HDRI"
                             else "Textures" if result.domain == "Textures_HDRI"
                             else "Assets")
    tag_pool = {result.sub_category.lower(), result.domain.lower()}
    if result.vendor:
        tag_pool.add(result.vendor.lower())
    tag_pool |= {t for t in tokens if len(t) > 3}
    result.host_tags = sorted(tag_pool)[:12]

    result.evidence.setdefault("layer1_scores", {k: round(v, 1) for k, v in l1_scores.items()})
    result.evidence["composition"] = {"geometry": comp["geometry"],
                                      "textures": comp["textures"]}
    if pack.uasset_classes:
        result.evidence["uasset_classes"] = dict(Counter(pack.uasset_classes).most_common(6))
    if pack.fbx_signals:
        result.evidence["fbx"] = pack.fbx_signals[:3]
    return result
