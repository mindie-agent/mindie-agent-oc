# Contributing to `mindie-agent-oc`

For the Chinese version, see [CONTRIBUTING.md](CONTRIBUTING.md).

`mindie-agent-oc` (`@mindie-agent/oc`) is the OpenCode v2 native host plugin repository (`host-plugin` profile) in the MindIE Agent organization.

## 1. Workspace Layout

Develop against a flat sibling checkout or an isolated `.worktrees/` checkout:

```text
code/
  mindie-agent/           # Hub architecture docs, channel manifests, and org gates
  harness/                # Shared mindie-harness substrate
  mindie-agent-oc/        # OpenCode v2 host plugin
  .worktrees/             # Isolated feature/refactoring worktrees
```

## 2. Non-Negotiable Host-Plugin Contracts

1. **Zero Pre-Call Binding on Deterministic Tools (`P2.7` / `AD-01`)**: Deterministic tools (`mindie-remote-dev` and `mindie-tools`) must never inject or require `_mindie_bind_id`, and standard MCP `tools/call` with zero `_meta`, zero `OPENCODE_SESSION_ID`, and zero prior JS `execute.before` hook must succeed 100%.
2. **Shared Remote State & Concurrent Stdio MCP (`AD-02` / `AD-03`)**: `REMOTE_DEV_STATE_DIR` must resolve to the shared profile-level `state_dir()/remote` directory so subagents can poll and stop parent `job_id`s, and MCP stdio must serve `Content-Length` and JSONL frames concurrently across separate read/control and mutation worker pools.
3. **Pure-Stdlib Windows Supervision & Zero Console Popups (`P1.5`)**: Never import uninstalled `pywin32` (`win32api`/`win32job`); delegate to `mindie_harness.process` and pure-stdlib `ctypes` Job Objects. Every JS `spawn`/`spawnSync` site must pass `windowsHide: true` and `detached: process.platform !== "win32"`, and every Windows Python subprocess call must include `CREATE_NO_WINDOW`.
4. **Read-Only Host DB & Unix LF**: Open OpenCode's `opencode.db` strictly read-only (`?mode=ro`), never mutate `~/.config/opencode` or `~/.local/share/opencode`, and keep Unix `LF` (`\n`) line endings across all tracked text files.

## 3. Test Isolation & Pre-PR Gate

Tests must isolate `HOME`, `USERPROFILE`, `XDG_*`, `OPENCODE_TEST_HOME`, `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`, and `UV_CACHE_DIR`.

Before opening or updating a PR, complete the `host-plugin` checklist (`HOST-NATIVE`, `HOST-WIRE`, `HOST-ADMISSION`, `HOST-UPGRADE`) in [`.github/pull_request_template.md`](.github/pull_request_template.md) and run:

```bash
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
node --check index.mjs && node --check plugins/mindie-agent/paths.mjs && node --check plugins/mindie-agent/opencode_host.mjs
pytest -q -m "not live_npu and not live_opencode"
```
