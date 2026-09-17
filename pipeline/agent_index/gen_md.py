import os, json

from _config import section_root
AA = section_root("audio", "AGENT_AUDIO_ROOT")
if not os.path.isdir(AA):
    raise SystemExit("FATAL: audio root does not exist: %s" % AA)
idx = json.load(open(os.path.join(AA, "library_index.json"), encoding="utf-8"))
T = idx["totals"]

def fmt_dur(s):
    if s is None:
        return "?"
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    return f"{s/3600:.1f}h"

L = []
A = L.append
A("# Audio Library — Agent Index (READ ME FIRST)")
A("")
A(f"> Machine-readable companions in this folder: **`library_index.json`** (taxonomy, counts, keywords) and **`library_files.jsonl`** (one JSON object per file: path/category/duration).")
A(f"> Generated {idx['generated']} · Root: `{idx['root']}`")
A("")
A("## What this is")
A(idx["purpose"])
A("")
A(f"- **{T['audio_files']:,} audio files**, **{T['categories']} categories**, **{T['subfolders']} subfolders**")
A(f"- **{T['size_gb']} GB** on disk · **~{T['total_duration_h']} hours** of audio")
A(f"- Formats: " + ", ".join(f"`{k}`×{v}" for k, v in idx["file_formats"].items()))
A("")
A("## How to use (for agents) — fastest path")
A("1. **Start with `library_index.json`** — read `categories` to see what exists and where, and `keywords` to route a request to the right folder.")
A("2. **Need a specific file?** Grep `library_files.jsonl` (one JSON per line) instead of walking the tree — far cheaper.")
A("3. **Only then open** the concrete path under the root above.")
A("")
A("```bash")
A("# find candidate categories/keywords")
A('grep -i "explosion" library_files.jsonl | head')
A("# everything in a category / subfolder")
A('grep \'"cat":"Gunshots"\' library_files.jsonl | wc -l')
A('grep \'"sub":"Pistol"\' library_files.jsonl')
A("# long ambience beds (>60s) — good for looping scene beds")
A('grep \'"cat":"Ambiance"\' library_files.jsonl | python -c "import sys,json; [print(json.loads(l)[\'p\']) for l in sys.stdin if (json.loads(l)[\'dur\'] or 0)>60]"')
A("```")
A("")
A("## Category map")
A("")
A("| Category | Files | Duration | Description |")
A("|---|---:|---:|---|")
for cat, c in sorted(idx["categories"].items(), key=lambda kv: -kv[1]["count"]):
    totd = sum((s["duration"]["total_s"] if s["duration"] else 0) for s in c["subfolders"].values())
    A(f"| **{cat}** | {c['count']} | {fmt_dur(totd)} | {c['description']} |")
A("")
A("## Categories in detail")
A("")
for cat, c in sorted(idx["categories"].items(), key=lambda kv: -kv[1]["count"]):
    A(f"### {cat}/  — {c['count']} files")
    A(c["description"])
    A("")
    A("| Subfolder | Files | Dur (min/mean/max) | Example files |")
    A("|---|---:|---|---|")
    for sub, s in sorted(c["subfolders"].items(), key=lambda kv: -kv[1]["count"]):
        d = s["duration"]
        if d:
            dstr = f"{fmt_dur(d['min_s'])} / {fmt_dur(d['mean_s'])} / {fmt_dur(d['max_s'])}"
        else:
            dstr = "?"
        ex = "<br>".join("`" + os.path.basename(x) + "`" for x in s["example_files"][:3])
        A(f"| `{sub}/` | {s['count']} | {dstr} | {ex} |")
    kws = ", ".join(f"`{k}`" for k in c["keywords"][:24])
    A("")
    if kws:
        A(f"*Routing keywords:* {kws}")
    A("")
A("## Notes & provenance")
A("")
for k, v in idx["notes"].items():
    A(f"- **{k}**: {v}")
A("")
A(f"- Related logs: `{idx['related_logs']}`")

text = "\n".join(L)
for _a, _b in [("—", "-"), ("·", "|"), ("×", "x"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-")]:
    text = text.replace(_a, _b)
open(os.path.join(AA, "AGENT_INDEX.md"), "w", encoding="utf-8").write(text)
print("wrote AGENT_INDEX.md", len(text), "chars")
