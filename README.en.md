<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>Native OpenCode (<code>v2.2.x+</code>) harness plugin: six thin host files, 14 default-enabled Ascend remote tools plus full diagnostic and graded L1-L4 knowledge MCP, pure-Node consent gates, and zero-overhead on-demand capture</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.11-0F766E.svg" alt="Release v0.1.11"></a>
  &nbsp;
  <a href="./README.md">简体中文</a>
</p>

---

- [1. Architecture & Exposure Boundary](#1-architecture--exposure-boundary)
- [2. Installation & Upgrades](#2-installation--upgrades)
- [3. Community Sharing & Privacy](#3-community-sharing--privacy)
- [4. Verification](#4-verification)

## 1. Architecture & Exposure Boundary

`mindie-agent-oc` (npm package `@mindie-agent/oc`, current release [`v0.1.11`](https://github.com/mindie-agent/mindie-agent-oc/releases/tag/v0.1.11)) targets OpenCode's v2 Promise Plugin API (`@opencode-ai/plugin`) directly. `v0.1.11` is the **Great Subtraction** thin-host release: the production surface is six files (`index.mjs`, `opencode_host.mjs`, `mindie_launch.py`, `mcp_server.py`, `opencode_transcript.py`, `setup.py`) totaling `< 1,200` lines, with all heavy lifting delegated to `mindie-harness`.

1. **Default-On Tools With Zero Manual Activation**:
   - `mindie-remote-dev`: the 14-tool `oc` native `remote_*` profile (`timeout.execution = 660000ms`).
   - `mindie-tools`: deterministic component tools, `mindie_status`, `mindie_doctor`, `diagnostics_status`/`diagnostics_bundle`, and the 5 default-enabled `knowledge_*` tools.
2. **Zero Subprocess Per Prompt & Consent-First Capture**: top-level main sessions (`!parentID && !forkSessionID`) receive cached `SKILL.md` guidance (frontmatter stripped) directly from Node.js. Terminal events are gated in pure Node.js on `mindie-community.json`, `mindie-consent.json` (`contribute` or `local-only`), and canonical `project_roots`; when sharing is off or out of scope, zero subprocesses are spawned and zero SQLite files are created. Authorized turns coalesce within `250ms` and spawn `mindie_launch.py hook stop`.
3. **Automatic Updates & Shared Runtime**: an on-demand `mindie_launch.py update --background` check, `mindie-harness-seed.pyz` verified cold provisioning, and all component updates and generation leases delegated to `mindie-harness`.

## 2. Installation & Upgrades

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
```

`gh` is entirely optional: when it is missing or logged out, the installer recommends `local-only` and never blocks. It then asks three questions with OpenCode's native **`question`** tool: workspace choice, community sharing tier (`contribute` when `gh` is authenticated, otherwise `local-only`; both capture locally), and a single-line Star postscript ("If this plugin helps, would you give mindie-agent a Star? ⭐"). In terminal mode the SSH NPU connection is completed in one step; append `--password` at the end to enter it hidden. Finish with the read-only `mindie_doctor` readiness check.

```bash
opencode plugin check
opencode plugin update
```

For local checkouts, mount `file:///abs/path/to/mindie-agent-oc` in the project `opencode.json` and run `uv run --no-project python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"`.

See [workspace setup](docs/install/workspace-setup.md), optional [GitHub sign-in](docs/install/github-authentication.md), and the [user notice](docs/install/user-notice.md).

## 3. Community Sharing & Privacy

Community sharing and sanitized fault reporting stay off by default; switch explicitly with `setup.py sharing --enable | --local-only | --disable` (`--status` is read-only). All state lives in MindIE-owned directories; `~/.config/opencode` and `~/.local/share/opencode` are never modified.

## 4. Verification

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
python3 .github/scripts/check_lf_eol.py
node --check index.mjs && node --check plugins/mindie-agent/opencode_host.mjs
```
