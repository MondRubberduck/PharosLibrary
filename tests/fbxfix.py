"""A hand-built binary FBX for the tests: any node tree, 32- or 64-bit
record headers. Nodes are (name, [props], [children]); props are made
with S(), I(), L(), D(), darr(), iarr(), farr().

    fbx_bytes([geometry(...), global_settings(up="Y", unit=1.0), ...])
"""

import struct


def S(text):
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return b"S" + struct.pack("<I", len(raw)) + raw


def I(v):               # noqa: E743
    return b"I" + struct.pack("<i", v)


def L(v):
    return b"L" + struct.pack("<q", v)


def D(v):
    return b"D" + struct.pack("<d", v)


def _arr(code, fmt, vals):
    body = struct.pack(f"<{len(vals)}{fmt}", *vals)
    return code + struct.pack("<III", len(vals), 0, len(body)) + body


def darr(vals):
    return _arr(b"d", "d", vals)


def farr(vals):
    return _arr(b"f", "f", vals)


def iarr(vals):
    return _arr(b"i", "i", vals)


def larr(vals):
    return _arr(b"l", "q", vals)


def fbx_bytes(nodes, version=7400) -> bytes:
    hfmt, hsize = ("<QQQ", 25) if version >= 7500 else ("<III", 13)

    def build(name, prop_list, children, start):
        name = name.encode("ascii") if isinstance(name, str) else name
        props = b"".join(prop_list)
        pos = start + hsize + len(name) + len(props)
        blob = b""
        for (cn, cp, cc) in children:
            b2, pos = build(cn, cp, cc, pos)
            blob += b2
        if children:
            blob += b"\x00" * hsize      # null record closes a node with children
            pos += hsize
        end = start + hsize + len(name) + len(props) + len(blob)
        return (struct.pack(hfmt, end, len(prop_list), len(props))
                + bytes([len(name)]) + name + props + blob), end

    out = bytearray(b"Kaydara FBX Binary  \x00\x1a\x00")
    out += struct.pack("<I", version)
    pos = 27
    for name, props, kids in nodes:
        name = name.encode("ascii") if isinstance(name, str) else name
        blob, pos = build(name, props, kids, pos)
        out += blob
    out += b"\x00" * hsize
    return bytes(out)


def P(name, typ, value, label=""):
    """A Properties70 'P' node: [name, type, label, flags, value]."""
    val = {int: I, float: D, str: S}[type(value)](value) if not isinstance(value, bool) else I(int(value))
    return ("P", [S(name), S(typ), S(label), S(""), val], [])


def global_settings(up="Y", unit=1.0, time_mode=6, custom_fps=-1.0):
    return ("GlobalSettings", [], [("Version", [I(1000)], []), ("Properties70", [], [
        P("UpAxis", "int", {"X": 0, "Y": 1, "Z": 2}[up]),
        P("UpAxisSign", "int", 1),
        P("UnitScaleFactor", "double", float(unit)),
        P("TimeMode", "enum", time_mode),
        P("CustomFrameRate", "double", float(custom_fps)),
    ])])


def geometry(verts, polys, gid=1000, uv_layers=1):
    """A Geometry with Vertices (flat x,y,z list) and PolygonVertexIndex
    (last index of each polygon negated: ~i)."""
    kids = [("Vertices", [darr(verts)], []), ("PolygonVertexIndex", [iarr(polys)], [])]
    for i in range(uv_layers):
        kids.append(("LayerElementUV", [I(i)], [("Name", [S(f"map{i}")], [])]))
    return ("Geometry", [L(gid), S("Geometry\x00\x01Geometry"), S("Mesh")], kids)


def model(mid, name, typ="Mesh"):
    return ("Model", [L(mid), S(f"{name}\x00\x01Model"), S(typ)], [])


def material(mid, name):
    return ("Material", [L(mid), S(f"{name}\x00\x01Material"), S("")], [])


def deformer(did, typ="Skin"):
    return ("Deformer", [L(did), S(f"Skin\x00\x01Deformer"), S(typ)], [])


def anim_stack(sid, name, stop_s, start_s=0.0):
    t = 46186158000
    return ("AnimationStack", [L(sid), S(f"{name}\x00\x01AnimStack"), S("")], [
        ("Properties70", [], [
            ("P", [S("LocalStart"), S("KTime"), S("Time"), S(""), L(int(start_s * t))], []),
            ("P", [S("LocalStop"), S("KTime"), S("Time"), S(""), L(int(stop_s * t))], []),
        ])])


def curve_node(cid, channel="T"):
    return ("AnimationCurveNode", [L(cid), S(f"{channel}\x00\x01AnimCurveNode"), S("")], [])


def curve(cid, times_s, values):
    t = 46186158000
    return ("AnimationCurve", [L(cid), S("\x00\x01AnimCurve"), S("")], [
        ("KeyTime", [larr([int(x * t) for x in times_s])], []),
        ("KeyValueFloat", [farr(values)], []),
    ])


def connections(links):
    """links: (child, parent) for OO, (child, parent, prop) for OP."""
    kids = []
    for ln in links:
        if len(ln) == 2:
            kids.append(("C", [S("OO"), L(ln[0]), L(ln[1])], []))
        else:
            kids.append(("C", [S("OP"), L(ln[0]), L(ln[1]), S(ln[2])], []))
    return ("Connections", [], kids)


def objects(*nodes):
    return ("Objects", [], list(nodes))


def box_mesh(size=100.0, base=0.0, uv_layers=1, gid=1000):
    """A unit cube of `size` file units standing on y=base (Y up),
    centred on x/z: 12 triangles."""
    s = size / 2
    v = []
    for x in (-s, s):
        for y in (base, base + size):
            for z in (-s, s):
                v += [x, y, z]
    # 6 faces as quads (indices into the 8 corners), last index negated
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 2, 6, 4), (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6)]
    polys = []
    for q in quads:
        polys += [q[0], q[1], q[2], ~q[3]]
    return geometry(v, polys, gid=gid, uv_layers=uv_layers)
