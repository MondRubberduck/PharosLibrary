"""pharos survey -- what each folder of a library holds, and how we know.

    python pharos.py survey <root> [--json] [--depth N]

Walks a library once and returns a tree of folder nodes, each with a
KIND, the EVIDENCE for it and whether that evidence is something we
KNOW (a structural marker: an export manifest, a Content folder of
.uasset files, a Unity project, a kit layout, a purchase catalogue) or
GUESS (file shares and names). The setup page shows the tree with the
guesses marked for the user to confirm; the library map stores the
answer. Nothing is written here; nothing is indexed here.

Kinds: 3d-assets, unreal, unity, blender, textures, hdri, animation,
audio, purchases, archive, other, ignore.

Where the walk stops (a node without classified children):
  - a structural marker (the folder IS the product or project);
  - asset files directly in the folder (its subfolders are support:
    textures, LODs, sources);
  - a textures or audio folder (its sets are the importer's business:
    only subfolders of ANOTHER kind are listed below it);
  - the depth limit.
A folder of folders takes its children's common kind, or 'other' when
they differ (a store folder holding many products).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import imgmeta, kinds

KINDS = ("3d-assets", "unreal", "unity", "blender", "textures", "hdri",
         "animation", "audio", "purchases", "archive", "other", "ignore")

# the decision a kind needs before anything slow or engine-bound runs
DECISIONS = {
    ("unreal", "raw"): ["export", "leave"],
    ("unreal", "project"): ["export", "leave"],
    ("blender", "kit"): ["extract", "leave"],
    ("blender", "files"): ["extract", "leave"],
    ("unity", "project"): ["index-files", "leave"],
    ("unity", "package"): ["index-files", "leave"],
    ("animation", ""): ["previews", "no-previews"],
}

# subfolder names that belong to a product rather than being products
_SUPPORT = frozenset((
    "textures", "texture", "tex", "materials", "material", "mats", "maps",
    "source", "sources", "src", "fbx", "obj", "blend", "glb", "gltf", "usd",
    "meshes", "mesh", "models", "model", "assets", "lods", "lod", "docs",
    "documentation", "preview", "previews", "renders", "render",
    "thumbnails", "thumbs", "images", "img", "references", "reference",
    "exports", "export", "1k", "2k", "4k", "8k", "512", "1024", "2048",
    "4096", "8192", "low", "high", "hi", "lo", "highpoly", "lowpoly"))
_SUPPORT_TAIL = ("fbx", "obj", "textures", "texture", "materials", "maps",
                 "meshes", "models", "lods", "sources", "source")
# Unity engine folders: generated, never assets
_UNITY_IGNORE = frozenset(("library", "temp", "logs", "obj", "usersettings",
                           "build", "builds", ".vs", ".idea"))
# files that say nothing about a folder's kind
_NOISE = frozenset((".txt", ".md", ".pdf", ".url", ".html", ".htm", ".json",
                    ".xml", ".ini", ".db", ".db-shm", ".db-wal", ".log",
                    ".nfo", ".ds_store", ".bak", ".tmp", ".lnk", ".download",
                    ".blend1", ".meta", ".xlsx", ".csv", ".py", ".pyc",
                    ".svg", ""))
_RES_RE = re.compile(r"^\d+k?$")
_PURCHASE_KEYS = ("url", "price", "seller", "store")
_FBX_SAMPLE = 3
_FBX_READ = 2 * 1024 * 1024        # the type definitions sit near the top


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _is_support(name: str) -> bool:
    n = name.lower()
    k = _norm(name)
    if k in _SUPPORT or _RES_RE.match(k):
        return True
    return any(n.endswith(t) or n.endswith("_" + t) or n.endswith(" " + t)
               for t in _SUPPORT_TAIL)


def _purchase_csv(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            header = (f.readline() or "").lower()
    except OSError:
        return False
    return "name" in header and any(k in header for k in _PURCHASE_KEYS)


def _fbx_motion(paths: list) -> tuple:
    """(sampled, with_motion): an FBX carries motion when its type
    definitions (near the top of the file) list animation CURVES. A static
    Unreal export carries an empty AnimationStack, every exporter writes a
    'Takes' node, and a rigged body has bones: none of those is motion."""
    sampled = motion = 0
    for p in paths[:_FBX_SAMPLE]:
        try:
            with open(p, "rb") as fh:
                blob = fh.read(_FBX_READ)
        except OSError:
            continue
        sampled += 1
        if b"AnimationCurve" in blob:        # ...Curve and ...CurveNode types
            motion += 1
    return sampled, motion


def _mostly_motion(paths: list) -> tuple:
    sampled, motion = _fbx_motion(paths)
    return (sampled, motion, bool(sampled) and motion * 2 > sampled)


def _hdri_panorama(paths: list) -> bool:
    """A .hdr/.exr whose width is about twice its height is a panorama."""
    for p in paths[:3]:
        size = imgmeta.image_size(p)
        if size and size[1] and 1.8 <= size[0] / size[1] <= 2.2:
            return True
    return False


class _Dir:
    """One folder: direct entries, aggregated subtree counts."""
    __slots__ = ("path", "files", "dirs", "direct", "total", "fbx", "hdr",
                 "images")

    def __init__(self, path: Path):
        self.path = path
        self.files: list = []
        self.dirs: list = []
        self.direct: Counter = Counter()
        self.total: Counter = Counter()      # subtree, by extension
        self.fbx: list = []                  # a few FBX paths to sample
        self.hdr: list = []
        self.images: list = []               # a few image names (map roles?)
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            self.dirs.append(e.name)
                        elif e.is_file(follow_symlinks=False):
                            self.files.append(e.name)
                    except OSError:
                        continue
        except OSError:
            pass
        self.files.sort()
        self.dirs.sort()
        for f in self.files:
            ext = os.path.splitext(f)[1].lower()
            self.direct[ext] += 1
            if ext == ".fbx" and len(self.fbx) < _FBX_SAMPLE:
                self.fbx.append(path / f)
            if ext in kinds.HDRI and len(self.hdr) < 3:
                self.hdr.append(path / f)
            if ext in kinds.IMAGE and len(self.images) < 24:
                self.images.append(f)
        self.total.update(self.direct)


def _subtree(d: _Dir, skip_hidden: bool = True) -> None:
    """Add every file below d to d.total (one walk, no classification)."""
    for dp, dn, fn in os.walk(d.path):
        if dp == str(d.path):
            dn[:] = [x for x in dn if not (skip_hidden and x.startswith((".", "_")))]
            continue
        for f in fn:
            ext = os.path.splitext(f)[1].lower()
            d.total[ext] += 1
            if ext == ".fbx" and len(d.fbx) < _FBX_SAMPLE:
                d.fbx.append(Path(dp) / f)
            if ext in kinds.HDRI and len(d.hdr) < 3:
                d.hdr.append(Path(dp) / f)
            if ext in kinds.IMAGE and len(d.images) < 24:
                d.images.append(f)


def _pbr_set(d: "_Dir") -> bool:
    """The sampled image names are PBR maps (albedo, normal, roughness...):
    a texture library ships them, a model pack's previews do not."""
    roles = [imgmeta.map_role(n) for n in d.images]
    roles = [r for r in roles if r and r != "preview"]
    return len(d.images) >= 3 and len(roles) * 2 >= len(d.images)


def _count(c: Counter, exts) -> int:
    return sum(c.get(e, 0) for e in exts)


def _shares(c: Counter) -> dict:
    meaningful = sum(n for e, n in c.items() if e not in _NOISE)
    out = {"files": sum(c.values()), "meaningful": meaningful,
           "models": _count(c, kinds.MODEL), "blend": _count(c, kinds.BLEND),
           "clips": _count(c, kinds.CLIP - {".fbx"}), "fbx": c.get(".fbx", 0),
           "audio": _count(c, kinds.AUDIO), "images": _count(c, kinds.IMAGE),
           "hdri": _count(c, kinds.HDRI), "unreal": _count(c, kinds.UNREAL),
           "unity": _count(c, kinds.UNITY), "archives": _count(c, kinds.ARCHIVE)}
    out["usd"] = _count(c, {".usd", ".usda", ".usdc", ".usdz"})
    return out


def _top(c: Counter, n: int = 4) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n) if k)


class Node:
    def __init__(self, rel: str, name: str):
        self.folder = rel
        self.name = name
        self.kind = "other"
        self.confidence = "none"       # know | guess | none
        self.evidence = ""
        self.state = ""                # raw / converted / kit / project ...
        self.counts: Counter = Counter()
        self.children: list = []

    @property
    def decision(self):
        return DECISIONS.get((self.kind, self.state)) or \
            (DECISIONS.get((self.kind, "")) if self.kind == "animation" else None)

    def flat(self, depth: int = 0):
        yield depth, self
        for ch in self.children:
            yield from ch.flat(depth + 1)

    def to_dict(self) -> dict:
        d = {"folder": self.folder, "name": self.name, "kind": self.kind,
             "confidence": self.confidence, "evidence": self.evidence,
             "files": sum(self.counts.values()),
             "counts": dict(self.counts.most_common(8))}
        if self.state:
            d["state"] = self.state
        if self.decision:
            d["decision"] = self.decision
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _know(node: Node, kind: str, state: str, evidence: str) -> Node:
    node.kind, node.state, node.evidence, node.confidence = kind, state, evidence, "know"
    return node


def _guess(node: Node, kind: str, evidence: str, state: str = "") -> Node:
    node.kind, node.state, node.evidence, node.confidence = kind, state, evidence, "guess"
    return node


def _marker(node: Node, d: _Dir, root: Path) -> bool:
    """Structural signals. True when one decided the node (and the walk
    stops here, apart from counting)."""
    dirs_l = {x.lower(): x for x in d.dirs}
    files_l = [f.lower() for f in d.files]
    exports = d.path / "Exports"
    if (exports / "manifest.json").is_file():
        _subtree(d)
        _know(node, "unreal", "converted",
              "Exports/manifest.json: converted Unreal pack (FBX + textures + recipes)")
        return True
    if (exports / "kit_manifest.json").is_file():
        _subtree(d)
        _know(node, "blender", "kit-exported",
              "Exports/kit_manifest.json: kit exported to FBX")
        return True
    if any(f.endswith(".uproject") for f in files_l):
        _subtree(d)
        _know(node, "unreal", "project",
              f"{next(f for f in d.files if f.lower().endswith('.uproject'))}: "
              f"Unreal project ({_count(d.total, kinds.UNREAL):,} .uasset/.umap)")
        return True
    if "assets" in dirs_l and "projectsettings" in dirs_l:
        _subtree(d)
        _know(node, "unity", "project",
              f"Assets/ + ProjectSettings/: Unity project "
              f"({d.total.get('.prefab', 0)} prefabs, {d.total.get('.unity', 0)} scenes)")
        for name in d.dirs:
            if name.lower() in _UNITY_IGNORE:
                ch = Node(_rel(root, d.path / name), name)
                _know(ch, "ignore", "", "Unity engine folder (generated)")
                node.children.append(ch)
        return True
    kit = [x for x in d.dirs if x.lower().endswith(".blender.native")]
    if kit:
        _subtree(d)
        _know(node, "blender", "kit", f"{kit[0]}/: KitBash3D kit layout")
        return True
    if "content" in dirs_l:
        content = _Dir(d.path / dirs_l["content"])
        _subtree(content)
        n_ue = _count(content.total, kinds.UNREAL)
        if n_ue:
            _subtree(d)
            _know(node, "unreal", "raw",
                  f"Content/ with {n_ue:,} .uasset/.umap files: raw Unreal pack "
                  f"(needs the editor to extract FBX)")
            return True
    csvs = [f for f in d.files if f.lower().endswith(".csv")
            and _purchase_csv(d.path / f)]
    if csvs:
        _subtree(d)
        _know(node, "purchases", "",
              f"{csvs[0]}: purchase catalogue (name + URL/price/store columns); "
              f"subfolders hold store images")
        return True
    if d.direct.get(".unitypackage"):
        _subtree(d)
        _know(node, "unity", "package",
              f"{d.direct['.unitypackage']} .unitypackage file(s)")
        return True
    return False


def _rel(root: Path, p: Path) -> str:
    try:
        r = p.relative_to(root).as_posix()
    except ValueError:
        r = p.as_posix()
    return r or "."


def _classify_leaf(node: Node, d: _Dir, direct: bool) -> None:
    """Kind from file shares. `direct`: asset files sit in this folder."""
    s = _shares(d.total)
    sd = _shares(d.direct)
    m = s["meaningful"] or 1
    strong_direct = sd["models"] - sd["usd"]      # .usd beside images: previews
    if (direct and sd["blend"] and not strong_direct
            and sd["images"] >= 3 * sd["blend"] and _pbr_set(d)):
        _guess(node, "textures", f"PBR map names ({_top(d.direct, 3)}); the "
                                 f".blend beside them is a material preview")
        return
    if direct and (strong_direct or sd["blend"]):
        if sd["blend"] and sd["blend"] >= strong_direct:
            _guess(node, "blender", f"{sd['blend']} .blend file(s) in the folder", "files")
            return
        if sd["fbx"] and not sd["clips"]:
            sampled, motion, most = _mostly_motion(d.fbx)
            if most:
                _guess(node, "animation",
                       f"{s['fbx']} .fbx, {motion} of {sampled} sampled carry motion")
                return
        if sd["clips"]:
            _guess(node, "animation", f"{s['clips']} clip files (.bvh/.anim)")
            return
        _guess(node, "3d-assets", f"model files in the folder: {_top(d.direct)}")
        return
    if direct and sd["clips"]:
        _guess(node, "animation", f"{s['clips']} clip files (.bvh/.anim)")
        return
    if direct and sd["audio"] and sd["audio"] >= sd["images"]:
        _guess(node, "audio", f"{s['audio']:,} sound files ({_top(d.total, 3)})")
        return
    if direct and sd["images"] and not strong_direct:
        strong_below = s["models"] - s["usd"]
        below = strong_below + s["blend"]
        # a .blend per PBR set is a material preview, not a product
        pbr_library = (_pbr_set(d) and not strong_below
                       and s["blend"] * 2 <= s["images"])
        if below and below >= sd["images"] and not pbr_library:
            # the images are previews; the product sits in its subfolders
            model_exts = Counter({k: v for k, v in d.total.items()
                                  if k in kinds.MODEL or k in kinds.BLEND})
            if s["blend"] >= (s["models"] - s["usd"]):
                _guess(node, "blender", f"{s['blend']} .blend files in the "
                       f"subfolders; the folder's images are previews", "files")
            else:
                _guess(node, "3d-assets", f"model files in the subfolders "
                       f"({_top(model_exts, 3)}); the folder's images are previews")
            return
        if s["hdri"] and s["hdri"] >= 0.5 * s["images"] and _hdri_panorama(d.hdr):
            _guess(node, "hdri", f"{s['hdri']} panoramic .hdr/.exr files")
            return
        _guess(node, "textures", f"images in the folder: {_top(d.direct, 3)}")
        return
    # no direct asset files: the subtree decides. Motion first (a clip
    # pack is .fbx too), then a product with 3D content, then shares.
    strong = s["models"] - s["usd"]
    if s["clips"] and s["clips"] / m >= 0.3:
        _guess(node, "animation", f"{s['clips']} clip files below")
        return
    if s["fbx"] and s["fbx"] / m >= 0.3:
        sampled, motion, most = _mostly_motion(d.fbx)
        if most:
            _guess(node, "animation",
                   f"{s['fbx']} .fbx below, {motion} of {sampled} sampled carry motion")
            return
    assets = strong + s["blend"] + s["unreal"]
    if assets and (assets >= 10 or assets / m >= 0.05) \
            and not (s["blend"] and not strong and not s["unreal"] and _pbr_set(d)):
        if s["unreal"] * 2 > assets:
            _guess(node, "unreal", f"{s['unreal']:,} .uasset files below without a "
                   f"Content/ folder ({_top(d.total, 3)})", "raw")
        elif s["blend"] >= strong:
            _guess(node, "blender", f"{s['blend']} .blend files below ({_top(d.total, 3)})", "files")
        else:
            _guess(node, "3d-assets", f"model files below: {_top(d.total, 4)}")
        return
    if s["images"] / m >= 0.6:
        if s["hdri"] and s["hdri"] >= 0.5 * s["images"] and _hdri_panorama(d.hdr):
            _guess(node, "hdri", f"{s['hdri']} panoramic .hdr/.exr files")
            return
        _guess(node, "textures", f"images are {100 * s['images'] // m}% of "
                                 f"{m:,} files ({_top(d.total, 3)})")
        return
    if s["audio"] / m >= 0.6:
        _guess(node, "audio", f"sounds are {100 * s['audio'] // m}% of {m:,} files")
        return
    if s["archives"] and s["archives"] / m >= 0.5:
        _guess(node, "archive", f"{s['archives']} archive(s), not unpacked: {_top(d.total, 3)}")
        return
    if strong or s["blend"]:
        _guess(node, "3d-assets", f"a few model files below: {_top(d.total, 4)}")
        return
    node.kind, node.confidence = "other", "none"
    node.evidence = f"no asset files recognised ({_top(d.total, 4) or 'empty'})"


def _scan(root: Path, path: Path, depth: int, max_depth: int) -> Node:
    node = Node(_rel(root, path), path.name or str(path))
    d = _Dir(path)
    # the root is never one thing: a marker there (a purchase CSV, a stray
    # Content/ folder) would hide every other folder of the library
    if depth > 0 and _marker(node, d, root):
        node.counts = d.total
        return node
    visible = [x for x in d.dirs if not x.startswith((".", "_"))]
    sd = _shares(d.direct)
    direct_assets = bool(sd["models"] or sd["blend"] or sd["clips"]
                         or sd["audio"] or sd["images"])
    support_only = bool(visible) and all(_is_support(x) for x in visible)
    if (direct_assets and visible and not support_only and depth < max_depth
            and not (sd["models"] or sd["blend"] or sd["clips"] or sd["audio"])):
        # only images lie here, beside folders that are not support folders:
        # when engine content, clips or sounds below outnumber the images,
        # the images are previews of a folder of products, not a texture set
        _subtree(d)
        below = _count(d.total, kinds.UNREAL | kinds.UNITY | kinds.AUDIO
                       | (kinds.CLIP - kinds.MODEL))
        if below >= sd["images"]:
            direct_assets = False
    # the root is never a leaf: a stray file beside the folders is reported
    # (root_files), it does not turn the whole library into one node
    leaf = depth > 0 and (depth >= max_depth or not visible or direct_assets
                          or support_only)
    if leaf:
        _subtree(d)
        node.counts = d.total
        _classify_leaf(node, d, direct_assets)
        if node.kind in ("textures", "audio", "hdri") and visible \
                and depth < max_depth:
            # sets are the importer's business; only a subfolder of another
            # kind (HDRIs inside a texture library, an add-on zip) is listed
            for name in visible:
                ch = _scan(root, path / name, depth + 1, max_depth)
                if ch.kind not in (node.kind, "other") and ch.counts:
                    node.children.append(ch)
        return node
    for name in visible:
        ch = _scan(root, path / name, depth + 1, max_depth)
        node.children.append(ch)
        node.counts.update(ch.counts)
    node.counts.update(d.direct)
    kinds_seen = Counter(c.kind for c in node.children if c.kind != "ignore")
    real = Counter({k: v for k, v in kinds_seen.items()
                    if k not in ("other", "archive")})
    total = sum(kinds_seen.values()) or 1
    threed = Counter({k: v for k, v in real.items()
                      if k in ("3d-assets", "blender", "unreal", "unity")})
    if threed and set(real) - set(threed) <= {"textures", "hdri"}:
        # a product: model folder(s) plus per-item texture folders
        kind, n = threed.most_common(1)[0]
        node.kind, node.confidence = kind, "guess"
        node.evidence = (f"{n} {kind} subfolder(s) with "
                         f"{sum(real.values()) - sum(threed.values())} texture folder(s)")
    elif real and real.most_common(1)[0][1] >= 0.6 * total:
        kind, n = real.most_common(1)[0]
        node.kind = kind
        node.confidence = ("know" if all(c.confidence == "know" for c in node.children
                                         if c.kind == kind) else "guess")
        node.evidence = f"{n} of {len(node.children)} subfolders are {kind}"
        if kind in ("textures", "audio", "hdri") and depth > 0:
            # categories and sets are the importer's business: only a
            # subfolder of another kind stays listed (the root keeps its
            # folders: they are the sections)
            node.children = [c for c in node.children
                             if c.kind not in (kind, "other") and c.counts]
    elif real:
        node.kind, node.confidence = "other", "guess"
        node.evidence = "mixed: " + ", ".join(
            f"{v} {k}" for k, v in kinds_seen.most_common())
    elif kinds_seen.get("archive"):
        node.kind, node.confidence = "archive", "guess"
        node.evidence = (f"{kinds_seen['archive']} subfolder(s) hold archives, "
                         f"not unpacked")
    else:
        node.kind, node.confidence = "other", "none"
        node.evidence = f"no asset files recognised ({_top(node.counts, 4) or 'empty'})"
    return node


def survey(root, max_depth: int = 4) -> dict:
    """The classified folder tree of one library root."""
    root = Path(root).resolve()
    t0 = time.perf_counter()
    if not root.is_dir():
        return {"root": str(root), "error": "root does not exist"}
    top = _scan(root, root, 0, max_depth)
    top.folder, top.name = ".", root.name
    d = _Dir(root)
    stray = {e: n for e, n in d.direct.items() if e not in _NOISE}
    out = {"root": root.as_posix(), "seconds": round(time.perf_counter() - t0, 1),
           "files": sum(top.counts.values()), "tree": top.to_dict(),
           "nodes": [{"depth": depth, **n.to_dict()} for depth, n in top.flat()
                     if n is not top or True]}
    for n in out["nodes"]:
        n.pop("children", None)
    if stray:
        out["root_files"] = stray     # invisible to every importer
    out["decisions"] = [{"folder": n.folder, "kind": n.kind, "state": n.state,
                         "options": n.decision}
                        for _, n in top.flat() if n.decision]
    return out


def print_tree(result: dict, out=None) -> None:
    out = out or sys.stdout
    if "error" in result:
        print(f"ERROR: {result['error']}: {result['root']}", file=out)
        return
    print(f"survey of {result['root']}: {result['files']:,} files in "
          f"{result['seconds']} s", file=out)

    def walk(n: dict, depth: int) -> None:
        mark = {"know": "=", "guess": "~", "none": "?"}[n["confidence"]]
        dec = f"  [{'|'.join(n['decision'])}]" if n.get("decision") else ""
        state = f" ({n['state']})" if n.get("state") else ""
        print(f"{'  ' * depth}{mark} {n['name']:<40} {n['kind']}{state}"
              f"  {n['files']:>7,} files  {n['evidence']}{dec}", file=out)
        for ch in n.get("children") or []:
            walk(ch, depth + 1)
    walk(result["tree"], 0)
    print("  (= known from structure, ~ guessed from file shares and names, "
          "? nothing recognised; [options] = decision needed)", file=out)
    if result.get("root_files"):
        print(f"  !! {sum(result['root_files'].values())} asset file(s) directly "
              f"in the root are invisible to every importer: "
              f"{_top(Counter(result['root_files']))}", file=out)


def run_survey(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    ap = argparse.ArgumentParser(prog="pharos survey",
                                 description="classify a library's folders by content")
    ap.add_argument("root", help="the library folder")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--depth", type=int, default=4, help="folder depth to classify (default 4)")
    args = ap.parse_args(argv)
    result = survey(args.root, max_depth=args.depth)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print_tree(result)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(run_survey(sys.argv[1:]))
