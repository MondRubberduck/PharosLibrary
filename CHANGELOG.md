# Changelog

## 0.2.0 (2026-09-18)
- Two-phase agent workflow (playbook), ingestion summaries + interview
- Per-slot material wiring, emissive/opacity, ground/asset texture sets,
  Y-up rotation conjugation, crowd stagger, orphan purge
- stdlib FBX dimension parser (fbx_dims), hardened against truncation
- Pipeline chains shipped in-repo (conversion / kitbash / native /
  agent_index) with config-driven paths
- Security: Host allow-list, real path jails, no shell=True
- Data retention: scanner rows survive every rebuild (meshes, audio,
  textures); indexer refuses to prune on unreachable roots
- Fresh-install smoke suite (44 checks) + CI (win/linux, py3.10/3.12)

## 0.1.0 (2026-09-16)
- Initial public shape: HTTP API + dashboard, MCP server (6 tools),
  SQLite registry rebuilt from crawler outputs, scene manifest +
  headless Blender builder
