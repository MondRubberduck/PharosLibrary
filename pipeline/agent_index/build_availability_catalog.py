from pathlib import Path
"""
build_availability_catalog.py

Builds the agent-facing catalogue the owner asked for: one list that covers
  * what is ON DISK right now, and
  * what is OWNED BUT NOT DOWNLOADED (gallery + product URL only)
so an agent can reason about "I could have access to this" instead of treating
non-local assets as non-existent.

Reads the human app's registry READ-ONLY. Writes only into _Agent_Files.
"""
import sqlite3, os, re, io, json, collections, datetime

DB = r"D:\Pipeline\kiosk\registry\assets.sqlite"
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _config import library_root
LIB = library_root() or "."
AGENT = os.path.join(LIB, "_Agent_Files")
MODEL_EXT = {".fbx", ".obj", ".usd", ".usda", ".usdc", ".blend", ".max"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

STOP = {"the", "and", "of", "a", "in", "for", "vol", "pack", "megapack", "set",
        "collection", "assets", "kit", "free"}


def tokens(s):
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t and t not in STOP and len(t) > 2}


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# manual aliases the fuzzy matcher cannot bridge (verified by hand)
ALIAS = {
    # The download renamed several KitBash3D folders, so an alias must always be
    # keyed to the CURRENT folder name.  Only the cases that naive matching
    # cannot bridge belong here.
    "minikitneocity": "neocity",                        # "Mini Kit: Neo City" -> NeoCity
    "orientalbuilding": "oriantelbuilding",             # typo in the pack itself
    "abandonedsubwaystationinberlin": "subwaystation",
}


def scan_disk():
    sections = {}
    for top in sorted(os.listdir(LIB)):
        root = os.path.join(LIB, top)
        if not os.path.isdir(root) or top.startswith("."):
            continue
        folders = []
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isdir(p):
                continue
            models = 0
            for dp, dn, fn in os.walk(p):
                for f in fn:
                    if os.path.splitext(f)[1].lower() in MODEL_EXT:
                        models += 1
            folders.append({"name": d, "path": p, "model_files": models})
        sections[top] = folders
    return sections


def match_on_disk(name, sections):
    """Return (section, folder, model_files, score) for the best disk match."""
    n, tk = norm(name), tokens(name)
    # key the alias off the SAME normaliser used for matching, otherwise a
    # non-ASCII dash in a product name defeats the replace-chain and the alias
    # silently stops applying.
    if n in ALIAS:
        n = ALIAS[n]
    best = None
    for sec, folders in sections.items():
        for f in folders:
            if f["model_files"] <= 0:
                continue
            fn, ftk = norm(f["name"]), tokens(f["name"])
            score = 0
            # Exact normalised equality is trustworthy.  Substring matching is only
            # allowed when BOTH sides are long enough to be specific -- otherwise a
            # short folder name like "SciFi" matches any product with "scifi" in its
            # title, which produced false 'local' verdicts for unrelated packs.
            if n and fn and n == fn:
                score = 3
            elif n and fn and min(len(n), len(fn)) >= 8 and (n in fn or fn in n):
                score = 3
            elif tk and ftk and tk == ftk:
                score = 3
            elif tk and ftk:
                jac = len(tk & ftk) / float(len(tk | ftk))
                if jac >= 0.6 and len(tk & ftk) >= 2:
                    score = 2
            if score and (best is None or score > best[3]):
                best = (sec, f["name"], f["model_files"], score)
    return best


sections = scan_disk()
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
cur = c.cursor()
cur.execute("""select name, store, type_group, type_raw, seller, price, purchased,
                      image_count, folder, url, tags from collection""")
rows = cur.fetchall()

# pack-level truth from the registry (targets/formats) for on-disk correspondence
cur.execute("select id, name, formats, targets, vendor, style, domain, sub_category from assets")
reg_packs = []
for pid, name, fmts, targets, vendor, style, domain, sub in cur.fetchall():
    reg_packs.append({"id": pid, "name": name, "formats": fmts, "targets": targets,
                      "vendor": vendor, "style": style, "domain": domain, "sub": sub})

now = datetime.datetime.now().isoformat(timespec="seconds")
records = []
counts = collections.Counter()

for name, store, tg, traw, seller, price, purchased, imgs, folder, url, tags in rows:
    hit = match_on_disk(name, sections)
    if hit:
        sec, fol, models, score = hit
        state = "local"
        local_path = os.path.join(LIB, sec, fol)
        counts["local"] += 1
    else:
        sec = fol = None
        models = 0
        score = 0
        state = "owned-not-downloaded"
        local_path = None
        counts["owned-not-downloaded"] += 1
    records.append({
        "name": name, "store": store, "type_group": tg, "type_raw": traw,
        "seller": seller, "availability": state,
        "local_path": local_path, "local_model_files": models, "match_confidence": score,
        "gallery_dir": folder, "gallery_images": imgs, "product_url": url,
        "download_hint": ("already on disk" if state == "local"
                          else "OWNED BUT NOT LOCAL - re-download from product_url, then convert"),
    })

# on-disk sections that are NOT part of the collection (already-local extras)
local_only = [{"name": s, "availability": "local", "folders": len(fs),
               "model_files": sum(f["model_files"] for f in fs)}
              for s, fs in sections.items()
              if sum(f["model_files"] for f in fs) > 0]

json.dump({"schema": "pharos.agent.availability/v1", "generated": now, "root": LIB,
           "note": ("availability is the key field: 'local' = usable now; "
                    "'owned-not-downloaded' = owned with gallery + product URL, "
                    "the agent may request it"),
           "counts": dict(counts), "items": records, "local_sections": local_only},
          io.open(os.path.join(AGENT, "availability.json"), "w", encoding="utf-8"),
          indent=1, ensure_ascii=False)

with io.open(os.path.join(AGENT, "availability.jsonl"), "w", encoding="utf-8") as fh:
    for r in records:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")

# ---- readable index ------------------------------------------------------
L = []
A = L.append
A("# Asset Availability - Agent Index")
A("")
A("> Machine-readable companions: **`availability.jsonl`** (one JSON per owned item) and")
A("> **`availability.json`** (same data + counts + on-disk sections).")
A("> Generated %s | Root: `%s`" % (now, LIB))
A("")
A("## Read this first")
A("The library has TWO kinds of asset and an agent must never confuse them:")
A("")
A("| `availability` | Meaning | What an agent may do |")
A("|---|---|---|")
A("| `local` | present on disk now | use it directly |")
A("| `owned-not-downloaded` | **owned, gallery + product URL only** | plan around it, and ASK THE HUMAN to download it |")
A("")
A("A pack that is `owned-not-downloaded` is **not** missing from the library. It is one")
A("download away. The gallery images in `gallery_dir` are there so an agent can judge")
A("what the pack contains before asking for it.")
A("")
A("## Totals")
A("")
A("| availability | items |")
A("|---|---:|")
for k, v in counts.most_common():
    A("| `%s` | %d |" % (k, v))
A("")
A("## Owned but not downloaded (the extraction queue)")
A("")
A("| Item | Store | Type | Images | Product URL |")
A("|---|---|---|---:|---|")
for r in sorted(records, key=lambda x: (x["store"], x["name"])):
    if r["availability"] != "owned-not-downloaded":
        continue
    A("| %s | %s | %s | %d | %s |" % (r["name"], r["store"], r["type_group"] or "",
                                     r["gallery_images"] or 0, r["product_url"] or ""))
A("")
A("## Already on disk")
A("")
A("| Item | Store | Local folder | Model files |")
A("|---|---|---|---:|")
for r in sorted(records, key=lambda x: x["name"]):
    if r["availability"] != "local":
        continue
    A("| %s | %s | `%s` | %d |" % (r["name"], r["store"], r["local_path"], r["local_model_files"]))
A("")
A("## On-disk sections not represented in the purchase collection")
A("")
A("| Section | Folders | Model files |")
A("|---|---:|---:|")
for r in sorted(local_only, key=lambda x: -x["model_files"]):
    A("| %s | %d | %d |" % (r["name"], r["folders"], r["model_files"]))
A("")
io.open(os.path.join(AGENT, "AVAILABILITY_AGENT_INDEX.md"), "w", encoding="utf-8").write("\n".join(L))

print("items:", len(records))
print("counts:", dict(counts))
print("wrote availability.json, availability.jsonl, AVAILABILITY_AGENT_INDEX.md")
print()
print("=== owned-not-downloaded, by store ===")
for s, n in collections.Counter(r["store"] for r in records if r["availability"] == "owned-not-downloaded").most_common():
    print("   %-14s %4d" % (s, n))
