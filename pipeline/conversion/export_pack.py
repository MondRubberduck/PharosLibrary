"""
export_pack.py -- headless Unreal Editor driver: Leartes .uasset -> .fbx/.png + manifest.json.

Runs INSIDE UnrealEditor (via -ExecutePythonScript).  Never writes to the source pack:
it only reads the sandbox copy of the assets and writes to the configured output dirs.

Job description is passed as JSON in the environment variable AMCONV_JOB:

{
  "pack_name":    "Oriantel Building",
  "pack_dir":     "D:/3D_Assets/Leartes Env_ gumroad/Oriantel Building",
  "content_root": "D:/3D_Assets/Leartes Env_ gumroad/Oriantel Building/Content",
  "project_name": "OriantelBuilding",
  "game_root":    "/Game/OriantalBuilding",   # primary, for the manifest
  "game_roots":   ["/Game/OriantalBuilding"], # every discovered top-level /Game path
  "fbx_out":      "<pack_dir>/Exports/FBX",
  "tex_out":      "<pack_dir>/Exports/Textures",
  "manifest":     "<pack_dir>/Exports/manifest.json",
  "report":       "D:/Pipeline/kiosk/conversion/logs/run.json",
  "exclude_dirs": ["EpicContent"],            # path-segment match, case-insensitive
  "limit_meshes":   5,      # 0 = unlimited
  "limit_skel":     1,
  "limit_textures": 2,
  "export_textures": true,
  "export_meshes":   true,
  "skip_meshes":    ["sm_Pipe_Straight_4m_03_11"],  # exact asset names, never exported
  "skip_patterns":  ["sm_Pipe_Straight_4m_03_"]     # substring / prefix match, never exported
}

Backwards compatible: a job without the new keys behaves like the original
single-game-root exporter.  The same is true of "skip_meshes" / "skip_patterns":
absent (or empty) means nothing is skipped.
"""

import datetime
import json
import os
import struct
import sys
import time
import traceback

import unreal

# --------------------------------------------------------------------------- #
# job loading
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


# --------------------------------------------------------------------------- #
# explicit skip list (convert_packs.sh --skip-meshes / --skip-pattern)
# --------------------------------------------------------------------------- #
# Unreal 5.7's own FBX exporter can hit a HARD C++ assertion on a bad asset
# ("Array index out of bounds" inside /Script/Engine.Exporter.RunAssetExportTask).
# That is not an exception: check() aborts the process, so Python cannot catch
# it, cannot log a failure, and cannot continue.  The only way to get such a pack
# to finish is to keep the offending asset out of the export loop entirely.
#
# Two matching modes, both applied to the mesh list BEFORE the first asset is
# loaded (so the bad asset is never even touched):
#   skip_meshes   -- exact asset name ("sm_Pipe_Straight_4m_03_11") or package path
#   skip_patterns -- case-insensitive substring, which also covers a prefix
#                    ("sm_Pipe_Straight_4m_03_" skips the whole numbered family)
# Skip is intentionally the LAST filter applied: a name that is skipped is never
# counted as exported and never counted as failed.  It is recorded in the
# manifest under "skipped" so the gap is visible instead of silent.

def _skip_spec(raw):
    """Normalise a job field into a list of non-empty strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(s).strip() for s in raw if str(s).strip()]


SKIP_NAMES = _skip_spec(JOB.get("skip_meshes") or JOB.get("skip_mesh"))
SKIP_PATTERNS = _skip_spec(JOB.get("skip_patterns") or JOB.get("skip_pattern"))


def mesh_is_skipped(package_path, name):
    """True when this mesh is on the explicit skip list or matches a pattern."""
    pkg_l = str(package_path).lower()
    name_l = str(name).lower()
    for n in SKIP_NAMES:
        n_l = n.lower()
        if name_l == n_l or pkg_l == n_l or pkg_l.endswith("/" + n_l):
            return True
    for pat in SKIP_PATTERNS:
        p_l = pat.lower()
        if p_l in name_l or p_l in pkg_l:
            return True
    return False


def apply_mesh_skips(inv):
    """Drop every skipped mesh from the StaticMesh/SkeletalMesh buckets of `inv`.

    Returns the removed package paths.  Called before the export loop, so a skipped
    asset is never loaded, never exported and never counted as either an export or
    a failure -- `counts.static_mesh`/`skeletal_mesh` are recomputed from `inv`
    afterwards, which keeps the manifest self-consistent for the verifier.
    """
    removed = []
    for cls in ("StaticMesh", "SkeletalMesh"):
        keep, drop = [], []
        for p in inv[cls]:
            nm = p.split(".")[0].rsplit("/", 1)[-1]
            (drop if mesh_is_skipped(p.split(".")[0], nm) else keep).append(p)
        inv[cls] = keep
        removed.extend(drop)
    return removed


def log(msg):
    line = "[amconv] %s" % msg
    LOG.append(line)
    unreal.log(line)


def log_warn(msg):
    line = "[amconv][WARN] %s" % msg
    LOG.append(line)
    unreal.log_warning(line)


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #

def fbx_magic(path):
    """Return 'binary' / 'ascii' / None by inspecting the file header.

    Binary FBX begins with the 23-byte string "Kaydara FBX Binary  \\x00\\x1a\\x00".
    NOTE: len("Kaydara") == 7 -- slicing [:6] silently never matches.
    """
    for attempt in range(6):
        try:
            with open(path, "rb") as fh:
                head = fh.read(32)
            break
        except FileNotFoundError:
            return None
        except Exception:
            if attempt == 5:
                return "unreadable"
            time.sleep(0.25)
    if head[:7] == b"Kaydara" and b"FBX" in head[:24]:
        return "binary"
    stripped = head.lstrip()
    if stripped[:4] in (b"; FBX", b"FBX "):
        return "ascii"
    return None


def image_magic(path):
    """Return a short description of an image file's real format."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except Exception:
        return "unreadable"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:4] == b"\x76\x2f\x31\x01":
        return "openexr"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] == b"DDS ":
        return "dds"
    return "unknown:%r" % head[:8]


def image_dims(path):
    """Real pixel dimensions read from the file on disk.  (w, h) or (None, None).

    The engine's Texture2D.blueprint_get_size_* returns the *resident* mip size,
    which for these streams textures is a 32x32 placeholder -- useless.  The
    asset-registry `Dimensions` tag is right, but the exported file is the
    authority for what actually landed on disk, so read that.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:4] == b"\x76\x2f\x31\x01":
                # OpenEXR: walk the header attributes looking for dataWindow
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
                    typ = buf[i:e]
                    i = e + 1
                    size = struct.unpack("<I", buf[i:i + 4])[0]
                    i += 4
                    if name == b"dataWindow" and size == 16:
                        x0, y0, x1, y1 = struct.unpack("<iiii", buf[i:i + 16])
                        return int(x1 - x0 + 1), int(y1 - y0 + 1)
                    i += size
    except Exception:
        pass
    return None, None


def set_props(obj, props):
    """Set editor properties defensively; returns list of applied names."""
    applied = []
    for key, value in props.items():
        try:
            obj.set_editor_property(key, value)
            applied.append(key)
        except Exception as exc:  # property missing in this engine version
            log_warn("could not set %s=%r on %s (%s)" % (key, value, type(obj).__name__, exc))
    return applied


def build_fbx_options():
    """FbxExportOption, tolerant of engine-version differences."""
    try:
        opts = unreal.FbxExportOption()
    except Exception as exc:
        log_warn("FbxExportOption unavailable (%s); falling back to exporter defaults" % exc)
        return None
    set_props(opts, {
        "ascii": False,
        "collision": False,
        "level_of_detail": False,
        "vertex_color": True,
        "full_precision_uv": True,
        "force_front_x_axis": False,
        "export_preview_mesh": True,
        "bake_animation": True,
        "export_local_time": False,
        "map_skeletal_mesh_morph_targets": True,
        "export_morph_targets": True,
        "export_source_mesh": True,
    })
    return opts


def export_asset(asset, filename_no_ext, options, want_ext):
    """Export one asset. Returns (ok, final_path, error_text)."""
    directory = os.path.dirname(filename_no_ext)
    if directory:
        os.makedirs(directory, exist_ok=True)
    desired = filename_no_ext + want_ext

    task = unreal.AssetExportTask()
    set_props(task, {
        "object": asset,
        "filename": desired,
        "automated": True,
        "prompt": False,
        "replace_identical": True,
        "write_empty_files": False,
        "errors": [],
    })
    if options is not None:
        try:
            task.set_editor_property("options", options)
        except Exception as exc:
            log_warn("could not attach export options (%s)" % exc)

    try:
        ok = unreal.Exporter.run_asset_export_task(task)
    except Exception as exc:
        return False, desired, "%s: %s" % (type(exc).__name__, exc)

    err_text = ""
    try:
        errs = task.get_editor_property("errors")
        if errs:
            err_text = " | ".join(str(e) for e in errs)
    except Exception:
        pass

    # Trust the file on disk over the return value.
    exists = os.path.isfile(desired)
    if ok and not exists:
        return False, desired, "task reported success but no file on disk" + (
            ("; " + err_text) if err_text else "")
    if not ok and exists:
        return True, desired, "task returned False but file exists" + (
            ("; " + err_text) if err_text else "")
    return bool(ok), desired, err_text


def rel_out_dir(package_path, game_root, out_root):
    """Mirror the package folder structure under out_root."""
    tail = package_path
    if game_root and tail.startswith(game_root):
        tail = tail[len(game_root):]
    tail = tail.strip("/")
    return os.path.join(out_root, tail.replace("/", os.sep)) if tail else out_root


def game_root_for(package_path, game_roots):
    """Longest matching /Game root for a package path ('' when none match)."""
    best = ""
    for gr in game_roots:
        if package_path == gr or package_path.startswith(gr + "/"):
            if len(gr) > len(best):
                best = gr
    return best


def fwd(p):
    return p.replace("\\", "/") if p else p


# --------------------------------------------------------------------------- #
# exclusion
# --------------------------------------------------------------------------- #

EXCLUDE = [str(s).lower() for s in (JOB.get("exclude_dirs") or DEFAULT_EXCLUDE)]


def is_excluded(package_path):
    """True when a path segment equals an excluded folder name (case-insensitive)."""
    if not EXCLUDE:
        return False
    segs = [s.lower() for s in package_path.split("/") if s]
    for e in EXCLUDE:
        if e in segs:
            return True
    return False


# --------------------------------------------------------------------------- #
# struct field access (unreal structs vary in attribute exposure)
# --------------------------------------------------------------------------- #

def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _field(obj, *names):
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
    x = _field(v, "x", "X")
    y = _field(v, "y", "Y")
    z = _field(v, "z", "Z")
    if x is None or y is None or z is None:
        return None
    return [_num(x), _num(y), _num(z)]


def dims_from_minmax(lo, hi, scale=0.01, nd=4):
    """cm -> m, rounded.  Returns [dx, dy, dz] or None."""
    if lo is None or hi is None:
        return None
    return [round((hi[i] - lo[i]) * scale, nd) for i in range(3)]


def static_mesh_metrics(mesh):
    """triangles / vertices / bbox_m read straight from the engine."""
    out = {"triangles": None, "vertices": None, "bbox_m": None,
           "bbox_min_m": None, "bbox_max_m": None, "lods": None, "bbox_source": None}
    for meth, key, idx in (("get_num_triangles", "triangles", 0),
                           ("get_num_vertices", "vertices", 0)):
        fn = getattr(mesh, meth, None)
        if fn is None:
            continue
        try:
            out[key] = int(fn(0))
        except Exception as exc:
            log_warn("%s(%s) failed: %s" % (meth, mesh.get_name(), exc))
    try:
        out["lods"] = int(mesh.get_num_lods())
    except Exception:
        pass
    box = None
    try:
        fn = getattr(mesh, "get_bounding_box", None)
        if fn is not None:
            box = fn()
    except Exception as exc:
        log_warn("get_bounding_box(%s) failed: %s" % (mesh.get_name(), exc))
    if box is not None:
        lo = _vec3(_field(box, "min"))
        hi = _vec3(_field(box, "max"))
        if lo and hi:
            out["bbox_m"] = dims_from_minmax(lo, hi)
            out["bbox_min_m"] = [round(v * 0.01, 4) for v in lo]
            out["bbox_max_m"] = [round(v * 0.01, 4) for v in hi]
            out["bbox_source"] = "engine:StaticMesh.get_bounding_box"
            return out
    return out


def skeletal_mesh_metrics(mesh):
    """bbox_m for a skeletal mesh (get_bounds -> BoxSphereBounds, cm -> m)."""
    out = {"triangles": None, "vertices": None, "bbox_m": None,
           "bbox_min_m": None, "bbox_max_m": None, "lods": None, "bbox_source": None}
    try:
        sysub = getattr(unreal, "SkeletalMeshEditorSubsystem", None)
        if sysub is not None and hasattr(sysub, "get_num_verts"):
            out["vertices"] = int(sysub.get_num_verts(mesh, 0))
        if sysub is not None and hasattr(sysub, "get_num_sections"):
            out["sections"] = int(sysub.get_num_sections(mesh, 0))
    except Exception as exc:
        log_warn("skm vert count(%s) failed: %s" % (mesh.get_name(), exc))
    try:
        box = mesh.get_bounds()
        origin = _vec3(_field(box, "origin"))
        extent = _vec3(_field(box, "box_extent", "extent"))
        if origin and extent:
            out["bbox_m"] = [round(2.0 * e * 0.01, 4) for e in extent]
            out["bbox_min_m"] = [round((origin[i] - extent[i]) * 0.01, 4) for i in range(3)]
            out["bbox_max_m"] = [round((origin[i] + extent[i]) * 0.01, 4) for i in range(3)]
            out["bbox_source"] = "engine:SkeletalMesh.get_bounds"
    except Exception as exc:
        log_warn("skm get_bounds(%s) failed: %s" % (mesh.get_name(), exc))
    return out


# --------------------------------------------------------------------------- #
# material -> texture resolution
# --------------------------------------------------------------------------- #

def tex_obj_path(tex):
    """'/Game/A/B.T' -> '/Game/A/B' ('' when None)."""
    if tex is None:
        return None
    try:
        pn = tex.get_path_name()
    except Exception:
        return None
    if not pn:
        return None
    return pn.split(".")[0] if "." in pn.rsplit("/", 1)[-1] else pn


def _collect_instance_params(inst, out):
    """TextureParameterValues off a MaterialInstance (param name + texture)."""
    try:
        tpv = inst.get_editor_property("texture_parameter_values")
    except Exception:
        return
    for pv in tpv or []:
        pname = None
        try:
            pname = str(pv.get_editor_property("parameter_info").get_editor_property("name"))
        except Exception:
            pname = None
        try:
            tex = pv.get_editor_property("parameter_value")
        except Exception:
            tex = None
        out.append({"param": pname, "asset_path": tex_obj_path(tex),
                    "source": "instance_override:%s" % inst.get_name()})


def _collect_material_params(mat, out):
    """Named texture parameters + their default values off a base Material."""
    mel = unreal.MaterialEditingLibrary
    names = []
    try:
        names = [str(n) for n in mel.get_texture_parameter_names(mat)]
    except Exception as exc:
        log_warn("get_texture_parameter_names(%s) failed: %s" % (mat.get_name(), exc))
    for n in names:
        tex = None
        try:
            tex = mel.get_material_default_texture_parameter_value(mat, n)
        except Exception as exc:
            log_warn("default texture param %r on %s failed: %s" % (n, mat.get_name(), exc))
        out.append({"param": n, "asset_path": tex_obj_path(tex),
                    "source": "material_default"})
    # anything wired in that is not a named parameter (still a real dependency)
    try:
        for t in mel.get_used_textures(mat):
            out.append({"param": None, "asset_path": tex_obj_path(t),
                        "source": "material_used_textures"})
    except Exception as exc:
        log_warn("get_used_textures(%s) failed: %s" % (mat.get_name(), exc))


def resolve_material_link(mi, slot, slot_name):
    """Walk MIC -> parent chain, collect param-named textures, dedupe.

    Returns a manifest `materials[]` record.  `base` is the root Material's name.
    A texture whose param name the engine will not expose gets param=null rather
    than an invented name.
    """
    rec = {"slot": slot, "name": None, "base": None, "chain": [], "textures": []}
    if mi is None:
        return rec
    raw = []
    cur = mi
    depth = 0
    seen = set()
    while cur is not None and depth < 12:
        cname = cur.get_name()
        try:
            ccls = cur.get_class().get_name()
        except Exception:
            ccls = "?"
        rec["chain"].append("%s(%s)" % (cname, ccls))
        if cname in seen:
            break
        seen.add(cname)
        depth += 1

        if rec["name"] is None:
            rec["name"] = cname

        if ccls in ("MaterialInstanceConstant", "MaterialInstance", "MaterialInstanceDynamic"):
            _collect_instance_params(cur, raw)
            nxt = None
            try:
                nxt = cur.get_editor_property("parent")
            except Exception:
                nxt = None
            cur = nxt
            continue
        if ccls == "Material":
            rec["base"] = cname
            _collect_material_params(cur, raw)
            break
        # anything else (MaterialFunctionInterface etc.) -- stop, do not guess
        break

    # order: instance overrides first (most specific), then material defaults,
    # then unnamed dependencies.  Dedupe on (param, asset_path).
    order = {"instance_override": 0, "material_default": 1, "material_used_textures": 2}
    raw.sort(key=lambda r: order.get(str(r["source"]).split(":")[0], 9))
    seen_keys = set()
    for r in raw:
        key = (r["param"], r["asset_path"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rec["textures"].append(r)
    return rec


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def scan(game_roots):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    # force_rescan: the assets were robocopied in moments ago and may not be in
    # the project's cached AssetRegistry yet.
    try:
        ar.scan_paths_synchronous(list(game_roots), force_rescan=True)
    except Exception as exc:
        log_warn("scan_paths_synchronous failed (%s)" % exc)
    try:
        ar.wait_for_completion()
    except Exception:
        pass

    found = {"StaticMesh": [], "SkeletalMesh": [], "Texture2D": [], "TextureCube": [],
             "Texture2DArray": [], "VolumeTexture": [], "TextureRenderTarget2D": [],
             "other": {}}
    for gr in game_roots:
        try:
            data = ar.get_assets_by_path(gr, recursive=True)
        except Exception as exc:
            log_warn("get_assets_by_path(%s) failed (%s)" % (gr, exc))
            data = None
        if not data:
            data = []
            for p in unreal.EditorAssetLibrary.list_assets(gr, recursive=True, include_folder=False):
                ad = unreal.EditorAssetLibrary.find_asset_data(p)
                if ad is not None:
                    data.append(ad)
        for ad in data:
            try:
                cls = str(ad.asset_class_path.asset_name)
            except Exception:
                cls = str(ad.get_editor_property("asset_class"))
            path = "%s.%s" % (ad.package_name, ad.asset_name)
            if cls in found:
                found[cls].append(path)
            else:
                found["other"].setdefault(cls, []).append(path)

    for key in ("StaticMesh", "SkeletalMesh", "Texture2D", "TextureCube",
                "Texture2DArray", "VolumeTexture", "TextureRenderTarget2D"):
        found[key] = sorted(set(found[key]))
    for key in found["other"]:
        found["other"][key] = sorted(set(found["other"][key]))
    return found


# --------------------------------------------------------------------------- #
# manifest assembly
# --------------------------------------------------------------------------- #

MANIFEST = {
    "schema": "pharos.pack.export/v1",
    "pack": None,
    "pack_dir": None,
    "content_root": None,
    "project_name": None,
    "game_root": None,
    "game_roots": [],
    "exported_at": None,
    "engine_version": None,
    "excluded_paths": [],
    "units": {
        "source": "centimetres",
        "export": "metres",
        "up_axis_source": "Z-up",
        "up_axis_fbx": "Y-up",
        # FBX from UE already carries metre-scale geometry: Blender's FBX importer
        # at its default scale 1.0 reads a 6.33 m lantern as 6.33 m (round-trip
        # verified).  The 0.01 factor is the cm->m conversion applied *before*
        # export, kept here so the relationship is explicit.
        "blender_import_scale": 1.0,
        "source_cm_to_export_m": 0.01,
    },
    "counts": {
        "static_mesh": 0, "skeletal_mesh": 0, "texture2d": 0,
        "texturecube_skipped": 0, "fbx_written": 0, "texture_files_written": 0,
        "failures": 0, "meshes_excluded": 0, "textures_excluded": 0,
        "meshes_skipped": 0,
    },
    "meshes": [],
    "textures": [],
    "not_exported": {"texturecube": [], "other_texture_classes": []},
    "excluded": {"meshes": [], "textures": [], "by_segment": []},
    "skipped": {"meshes": [], "names": [], "patterns": [],
                "reason": "explicitly skipped by --skip-meshes/--skip-pattern: this "
                          "asset aborts the engine's FBX exporter with an uncatchable "
                          "C++ assertion, so it is never loaded or exported"},
    "failures": [],
}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    t0 = time.time()
    fbx_out = JOB["fbx_out"].replace("\\", "/")
    tex_out = JOB["tex_out"].replace("\\", "/")
    game_roots = JOB.get("game_roots") or [JOB["game_root"]]
    game_root = JOB.get("game_root") or game_roots[0]
    os.makedirs(fbx_out, exist_ok=True)
    os.makedirs(tex_out, exist_ok=True)
    # Paths in the manifest must be relative to the Exports folder (the parent of
    # FBX/ and Textures/), otherwise a consumer resolving them against the Exports
    # dir gets a dangling link.  Derive the prefixes instead of hardcoding them.
    _exp_fbx = (os.path.basename(fbx_out.rstrip("/")) or "FBX") + "/"
    _exp_tex = (os.path.basename(tex_out.rstrip("/")) or "Textures") + "/"

    MANIFEST["pack"] = JOB.get("pack_name")
    MANIFEST["pack_dir"] = fwd(JOB.get("pack_dir") or "")
    MANIFEST["content_root"] = fwd(JOB.get("content_root") or "")
    MANIFEST["project_name"] = JOB.get("project_name")
    MANIFEST["game_root"] = game_root
    MANIFEST["game_roots"] = list(game_roots)
    MANIFEST["excluded_paths"] = list(JOB.get("exclude_dirs") or DEFAULT_EXCLUDE)
    MANIFEST["engine_version"] = ".".join(
        str(unreal.SystemLibrary.get_engine_version()).split("-")[0].split(".")[:2])
    MANIFEST["engine_version_full"] = str(unreal.SystemLibrary.get_engine_version())
    MANIFEST["exported_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    log("job: %s" % json.dumps(JOB, indent=None))
    log("engine: %s" % unreal.SystemLibrary.get_engine_version())
    log("game roots: %s" % json.dumps(game_roots))
    log("exclude segments: %s" % json.dumps(MANIFEST["excluded_paths"]))

    inv = scan(game_roots)
    raw_total = (len(inv["StaticMesh"]) + len(inv["SkeletalMesh"]) + len(inv["Texture2D"])
                 + len(inv["TextureCube"]) + len(inv["Texture2DArray"])
                 + len(inv["VolumeTexture"]) + len(inv["TextureRenderTarget2D"]))
    if raw_total == 0:
        # Fail LOUDLY.  A wrong game root produces a silently empty export, which
        # is the single worst failure mode of this pipeline.
        log_warn("ZERO assets found under %s -- the game root is wrong, or the "
                 "sandbox copy is missing.  Refusing to report success."
                 % json.dumps(list(game_roots)))
    log("inventory(raw): StaticMesh=%d SkeletalMesh=%d Texture2D=%d TextureCube=%d"
        % (len(inv["StaticMesh"]), len(inv["SkeletalMesh"]),
           len(inv["Texture2D"]), len(inv["TextureCube"])))
    log("other classes: %s" % json.dumps({k: len(v) for k, v in sorted(inv["other"].items())}))

    # --- apply the EpicContent (or whatever) exclusion -------------------
    ex = {"meshes": [], "textures": [], "by_segment": list(MANIFEST["excluded_paths"])}
    kept = {}
    for cls in ("StaticMesh", "SkeletalMesh", "Texture2D", "TextureCube",
                "Texture2DArray", "VolumeTexture", "TextureRenderTarget2D"):
        keep, drop = [], []
        for p in inv[cls]:
            (drop if is_excluded(p.split(".")[0]) else keep).append(p)
        kept[cls] = keep
        if cls in ("StaticMesh", "SkeletalMesh"):
            ex["meshes"].extend(drop)
        else:
            ex["textures"].extend(drop)
    MANIFEST["excluded"]["meshes"] = ex["meshes"]
    MANIFEST["excluded"]["textures"] = ex["textures"]
    for cls in ("StaticMesh", "SkeletalMesh", "Texture2D", "TextureCube",
                "Texture2DArray", "VolumeTexture", "TextureRenderTarget2D"):
        inv[cls] = kept[cls]
    log("after exclusion: StaticMesh=%d SkeletalMesh=%d Texture2D=%d TextureCube=%d"
        % (len(inv["StaticMesh"]), len(inv["SkeletalMesh"]),
           len(inv["Texture2D"]), len(inv["TextureCube"])))
    log("excluded by segment %s: meshes=%d textures=%d"
        % (json.dumps(MANIFEST["excluded_paths"]), len(ex["meshes"]), len(ex["textures"])))

    # --- explicit skip list (LAST filter: runs BEFORE the export loop) ----
    # This is not the same thing as the exclusion above: a skipped mesh stays out
    # of counts.static_mesh/skeletal_mesh, so the manifest stays self-consistent
    # and verify_pack_export.py does not report a false count mismatch.
    meshes_before_skip = len(inv["StaticMesh"]) + len(inv["SkeletalMesh"])
    skipped = apply_mesh_skips(inv)
    MANIFEST["skipped"]["meshes"] = skipped
    MANIFEST["skipped"]["names"] = list(SKIP_NAMES)
    MANIFEST["skipped"]["patterns"] = list(SKIP_PATTERNS)
    MANIFEST["counts"]["meshes_skipped"] = len(skipped)
    if SKIP_NAMES or SKIP_PATTERNS:
        log("skip filter: names=%s patterns=%s"
            % (json.dumps(SKIP_NAMES), json.dumps(SKIP_PATTERNS)))
        log("skip filter: removed %d of %d mesh(es) before the export loop"
            % (len(skipped), meshes_before_skip))
        for p in skipped:
            log("  SKIPPED %s" % p)
        if not skipped:
            # A typo here would silently export the very asset we are trying to
            # avoid, which is exactly the run we cannot afford to burn.
            log_warn("skip filter matched NOTHING -- check --skip-meshes / "
                     "--skip-pattern against the real asset names")

    report = {
        "pack_name": JOB.get("pack_name"),
        "game_root": game_root,
        "game_roots": list(game_roots),
        "engine_version": str(unreal.SystemLibrary.get_engine_version()),
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "inventory": {k: len(v) for k, v in inv.items() if k != "other"},
        "inventory_other": {k: len(v) for k, v in sorted(inv["other"].items())},
        "excluded": {"meshes": len(ex["meshes"]), "textures": len(ex["textures"])},
        "skipped": {"meshes": len(skipped), "names": list(SKIP_NAMES),
                    "patterns": list(SKIP_PATTERNS)},
        "meshes": {"exported": [], "failed": []},
        "textures": {"exported": [], "failed": []},
    }

    MANIFEST["counts"]["static_mesh"] = len(inv["StaticMesh"])
    MANIFEST["counts"]["skeletal_mesh"] = len(inv["SkeletalMesh"])
    MANIFEST["counts"]["texture2d"] = len(inv["Texture2D"])
    MANIFEST["counts"]["texturecube_skipped"] = len(inv["TextureCube"])
    MANIFEST["counts"]["meshes_excluded"] = len(ex["meshes"])
    MANIFEST["counts"]["textures_excluded"] = len(ex["textures"])

    # ---------------- textures (FIRST: meshes need the file map) ----------------
    tex_file_by_pkg = {}     # '/Game/A/T_X' -> 'Textures/rel/T_X.png'
    tex_meta_by_pkg = {}     # '/Game/A/T_X' -> {width,height,bytes,...}
    if JOB.get("export_textures", True):
        tex_jobs = list(inv["Texture2D"])
        lim_t = int(JOB.get("limit_textures", 0) or 0)
        if lim_t:
            tex_jobs = tex_jobs[:lim_t]
            log("texture limit applied -> %d jobs" % len(tex_jobs))

        for i, path in enumerate(tex_jobs, 1):
            pkg = path.split(".")[0]
            gr = game_root_for(pkg, game_roots)
            out_dir = rel_out_dir(pkg, gr, tex_out)
            name = pkg.rsplit("/", 1)[-1]
            base = os.path.join(out_dir, name)
            t1 = time.time()
            try:
                asset = unreal.EditorAssetLibrary.load_asset(path)
            except Exception as exc:
                report["textures"]["failed"].append(
                    {"asset": path, "error": "load failed: %s" % exc})
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "texture_load", "error": "%s: %s" % (type(exc).__name__, exc)})
                log_warn("[%d/%d] TEX LOAD FAIL %s: %s" % (i, len(tex_jobs), path, exc))
                continue
            if asset is None:
                report["textures"]["failed"].append({"asset": path, "error": "load returned None"})
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "texture_load", "error": "load returned None"})
                continue

            # HDR textures need a float-capable container; everything else -> PNG.
            want = ".png"
            cs_name = None
            try:
                cs = asset.get_editor_property("compression_settings")
                cs_name = str(cs)
                if cs_name in ("TC_HDR", "TextureCompressionSettings.TC_HDR",
                               "TC_HDR_COMPRESSED", "TextureCompressionSettings.TC_HDR_COMPRESSED"):
                    want = ".exr"
            except Exception:
                pass

            ok, final, err = export_asset(asset, base, None, want)
            size = os.path.getsize(final) if os.path.isfile(final) else 0
            rec = {"asset": path, "file": final.replace("/", "\\"), "bytes": size,
                   "seconds": round(time.time() - t1, 2)}
            if ok and size > 0:
                rec["magic"] = image_magic(final)
                w, h = image_dims(final)
                rec["width"], rec["height"] = w, h
                report["textures"]["exported"].append(rec)
                rel_file = fwd(os.path.relpath(final, tex_out))
                tex_file_by_pkg[pkg] = _exp_tex + rel_file
                tex_meta_by_pkg[pkg] = {
                    "name": name, "asset_path": pkg, "file": tex_file_by_pkg[pkg],
                    "format": rec["magic"], "width": w, "height": h, "bytes": size,
                    "compression": cs_name,
                }
                log("[%d/%d] TEX OK %s -> %d bytes (%s %sx%s)"
                    % (i, len(tex_jobs), name, size, rec["magic"], w, h))
            else:
                rec["error"] = err or "unknown export failure"
                report["textures"]["failed"].append(rec)
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "texture_export", "error": rec["error"]})
                log_warn("[%d/%d] TEX FAIL %s: %s" % (i, len(tex_jobs), path, rec["error"]))

            try:
                unreal.EditorAssetLibrary.unload_asset(path)
            except Exception:
                pass
            if i % 25 == 0:
                try:
                    unreal.SystemLibrary.collect_garbage()
                except Exception:
                    pass
    else:
        log("texture export disabled by job")

    # ---------------- meshes ----------------
    if JOB.get("export_meshes", True):
        # inv[] already had the exclusion and the explicit skip list applied, so
        # neither a skipped nor an excluded asset can reach export_asset().
        mesh_jobs = [(p, "StaticMesh") for p in inv["StaticMesh"]]
        mesh_jobs += [(p, "SkeletalMesh") for p in inv["SkeletalMesh"]]
        log("mesh jobs: %d (skipped %d, excluded %d, inventory %d)"
            % (len(mesh_jobs), MANIFEST["counts"]["meshes_skipped"],
               len(ex["meshes"]), meshes_before_skip))

        lim_m = int(JOB.get("limit_meshes", 0) or 0)
        lim_s = int(JOB.get("limit_skel", 0) or 0)
        if lim_m or lim_s:
            sel = []
            n_m = n_s = 0
            for path, kind in mesh_jobs:
                if kind == "StaticMesh" and (not lim_m or n_m < lim_m):
                    sel.append((path, kind)); n_m += 1
                elif kind == "SkeletalMesh" and (not lim_s or n_s < lim_s):
                    sel.append((path, kind)); n_s += 1
            mesh_jobs = sel
            log("mesh limit applied -> %d jobs" % len(mesh_jobs))

        fbx_opts = build_fbx_options()

        for i, (path, kind) in enumerate(mesh_jobs, 1):
            pkg = path.split(".")[0]
            gr = game_root_for(pkg, game_roots)
            out_dir = rel_out_dir(pkg, gr, fbx_out)
            name = pkg.rsplit("/", 1)[-1]
            base = os.path.join(out_dir, name)
            t1 = time.time()
            try:
                asset = unreal.EditorAssetLibrary.load_asset(path)
            except Exception as exc:
                report["meshes"]["failed"].append(
                    {"asset": path, "kind": kind, "error": "load failed: %s" % exc})
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "mesh_load", "error": "%s: %s" % (type(exc).__name__, exc)})
                log_warn("[%d/%d] LOAD FAIL %s: %s" % (i, len(mesh_jobs), path, exc))
                continue
            if asset is None:
                report["meshes"]["failed"].append(
                    {"asset": path, "kind": kind, "error": "load returned None"})
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "mesh_load", "error": "load returned None"})
                log_warn("[%d/%d] LOAD NONE %s" % (i, len(mesh_jobs), path))
                continue

            # ---- engine-side geometry numbers, before the asset is unloaded ----
            if kind == "StaticMesh":
                met = static_mesh_metrics(asset)
            else:
                met = skeletal_mesh_metrics(asset)

            # ---- material slots -> textures ----
            mats = []
            try:
                if kind == "StaticMesh":
                    smats = asset.get_editor_property("static_materials")
                    for slot_i, sm in enumerate(smats or []):
                        try:
                            slot_name = str(sm.get_editor_property("material_slot_name"))
                        except Exception:
                            slot_name = None
                        try:
                            mi = sm.get_editor_property("material_interface")
                        except Exception:
                            mi = None
                        mats.append(resolve_material_link(mi, slot_i, slot_name))
                else:
                    for slot_i, sm in enumerate(asset.get_editor_property("materials") or []):
                        try:
                            mi = sm.get_editor_property("material_interface")
                        except Exception:
                            mi = None
                        mats.append(resolve_material_link(mi, slot_i, None))
            except Exception as exc:
                log_warn("material slots for %s failed: %s" % (path, exc))
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "material_slots",
                     "error": "%s: %s" % (type(exc).__name__, exc)})

            ok, final, err = export_asset(asset, base, fbx_opts, ".fbx")
            size = os.path.getsize(final) if os.path.isfile(final) else 0
            rec = {"asset": path, "kind": kind, "file": final.replace("/", "\\"),
                   "bytes": size, "seconds": round(time.time() - t1, 2)}
            if ok and size > 0:
                rec["magic"] = fbx_magic(final)
                report["meshes"]["exported"].append(rec)
                rel_fbx = fwd(os.path.relpath(final, fbx_out))
                log("[%d/%d] OK %s -> %d bytes (tri=%s vert=%s bbox=%s)"
                    % (i, len(mesh_jobs), name, size, met.get("triangles"),
                       met.get("vertices"), met.get("bbox_m")))

                # ---- attach texture file names to the material links ----
                for m in mats:
                    for t in m.get("textures", []):
                        ap = t.get("asset_path")
                        f = tex_file_by_pkg.get(ap) if ap else None
                        t["file"] = f
                        if f is None:
                            if ap is None:
                                t["note"] = "material param has no texture assigned"
                            elif is_excluded(ap):
                                t["note"] = "texture in excluded path %s" % json.dumps(MANIFEST["excluded_paths"])
                            elif not ap.startswith("/Game/"):
                                t["note"] = "texture lives outside this project (%s)" % ap.split("/")[1]
                            else:
                                t["note"] = "texture not exported"

                MANIFEST["meshes"].append({
                    "name": name,
                    "asset_path": pkg,
                    "game_root": gr,
                    "kind": kind,
                    "fbx": _exp_fbx + rel_fbx,
                    "bytes": size,
                    "triangles": met.get("triangles"),
                    "vertices": met.get("vertices"),
                    "lods": met.get("lods"),
                    "bbox_m": met.get("bbox_m"),
                    "bbox_min_m": met.get("bbox_min_m"),
                    "bbox_max_m": met.get("bbox_max_m"),
                    "bbox_source": met.get("bbox_source"),
                    "dimension_method": "engine" if kind == "StaticMesh" else None,
                    "materials": mats,
                })
            else:
                rec["error"] = err or "unknown export failure"
                report["meshes"]["failed"].append(rec)
                MANIFEST["failures"].append(
                    {"asset": path, "stage": "mesh_export", "error": rec["error"]})
                log_warn("[%d/%d] FAIL %s: %s" % (i, len(mesh_jobs), path, rec["error"]))

            try:
                unreal.EditorAssetLibrary.unload_asset(path)
            except Exception:
                pass
            if i % 25 == 0:
                try:
                    unreal.SystemLibrary.collect_garbage()
                except Exception:
                    pass
    else:
        log("mesh export disabled by job")

    # ---------------- manifest: textures + non-exported classes ----------------
    MANIFEST["textures"] = [tex_meta_by_pkg[k] for k in sorted(tex_file_by_pkg)]

    for p in inv["TextureCube"]:
        MANIFEST["not_exported"]["texturecube"].append(
            {"name": p.rsplit("/", 1)[-1].split(".")[0], "asset_path": p.split(".")[0],
             "reason": "TextureCube cannot be flattened to a single 2D image"})
    for cls in ("Texture2DArray", "VolumeTexture", "TextureRenderTarget2D"):
        for p in inv[cls]:
            MANIFEST["not_exported"]["other_texture_classes"].append(
                {"class": cls, "asset_path": p.split(".")[0], "reason": "not a Texture2D"})

    MANIFEST["counts"]["fbx_written"] = len(MANIFEST["meshes"])
    MANIFEST["counts"]["texture_files_written"] = len(MANIFEST["textures"])
    MANIFEST["counts"]["failures"] = len(MANIFEST["failures"])

    # ---------------- verification post-pass ----------------
    # Re-read every produced file from scratch.  This runs after all exporter
    # handles are closed, so it is the authoritative check that the bytes on
    # disk are real FBX / real images rather than just "a file appeared".
    def verify_tree(root, probe):
        stats = {}
        bad = []
        total = 0
        bytes_total = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                total += 1
                try:
                    bytes_total += os.path.getsize(p)
                except OSError:
                    pass
                kind = probe(p)
                stats[kind] = stats.get(kind, 0) + 1
                if kind in (None, "unreadable") or str(kind).startswith("unknown"):
                    bad.append(p.replace("/", "\\"))
        return {"files": total, "bytes": bytes_total,
                "by_format": {str(k): v for k, v in sorted(stats.items(), key=lambda x: str(x[0]))},
                "invalid": bad[:40], "invalid_count": len(bad)}

    report["verify_fbx"] = verify_tree(fbx_out, fbx_magic)
    report["verify_textures"] = verify_tree(tex_out, image_magic)
    log("verify FBX dir: %s" % json.dumps(report["verify_fbx"]["by_format"]))
    log("verify texture dir: %s" % json.dumps(report["verify_textures"]["by_format"]))

    report["exported_meshes"] = len(report["meshes"]["exported"])
    report["failed_meshes"] = len(report["meshes"]["failed"])
    report["exported_textures"] = len(report["textures"]["exported"])
    report["failed_textures"] = len(report["textures"]["failed"])
    report["elapsed_seconds"] = round(time.time() - t0, 1)

    # ---------------- write manifest ----------------
    man_path = JOB.get("manifest")
    if man_path:
        MANIFEST["elapsed_seconds"] = report["elapsed_seconds"]
        MANIFEST["ok"] = (MANIFEST["counts"]["fbx_written"] + MANIFEST["counts"]["texture_files_written"]) > 0
        os.makedirs(os.path.dirname(man_path), exist_ok=True)
        with open(man_path, "w", encoding="utf-8") as fh:
            json.dump(MANIFEST, fh, indent=1)
        log("manifest written: %s (%d bytes)" % (man_path, os.path.getsize(man_path)))
        if not MANIFEST["ok"]:
            log_warn("manifest ok=false: zero files written for this pack")

    rep_path = JOB.get("report")
    if rep_path:
        os.makedirs(os.path.dirname(rep_path), exist_ok=True)
        with open(rep_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)

    log("DONE meshes=%d/%d textures=%d/%d in %.1fs"
        % (report["exported_meshes"], report["exported_meshes"] + report["failed_meshes"],
           report["exported_textures"], report["exported_textures"] + report["failed_textures"],
           report["elapsed_seconds"]))

    if rep_path:
        with open(os.path.join(os.path.dirname(rep_path), "amconv_run.log"), "a", encoding="utf-8") as fh:
            fh.write("\n===== %s : %s =====\n" % (report["started"], JOB.get("pack_name")))
            fh.write("\n".join(LOG) + "\n")


try:
    main()
except Exception:
    unreal.log_error("[amconv] FATAL:\n%s" % traceback.format_exc())
finally:
    try:
        unreal.SystemLibrary.quit_editor()
    except Exception:
        pass
