import os, sys, json, collections, datetime, struct, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))          # classify
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # _config
import classify as C

# Root of the audio library.  Overridable, but it MUST exist -- a wrong root
# used to produce a perfectly valid-looking index that pointed at nothing.
from _config import section_root
AA = section_root("audio", "AGENT_AUDIO_ROOT")
TMP = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(AA):
    raise SystemExit("FATAL: audio root does not exist: %s" % AA)


def _wav_stats(path):
    """duration/samplerate/channels from a WAV header, best effort."""
    try:
        with wave.open(path, "rb") as w:
            return (round(w.getnframes() / max(w.getframerate(), 1), 3),
                    w.getframerate(), w.getnchannels())
    except Exception:
        return None, None, None


def _scan_audio_root():
    """Build the aa_meta cache from disk (fresh installs have no cache):
    walk via classify.walk_audio, cat/sub via the taxonomy rules."""
    C.ROOT = AA            # classify's ROOT is env-only; config wins here
    out = []
    for rel in C.walk_audio():
        full = os.path.join(C.ROOT, rel)
        (cat, sub), _score = C.classify(rel)
        dur, sr, ch = _wav_stats(full)
        out.append({"path": full, "rel": rel.replace(os.sep, "/"),
                    "cat": cat, "sub": sub,
                    "name": os.path.basename(rel),
                    "bytes": os.path.getsize(full),
                    "dur": dur, "sr": sr, "ch": ch})
    return out


_meta_path = os.path.join(TMP, "aa_meta.json")
if os.path.isfile(_meta_path):
    meta = json.load(open(_meta_path, encoding="utf-8"))
else:
    print("no aa_meta.json cache -- scanning %s (cached for next run)" % AA)
    meta = _scan_audio_root()
    json.dump(meta, open(_meta_path, "w", encoding="utf-8"))

DESC = {
    "Alarms": "Alarms, sirens, warnings, buzzers and emergency tones.",
    "Ambiance": "Continuous background/environment recordings (rooms, streets, nature beds) used as scene beds.",
    "Animals": "Animal and creature vocalizations: real mammals/birds/insects plus monsters and fantasy creatures.",
    "Explosions": "Explosions, blasts, detonations, fireworks and destruction/debris.",
    "Foley": "Performed everyday object sounds: doors, cloth, paper, tools, kitchen items, misc props.",
    "Footsteps": "Walking/running footsteps, organized by surface where known.",
    "Gunshots": "Firearm discharges organized by weapon type, plus sci-fi/energy guns.",
    "Horror": "Dark, creepy, gore and magic designed sounds for horror/scary scenes.",
    "Human": "Human voice, efforts (breaths/grunts/screams), body sounds and crowd reactions.",
    "Impacts": "Single-hit collisions and smashes, organized by material.",
    "Machines": "Mechanical, electric, robotic and steampunk devices and mechanisms.",
    "Music": "Musical material: loops/stems, instrument recordings and stingers.",
    "Nature": "Natural elements: water, fire, wind, birds, insects, small animals.",
    "SciFi": "Designed sci-fi sounds: drones, robots, textures, UI, voices, energy weapons.",
    "Transitions": "Whooshes, risers and cinematic transitions/booms for editing.",
    "UI": "Interface sounds: buttons, computer/terminal, notifications.",
    "Unsorted": "Files that could not be confidently auto-tagged. Review manually.",
    "Vehicles": "Vehicles: cars, motorcycles, boats, aircraft, trains, tanks and raw engines.",
    "Weapons": "Weapon handling: reloads, melee, bows, bullet impacts and generic handling (non-discharge).",
    "Weather": "Weather: rain, thunder, storm, snow.",
}

# keyword map from classifier rules
KW = collections.defaultdict(set)
for top, sub, w, kws in C.RULES:
    KW[(top, sub)].update(kws)

cat_sub = collections.defaultdict(lambda: collections.defaultdict(list))
cat_files = collections.defaultdict(list)
for r in meta:
    cat_sub[r["cat"]][r["sub"]].append(r)
    cat_files[r["cat"]].append(r)

def dur_stats(rows):
    ds = [r["dur"] for r in rows if r.get("dur") is not None]
    if not ds:
        return None
    return {"min_s": round(min(ds), 2), "max_s": round(max(ds), 2),
            "mean_s": round(sum(ds) / len(ds), 2), "total_s": round(sum(ds), 1)}

def samples(rows, n=4):
    rows = sorted(rows, key=lambda r: r["rel"])
    if len(rows) <= n:
        pick = rows
    else:
        idx = [0, len(rows) // 3, (2 * len(rows)) // 3, len(rows) - 1]
        pick = [rows[i] for i in idx]
    return [r["rel"] for r in pick]

categories = {}
for cat in sorted(cat_files):
    subs = {}
    kws = set()
    for sub in sorted(cat_sub[cat]):
        rows = cat_sub[cat][sub]
        kws |= KW.get((cat, sub), set())
        subs[sub] = {
            "path": f"{cat}/{sub}",
            "count": len(rows),
            "duration": dur_stats(rows),
            "example_files": samples(rows),
        }
    categories[cat] = {
        "description": DESC.get(cat, ""),
        "count": len(cat_files[cat]),
        "size_bytes": sum(r["bytes"] for r in cat_files[cat]),
        "keywords": sorted(kws),
        "subfolders": subs,
    }

total_bytes = sum(r["bytes"] for r in meta)
total_dur = sum(r["dur"] or 0 for r in meta)

index = {
    "schema": "audioclibrary-index/1",
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "root": AA,
    "purpose": "Machine-readable index of a game-audio/SFX library so agents can locate sounds fast without scanning files.",
    "read_order": [
        "1. Read this file (library_index.json) for the taxonomy, counts and keyword routing.",
        "2. Grep library_files.jsonl for specific names, durations or categories (one JSON per line).",
        "3. Open the concrete path under 'root' to use the file.",
    ],
    "totals": {
        "audio_files": len(meta),
        "categories": len(categories),
        "subfolders": sum(len(v["subfolders"]) for v in categories.values()),
        "size_bytes": total_bytes,
        "size_gb": round(total_bytes / 1e9, 2),
        "total_duration_s": round(total_dur, 1),
        "total_duration_h": round(total_dur / 3600, 2),
    },
    "file_formats": dict(collections.Counter(os.path.splitext(r["name"])[1].lower() for r in meta)),
    "categories": categories,
    "query_cookbook": {
        "by_name": 'grep -i "explosion" library_files.jsonl',
        "by_category": 'grep \'"cat":"Gunshots"\' library_files.jsonl',
        "by_subfolder": 'grep \'"sub":"Pistol"\' library_files.jsonl',
        "long_ambience_beds": 'grep \'"cat":"Ambiance"\' library_files.jsonl | filter dur > 60',
        "short_one_shots": 'filter dur < 2 for impacts/UI/gunshots',
    },
    "notes": {
        "tagging_method": "Folder-name + filename keyword rules (see keywords per category). Not audio ML.",
        "unsorted": "Files in Unsorted/Needs_Review could not be auto-tagged - inspect manually.",
        "origin": "Consolidated from Sonniss GDC 2015-2024 bundles, USBStick, Minecraft SFX, Musik.",
        "duplicates": "Exact duplicates were removed (kept newest pack); see _sort_logs/dedupe_plan.csv.",
        "non_audio": "Only audio files were moved here; PDFs/videos/instruments remained in their source folders.",
    },
    "related_logs": r"D:\Audio_Foley_FX\_sort_logs",
}
json.dump(index, open(os.path.join(AA, "library_index.json"), "w", encoding="utf-8"),
          ensure_ascii=True, indent=1)

with open(os.path.join(AA, "library_files.jsonl"), "w", encoding="utf-8") as fh:
    for r in meta:
        fh.write(json.dumps({
            "p": r["rel"], "cat": r["cat"], "sub": r["sub"],
            "ext": os.path.splitext(r["name"])[1].lower(),
            "bytes": r["bytes"], "dur": r.get("dur"),
            "sr": r.get("sr"), "ch": r.get("ch"),
        }, ensure_ascii=True) + "\n")

print("categories:", len(categories))
print("wrote library_index.json, library_files.jsonl")
