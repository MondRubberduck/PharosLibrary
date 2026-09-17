"""
relink_materials.py -- rebuild manifest.json material->texture WIRING from the
engine's own material parameter values.

Runs INSIDE UnrealEditor (via -ExecutePythonScript), exactly like export_pack.py,
and reuses that script's job/env plumbing and logging conventions.  Job JSON is
passed in the environment variable AMCONV_JOB.

WHAT THIS DOES
  For every mesh already listed in <pack>/Exports/manifest.json it loads the mesh
  asset, walks mesh -> section -> material slot, and reads the texture parameter
  values the ENGINE holds:
    * MaterialInstanceConstant : texture_parameter_values (the instance overrides)
    * Material                 : texture parameter names + their default values
  Each resolved parameter becomes one manifest entry
      { "param": "<UE param name>", "role": "<role>",
        "file": "<path relative to Exports>", "asset_path": "/Game/..." }
  and every slot is scored into a pack-level `wiring` block.

  A parameter whose name denotes a PACKED texture (ORM/RMA/ARM/ORMH/... or a
  pack/mask/RGB texture) gets role "packed" instead of "other", plus
      "channels": { "r": "ao", "g": "roughness", "b": "metallic" [, "a": "height"] }
  derived from the parameter name -- a bare "packed" role would not be actionable,
  because the importer cannot wire a Separate Color node without knowing which map
  is in which channel.  When the name does not encode an order, `channels` is null
  and the entry carries the note (or "channels_note" when `note` is already used
  for a missing file) "packed-channel order not determinable from the parameter
  name".  The single-map rules are consulted first and win, so this only ever moves
  a parameter OUT OF `other`.
  Each mesh also gets world-space `bbox_min`/`bbox_max` in metres (item 4); the v1
  `bbox_m`, `bbox_min_m` and `bbox_max_m` fields are left exactly as they were.

WHAT THIS DELIBERATELY DOES NOT DO
  * no geometry is re-exported: not one .fbx and not one texture file is written
  * no source .uasset is ever opened for writing (assets are loaded read-only out
    of the disposable sandbox copy the driver synced; the engine's own save path
    is never reached)
  * manifest.json is the ONLY file rewritten in place, and a pristine copy of the
    previous version is kept beside it first (default: manifest.v1.bak)

WHY IT CANNOT DAMAGE THE PIPELINE
  The v1 manifest is written by export_pack.py and is the pipeline's inventory of
  record.  This script only ever *adds* to it: v1 keys are preserved verbatim, the
  `schema` is bumped so consumers can tell the difference, and slots the engine
  cannot resolve keep their v1 texture entries untouched.  Rolling back is
  `copy manifest.v1.bak manifest.json`.
  The backup deliberately uses the extension `.bak` and NOT `.json`: it sits inside
  the pack's Exports folder, and a crawler globbing `*.json` or `manifest*.json` there
  must not see two manifests per pack (the stale v1 one would be just as ingestible as the
  live one).  `manifest.v1.bak` matches none of those globs.

Job description (AMCONV_JOB):

{
  "pack_name":     "MyPack",
  "pack_dir":      "C:/path/to/assets/MyPack",
  "manifest":      "<pack_dir>/Exports/manifest.json",   # REQUIRED: read + rewritten
  "report":        "C:/path/to/logs/relink_<slug>.json",
  "wiring_report": "C:/path/to/logs/wiring_<slug>.json",  # full detail
  "game_roots":    ["/Game/MyPack"],          # from pack_discovery.py; cross-check only
  "exclude_dirs":  ["EpicContent"],           # used to explain a null `file` in a note
  "apply":         true,     # false = dry run: resolve everything, write nothing
  "manifest_out":  "C:/path/to/logs/preview_<slug>.json",
                             # dry run only: write the fully patched manifest HERE
                             # (never at the live path) so it can be verified first
  "backup_name":   "manifest.v1.bak",       # beside manifest.json; never overwritten
  "limit_meshes":  0         # 0 = unlimited (smoke tests)
}

Exit behaviour mirrors export_pack.py: a fatal error is logged loudly and the
editor still quits.  The manifest on disk - never the process exit code - is the
authority on whether the run did its job (UE 5.7 can return non-zero from a CEF
teardown crash long after the work is done).
"""

import datetime
import json
import os
import re
import sys
import time
import traceback

import unreal

# --------------------------------------------------------------------------- #
# job loading  (identical contract to export_pack.py)
# --------------------------------------------------------------------------- #

def _load_job():
    raw = os.environ.get("AMCONV_JOB", "")
    if not raw:
        # fall back to "script.py -- /path/to/job.json" style invocation
        argv = [a for a in sys.argv if a.lower().endswith(".json")]
        if argv:
            with open(argv[-1], "r", encoding="utf-8") as fh:
                return json.load(fh)
        raise RuntimeError("no job given: set AMCONV_JOB to a JSON file path")
    return json.loads(raw)


JOB = _load_job()
LOG = []

DEFAULT_EXCLUDE = ["EpicContent"]
EXCLUDE = [str(s).lower() for s in (JOB.get("exclude_dirs") or DEFAULT_EXCLUDE)]

SCHEMA_V1 = "pharos.pack.export/v1"
SCHEMA_V1_LEGACY = "kiosk.pack.export/v1"
SCHEMA_V2 = "pharos.pack.export/v2"
SCHEMA_V2_LEGACY = "kiosk.pack.export/v2"
METHOD = "ue-param"


def log(msg):
    line = "[relink] %s" % msg
    LOG.append(line)
    unreal.log(line)


def log_warn(msg):
    line = "[relink][WARN] %s" % msg
    LOG.append(line)
    unreal.log_warning(line)


def fwd(p):
    return p.replace("\\", "/") if p else p


# --------------------------------------------------------------------------- #
# filesystem / naming helpers
# --------------------------------------------------------------------------- #

def tex_obj_path(tex):
    """'/Game/A/B.T' -> '/Game/A/B' ('' when None).  Same rule as export_pack.py."""
    if tex is None:
        return None
    try:
        pn = tex.get_path_name()
    except Exception:
        return None
    if not pn:
        return None
    return pn.split(".")[0] if "." in pn.rsplit("/", 1)[-1] else pn


def obj_class_name(obj):
    try:
        return str(obj.get_class().get_name())
    except Exception:
        return "?"


def obj_name(obj):
    try:
        return str(obj.get_name())
    except Exception:
        return "?"


def is_excluded(package_path):
    """True when a path segment equals an excluded folder name (case-insensitive)."""
    if not EXCLUDE or not package_path:
        return False
    segs = [s.lower() for s in package_path.split("/") if s]
    for e in EXCLUDE:
        if e in segs:
            return True
    return False


# --------------------------------------------------------------------------- #
# world-space bounds per mesh (bbox_min / bbox_max, in metres)
# --------------------------------------------------------------------------- #
#
# bbox_m says how big an assembly is but not where its pivot sits, which is what
# auto-placement needs before it can drop an object on a ground plane.  The v1
# exporter already writes the engine's own AABB as bbox_min_m/bbox_max_m (cm->m),
# so the relink carries those over verbatim and only falls back to the engine when
# a mesh has no usable pair -- no value is ever derived from bbox_m and a zero
# point, and a mesh with no bounds gets explicit nulls rather than a guess.

def _num_or_none(v):
    try:
        return float(v)
    except Exception:
        return None


def _field(obj, *names):
    if obj is None:
        return None
    for n in names:
        try:
            v = getattr(obj, n)
            if v is not None:
                return v
        except Exception:
            pass
        try:
            v = obj.get_editor_property(n)
            if v is not None:
                return v
        except Exception:
            pass
    return None


def _vec3(v):
    if v is None:
        return None
    x, y, z = _field(v, "x", "X"), _field(v, "y", "Y"), _field(v, "z", "Z")
    if x is None or y is None or z is None:
        return None
    vals = [_num_or_none(x), _num_or_none(y), _num_or_none(z)]
    return None if any(p is None for p in vals) else vals


def _is_vec3(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def bbox_from_manifest(mesh):
    """(min, max) in metres straight out of the v1 record, or (None, None)."""
    lo, hi = mesh.get("bbox_min_m"), mesh.get("bbox_max_m")
    if _is_vec3(lo) and _is_vec3(hi):
        return [float(v) for v in lo], [float(v) for v in hi]
    return None, None


def bbox_from_engine(asset, kind):
    """(min, max) in metres from the engine's own AABB, or (None, None).

    Same two APIs export_pack.py uses, so the numbers agree by construction:
    StaticMesh.get_bounding_box() -> FBox(min,max) in cm, and
    SkeletalMesh.get_bounds() -> BoxSphereBounds(origin, box_extent) in cm.
    """
    if asset is None:
        return None, None
    if (kind or "StaticMesh") != "StaticMesh":
        try:
            box = asset.get_bounds()
        except Exception:
            return None, None
        origin = _vec3(_field(box, "origin"))
        extent = _vec3(_field(box, "box_extent", "extent"))
        if origin is None or extent is None:
            return None, None
        return ([round((origin[i] - extent[i]) * 0.01, 4) for i in range(3)],
                [round((origin[i] + extent[i]) * 0.01, 4) for i in range(3)])
    try:
        fn = getattr(asset, "get_bounding_box", None)
        box = fn() if fn is not None else None
    except Exception:
        box = None
    lo, hi = _vec3(_field(box, "min")), _vec3(_field(box, "max"))
    if lo is None or hi is None:
        return None, None
    return ([round(v * 0.01, 4) for v in lo], [round(v * 0.01, 4) for v in hi])


def write_mesh_bounds(mesh, asset=None, kind=None):
    """Add bbox_min/bbox_max (metres, world space) to one manifest mesh record.

    Returns "carried", "engine" or None (nothing available).
    """
    lo, hi = bbox_from_manifest(mesh)
    how = "carried"
    if lo is None:
        lo, hi = bbox_from_engine(asset, kind or mesh.get("kind"))
        how = "engine"
    if lo is None:
        mesh["bbox_min"] = None
        mesh["bbox_max"] = None
        return None
    mesh["bbox_min"] = lo
    mesh["bbox_max"] = hi
    # cheap invariant: max-min must reproduce the existing bbox_m (rounded to mm)
    bb = mesh.get("bbox_m")
    if _is_vec3(bb):
        d = [round(hi[i] - lo[i], 4) for i in range(3)]
        if any(abs(d[i] - bb[i]) > 0.001 for i in range(3)):
            return how + ":mismatch"
    return how


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
# "Albedo ", "02 - Diffuse Texture", "Base Color TX2", "Normal TX2", "NRM"? no --
# NRM is NOT in the spec list and therefore stays "other" (see the pack report:
# the observed `other` names are reported with counts so the list can be extended
# deliberately rather than by guesswork).

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
    """Role for a UE texture parameter name, per the single-map spec vocabulary."""
    if not param:
        return "other"
    flat = re.sub(r"[^a-z0-9]", "", str(param).lower())
    for role, tokens in ROLE_RULES:
        for t in tokens:
            if t in flat:
                return role
    return "other"


# --------------------------------------------------------------------------- #
# engine-side reading
# --------------------------------------------------------------------------- #
#
# Verified against UE 5.7.4 by an in-editor probe (see the run report):
#   * MaterialInstanceConstant.texture_parameter_values -> FTextureParameterValue[]
#     each carrying parameter_info.name and parameter_value (a Texture object).
#     This is the EXACT override set and is the primary source.
#   * MaterialEditingLibrary.get_texture_parameter_names(material) +
#     .get_material_default_texture_parameter_value(material, name) work on a
#     *Material* only -- passing a MaterialInstanceConstant raises
#     "Failed to convert parameter 'material'", which is also why the v1 exporter
#     only saw `material_used_textures` (unnamed dependencies) for many slots.
#   * get_used_textures() likewise accepts a Material but not an instance.

OVERRIDE_ARRAYS = (
    ("texture_parameter_values", "instance_override"),
    ("runtime_virtual_texture_parameter_values", "instance_override_vt"),
    ("sparse_volume_texture_parameter_values", "instance_override_vt"),
)


def read_override_params(inst, slot_errors):
    """[{'param','asset_path','source'}] from one MaterialInstance's override arrays."""
    out = []
    for prop, source in OVERRIDE_ARRAYS:
        try:
            values = inst.get_editor_property(prop)
        except Exception as exc:
            if prop == "texture_parameter_values":
                slot_errors.append("%s.%s unreadable: %s: %s"
                                   % (obj_name(inst), prop, type(exc).__name__, exc))
            continue
        for pv in values or []:
            pname = None
            try:
                pname = str(pv.get_editor_property("parameter_info").get_editor_property("name"))
            except Exception as exc:
                slot_errors.append("%s: parameter_info.name unreadable: %s: %s"
                                   % (obj_name(inst), type(exc).__name__, exc))
            tex = None
            try:
                tex = pv.get_editor_property("parameter_value")
            except Exception as exc:
                slot_errors.append("%s/%r: parameter_value unreadable: %s: %s"
                                   % (obj_name(inst), pname, type(exc).__name__, exc))
            out.append({"param": pname, "asset_path": tex_obj_path(tex), "source": source})
    return out


def read_material_defaults(mat, slot_errors, with_unnamed):
    """[{'param','asset_path','source'}] from a base Material.

    `with_unnamed` adds the material's unnamed texture dependencies
    (get_used_textures) as param=None entries -- the same fallback export_pack.py
    used, kept so a slot that resolves to nothing still has honest evidence.
    """
    out = []
    mel = unreal.MaterialEditingLibrary
    names = []
    try:
        names = [str(n) for n in mel.get_texture_parameter_names(mat)]
    except Exception as exc:
        slot_errors.append("get_texture_parameter_names(%s) failed: %s: %s"
                           % (obj_name(mat), type(exc).__name__, exc))
    for n in names:
        tex = None
        try:
            tex = mel.get_material_default_texture_parameter_value(mat, n)
        except Exception as exc:
            slot_errors.append("default texture param %r on %s failed: %s: %s"
                               % (n, obj_name(mat), type(exc).__name__, exc))
        out.append({"param": n, "asset_path": tex_obj_path(tex), "source": "material_default"})
    if with_unnamed:
        try:
            for t in mel.get_used_textures(mat):
                out.append({"param": None, "asset_path": tex_obj_path(t),
                            "source": "material_used_textures"})
        except Exception as exc:
            slot_errors.append("get_used_textures(%s) failed: %s: %s"
                               % (obj_name(mat), type(exc).__name__, exc))
    return out


def walk_material(mi, max_depth=12):
    """Walk MaterialInstance -> ... -> Material.

    Returns (chain_names, overrides, defaults, base_name, errors).  Overrides come
    from every instance in the chain (nearest first, so the nearest override wins);
    defaults come from the root Material only, and only for parameters no instance
    in the chain overrode.
    """
    chain, overrides, errors = [], [], []
    base = None
    cur, depth, seen = mi, 0, set()
    while cur is not None and depth < max_depth:
        cname = obj_name(cur)
        ccls = obj_class_name(cur)
        chain.append(cname)
        if cname in seen:
            errors.append("parent chain revisits %s -- stopped" % cname)
            break
        seen.add(cname)
        depth += 1

        if ccls in ("MaterialInstanceConstant", "MaterialInstance", "MaterialInstanceDynamic"):
            overrides.extend(read_override_params(cur, errors))
            nxt = None
            try:
                nxt = cur.get_editor_property("parent")
            except Exception as exc:
                errors.append("%s.parent unreadable: %s: %s" % (cname, type(exc).__name__, exc))
            if nxt is None and ccls == "MaterialInstanceConstant":
                try:
                    nxt = cur.get_base_material()
                    if nxt is not None:
                        errors.append("%s.parent was null; fell back to get_base_material()" % cname)
                except Exception:
                    pass
            cur = nxt
            continue

        if ccls == "Material":
            base = cname
            return chain, overrides, read_material_defaults(cur, errors, True), base, errors

        # anything else (MaterialFunctionInterface, null, ...) -- stop, do not guess
        errors.append("parent chain hit %s (%s) -- stopped" % (cname, ccls))
        break
    return chain, overrides, [], base, errors


# --------------------------------------------------------------------------- #
# slot resolution
# --------------------------------------------------------------------------- #

def merge_params(overrides, defaults):
    """Override beats default, first writer wins, output order is deterministic."""
    merged, order = {}, []
    for src in (overrides, defaults):
        for r in src:
            p = r.get("param")
            if p is None:
                continue
            if p not in merged:
                merged[p] = r
                order.append(p)
    return [merged[p] for p in order]


def resolve_slot(mi, file_for_asset, excluded_note):
    """Resolve one mesh->section->material slot.

    Returns a dict:
      chain, parent_chain, base, exact      (exact = [{param,role,file,asset_path,source}])
      unnamed                               (material_used_textures evidence, param=None)
      errors, reason                        (reason set when the slot is unresolved)
    """
    res = {"chain": [], "parent_chain": [], "base": None, "exact": [], "unnamed": [],
           "errors": [], "reason": None}
    if mi is None:
        res["reason"] = "no material interface on the slot"
        return res

    try:
        chain, overrides, defaults, base, errors = walk_material(mi)
    except Exception as exc:
        res["reason"] = "material walk failed: %s: %s" % (type(exc).__name__, exc)
        res["errors"].append(res["reason"])
        return res

    res["chain"] = chain
    res["parent_chain"] = chain
    res["base"] = base
    res["errors"] = errors

    merged = merge_params(overrides, defaults)
    for r in merged:
        ap = r.get("asset_path")
        role, channels, info = classify_param(r.get("param"))
        entry = {"param": r.get("param"), "role": role,
                 "asset_path": ap, "source": r.get("source")}
        if role == "packed":
            # `channels` says which map is in which channel; null means the name
            # did not encode an order, and then the note says exactly that.
            entry["channels"] = channels
        f = file_for_asset(ap) if ap else None
        entry["file"] = f
        if f is None:
            entry["note"] = (excluded_note(ap) if ap
                             else "material param has no texture assigned")
            if role == "packed" and channels is None:
                # `note` already carries the file reason for this entry -- keep it
                # and put the packed-order note beside it rather than over it.
                entry["channels_note"] = info["note"]
        elif role == "packed" and channels is None:
            entry["note"] = info["note"]
        res["exact"].append(entry)

    # unnamed material dependencies -- evidence only, never a resolved param
    for r in defaults:
        if r.get("param") is None:
            res["unnamed"].append({"param": None, "asset_path": r.get("asset_path"),
                                   "source": r.get("source")})

    n_exact = len(res["exact"])
    n_with_file = sum(1 for e in res["exact"] if e.get("file"))
    if n_exact == 0:
        if base:
            res["reason"] = ("material chain exposes no texture parameters "
                             "(master %s)" % base)
        else:
            res["reason"] = "material chain exposes no texture parameters (no Material reached)"
    elif n_with_file == 0:
        res["reason"] = ("%d texture param(s) resolved but none has a file in this export"
                         % n_exact)
    return res


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    t0 = time.time()
    man_path = JOB.get("manifest")
    if not man_path:
        raise RuntimeError("job has no 'manifest' -- nothing to relink")
    man_path = os.path.abspath(man_path)
    exports_dir = os.path.dirname(man_path)
    apply_changes = bool(JOB.get("apply", True))
    # .bak, deliberately NOT .json: this file sits inside <pack>\Exports, and a
    # crawler globbing *.json / manifest*.json there would otherwise see two
    # manifests per pack and could ingest the stale v1 one.
    backup_name = JOB.get("backup_name") or "manifest.v1.bak"
    limit_meshes = int(JOB.get("limit_meshes", 0) or 0)

    log("job: %s" % json.dumps({k: v for k, v in JOB.items() if k != "exclude_dirs"}))
    log("engine: %s" % unreal.SystemLibrary.get_engine_version())
    log("manifest: %s" % man_path)
    log("apply=%s backup=%s" % (apply_changes, backup_name))

    if not os.path.isfile(man_path):
        raise RuntimeError("manifest not found: %s -- relink is a manifest patch-up, "
                           "run convert_packs.sh first" % man_path)

    with open(man_path, "r", encoding="utf-8") as fh:
        MANIFEST = json.load(fh)

    v1_schema = MANIFEST.get("schema")
    if v1_schema not in (SCHEMA_V1, SCHEMA_V1_LEGACY,
                       SCHEMA_V2, SCHEMA_V2_LEGACY):
        log_warn("unexpected schema %r -- continuing, but the v1 keys are what this "
                 "tool preserves" % v1_schema)

    meshes = MANIFEST.get("meshes") or []
    if not meshes:
        # same failure mode export_pack.py refuses to hide: an empty manifest must
        # never look like a clean run
        log_warn("manifest lists ZERO meshes -- refusing to rewrite anything")
        _write_report({"pack_name": JOB.get("pack_name"), "ok": False,
                       "error": "manifest has zero meshes", "manifest": man_path})
        return

    # ---- texture asset path -> Exports-relative file ------------------------- #
    # Built from the manifest itself so the relink has exactly the same view of
    # "what landed on disk" as the exporter had; a second pass over the v1 material
    # entries catches anything the textures[] array somehow missed.
    file_by_asset = {}
    for t in MANIFEST.get("textures") or []:
        ap, f = t.get("asset_path"), t.get("file")
        if ap and f:
            if ap in file_by_asset and file_by_asset[ap] != f:
                log_warn("texture asset %s maps to two files (%s / %s); keeping the first"
                         % (ap, file_by_asset[ap], f))
                continue
            file_by_asset[ap] = f
    n_before = len(file_by_asset)
    for mesh in meshes:
        for mat in mesh.get("materials") or []:
            for t in mat.get("textures") or []:
                ap, f = t.get("asset_path"), t.get("file")
                if ap and f:
                    file_by_asset.setdefault(ap, f)
    log("texture file map: %d entries from textures[], %d after v1 material fallback"
        % (n_before, len(file_by_asset)))

    def file_for_asset(ap):
        return file_by_asset.get(ap) if ap else None

    def excluded_note(ap):
        """Why a resolved parameter has no file -- same wording as export_pack.py."""
        if is_excluded(ap):
            return "texture in excluded path %s" % json.dumps(MANIFEST.get("excluded_paths")
                                                              or DEFAULT_EXCLUDE)
        if not ap.startswith("/Game/"):
            return "texture lives outside this project (%s)" % ap.split("/")[1]
        return "texture not exported"

    # ---- discovery cross-check (game roots come from pack_discovery.py) ------ #
    job_roots = [g for g in (JOB.get("game_roots") or []) if g]
    if job_roots:
        man_roots = set(MANIFEST.get("game_roots") or [MANIFEST.get("game_root")])
        if not man_roots.issubset(set(job_roots)):
            log_warn("manifest game roots %s are not all in the discovered roots %s -- "
                     "the sandbox copy may not match the manifest"
                     % (json.dumps(sorted(man_roots)), json.dumps(job_roots)))
        else:
            log("game roots cross-check ok: %s" % json.dumps(sorted(man_roots)))

    # ---- resolve every slot ------------------------------------------------- #
    wiring_detail = {"pack": JOB.get("pack_name"), "manifest": man_path,
                     "engine": str(unreal.SystemLibrary.get_engine_version()),
                     "started": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                     "meshes": [], "role_rules": {r: list(t) for r, t in ROLE_RULES},
                     "packed_order_tokens": dict((t, list(c)) for t, c in PACKED_ORDER_TOKENS),
                     "packed_hint_tokens": list(PACKED_HINT_TOKENS),
                     "packed_order_note": PACKED_ORDER_NOTE}
    unresolved = []
    reason_hist = {}
    role_hist = dict((r, 0) for r in ROLE_ORDER)
    other_param_hist = {}
    # every parameter this run moved out of `other` into `packed`, with the mapping
    packed_param_hist = {}          # param -> {"count": n, "token": t|None,
                                    #           "hint": h|None, "channels": {...}|None}
    bbox_carry = 0                  # bounds carried over from the v1 manifest
    bbox_engine = 0                 # bounds read from the engine this run
    bbox_missing = 0                # no bounds available -> null, never guessed
    param_total = 0
    param_with_file = 0
    params_by_source = {}
    slots_total = 0
    slots_resolved = 0
    slots_resolved_from_instance = 0
    slots_resolved_from_defaults_only = 0
    meshes_loaded = 0
    load_failures = []
    samples = []

    work = meshes[:limit_meshes] if limit_meshes else meshes
    if limit_meshes:
        log("mesh limit applied -> %d of %d meshes" % (len(work), len(meshes)))

    for idx, mesh in enumerate(work, 1):
        ap = mesh.get("asset_path")
        name = mesh.get("name") or (ap.rsplit("/", 1)[-1] if ap else "?")
        kind = mesh.get("kind") or "StaticMesh"
        v1_mats = mesh.get("materials") or []
        v1_by_slot = {}
        for i, rec in enumerate(v1_mats):
            try:
                v1_by_slot[int(rec.get("slot", i))] = rec
            except Exception:
                v1_by_slot[i] = rec

        mrec = {"name": name, "asset_path": ap, "kind": kind, "loaded": False,
                "v1_slots": len(v1_mats), "slots": []}

        slots = []
        asset = None
        if not ap:
            load_failures.append({"asset": None, "mesh": name, "error": "mesh has no asset_path"})
        else:
            try:
                asset = unreal.EditorAssetLibrary.load_asset(ap)
            except Exception as exc:
                asset = None
                load_failures.append({"asset": ap, "mesh": name,
                                      "error": "load failed: %s: %s" % (type(exc).__name__, exc)})
            if asset is None and not any(f["mesh"] == name for f in load_failures):
                load_failures.append({"asset": ap, "mesh": name, "error": "load returned None"})

        if asset is not None:
            meshes_loaded += 1
            mrec["loaded"] = True

        # ---- world-space bounds (item 4) ------------------------------------ #
        # Written for EVERY mesh in the manifest, including the ones this run did
        # not examine, so the build layer never has to special-case a pack.
        how = write_mesh_bounds(mesh, asset, kind)
        if how is None:
            bbox_missing += 1
            log_warn("no world-space bounds for mesh %s (bbox_min/bbox_max null)" % name)
        elif how.startswith("engine"):
            bbox_engine += 1
            if "mismatch" in how:
                log_warn("mesh %s: engine bbox_max-bbox_min disagrees with bbox_m" % name)
        else:
            bbox_carry += 1
            if "mismatch" in how:
                log_warn("mesh %s: carried bbox_max-bbox_min disagrees with bbox_m" % name)

        if asset is not None:
            raw_slots = []
            try:
                if kind == "StaticMesh":
                    raw_slots = list(asset.get_editor_property("static_materials") or [])
                else:
                    raw_slots = list(asset.get_editor_property("materials") or [])
            except Exception as exc:
                mrec["slot_read_error"] = "%s: %s" % (type(exc).__name__, exc)
                log_warn("material slots for %s failed: %s: %s" % (name, type(exc).__name__, exc))
            for slot_i, sm in enumerate(raw_slots):
                slot_name = None
                try:
                    slot_name = str(sm.get_editor_property("material_slot_name"))
                except Exception:
                    pass
                mi = None
                try:
                    mi = sm.get_editor_property("material_interface")
                except Exception as exc:
                    mrec.setdefault("slot_errors", []).append(
                        "slot %d material_interface unreadable: %s: %s"
                        % (slot_i, type(exc).__name__, exc))
                res = resolve_slot(mi, file_for_asset, excluded_note)
                res["slot"] = slot_i
                res["slot_name"] = slot_name
                res["material"] = obj_name(mi) if mi is not None else None
                res["material_class"] = obj_class_name(mi) if mi is not None else None
                res["material_path"] = tex_obj_path(mi) if mi is not None else None
                slots.append(res)

            # a slot the v1 manifest listed but the loaded asset no longer shows --
            # never silently dropped
            for slot_i in sorted(v1_by_slot):
                if slot_i >= len(raw_slots):
                    slots.append({"slot": slot_i, "slot_name": None, "material": None,
                                  "material_class": None, "material_path": None,
                                  "chain": [], "parent_chain": [], "base": None,
                                  "exact": [], "unnamed": [], "errors": [],
                                  "reason": "slot %d is in the v1 manifest but not in the "
                                            "loaded asset" % slot_i,
                                  "missing_from_asset": True})

        mrec["slots"] = [{"slot": s["slot"], "slot_name": s.get("slot_name"),
                          "material": s.get("material"), "material_class": s.get("material_class"),
                          "chain": s.get("chain"), "parent_chain": s.get("parent_chain"),
                          "base": s.get("base"),
                          "exact": s.get("exact"), "unnamed_count": len(s.get("unnamed") or []),
                          "reason": s.get("reason"), "errors": s.get("errors")}
                         for s in slots]
        wiring_detail["meshes"].append(mrec)

        # ---- score the slots ------------------------------------------------ #
        for s in slots:
            slots_total += 1
            slot_key = {"mesh": name, "asset_path": ap, "slot": s["slot"],
                        "slot_name": s.get("slot_name"), "material": s.get("material"),
                        "parent_chain": list(s.get("parent_chain") or [])}
            if not asset:
                why = "mesh asset could not be loaded"
            else:
                why = s.get("reason")
            has_file = any(e.get("file") for e in (s.get("exact") or []))
            if has_file:
                slots_resolved += 1
                # how was it resolved?  an instance override is the strongest form
                # of evidence; a master default is what the slot renders with when
                # nothing overrode it.  Both are exact, but the app may want to
                # prefer the former, so the split is published instead of hidden.
                if any(e.get("file") and str(e.get("source", "")).startswith("instance_override")
                       for e in s["exact"]):
                    slots_resolved_from_instance += 1
                else:
                    slots_resolved_from_defaults_only += 1
                if len(samples) < 6:
                    samples.append(dict(slot_key, textures=[
                        {"param": e["param"], "role": e["role"], "file": e["file"]}
                        for e in s["exact"] if e.get("file")]))
            else:
                why = why or "slot has no resolvable texture parameter"
                rec = dict(slot_key, reason=why, params_resolved=len(s.get("exact") or []))
                unresolved.append(rec)
                reason_hist[why] = reason_hist.get(why, 0) + 1

            for e in (s.get("exact") or []):
                param_total += 1
                src = str(e.get("source") or "?")
                params_by_source[src] = params_by_source.get(src, 0) + 1
                r = e.get("role") or "other"
                role_hist[r] = role_hist.get(r, 0) + 1
                if e.get("file"):
                    param_with_file += 1
                if r == "other":
                    p = (e.get("param") or "").strip()
                    other_param_hist[p] = other_param_hist.get(p, 0) + 1
                elif r == "packed":
                    # recomputed from the name here so no private bookkeeping keys
                    # ever leak into the manifest's texture entries
                    p = (e.get("param") or "").strip()
                    info = packed_for(e.get("param")) or {}
                    slot = packed_param_hist.setdefault(
                        p, {"count": 0, "token": info.get("token"),
                            "hint": info.get("hint"),
                            "channels": e.get("channels")})
                    slot["count"] += 1

        try:
            unreal.EditorAssetLibrary.unload_asset(ap)
        except Exception:
            pass
        if idx % 25 == 0:
            try:
                unreal.SystemLibrary.collect_garbage()
            except Exception:
                pass
        if idx % 25 == 0 or idx == len(work):
            log("[%d/%d] meshes -- slots=%d resolved=%d unresolved=%d"
                % (idx, len(work), slots_total, slots_resolved, len(unresolved)))

    # ---- ratio + degraded guards -------------------------------------------- #
    # Meshes the run did not examine (mesh limit) still exist in the manifest, so
    # their v1 slots must be counted -- as unresolved -- or wiring.slots_total
    # would disagree with the manifest's own slot count.  The verifier enforces
    # that agreement, so this is a contract, not a nicety.
    for mesh in meshes[len(work):]:
        # bounds still have to exist for the meshes this run skipped
        if write_mesh_bounds(mesh, None, mesh.get("kind")) is None:
            bbox_missing += 1
        else:
            bbox_carry += 1
        for rec in mesh.get("materials") or []:
            slots_total += 1
            unresolved.append({"mesh": mesh.get("name"), "asset_path": mesh.get("asset_path"),
                               "slot": rec.get("slot"), "slot_name": None,
                               "material": rec.get("name"),
                               "parent_chain": list(rec.get("parent_chain") or []),
                               "reason": "not examined in this run (mesh limit)",
                               "params_resolved": 0})
            reason_hist["not examined in this run (mesh limit)"] = \
                reason_hist.get("not examined in this run (mesh limit)", 0) + 1

    ratio = (float(slots_resolved) / slots_total) if slots_total else 0.0
    if meshes_loaded == 0:
        log_warn("ZERO mesh assets could be loaded (%d attempts).  The sandbox copy is "
                 "missing or the manifest's asset paths do not match this project.  "
                 "Refusing to rewrite the manifest." % len(load_failures))
        _write_report({"pack_name": JOB.get("pack_name"), "ok": False,
                       "error": "zero meshes loaded", "manifest": man_path,
                       "load_failures": load_failures[:40],
                       "slots_total": slots_total})
        return
    degraded = len(load_failures) > max(1, int(0.05 * len(work)))
    if load_failures:
        log_warn("%d of %d meshes could not be loaded; their slots are reported "
                 "unresolved rather than silently dropped" % (len(load_failures), len(work)))

    # ---- patch the manifest -------------------------------------------------- #
    patched_slots = 0
    kept_v1_slots = 0
    for mesh in meshes:
        detail = next((m for m in wiring_detail["meshes"]
                       if m["asset_path"] == mesh.get("asset_path")), None)
        if detail is None:
            kept_v1_slots += len(mesh.get("materials") or [])
            continue
        by_slot = dict((s["slot"], s) for s in detail["slots"])
        v1_mats = mesh.get("materials") or []
        by_slot_v1 = {}
        for i, rec in enumerate(v1_mats):
            try:
                by_slot_v1[int(rec.get("slot", i))] = rec
            except Exception:
                by_slot_v1[i] = rec

        new_mats = []
        for slot_i in sorted(set(list(by_slot_v1.keys()) + list(by_slot.keys()))):
            s = by_slot.get(slot_i)
            old = by_slot_v1.get(slot_i)
            if s is None:
                # not examined this run (mesh limit / different asset) -> untouched v1
                if old is not None:
                    new_mats.append(old)
                    kept_v1_slots += 1
                continue
            has_file = any(e.get("file") for e in (s.get("exact") or []))
            if has_file:
                rec = {
                    "slot": slot_i,
                    "name": s.get("material") or (old or {}).get("name"),
                    "base": s.get("base") or (old or {}).get("base"),
                    "chain": list(old.get("chain") or []) if old else [],
                    "parent_chain": list(s.get("parent_chain") or []),
                    "method": METHOD,
                    "textures": [dict(e) for e in s["exact"]],
                }
                # Re-running over an already-relinked (v2) manifest must converge,
                # not drift: the superseded count is carried forward instead of
                # being recomputed from the v2 list (which is no longer heuristic).
                if old and old.get("heuristic_textures_superseded") is not None:
                    rec["heuristic_textures_superseded"] = old["heuristic_textures_superseded"]
                elif old and (old.get("textures") or []):
                    rec["heuristic_textures_superseded"] = len(old["textures"])
                patched_slots += 1
            else:
                # unresolved: keep the v1 entry exactly as it was, mark partialness
                rec = dict(old) if old else {"slot": slot_i, "name": s.get("material"),
                                             "base": s.get("base"), "chain": [],
                                             "textures": []}
                rec["slot"] = slot_i
                rec["parent_chain"] = list(s.get("parent_chain") or [])
                rec["note"] = "unresolved"
                rec["unresolved_reason"] = s.get("reason") or "slot has no resolvable texture parameter"
                rec.pop("unexported_params", None)   # never leave a stale one behind
                if s.get("exact"):
                    rec["unexported_params"] = [
                        {"param": e.get("param"), "role": e.get("role"),
                         "asset_path": e.get("asset_path"), "note": e.get("note"),
                         **({"channels": e["channels"]} if e.get("role") == "packed" else {})}
                        for e in s["exact"]]
                for t in rec.get("textures") or []:
                    t.setdefault("note", "unresolved")
                    t["parent_chain"] = list(s.get("parent_chain") or [])
                if not (rec.get("textures") or []) and s.get("unnamed"):
                    rec["textures"] = [dict(u) for u in s["unnamed"]]
                    for t in rec["textures"]:
                        t["file"] = file_for_asset(t.get("asset_path"))
                        t.setdefault("note", "unresolved")
                        t["parent_chain"] = list(s.get("parent_chain") or [])
            # NOTE: the append is OUTSIDE the if/else on purpose.  Nested inside it,
            # every exactly-wired slot silently vanished from the manifest -- caught
            # by a preview run + verify_pack_export before it reached a live file.
            new_mats.append(rec)
        mesh["materials"] = new_mats

    MANIFEST["schema"] = SCHEMA_V2
    MANIFEST["wiring"] = {
        "slots_total": slots_total,
        "slots_resolved": slots_resolved,
        "method": METHOD,
        "unresolved": unresolved,
        "slots_unresolved": slots_total - slots_resolved,
        "slots_ratio": round(ratio, 4),
        "slots_resolved_from_instance": slots_resolved_from_instance,
        "slots_resolved_from_defaults_only": slots_resolved_from_defaults_only,
        "params_total": param_total,
        "params_with_file": param_with_file,
        "params_by_role": role_hist,
        "params_by_source": params_by_source,
        "unresolved_reasons": dict(sorted(reason_hist.items(), key=lambda kv: -kv[1])),
        "other_param_names": dict(sorted(other_param_hist.items(), key=lambda kv: -kv[1])[:40]),
        # item 3: every parameter moved out of `other` into `packed`, with the
        # mapping actually used (token -> channels, or null + why).
        "packed_params": dict(sorted(packed_param_hist.items(), key=lambda kv: -kv[1]["count"])),
        "packed_params_total": sum(v["count"] for v in packed_param_hist.values()),
        "packed_params_with_channels": sum(v["count"] for v in packed_param_hist.values()
                                           if v["channels"]),
        "packed_params_without_channels": sum(v["count"] for v in packed_param_hist.values()
                                              if not v["channels"]),
        "packed_channel_tokens": dict((t, list(c)) for t, c in PACKED_ORDER_TOKENS),
        "packed_hint_tokens": list(PACKED_HINT_TOKENS),
        "packed_order_note": PACKED_ORDER_NOTE,
        # item 4: world-space bounds per mesh
        "bbox_fields": {"bbox_min": "metres, world space, per mesh",
                        "bbox_max": "metres, world space, per mesh",
                        "bbox_m": "unchanged size only, kept for compatibility"},
        "meshes_with_bbox_min": sum(1 for m in meshes if _is_vec3(m.get("bbox_min"))),
        "bbox_carried_from_v1": bbox_carry,
        "bbox_from_engine": bbox_engine,
        "bbox_missing": bbox_missing,
        "meshes_total": len(meshes),
        "meshes_examined": len(work),
        "meshes_loaded": meshes_loaded,
        "meshes_load_failed": len(load_failures),
        "degraded": degraded,
        "engine_version": str(unreal.SystemLibrary.get_engine_version()),
        "relinked_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "patched_from_schema": v1_schema,
        "tool": "conversion/tools/relink_materials.py",
        "note": ("role is inferred from the UE parameter name; `file` is relative to the "
                 "Exports folder; slots not listed here carry exact wiring from the engine's "
                 "material parameter values (method=ue-param)"),
    }
    MANIFEST["relinked_at"] = MANIFEST["wiring"]["relinked_at"]

    # ---- write --------------------------------------------------------------- #
    report = {
        "pack_name": JOB.get("pack_name"),
        "manifest": man_path,
        "exports_dir": exports_dir,
        "engine_version": str(unreal.SystemLibrary.get_engine_version()),
        "started": wiring_detail["started"],
        "apply": apply_changes,
        "schema_in": v1_schema,
        "schema_out": SCHEMA_V2,
        "slots_total": slots_total,
        "slots_resolved": slots_resolved,
        "slots_resolved_from_instance": slots_resolved_from_instance,
        "slots_resolved_from_defaults_only": slots_resolved_from_defaults_only,
        "slots_ratio": round(ratio, 4),
        "params_total": param_total,
        "params_with_file": param_with_file,
        "params_by_role": role_hist,
        "params_by_source": params_by_source,
        "other_param_names_top": dict(sorted(other_param_hist.items(), key=lambda kv: -kv[1])[:25]),
        "packed_params": dict(sorted(packed_param_hist.items(), key=lambda kv: -kv[1]["count"])),
        "packed_params_total": sum(v["count"] for v in packed_param_hist.values()),
        "packed_params_with_channels": sum(v["count"] for v in packed_param_hist.values()
                                           if v["channels"]),
        "packed_params_without_channels": sum(v["count"] for v in packed_param_hist.values()
                                              if not v["channels"]),
        "bbox_carried_from_v1": bbox_carry,
        "bbox_from_engine": bbox_engine,
        "bbox_missing": bbox_missing,
        "meshes_with_bbox_min": sum(1 for m in meshes if _is_vec3(m.get("bbox_min"))),
        "unresolved_reasons": dict(sorted(reason_hist.items(), key=lambda kv: -kv[1])),
        "meshes_total": len(meshes),
        "meshes_examined": len(work),
        "meshes_loaded": meshes_loaded,
        "meshes_load_failed": len(load_failures),
        "load_failures": load_failures[:40],
        "slots_patched_exact": patched_slots,
        "slots_kept_as_v1": kept_v1_slots,
        "unresolved_count": len(unresolved),
        "samples": samples,
        "degraded": degraded,
        "elapsed_seconds": round(time.time() - t0, 1),
        "ok": True,
    }

    wiring_detail["totals"] = {k: report[k] for k in
                               ("slots_total", "slots_resolved", "slots_ratio",
                                "params_total", "params_by_role", "unresolved_reasons",
                                "meshes_loaded", "meshes_load_failed", "degraded")}
    wiring_detail["finished"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    wr = JOB.get("wiring_report")
    if wr:
        os.makedirs(os.path.dirname(wr), exist_ok=True)
        with open(wr, "w", encoding="utf-8") as fh:
            json.dump(wiring_detail, fh, indent=1)
        log("wiring report written: %s (%d bytes)" % (wr, os.path.getsize(wr)))

    if not apply_changes:
        # A dry run still produces the EXACT artifact (when the job asks for it) so
        # it can be verified against the real Exports tree before the live manifest
        # is touched -- evidence first, mutation second.
        preview = JOB.get("manifest_out")
        if preview:
            os.makedirs(os.path.dirname(os.path.abspath(preview)), exist_ok=True)
            with open(preview, "w", encoding="utf-8") as fh:
                json.dump(MANIFEST, fh, indent=1)
            report["manifest_out"] = preview
            report["manifest_out_bytes"] = os.path.getsize(preview)
            log("DRY RUN preview manifest written: %s (%d bytes)"
                % (preview, os.path.getsize(preview)))
        log("DRY RUN (apply=false): the live manifest was NOT touched")
        report["applied"] = False
        _write_report(report)
        log("DONE dry-run slots=%d/%d (%.3f) in %.1fs"
            % (slots_resolved, slots_total, ratio, report["elapsed_seconds"]))
        return

    backup_path = os.path.join(exports_dir, backup_name)
    if os.path.exists(backup_path):
        log("backup already exists, left untouched: %s" % backup_path)
    else:
        with open(man_path, "rb") as src, open(backup_path, "wb") as dst:
            dst.write(src.read())
        log("backup written: %s (%d bytes)" % (backup_path, os.path.getsize(backup_path)))

    tmp_path = man_path + ".relink-tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(MANIFEST, fh, indent=1)
    try:
        os.replace(tmp_path, man_path)
    except Exception as exc:
        log_warn("atomic replace failed (%s: %s); writing in place" % (type(exc).__name__, exc))
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(MANIFEST, fh, indent=1)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    log("manifest rewritten: %s (%d bytes), schema=%s" % (man_path, os.path.getsize(man_path),
                                                          MANIFEST["schema"]))
    report["applied"] = True
    report["backup"] = backup_path
    report["manifest_bytes"] = os.path.getsize(man_path)
    _write_report(report)

    if ratio < 0.9:
        log_warn("wiring ratio %.3f is below the 0.9 target: %d of %d slots unresolved "
                 "(top reasons: %s)"
                 % (ratio, len(unresolved), slots_total,
                    json.dumps(dict(sorted(reason_hist.items(), key=lambda kv: -kv[1])[:3]))))
    log("DONE slots=%d/%d (%.3f) params=%d/%d packed=%d/%d (channels known %d) "
        "bbox=%d/%d meshes (carried %d, engine %d, missing %d) in %.1fs"
        % (slots_resolved, slots_total, ratio, param_with_file, param_total,
           role_hist.get("packed", 0), param_total,
           sum(v["count"] for v in packed_param_hist.values() if v["channels"]),
           sum(1 for m in meshes if _is_vec3(m.get("bbox_min"))), len(meshes),
           bbox_carry, bbox_engine, bbox_missing, report["elapsed_seconds"]))


def _write_report(report):
    rep_path = JOB.get("report")
    report["finished"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if rep_path:
        os.makedirs(os.path.dirname(rep_path), exist_ok=True)
        with open(rep_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
        log("report written: %s" % rep_path)
        with open(os.path.join(os.path.dirname(rep_path), "relink_run.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("\n===== %s : %s =====\n" % (report.get("finished"),
                                                  JOB.get("pack_name")))
            fh.write("\n".join(LOG) + "\n")


try:
    main()
except Exception:
    unreal.log_error("[relink] FATAL:\n%s" % traceback.format_exc())
    try:
        _write_report({"pack_name": JOB.get("pack_name"), "ok": False,
                       "fatal": traceback.format_exc()})
    except Exception:
        pass
finally:
    try:
        unreal.SystemLibrary.quit_editor()
    except Exception:
        pass
