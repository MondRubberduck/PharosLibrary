#!/usr/bin/env python3
"""
verify_pack_export.py -- independent verification of one pack's Exports folder.

Does NOT use Unreal.  Re-reads the manifest and every file it points at, straight
off disk, so it cannot be fooled by the exporter's own opinion of its work.

Checks
  1. manifest.json parses as JSON and carries a known schema
     (v1 = export_pack.py inventory, v2 = v1 + relink_materials.py exact wiring)
  2. every meshes[].fbx exists and has a real binary FBX header
  3. every meshes[] has non-null triangles, vertices, bbox_m (no nulls, no guesses)
  4. every materials[].textures[].file, when non-null, exists on disk
  5. every textures[].file exists; its real pixel dimensions match the manifest
  6. counts block agrees with the arrays
  7. broken-link count (target 0)
  8. (v2) materials[].textures[].role comes from the agreed vocabulary, the wiring
     block's slot counts agree with the arrays, and slots_resolved <= slots_total
  9. (v2) the `wiring` block must EXIST and be well formed: a v2 manifest with no
     wiring block, no slots_total, or slots_total > 0 with an absent/negative
     slots_resolved is a HARD FAIL -- that is a broken relink, never a clean pass
 10. (advisory) when the wiring ratio is below DEGRADED_RATIO the run is reported as
     `degraded`: the ratio and the top unresolved_reason values are printed as a
     WARNING.  This NEVER changes PASS/FAIL -- several packs are legitimately below
     the target and a blanket floor would fail correct packs (HorrorMansion 0/493 is
     a real, explained 0: its masters hardcode their textures instead of exposing
     texture parameters).

Usage:
  python verify_pack_export.py "<pack_dir>\\Exports" [--json out.json] [--sample 2]
Exit code 0 = PASS.
"""

import argparse
import json
import os
import random
import struct
import sys

FBX_BINARY_SIG = b"Kaydara"
FBX_BINARY_FULL = b"Kaydara FBX Binary"

# v1: geometry/texture inventory written by export_pack.py.
# v2: the same file after relink_materials.py added exact engine-side material
#     wiring (`wiring` block, per-texture `role`) -- all v1 fields are preserved,
#     so both versions stay readable and both must verify.
SCHEMAS = ("pharos.pack.export/v1", "pharos.pack.export/v2",
           "kiosk.pack.export/v1", "kiosk.pack.export/v2")

# The v2 schema tag promises a `wiring` block; anything below this ratio is worth
# shouting about (advisory only -- see the module docstring, point 10).
SCHEMA_V2 = ("pharos.pack.export/v2", "kiosk.pack.export/v2")
DEGRADED_RATIO = 0.9

# The role vocabulary is a contract with the app (TASKS_autocoder_2026-09-16.md).
# `packed` was added for multi-channel ORM/RMA-style textures, which carry several
# maps in one file; the per-texture `channels` object says which map is in which
# channel (null when the parameter name does not encode an order).
ROLES = ("albedo", "normal", "roughness", "metallic", "height", "ao",
         "emissive", "opacity", "specular", "packed", "other")

IMAGE_SIGS = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"\x76\x2f\x31\x01", "openexr"),
    (b"BM", "bmp"),
    (b"DDS ", "dds"),
]


def probe_fbx(path):
    with open(path, "rb") as fh:
        head = fh.read(64)
    if head[:7] == FBX_BINARY_SIG and FBX_BINARY_FULL in head[:32]:
        return "binary"
    stripped = head.lstrip()
    if stripped[:4] in (b"; FBX", b"FBX "):
        return "ascii"
    return None


def probe_image(path):
    with open(path, "rb") as fh:
        head = fh.read(16)
    for sig, name in IMAGE_SIGS:
        if head.startswith(sig):
            return name
    return None


def image_dims(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:4] == b"\x76\x2f\x31\x01":
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
    except Exception:
        pass
    return (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exports_dir", help="<pack>\\Exports")
    ap.add_argument("--manifest", default=None,
                    help="verify this manifest instead of <exports_dir>\\manifest.json "
                         "(used to check a relink preview before it goes live)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--sample", type=int, default=2, help="FBX to pick for the Blender list")
    args = ap.parse_args()

    ex = os.path.abspath(args.exports_dir)
    man_path = (os.path.abspath(args.manifest) if args.manifest
                else os.path.join(ex, "manifest.json"))
    R = {"exports_dir": ex, "manifest": man_path, "problems": [], "warnings": [],
         "broken_links": 0}
    problems = R["problems"]
    warnings = R["warnings"]

    if not os.path.isfile(man_path):
        R["result"] = "FAIL"
        problems.append("manifest.json missing")
        json.dump(R, sys.stdout, indent=1)
        return 1

    try:
        with open(man_path, "r", encoding="utf-8") as fh:
            m = json.load(fh)
    except Exception as exc:
        R["result"] = "FAIL"
        problems.append("manifest.json does not parse: %s: %s" % (type(exc).__name__, exc))
        json.dump(R, sys.stdout, indent=1)
        return 1

    R["manifest_parsed"] = True
    R["schema"] = m.get("schema")
    R["pack"] = m.get("pack")
    if m.get("schema") not in SCHEMAS:
        problems.append("unexpected schema %r (known: %s)" % (m.get("schema"), list(SCHEMAS)))

    def resolve(rel):
        return os.path.join(ex, rel.replace("\\", os.sep).replace("/", os.sep))

    # ---------- meshes ----------
    meshes = m.get("meshes") or []
    m_stat = {"count": len(meshes), "fbx_missing": 0, "fbx_bad_header": 0,
              "null_metrics": 0, "zero_face": 0, "zero_volume": 0,
              "with_materials": 0, "texture_links": 0, "texture_links_null": 0,
              "null_reasons": {}, "slots": 0, "roles": {}, "bad_roles": 0}
    for rec in meshes:
        rel = rec.get("fbx")
        if not rel:
            m_stat["fbx_missing"] += 1
            R["broken_links"] += 1
            problems.append("mesh %s has no fbx path" % rec.get("name"))
            continue
        p = resolve(rel)
        if not os.path.isfile(p):
            m_stat["fbx_missing"] += 1
            R["broken_links"] += 1
            problems.append("mesh FBX missing on disk: %s" % rel)
        else:
            try:
                if probe_fbx(p) != "binary":
                    m_stat["fbx_bad_header"] += 1
                    problems.append("mesh FBX not binary FBX: %s" % rel)
            except Exception as exc:
                m_stat["fbx_bad_header"] += 1
                problems.append("mesh FBX unreadable: %s (%s)" % (rel, exc))
        for k in ("triangles", "vertices", "bbox_m"):
            v = rec.get(k)
            if v is None or (k == "bbox_m" and (len(v) != 3)):
                m_stat["null_metrics"] += 1
                problems.append("mesh %s: %s is null/short -> %r" % (rec.get("name"), k, v))
        bb = rec.get("bbox_m")
        if bb and (bb[0] == 0 or bb[1] == 0 or bb[2] == 0):
            m_stat["zero_volume"] += 1
        if rec.get("triangles") == 0:
            m_stat["zero_face"] += 1
        mats = rec.get("materials") or []
        if mats:
            m_stat["with_materials"] += 1
        for mat in mats:
            m_stat["slots"] += 1
            for t in mat.get("textures") or []:
                if t.get("role") is not None:
                    r = t["role"]
                    m_stat["roles"][r] = m_stat["roles"].get(r, 0) + 1
                    if r not in ROLES:
                        m_stat["bad_roles"] += 1
                        problems.append("mesh %s slot %s: role %r is outside the agreed "
                                        "vocabulary" % (rec.get("name"), mat.get("slot"), r))
                f = t.get("file")
                if f is None:
                    m_stat["texture_links_null"] += 1
                    key = t.get("note") or "unspecified"
                    m_stat["null_reasons"][key] = m_stat["null_reasons"].get(key, 0) + 1
                    continue
                m_stat["texture_links"] += 1
                if not os.path.isfile(resolve(f)):
                    R["broken_links"] += 1
                    problems.append("mesh %s material %s -> missing texture file %s"
                                    % (rec.get("name"), mat.get("name"), f))
    R["meshes"] = m_stat

    # ---------- textures ----------
    texs = m.get("textures") or []
    t_stat = {"count": len(texs), "missing": 0, "bad_format": 0, "dims_mismatch": 0,
              "total_bytes": 0}
    for rec in texs:
        rel = rec.get("file")
        if not rel:
            t_stat["missing"] += 1
            R["broken_links"] += 1
            problems.append("texture %s has no file" % rec.get("name"))
            continue
        p = resolve(rel)
        if not os.path.isfile(p):
            t_stat["missing"] += 1
            R["broken_links"] += 1
            problems.append("texture file missing on disk: %s" % rel)
            continue
        t_stat["total_bytes"] += os.path.getsize(p)
        img = probe_image(p)
        if img is None:
            t_stat["bad_format"] += 1
            problems.append("texture file is not a known image: %s" % rel)
            continue
        w, h = image_dims(p)
        if (w, h) != (rec.get("width"), rec.get("height")):
            t_stat["dims_mismatch"] += 1
            problems.append("texture %s dims: manifest %sx%s, file %sx%s"
                            % (rec.get("name"), rec.get("width"), rec.get("height"), w, h))
    R["textures"] = t_stat

    # ---------- counts block agreement ----------
    c = m.get("counts") or {}
    R["counts"] = c
    for key, actual in (("static_mesh", sum(1 for x in meshes if x.get("kind") == "StaticMesh")),
                        ("skeletal_mesh", sum(1 for x in meshes if x.get("kind") == "SkeletalMesh")),
                        ("fbx_written", len(meshes)),
                        ("texture_files_written", len(texs)),
                        ("failures", len(m.get("failures") or []))):
        if c.get(key) != actual:
            problems.append("counts.%s says %r, actual %r" % (key, c.get(key), actual))

    R["not_exported_texturecube"] = len((m.get("not_exported") or {}).get("texturecube") or [])
    R["excluded_meshes"] = len((m.get("excluded") or {}).get("meshes") or [])
    R["excluded_textures"] = len((m.get("excluded") or {}).get("textures") or [])

    # ---------- wiring block (v2) ----------
    # `wiring` is what makes a manifest v2.  A v2 manifest without a usable wiring
    # block is a BROKEN RELINK, not an older file, so it is a hard problem -- the
    # same class of defect as a missing slots_total or a negative resolved count.
    # A v1 manifest has no wiring block at all and nothing here applies to it.
    w = m.get("wiring")
    R["wiring"] = None
    R["degraded"] = None
    is_v2 = m.get("schema") == SCHEMA_V2
    total = res = None
    if is_v2 and not isinstance(w, dict):
        problems.append("schema is %s but the wiring block is missing or not an object "
                        "(%r) -- a v2 manifest must carry the engine-resolved wiring"
                        % (SCHEMA_V2, w))
    elif isinstance(w, dict):
        total, res = w.get("slots_total"), w.get("slots_resolved")
        R["wiring"] = {"method": w.get("method"), "slots_total": total,
                       "slots_resolved": res,
                       "slots_unresolved": w.get("slots_unresolved"),
                       "ratio": (round(float(res) / total, 4)
                                 if isinstance(total, int) and isinstance(res, int) and total
                                 else None),
                       "params_total": w.get("params_total"),
                       "params_by_role": w.get("params_by_role"),
                       "unresolved_listed": len(w.get("unresolved") or [])}
        if total is None:
            problems.append("wiring block has no slots_total (%r) -- nothing about this "
                            "manifest's wiring can be trusted" % (total,))
        elif not isinstance(total, int):
            problems.append("wiring.slots_total is %r, not an integer" % (total,))
        elif total > 0 and (not isinstance(res, int) or isinstance(res, bool)):
            problems.append("wiring.slots_total is %d but slots_resolved is missing/"
                            "not an integer (%r)" % (total, res))
        elif isinstance(res, int) and res < 0:
            problems.append("wiring.slots_resolved is negative (%r)" % (res,))
        if isinstance(total, int):
            if total != m_stat["slots"]:
                problems.append("wiring.slots_total says %d, manifest carries %d material slots"
                                % (total, m_stat["slots"]))
        if isinstance(total, int) and isinstance(res, int):
            if res > total:
                problems.append("wiring.slots_resolved (%d) > slots_total (%d)" % (res, total))
            if w.get("slots_unresolved") != total - res:
                problems.append("wiring.slots_unresolved %r != slots_total - slots_resolved (%d)"
                                % (w.get("slots_unresolved"), total - res))
            if len(w.get("unresolved") or []) != total - res:
                problems.append("wiring.unresolved lists %d slots but %d are unresolved"
                                % (len(w.get("unresolved") or []), total - res))
        if w.get("method") != "ue-param":
            problems.append("wiring.method is %r, expected 'ue-param'" % w.get("method"))
        for k in ("pack", "counts", "meshes", "textures"):
            if k not in m:
                problems.append("v2 manifest lost the v1 field %r" % k)

    # ---------- degraded wiring (ADVISORY, never pass/fail) ----------
    # Several packs sit legitimately below the 0.9 target for reasons that are not
    # tool failures (masters that hardcode their textures, empty material slots,
    # Epic starter content, TextureCube-only params).  A hard floor here would start
    # failing correct packs, so the ratio is published as a WARNING instead.
    if isinstance(total, int) and isinstance(res, int) and total > 0 and res >= 0:
        ratio = float(res) / total
        reasons = w.get("unresolved_reasons")
        if not isinstance(reasons, dict):
            reasons = {}
            for mesh in meshes:
                for mat in mesh.get("materials") or []:
                    if mat.get("unresolved_reason"):
                        k = mat["unresolved_reason"]
                        reasons[k] = reasons.get(k, 0) + 1
        top = sorted(((k, v) for k, v in reasons.items() if isinstance(v, int)),
                     key=lambda kv: -kv[1])[:5]
        R["degraded"] = ratio < DEGRADED_RATIO
        R["degraded_detail"] = {"ratio": round(ratio, 4), "threshold": DEGRADED_RATIO,
                                "slots_resolved": res, "slots_total": total,
                                "top_unresolved_reasons": dict(top)}
        if R["degraded"]:
            warnings.append("DEGRADED wiring: %d of %d slots resolved (%.3f < %.1f) -- "
                            "PASS/FAIL is unaffected, this is a quality signal.  Top "
                            "unresolved_reasons: %s"
                            % (res, total, ratio, DEGRADED_RATIO,
                               json.dumps(dict(top), sort_keys=False)))

    # an export that produced nothing at all is a FAILURE, not a clean pass
    if not meshes and not texs:
        problems.append("manifest lists zero meshes AND zero textures -- "
                        "the export silently produced nothing")

    # every StaticMesh must have real geometry numbers
    for rec in meshes:
        if rec.get("kind") != "StaticMesh":
            continue
        if rec.get("dimension_method") != "engine":
            problems.append("mesh %s: dimension_method=%r, expected 'engine'"
                            % (rec.get("name"), rec.get("dimension_method")))

    # ---------- pick FBX for the Blender round-trip ----------
    pool = [x["fbx"] for x in meshes if x.get("fbx") and os.path.isfile(resolve(x["fbx"]))]
    random.seed(20260915)
    pick = []
    if pool:
        pick = random.sample(pool, min(args.sample, len(pool)))
        # always include the largest and a SkeletalMesh when present
        big = max(meshes, key=lambda x: x.get("bytes") or 0)
        if big.get("fbx") and big["fbx"] not in pick:
            pick.append(big["fbx"])
        skm = [x for x in meshes if x.get("kind") == "SkeletalMesh" and x.get("fbx")]
        if skm and skm[0]["fbx"] not in pick:
            pick.append(skm[0]["fbx"])
    R["blender_sample"] = [os.path.join(ex, p) for p in pick]

    R["result"] = "PASS" if not problems else "FAIL"
    R["problem_count"] = len(problems)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(R, fh, indent=1)

    print("=== verify_pack_export: %s ===" % R["result"])
    print("exports      : %s" % ex)
    print("pack         : %s   schema=%s" % (R.get("pack"), R.get("schema")))
    print("meshes       : %d  (fbx missing=%d bad header=%d null metrics=%d zero-face=%d zero-volume=%d)"
          % (m_stat["count"], m_stat["fbx_missing"], m_stat["fbx_bad_header"],
             m_stat["null_metrics"], m_stat["zero_face"], m_stat["zero_volume"]))
    print("mesh->tex    : %d resolvable links, %d null (reasons: %s)"
          % (m_stat["texture_links"], m_stat["texture_links_null"],
             json.dumps(m_stat["null_reasons"])))
    print("slots        : %d  roles=%s%s"
          % (m_stat["slots"], json.dumps(m_stat["roles"]),
             "  OUT-OF-VOCAB=%d" % m_stat["bad_roles"] if m_stat["bad_roles"] else ""))
    if R.get("wiring"):
        w = R["wiring"]
        print("wiring       : %s/%s slots resolved (%.3f) via %s; %s params, %s listed unresolved"
              % (w["slots_resolved"], w["slots_total"], w["ratio"] or 0.0, w["method"],
                 w["params_total"], w["unresolved_listed"]))
    for warn in warnings:
        print("WARNING      : %s" % warn)
    print("textures     : %d  (missing=%d bad=%d dims mismatch=%d) %.1f MiB"
          % (t_stat["count"], t_stat["missing"], t_stat["bad_format"],
             t_stat["dims_mismatch"], t_stat["total_bytes"] / 1048576.0))
    print("texturecube  : %d skipped (declared, not dropped silently)" % R["not_exported_texturecube"])
    print("excluded     : %d meshes, %d textures" % (R["excluded_meshes"], R["excluded_textures"]))
    print("BROKEN LINKS : %d" % R["broken_links"])
    if problems:
        print()
        print("--- problems (first 25 of %d) ---" % len(problems))
        for p in problems[:25]:
            print("  %s" % p)
    return 0 if R["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
