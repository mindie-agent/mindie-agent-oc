# MindIE Agent for OpenCode `v0.1.12` — Windows Path Unification, Transcript Cursor Integrity & `mindie-harness v0.1.7` Alignment

[中文](mindie-agent-oc-0.1.12.md)

- **Removed Broken Local `_rpc` Shadow Fallback**: Deleted `_rpc` in `scripts/mcp_server.py` (`33` lines), delegating all 5 `knowledge_*` MCP tools cleanly to `mindie-harness v0.1.7`.
- **Unified Windows/POSIX Profile Paths & Safe Manifest Launch**: Aligned `opencode_host.mjs`, `mindie_launch.py`, and `setup.py` onto `ProfileRef.resolve("oc")` (`%APPDATA%` / `%LOCALAPPDATA%` on Windows, XDG on POSIX); eliminated bare `"python"` in `.mcp.json`, aligned `pending-update-check.json` schema, and restricted capture triggers strictly to terminal session-idle events.
- **Complete Transcript Cursor & Locked Spool GC**: Implemented `FileIdentity.unserialize()` (empty-file safe), `read_material(..., scan_until=...)` record boundary fencing, and interprocess `_spool_gc.lock` spool pruning in `opencode_transcript.py`; wired single-lock 9-domain `ensure_knowledge_binding` and 3-OS `verify-host-plugin.yml` CI verification.
