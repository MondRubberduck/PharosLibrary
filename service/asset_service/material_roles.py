"""Material-parameter role rules, shared by the Unreal relink step
(pipeline/conversion/relink_materials.py) and the registry importer
(meshes_import.py). Stdlib only: this module must never import `unreal`.
"""

import re

# --------------------------------------------------------------------------- #
# role vocabulary
# --------------------------------------------------------------------------- #
#
# The mapping is fixed by the task spec (TASKS_autocoder_2026-09-16.md TASK 1):
#   BaseColor/Albedo/Diffuse -> albedo · Normal/NormalMap -> normal
#   Roughness/Glossiness -> roughness · Metallic/Metalness -> metallic
#   Height/Displacement -> height · AmbientOcclusion -> ao
#   Emissive -> emissive · Opacity/OpacityMask/Alpha -> opacity
#   Specular -> specular · anything else -> other
#
# Matching is case-insensitive and ignores separators, so real-world UE parameter
# names on these packs resolve: "BaseColor", "Base_color", "00_BaseColor",
# "Albedo ", "02 - Diffuse Texture", "Base Color TX2", "Normal TX2".

ROLE_RULES = (
    ("albedo",    ("basecolor", "albedo", "diffuse")),
    ("normal",    ("normalmap", "normal")),
    ("roughness", ("roughness", "glossiness")),
    ("metallic",  ("metallic", "metalness")),
    ("height",    ("height", "displacement")),
    ("ao",        ("ambientocclusion",)),
    ("emissive",  ("emissive",)),
    ("opacity",   ("opacitymask", "opacity", "alpha")),
    ("specular",  ("specular",)),
)

# Exact aliases, checked on the normalised name BEFORE the substring rules.
# Exact only: a substring "base" would also catch layer params such as
# Rust_Base, Paint_Base, SecondBase and BlendBase, which must stay `other`.
ROLE_ALIASES = {
    "base": "albedo", "basemap": "albedo", "basecolour": "albedo",
    "basec": "albedo", "bc": "albedo", "albido": "albedo", "dif": "albedo",
    "colour": "albedo", "colortexture": "albedo", "mainbc": "albedo",
    "emmisive": "emissive", "emmisivemap": "emissive", "emission": "emissive",
    "emissiontexture": "emissive", "basee": "emissive",
    "nrm": "normal", "n": "normal", "norm": "normal", "basen": "normal",
    "mainn": "normal",
    "ao": "ao", "aomap": "ao",
    "metalic": "metallic",
}

# --------------------------------------------------------------------------- #
# packed channels  (role "packed")
# --------------------------------------------------------------------------- #
#
# One packed file holds several maps, one per channel.  A bare role cannot be
# acted on -- the importer has to know WHICH map is in which channel before it can
# wire a Separate Color node -- so every packed texture also carries `channels`.
#
# `channels` is derived from the parameter NAME, and only from the name.  The
# table below is the set of conventions these packs actually use:
#
#   token   r            g           b          a        where it is used
#   orm     ao           roughness   metallic   -        Unreal's standard pack
#   arm     ao           roughness   metallic   -        same order, ARM spelling
#   ormh    ao           roughness   metallic   height   Leartes (height in alpha)
#   armh    ao           roughness   metallic   height   same, ARM spelling
#   aorm    ao           roughness   metallic   -        "AO"+RM spelling, see note
#   aormh   ao           roughness   metallic   height   "AO"+RMH, see note
#   rma     roughness    metallic    ao         -        Unity/HDRP style
#   rmah    roughness    metallic    ao         height
#   mra     metallic     roughness   ao         -        MRA variant
#   mrao    metallic     roughness   ao         -
#   mraoh   metallic     roughness   ao         height
#   ord     ao           roughness   height     -        no metallic in the pack
#   orh     ao           roughness   height     -
#
# NOTE on the "AO" prefix: the same corpus spells it "AORM", "AoRM" and "AoRMH"
# (mixed case "Ao" = ambient occlusion followed by R/M/H), which is what fixes
# AORM as ao-roughness-metallic and NOT as a four-letter letter-for-letter decode.
#
# Matching is ANCHORED: a token counts only at a word start or at a lower->upper
# camel boundary, and only when the rest of the word is one of
# PACKED_WORD_SUFFIXES or digits.  Raw substring search is wrong here -- it turns
# 'Color Mask' into ...col-ORM, 'WindDeformNoise' into def-ORM, and
# 'Color Var Mask WS' into v-ARM (all three are in this corpus and all three were
# observed as false positives while building this table).
PACKED_ORDER_TOKENS = (
    ("aormh",  ("ao", "roughness", "metallic", "height")),
    ("ormh",   ("ao", "roughness", "metallic", "height")),
    ("armh",   ("ao", "roughness", "metallic", "height")),
    ("rmah",   ("roughness", "metallic", "ao", "height")),
    ("mraoh",  ("metallic", "roughness", "ao", "height")),
    ("mrao",   ("metallic", "roughness", "ao", None)),
    ("aorm",   ("ao", "roughness", "metallic", None)),
    ("orm",    ("ao", "roughness", "metallic", None)),
    ("arm",    ("ao", "roughness", "metallic", None)),
    ("rma",    ("roughness", "metallic", "ao", None)),
    ("mra",    ("metallic", "roughness", "ao", None)),
    ("ord",    ("ao", "roughness", "height", None)),
    ("orh",    ("ao", "roughness", "height", None)),
)
# ... and the names that say "this file is a pack/mask" without saying in which
# order.  These words are distinctive enough to match anywhere in the name.
PACKED_HINT_TOKENS = ("packed", "pack", "mask", "rgb")
PACKED_WORD_SUFFIXES = ("", "s", "t", "map", "maps", "tex", "texture", "textures",
                        "pack", "packed", "mask", "masks", "masked")
PACKED_ORDER_NOTE = "packed-channel order not determinable from the parameter name"
CHANNEL_NAMES = ("r", "g", "b", "a")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

ROLE_ORDER = [r for r, _ in ROLE_RULES] + ["packed", "other"]


def _packed_token_at(word, j, token):
    """word[j:] starts with `token` at an anchor and leaves nothing unexpected."""
    if j != 0 and not (word[j - 1].islower() and word[j].isupper()):
        return False
    rest = word[j + len(token):].lower()
    return rest in PACKED_WORD_SUFFIXES or rest.isdigit()


def _packed_anchored_match(name, table):
    """Longest token from `table` anchored in `name`; None when nothing matches."""
    best = None
    for word in _WORD_RE.findall(str(name)):
        low = word.lower()
        for token, chans in table:
            if token not in low:
                continue
            for j in range(len(low)):
                if low.startswith(token, j) and _packed_token_at(word, j, token):
                    if best is None or len(token) > len(best[0]):
                        best = (token, chans)
                    break
    return best


def packed_for(param):
    """Describe a packed/mask-style parameter name.

    Returns None when the name is not packed-style, else
    {"channels": {...}|None, "token": str|None, "hint": str|None, "note": str|None}.
    `channels` is None (plus the prescribed note) when the name does not encode an
    order -- the order is then genuinely unknown and is never invented.
    """
    if not param:
        return None
    name = str(param)
    hit = _packed_anchored_match(name, PACKED_ORDER_TOKENS)
    if hit:
        token, chans = hit
        return {"channels": {k: v for k, v in zip(CHANNEL_NAMES, chans) if v},
                "token": token, "hint": None, "note": None}
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    for hint in PACKED_HINT_TOKENS:
        if hint in flat:
            return {"channels": None, "token": None, "hint": hint,
                    "note": PACKED_ORDER_NOTE}
    return None


def classify_param(param):
    """(role, channels, packed_info) for one UE texture parameter name.

    The single-map rules are consulted FIRST and win, so this change can only ever
    move a parameter out of `other` -- no existing role assignment moves.
    """
    role = _role_for_single_map(param)
    if role != "other":
        return role, None, None
    info = packed_for(param)
    if info:
        return "packed", info["channels"], info
    return "other", None, None


def _role_for_single_map(param):
    """Role for a UE texture parameter name: exact aliases, then the spec rules."""
    if not param:
        return "other"
    flat = re.sub(r"[^a-z0-9]", "", str(param).lower())
    if flat in ROLE_ALIASES:
        return ROLE_ALIASES[flat]
    for role, tokens in ROLE_RULES:
        for t in tokens:
            if t in flat:
                return role
    return "other"
