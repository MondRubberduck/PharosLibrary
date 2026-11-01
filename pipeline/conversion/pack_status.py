"""The conversion driver's verdict for one pack (step 7 of convert_packs.sh).

    python pack_status.py <manifest.json> <verify.json> <status.json> <status.log>
                          <pack> <label> <ue_rc> <ue_elapsed_s> <verify_rc>

PASS means: the verifier passed and every mesh the engine was asked for was
exported. Textures the engine's exporter refused (no PNG exporter for an
AO or pivot map, a load that failed) are GAPS, listed in the status, not a
failed pack: the FBX and the recipes are there, and a FAIL made
`pharos.py convert` skip the relink, so a pack with 13 missing textures out
of 706 had no material recipes at all."""
import datetime
import json
import os
import sys

MESH_STAGES = {"mesh_load", "mesh_export"}


def verdict_of(manifest: dict, verify: dict) -> tuple:
    """(status, mesh failures, texture failures, other failures)."""
    fails = manifest.get("failures") or []
    if not isinstance(fails, list):
        fails = []
    mesh = [f for f in fails if (f.get("stage") or "") in MESH_STAGES or not f.get("stage")]
    tex = [f for f in fails if (f.get("stage") or "").startswith("texture")]
    other = [f for f in fails if f not in mesh and f not in tex]
    status = "PASS" if (verify.get("result") == "PASS" and not mesh) else "FAIL"
    return status, mesh, tex, other


def main(argv) -> int:
    man_p, ver_p, st_p, log_p, pack, label, ue_rc, ue_el, ver_rc = argv[:9]
    m = json.load(open(man_p, encoding="utf-8"))
    try:
        v = json.load(open(ver_p, encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        v = {}
    c = m.get("counts") or {}
    status, mesh_fail, tex_fail, other = verdict_of(m, v)
    st = {
        "pack": pack, "label": label,
        "status": status,
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "exports_dir": os.path.dirname(os.path.abspath(man_p)),
        "manifest": os.path.abspath(man_p),
        "game_root": m.get("game_root"), "game_roots": m.get("game_roots"),
        "content_root": m.get("content_root"),
        "counts": c,
        "broken_links": v.get("broken_links"),
        "mesh_stats": v.get("meshes"),
        "texture_stats": v.get("textures"),
        "ue_exit_code": int(ue_rc), "ue_elapsed_s": int(ue_el),
        "verify_exit_code": int(ver_rc),
        "engine_elapsed_s": m.get("elapsed_seconds"),
        "not_exported_texturecube": len((m.get("not_exported") or {}).get("texturecube") or []),
        "failures": m.get("failures") or [],
        # what PASS leaves out: textures the exporter refused, and other
        # non-mesh steps that failed; the pack is usable without them
        "gaps": {"textures_failed": len(tex_fail), "other_failures": len(other),
                 "meshes_failed": len(mesh_fail),
                 "textures": [f.get("asset") for f in tex_fail][:100],
                 # meshes whose FBX holds far more geometry than the manifest
                 # says (Nanite source): the verifier's advisory
                 "geometry_differs": len(v.get("geometry_mismatch") or [])},
    }
    json.dump(st, open(st_p, "w", encoding="utf-8"), indent=1)
    line = ("%s  %-52s %-4s  sm=%-5s skm=%-3s tex=%-5s cube=%-3s fbx=%-5s tfiles=%-5s "
            "meshfail=%-3s texfail=%-3s broken=%-3s eng=%ss  ue_rc=%s") % (
        st["at"], pack[:52], status,
        c.get("static_mesh"), c.get("skeletal_mesh"), c.get("texture2d"),
        c.get("texturecube_skipped"), c.get("fbx_written"),
        c.get("texture_files_written"), len(mesh_fail), len(tex_fail), v.get("broken_links"),
        m.get("elapsed_seconds"), ue_rc)
    with open(log_p, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)
    geo = len(v.get("geometry_mismatch") or [])
    if status == "PASS" and (tex_fail or other or geo):
        print("PASS with gaps: %d texture(s) the engine could not export, %d other step(s) "
              "failed, %d mesh(es) whose FBX holds more geometry than the manifest says "
              "-- listed under 'gaps' in %s" % (len(tex_fail), len(other), geo, st_p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
