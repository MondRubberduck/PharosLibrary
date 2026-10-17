# AGENT SETUP BRIEF

Superseded in 1.0 by [`AGENT_SETUP.md`](AGENT_SETUP.md). The decision
matrix this file used to hold is now data: the survey writes what it
found into `<library>/_Agent_Files/library_map.csv`, `pharos.py ingest`
asks only the decisions that are missing there, and `pharos.py convert |
extract | previews` run only what the map allows.
