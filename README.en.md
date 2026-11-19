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
- [2. MCP & Tool Matrix](#2-mcp--tool-matrix)
- [3. Installation, Upgrades & Onboarding](#3-installation-upgrades--onboarding)
- [4. Community Sharing & Privacy](#4-community-sharing--privacy)
- [5. Verification](#5-verification)

## 1. Architecture & Exposure Boundary

`mindie-agent-oc` (npm package `@mindie-agent/oc`, current release [`v0.1.11`](https://github.com/mindie-agent/mindie-agent-oc/releases/tag/v0.1.11)) targets OpenCode's v2 Promise Plugin API (`@opencode-ai/plugin`) directly. `v0.1.11` is the **Great Subtraction** thin-host release: the production surface is six files (`index.mjs`, `opencode_host.mjs`, `mindie_launch.py`, `mcp_server.py`, `opencode_transcript.py`, `setup.py`) totaling `< 1,200` lines, with all heavy lifting delegated to `mindie-harness`.

1. **Default-On Tools With Zero Manual Activation**:
   - `mindie-remote-dev`: the 14-tool `oc` native `remote_*` profile (`timeout.execution = 660000ms`).
   - `mindie-tools`: deterministic component and RSI tools, `mindie_status`, `mindie_doctor`, `diagnostics_status`/`diagnostics_bundle`, and the 5 default-enabled `knowledge_*` tools.
2. **Zero Subprocess Per Prompt & Consent-First Capture**: top-level main sessions (`!parentID && !forkSessionID`) receive cached `SKILL.md` guidance (frontmatter stripped) directly from Node.js. Terminal events are gated in pure Node.js on `mindie-community.json`, `mindie-consent.json` (`contribute` or `local-only`), and canonical `project_roots`; when sharing is off or out of scope, zero subprocesses are spawned and zero SQLite files are created. Authorized turns coalesce within `250ms` and spawn `mindie_launch.py hook stop`.
3. **Automatic Updates & Shared Runtime**: an on-demand `mindie_launch.py update --background` check, `mindie-harness-seed.pyz` verified cold provisioning, and all component updates and generation leases delegated to `mindie-harness`.

## 2. MCP & Tool Matrix

| Channel | Surface | Exposed Tools |
| :--- | :--- | :--- |
| **Default Public MCP** | `mindie-remote-dev` | `remote_auth`, `remote_probe`, `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_list`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_output`, `remote_job_stop` (14 `oc`-profile tools) |
| **Default Public MCP** | `mindie-tools` | `mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, `diagnostics_bundle`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |

## 3. Installation, Upgrades & Onboarding

### One-Command Install (Recommended)

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
```

`gh` is entirely optional: when it is missing or logged out, `install.py --host oc` emits `recommended_tier: "local-only"` alongside structured `next_questions` (`workspace_roots`, `privacy_tier`, `optional_ssh_npu_host`) in its terminal JSON receipt without blocking shared runtime provisioning. Inside your OpenCode session, the host Agent then consumes that receipt via OpenCode's native **`question`** tool to confirm:

1. **Workspace**: initialize an Ascend workspace in the current directory, continue using the current directory, or point to an existing directory.
2. **Community Sharing**: follows `recommended_tier` (`contribute` when `gh` is authenticated, otherwise `local-only`; both capture locally, and only `contribute` exports sanitized results).
3. **Optional SSH NPU Host & Single-Line Star Postscript**: optionally set up the SSH NPU connection in one step (in terminal mode, append `--password` at the end for hidden input), followed by a single-line closing postscript ("If this plugin helps, consider giving `mindie-agent` a Star ⭐").

Finish with the read-only readiness check tool:

```text
mindie_doctor
```

### Manual Upgrade & Local Development

```bash
opencode plugin check
opencode plugin update
```

For local checkouts, mount `file:///abs/path/to/mindie-agent-oc` in the project `opencode.json` and run `uv run --no-project python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"`.

See [workspace setup](docs/install/workspace-setup.md), optional [GitHub sign-in](docs/install/github-authentication.md), and the [user notice](docs/install/user-notice.md).

## 4. Community Sharing & Privacy

Community sharing and sanitized fault reporting stay off by default; switch explicitly with `setup.py sharing --enable | --local-only | --disable` (`--status` is read-only). All state lives in MindIE-owned directories; `~/.config/opencode` and `~/.local/share/opencode` are never modified.

## 5. Verification

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
python3 .github/scripts/check_lf_eol.py
node --check index.mjs && node --check plugins/mindie-agent/opencode_host.mjs
```
