<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>Native OpenCode (<code>v2.2.x+</code>) harness plugin featuring dual-channel GitHub &amp; npm distribution, 34 always-on deterministic Ascend remote &amp; diagnostic MCP tools, and per-session <code>/mindie-agent</code> knowledge flywheel activation</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.4-0F766E.svg" alt="Release v0.1.4"></a>
  &nbsp;
  <a href="./README.md">简体中文</a>
</p>

---

## 1. Architecture & Exposure Boundary

`mindie-agent-oc` (npm package `@mindie-agent/oc`, current release [`v0.1.4`](https://github.com/mindie-agent/mindie-agent-oc/releases/tag/v0.1.4)) targets OpenCode's v2 Promise Plugin API (`@opencode-ai/plugin`) directly and enforces the same strict boundary as `mindie-agent-agy` and `mindie-agent-codex`:

1. **Dual-Channel Distribution & Automatic Updates**: Supports `opencode plugin add github:mindie-agent/mindie-agent-oc`, GitHub Release npm tarballs (`opencode plugin add https://github.com/mindie-agent/mindie-agent-oc/releases/download/v0.1.4/mindie-agent-oc-0.1.4.tgz`), and local directory URIs (`"plugin": ["file:///abs/path/to/mindie-agent-oc"]`), native `opencode plugin update`, and monotonic `300s` background updates (`max(installed, baseline, latest)`) for shared runtime components.
2. **Always-On Deterministic MCP Tools (34 Tools, Zero `knowledge_*` in Default `tools/list`)**:
   - `mindie-remote-dev`: 20 `remote_*` development and background job tools (`timeout.execution = 660000ms`).
   - `mindie-tools`: 14 deterministic tools (`mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top` + 6 live observation tools, `diagnostics_status`, `diagnostics_bundle`).
3. **Explicit Per-Session `/mindie-agent` Knowledge Flywheel**:
   - Unactivated sessions incur zero subprocess spawns on ordinary prompts, zero system-prompt tokens, and zero transcript capture.
   - Invoking `/mindie-agent` (or `$mindie-agent`) in a top-level main session (`!parentID && !forkSessionID`) activates the lease in `admission.sqlite3`, injects `SKILL.md` via `session.hook("context")`, binds `OPENCODE_SESSION_ID` into session shells via `shell.hook("create.before")` to unlock `knowledge_cli.py`, and spools sanitized public turns read-only from `opencode.db` on `session.idle`.

## 2. Installation & Upgrades

```bash
# Channel 1: GitHub repository / release
opencode plugin add github:mindie-agent/mindie-agent-oc

# Channel 2: GitHub Release npm tarball (no external npm registry required)
opencode plugin add https://github.com/mindie-agent/mindie-agent-oc/releases/download/v0.1.4/mindie-agent-oc-0.1.4.tgz

# Check and apply plugin updates
opencode plugin check
opencode plugin update
```

For local checkout provisioning (without touching `~/.config/opencode` or `~/.local/share/opencode`):

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 \
  python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"
```

## 3. Session Commands

- `/mindie-agent` or `/mindie-agent <task>`: Activate the MindIE knowledge flywheel for the current OpenCode session.
- `/mindie-agent status`: Inspect factual session lease and runtime status.
- `/mindie-agent deactivate`: Revoke the session lease while keeping deterministic MCP tools available.

## 4. Verification

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
```
