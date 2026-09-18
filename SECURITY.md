# Security policy

Pharos is a **single-user, localhost tool with no authentication by design**.

- The server binds `127.0.0.1` by default and validates the `Host` header
  (loopback names only) to block DNS-rebinding from web pages you visit.
- Setting `network.host` to a non-loopback address exposes your ENTIRE
  library (paths, purchase history, file bytes) and every mutating
  endpoint to your network with zero authentication. The server prints a
  loud warning if you do. Don't.
- File-serving routes are path-jailed to the configured library roots.
- The library on disk is never modified; only the registry DB, previews
  cache, and generated index files are written.

Report issues via GitHub issues. There is no bounty program; this is a
personal tool shared as MIT source.
