"""Image facts without decoding: pixel size from the file header, map role
from the file name. Stdlib only.

Why: folder names lie ("..._4K" stays on a folder whose maps were
re-exported at 2048 px), and a set that only lists its files leaves the
agent guessing which one is the colour map and which the normal map.
"""

from __future__ import annotations

import re
import struct

# extensions image_size() can measure
SIZE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tga", ".bmp", ".gif",
            ".hdr", ".exr", ".tif", ".tiff"}


def image_size(path) -> tuple[int, int] | None:
    """(width, height) from the header bytes of an image file, else None.

    Never raises: this runs over a whole library at server start, where a
    truncated, mislabelled or locked file must cost one set its
    measurement, not everyone the import."""
    try:
        with open(path, "rb") as fh:
            w, h = _size(fh, fh.read(32), str(path).lower())
        return (int(w), int(h)) if w > 0 and h > 0 else None
    except Exception:       # noqa: BLE001  (see docstring)
        return None


def _size(fh, head: bytes, name: str):
    """Dispatch on the magic bytes (a .jpg that is really a PNG still
    measures). Raises on anything unexpected; image_size() turns that
    into None."""
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    if head[:2] == b"\xff\xd8":
        return _jpeg(fh)
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", head[6:10])
    if head[:2] == b"BM":
        if struct.unpack("<I", head[14:18])[0] == 12:   # OS/2 core header
            return struct.unpack("<HH", head[18:22])
        w, h = struct.unpack("<ii", head[18:26])
        return w, abs(h)                    # negative height = top-down rows
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        kind, body = head[12:16], head[20:]
        if kind == b"VP8X":                 # 24-bit canvas size minus one
            return (int.from_bytes(body[4:7], "little") + 1,
                    int.from_bytes(body[7:10], "little") + 1)
        if kind == b"VP8L" and body[0] == 0x2F:     # 14 bits each, minus one
            bits = struct.unpack("<I", body[1:5])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if kind == b"VP8 " and body[3:6] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", body[6:10])
            return w & 0x3FFF, h & 0x3FFF   # top two bits are a scale hint
        raise ValueError("unknown WebP chunk")
    if head[:4] == b"\x76\x2f\x31\x01":
        return _exr(fh)
    if head[:2] == b"#?":
        return _hdr(fh)
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return _tiff(fh, head)
    if name.endswith(".tga"):
        # TGA has no magic number: only a plausible header counts
        w, h = struct.unpack("<HH", head[12:16])
        if (head[1] in (0, 1) and head[2] in (1, 2, 3, 9, 10, 11)
                and head[16] in (8, 15, 16, 24, 32)):
            return w, h
    raise ValueError("not a measurable image")


def _jpeg(fh):
    """Walk the segment chain to the frame header (SOF). Searching the
    bytes for a SOF marker finds the one of an embedded EXIF thumbnail
    first; a fixed-size head read misses a frame behind large ICC/EXIF
    blocks. Each step is one small read plus a seek."""
    fh.seek(2)
    for _ in range(512):
        marker = fh.read(2)
        while marker[1] == 0xFF:            # fill bytes before a marker
            marker = b"\xff" + fh.read(1)
        if marker[0] != 0xFF or marker[1] in (0xD9, 0xDA):
            break               # garbage, or image data without a frame
        if marker[1] == 0x01 or 0xD0 <= marker[1] <= 0xD7:
            continue                        # markers without a length
        length = struct.unpack(">H", fh.read(2))[0]
        if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", fh.read(5)[1:])
            return w, h
        fh.seek(length - 2, 1)
    raise ValueError("no JPEG frame header")


def _exr(fh):
    """OpenEXR header = attributes `name NUL type NUL size value`, closed
    by an empty name. The pixel area is the dataWindow box (inclusive
    xMin yMin xMax yMax). Names are at most 255 bytes each."""
    pos = 8
    for _ in range(256):
        fh.seek(pos)
        buf = fh.read(540)
        a = buf.index(b"\x00")
        if a == 0:
            break
        b = buf.index(b"\x00", a + 1)
        if buf[:a] == b"dataWindow":
            x0, y0, x1, y1 = struct.unpack_from("<4i", buf, b + 5)
            return x1 - x0 + 1, y1 - y0 + 1
        pos += b + 5 + struct.unpack_from("<I", buf, b + 1)[0]
    raise ValueError("no dataWindow")


_HDR_RES = re.compile(rb"\n([-+][XY]) +(\d+) +([-+][XY]) +(\d+)")


def _hdr(fh):
    """Radiance: text header, then the resolution line '-Y 1024 +X 2048'
    (the axis letters say which number is which)."""
    fh.seek(0)
    m = _HDR_RES.search(fh.read(4096))
    dims = {m.group(1)[1:]: int(m.group(2)), m.group(3)[1:]: int(m.group(4))}
    return dims[b"X"], dims[b"Y"]


def _tiff(fh, head: bytes):
    """Tags 256/257 of the first directory. Entries are sorted by tag, so
    both sit in the first few (the directory itself may be anywhere in
    the file -- one seek)."""
    e = "<" if head[:2] == b"II" else ">"
    fh.seek(struct.unpack(e + "I", head[4:8])[0])
    count = struct.unpack(e + "H", fh.read(2))[0]
    body = fh.read(12 * min(count, 16))
    dims = {}
    for off in range(0, len(body) - 11, 12):
        tag, typ = struct.unpack_from(e + "HH", body, off)
        if tag in (256, 257):               # SHORT (3) or LONG value
            dims[tag] = struct.unpack_from(
                e + ("H" if typ == 3 else "I"), body, off + 8)[0]
    return dims[256], dims[257]


# --------------------------------------------------------------------------- #
# map role from the file name
# --------------------------------------------------------------------------- #
#
# A superset of the vocabularies in scanner.CHANNEL_PATTERNS and
# material_roles (file-name spellings added), with two deliberate
# differences:
#   - glossiness is its OWN role. It is the inverse of roughness; a recipe
#     that plugs a gloss map into a roughness slot renders wrong.
#   - "base" is left out. material_roles matches it as a WHOLE parameter
#     name; as the last word of a file name it is a noun (column_base.jpg).
# "preview" is not a map: it marks the render/thumbnail image of a set.
_ROLE_TOKENS = {
    "albedo": "albedo alb albido basecolor basecolour basemap basec mainbc "
              "bc diffuse diff dif color colour col colortexture d",
    "normal": "normal normalmap normaldx normalgl norm nrm nor nml basen "
              "mainn n",
    "roughness": "roughness rough rgh r",
    "glossiness": "glossiness gloss smoothness",
    "metallic": "metallic metalic metalness metal met mtl m",
    "ao": "ao aomap occlusion ambientocclusion occ o",
    "height": "height displacement disp bump h",
    "opacity": "opacity opacitymask alpha mask op",
    "emissive": "emissive emmisive emmisivemap emission emissiontexture "
                "emit basee e",
    "specular": "specular spec reflection refl s",
    "packed": "packed orm arm rma mra orma aorm ormh armh aormh rmah mrao "
              "mraoh ord orh",
    "preview": "render preview thumb thumbnail sphere",
}
_ROLE = {tok: role for role, toks in _ROLE_TOKENS.items()
         for tok in toks.split()}

# trailing tokens that say nothing about the map: counters, resolutions,
# copy markers, "map"/"texture", and the normal-map convention after a
# separator ("rock_nor_gl_4k")
_NOISE = re.compile(r"\d+|\d+k|\d+x\d+|kopie|copy|map|tex|texture|"
                    r"gl|dx|ogl|opengl|directx", re.I)
_TOKEN = re.compile(r"[^\W_]+")      # unicode-aware: an umlaut splits nothing
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_RES_TAIL = re.compile(r"[ _-]+\d+k$")


def role_parts(filename) -> tuple[str, str, str]:
    """(role, token, prefix) for a texture file name or path: the role as
    map_role() reports it, the name token that says so, and the lowercased
    name in front of that token. Two files of one material share the
    prefix -- which is how a caller tells the metallic map "crate_m" (next
    to "crate_d") from the medium-sized photo "Gantry0042_M"."""
    folder, _, base = str(filename).replace("\\", "/").rpartition("/")
    stem = base.rpartition(".")[0] or base
    low = stem.lower()
    folder = folder.rpartition("/")[2].lower()
    if folder and low in (folder, _RES_TAIL.sub("", folder)):
        return "preview", "", low
    toks = list(_TOKEN.finditer(stem))
    while toks and _NOISE.fullmatch(toks[-1].group()):
        toks.pop()
    if not toks:
        return "", "", low
    word, last = toks[-1].group(), toks[-1].group().lower()
    # two separated words spelling one role: base_color, ambient_occlusion
    if len(toks) > 1 and len(last) > 1 and len(toks[-2].group()) > 1:
        pair = toks[-2].group().lower() + last
        if pair in _ROLE:
            return _ROLE[pair], pair, low[:toks[-2].start()]
    if last in _ROLE:               # the only place a one-letter token counts
        return _ROLE[last], last, low[:toks[-1].start()]
    # camelCase tail without a separator: brickBaseColor, RockAO
    parts = [p.lower() for p in _CAMEL.findall(word)]
    while parts and _NOISE.fullmatch(parts[-1]):
        parts.pop()
    for cand in ("".join(parts[-2:]), parts[-1] if parts else ""):
        if len(cand) > 1 and cand in _ROLE:
            return _ROLE[cand], cand, low[:low.rfind(cand)]
    # "<name>render.jpg" was always taken as the preview (thumb choice)
    return ("preview", "render", low[:-6]) if low.endswith("render") \
        else ("", "", low)


def map_role(filename) -> str:
    """Role of a texture file from the LAST meaningful token of its name:
    albedo, normal, roughness, glossiness, metallic, ao, height, opacity,
    emissive, specular, packed, preview -- or "" when the name does not say.

    Takes a file name or a path. With a path, the image named like its
    folder ("oak_floor_4K/oak_floor.jpg") is that set's preview.
    One-letter tokens ("_n", "_d") count only as the final token after a
    separator; whether a lone "_m" is a map at all needs the other files
    of the set (see role_parts)."""
    return role_parts(filename)[0]
