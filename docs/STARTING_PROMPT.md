# Pharos: starting prompt for your coding agent

Paste this at the very start. Replace the asset folder path in angle
brackets (`<repo>` is wherever the agent cloned Pharos; it fills that
in). If you give no folder, the agent must ask you for it before running
anything. The agent's step-by-step contract is
[`AGENT_SETUP.md`](AGENT_SETUP.md).

---

```
Set up Pharos for my asset library. Setup only, do not build scenes yet.

0. Read <repo>/README.md, then <repo>/docs/AGENT_SETUP.md (your contract).

1. python <repo>/pharos.py doctor --json
   Then EITHER open the setup page for me (python <repo>/pharos.py setup;
   I pick the folder, correct the survey and press Start indexing; you
   wait and watch /api/setup/state), OR do it in the shell:
   python <repo>/pharos.py init "<D:/path/to/your/assets>" --force
   python <repo>/pharos.py survey "<D:/path/to/your/assets>"
   and show me the top-level folders the way the survey read them (kind
   and evidence) so I can correct them before anything is indexed.

2. python <repo>/pharos.py ingest
   Relay its ASK YOUR USER questions to me in one message, in plain
   words, with the cost of each. WAIT for my answers and record them
   with pharos.py map set.

3. Run only the chains I approved (pharos.py convert / extract /
   previews), then ingest again, until pharos.py doctor reports READY
   (or READY-WITH-GAPS where I confirmed the gap).

4. python <repo>/pharos.py serve --no-open; report the per-section
   numbers; write our decisions into <library>/_Agent_Files/SETUP_NOTES.md.
```

---

Why the questions are not optional: the shape of your library decides
which of Pharos' chains matter, and the expensive ones (Unreal export,
Blender extraction) never start without your yes. Your answers live in
`<library>/_Agent_Files/library_map.csv`, so a second setup on the same
library asks nothing twice.
