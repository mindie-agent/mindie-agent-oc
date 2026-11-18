# MindIE Agent for OpenCode `v0.1.11` — Great Subtraction + UX Upgrade

`v0.1.11` collapses the OpenCode adapter into **six thin host production files** and delegates runtime management, process supervision, concurrent MCP, updates, and generation leases to `mindie-harness v0.1.3`, while reworking installation and first-run setup.

## 1. Great Subtraction

- Deleted `paths.mjs`, `session_support.mjs`, and 23 legacy Python modules (`admission.py`, `bounded.py`, `bridge.py`, `capture_stop.py`, `component_update.py`, `contract_validation.py`, `diagnostic_support.py`, `experiment_ledger.py`, `host_process.py`, `host_adapter.py`, `identity.py`, `knowledge_cli.py`, `mcp_stdio.py`, `oc_native_compat.py`, `opencode_spool.py`, `organizer.py`, `paths.py`, `release_metadata.py`, `rsi_loop.py`, `runtime_bootstrap.py`, `storage_gc.py`, `storage_spool.py`, `storage_support.py`).
- The production surface is now `index.mjs`, `opencode_host.mjs`, `mindie_launch.py`, `mcp_server.py`, `opencode_transcript.py`, and `setup.py`, totaling `< 1,200` lines.
- `storage_lifecycle_gate.py` enforces the combined `.mjs` + `.py` budget; no workflow, `code_paths`, or `test_ids` references a deleted file.

## 2. Thin Host Behavior

- **MCP**: `mindie-remote-dev` exposes the 14-tool `oc` native remote profile; `mindie-tools` exposes deterministic components, `mindie_status`, `mindie_doctor`, diagnostics, and the 5 default-enabled `knowledge_*` tools. `OCManagedAdapter` shares `state/oc/remote` across sessions and subagents.
- **Context**: `session.hook("context")` keeps injecting cached `SKILL.md` in pure Node.js for top-level sessions only (no subagent/fork inheritance), with zero Python subprocess per prompt.
- **Capture**: terminal events check `mindie-community.json`, `mindie-consent.json` (`contribute` / `local-only`), and canonical `project_roots` in pure Node.js. Unauthorized turns spawn zero subprocesses and create zero SQLite files; authorized turns coalesce within `250ms` and spawn `mindie_launch.py hook stop`.
- **Transcript**: `opencode_transcript.py` opens `opencode.db` strictly `?mode=ro`, filters `reasoning`/`tool`/`synthetic`/`ignored`, recovers torn tails, accounts bytes exactly over `[start, end)` with SHA-256, and caps cold spools at `64` JSONL files.
- **Launcher**: `mindie_launch.py` provides pure-stdlib fast paths, seed integrity verification, active-interpreter re-exec, a `summary` HTTP bridge, and `mcp` / `setup` dispatch.
- **Setup**: `setup.py` writes `oc.engine.json` / `oc.json`, delegates `sharing` / `doctor` / `update` to `mindie-harness`, and adds `release-metadata` in place of `release_metadata.py`.

## 3. UX Upgrade

- One-line install:

  ```bash
  uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
  ```

- `gh` is entirely optional: authenticated setups recommend `contribute`, otherwise `local-only`; a missing or logged-out `gh` never blocks installation.
- Three dynamic `question` prompts: workspace choice, community sharing tier, and a single-line Star postscript.
- Terminal mode completes the SSH NPU connection in one step; append `--password` at the end for hidden entry.
- Run the read-only `mindie_doctor` readiness check after installation.
- New Non-Blocking Task Continuity Rule: MindIE tools are accelerators, never roadblocks. On failure, report once (reconciling uncertain writes first) and continue immediately through native `ssh`/`scp` or local scripts.

## 4. Safety & Verification

- Every Node child process uses `windowsHide: true` + `detached: process.platform !== "win32"`; every Python child process uses `creationflags=CREATE_NO_WINDOW`.
- Temporary files are cleaned in `finally`; `except TimeoutError: raise` always precedes `OSError` / broad handlers.
- A fresh isolated suite covers `tests/test_oc_plugin.py`, `tests/test_oc_lifecycle.py`, `tests/test_oc_mcp_and_knowledge.py`, and `tests/test_oc_transcript.py`.
- `benchmarks/release.py` calls `sync_session_spool` and `read_material` directly and passes.
