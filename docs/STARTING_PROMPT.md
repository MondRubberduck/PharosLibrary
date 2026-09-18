# Pharos — starting prompt for your coding agent

This is the one thing you paste at the very start. Edit the two paths in
angle brackets, copy the fenced block, send it. Everything else — the
census, the decisions, the interview — is the agent's job; the agent's
step-by-step contract lives in [`AGENT_SETUP_BRIEF.md`](AGENT_SETUP_BRIEF.md).

---

```
Set up Pharos for my asset library. Setup only — do not build scenes yet.

0. Read, in this order:
   <repo>/README.md
   <repo>/docs/AGENT_SETUP_BRIEF.md   <- your setup contract: the decision
                                         matrix and the questions you must
                                         relay to me
   <repo>/docs/AGENT_PLAYBOOK.md      <- the two-phase doctrine

1. Probe the machine, don't assume:
   python <repo>/pharos.py doctor --json
   python <repo>/pharos.py init "<D:/path/to/your/assets>" --force
   Fix anything the detector got wrong in pharos_config.json (it prints
   everything it decided on purpose).

2. Run: python <repo>/pharos.py ingest
   It executes every chain that is safe to run automatically (scanner,
   animation indexer, importers, agent docs) and prints an
   ASK YOUR USER block with the decisions that need me.

3. Relay those questions to me, PLUS every question the BRIEF's decision
   matrix triggers for what the census actually found — at minimum:
   UE pack crawl yes/no, .blend handling (enumerate / kit-export /
   leave as-is), purchase CSV, crawl-quality audio+texture indexes,
   other drives. WAIT for my answers before running anything that needs
   Unreal or Blender.

4. After my answers: run the consented chains, then re-run ingest until
   `pharos.py doctor` reports READY or READY-WITH-GAPS where every
   remaining gap is a section I confirmed is genuinely empty.

5. Start the server (python <repo>/pharos.py serve --no-open), wait for
   INGESTION COMPLETE, report the per-section numbers to me, and write
   our decisions plus any code defects you hit into
   <library>/_Agent_Files/SETUP_NOTES.md.
```

---

Why the interview is not optional: your library's shape decides which of
Pharos' chains matter. Pharos can handle UE packs, Blender kits and
containers, raw FBX/OBJ folders, texture sets, audio, and purchase
catalogs — but *your* setup needs only some of them, and the expensive
ones (Unreal crawl, kit export) must never start without your yes.
