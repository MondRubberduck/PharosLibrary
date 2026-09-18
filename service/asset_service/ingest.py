"""pharos ingest — run every SAFE import chain in the right order, then
hand the decision-y chains to the user as questions.

    python pharos.py ingest [--dry-run]

What runs AUTOMATICALLY (no engine, no consent needed):
    scanner.py   on every detected model folder AND on the audio section
                 (when no crawl index exists yet)
    indexer.py   on the library root (animation clip packs)
    importers    collection / textures / audio / meshes (same order the
                 server uses at startup)
    docs         AGENT_START_HERE.md / AGENT_API.md with live counts

What NEVER runs automatically (engines, hours, or judgment calls) --
these are printed as QUESTIONS for the running agent to relay:
    chain 1      UE pack crawl (needs UE 5.x + the local sandbox)
    chain 2      KitBash3D kit export (needs Blender; restructures nothing)
    chain 3      .blend / native container enumeration (needs Blender)
    chain 4      crawl-quality audio/texture indexes (needs a vision crawl)

Safety: same rules as the app -- the library is read-only except
_Agent_Files/ and Exports/; the registry is derived data; scanner rows
survive every rebuild.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import config
from .init import detect

REPO = str(Path(__file__).resolve().parents[2])


def _run(cmd: list[str], dry: bool) -> int:
    print("\n> " + " ".join(cmd), flush=True)
    if dry:
        print("  (dry-run: skipped)", flush=True)
        return 0
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"  WARNING: exit {r.returncode} -- continuing with the "
              f"remaining steps", flush=True)
    return r.returncode


def run_ingest(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[2:]
    ap = argparse.ArgumentParser(
        prog="pharos ingest",
        description="run the safe import chains in order, print the "
                    "decision questions for the user")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact commands without running them")
    args = ap.parse_args(argv)
    dry = args.dry_run

    cfg = config.load()
    lib = (cfg.get("library_root") or "").strip()
    if not cfg.get("library_root") or not cfg.get("registry_dir"):
        print("pharos_config.json is not configured yet -- run "
              f'pharos.py init <assets-root> first, or run '
              f'pharos.py doctor to see what is missing')
        return 2
    root = Path(lib)
    py = sys.executable

    print(f"pharos ingest -- library: {root}"
          f"{'  (DRY RUN)' if dry else ''}")
    report = detect(root)
    picks = report["sections"]
    print(f"  sections detected: "
          f"animation={picks.get('animation', ['-'])[0] if picks.get('animation') else '-'} | "
          f"textures={picks.get('textures', ['-'])[0] if picks.get('textures') else '-'} | "
          f"audio={picks.get('audio', ['-'])[0] if picks.get('audio') else '-'}")
    if report["mesh_folders"]:
        print(f"  model folders     : {', '.join(report['mesh_folders'])}")
    if report.get("manifest_roots"):
        print(f"  manifest packs    : "
              f"{len(report.get('manifest_packs_found', []))} "
              f"under {', '.join(report['manifest_roots'])}")
    if report.get("blend_folders"):
        print(f"  blend folders     : {', '.join(report['blend_folders'])}")

    # ---- automatic chains -------------------------------------------------
    from asset_service import collection_import, textures_import, \
        audio_import, meshes_import, agent_docs

    for folder in report["mesh_folders"]:
        _run([py, str(Path(REPO) / "service" / "asset_service" /
                      "scanner.py"), str(root / folder)], dry)

    audio_jsonl = (root / (picks.get("audio", [""])[0])
                   / "library_files.jsonl") if picks.get("audio") else None
    if picks.get("audio"):
        if audio_jsonl and audio_jsonl.is_file():
            print(f"\n  audio: crawl index found ({audio_jsonl.name}) -- "
                  f"skipping the folder scan (the crawl's own metadata is "
                  f"richer; scanner rows survive regardless)")
        else:
            _run([py, str(Path(REPO) / "service" / "asset_service" /
                          "scanner.py"),
                  str(root / picks["audio"][0])], dry)

    if picks.get("animation"):
        _run([py, str(Path(REPO) / "service" / "asset_service" /
                      "indexer.py"), "--root", str(root)], dry)
    else:
        print("\n  animation: no clip folder detected -- skipping indexer")

    print("", flush=True)
    if not dry:
        c_n = collection_import.import_collection(config.DB_PATH)
        print(f"  collection imported: {c_n}", flush=True)
        t_n = textures_import.import_textures(config.DB_PATH)
        print(f"  textures imported: {t_n}", flush=True)
        a_n = audio_import.import_audio(config.DB_PATH)
        print(f"  audio imported: {a_n}", flush=True)
        m_n = meshes_import.import_meshes(config.DB_PATH)
        print(f"  meshes imported: {m_n}", flush=True)
        for p in agent_docs.generate(config.DB_PATH):
            print(f"  wrote {p}", flush=True)

    # ---- decisions: agent relays these to the user ------------------------
    print("\n" + "-" * 24 + " ASK YOUR USER " + "-" * 24)
    print("  (setup is not complete until these are answered; every 'yes'")
    print("   needs the engine named next to it -- see pipeline/README.md)")
    qs = []
    qs.append("1. Are these ALL your asset folders? (other drives / "
              "external disks?) detected so far:")
    qs.append(f"   {root}")
    if report.get("manifest_packs_found"):
        qs.append(f"2. Crawl the {len(report['manifest_packs_found'])} Unreal "
                  f"pack(s) for material recipes? (needs UE 5.x + "
                  f"pipeline/conversion sandbox; chain 1)")
    else:
        qs.append("2. Any Unreal .uasset packs to convert? (none detected; "
                  "chain 1 needs UE 5.x)")
    if report.get("kitbash_root") or report.get("blend_folders"):
        qs.append("3. Export KitBash3D kits to per-assembly FBX and/or "
                  "enumerate .blend containers? (needs Blender; chains 2+3; "
                  f"blend folders: {', '.join(report.get('blend_folders', [])) or 'none'})")
    else:
        qs.append("3. Any .blend kits/containers to index, or leave them "
                  "as-is? (none detected; chains 2+3 need Blender)")
    qs.append("4. Is there a purchase CSV for the collection? (ground "
              "truth for owned vs on-disk)")
    qs.append("5. Audio/texture crawl-quality indexes (categories, "
              "keywords) need a vision crawl pass -- run it, or ship with "
              "scanner-only metadata?")
    for q in qs:
        print("  " + q)
    print("-" * 66)
    print("\nnext steps:")
    print(f'  verify setup   : python "{REPO}/pharos.py" doctor')
    print(f'  start serving  : python "{REPO}/pharos.py" serve --no-open')
    print("  build prompts  : docs/STARTING_PROMPT.md + "
          "docs/AGENT_SETUP_BRIEF.md")
    return 0
