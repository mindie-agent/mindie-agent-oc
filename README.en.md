<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>Native OpenCode (<code>v2.2.x+</code>) harness plugin featuring dual-channel GitHub &amp; npm distribution, 39 default-enabled Ascend remote, diagnostic, and graded L1–L4 knowledge MCP tools, and zero-overhead consent-gated turn capture</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.10-0F766E.svg" alt="Release v0.1.10"></a>
  &nbsp;
  <a href="./README.md">简体中文</a>
</p>

---

## 1. Architecture & Exposure Boundary

`mindie-agent-oc` (npm package `@mindie-agent/oc`, current release [`v0.1.10`](https://github.com/mindie-agent/mindie-agent-oc/releases/tag/v0.1.10)) targets OpenCode's v2 Promise Plugin API (`@opencode-ai/plugin`) directly with all tools enabled on startup and zero manual activation commands:

1. **GitHub / Local Installation & Automatic Updates**: Supports `opencode plugin add github:mindie-agent/mindie-agent-oc` (OpenCode 2.2.7 rejects direct tarball URLs; release tarballs remain distribution artifacts), local directory URIs (`"plugin": ["file:///abs/path/to/mindie-agent-oc"]`), native `opencode plugin update`, and monotonic `300s` background updates (`max(installed, baseline, latest)`) for shared runtime components.
2. **Default-Enabled MCP Tools (39 Tools Across Two Surfaces)**:
   - `mindie-remote-dev`: 20 `remote_*` development and background job tools (`timeout.execution = 660000ms`).
   - `mindie-tools`: 19 tools (`mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top` + 6 live observation tools, `diagnostics_status`, `diagnostics_bundle`, plus the 5 default-enabled `knowledge_*` tools `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`), alongside direct `knowledge_cli.py` invocation without prior session activation.
3. **Pure-Node Context Guidance & Consent-First `session.idle` Capture**:
   - Top-level main sessions (`!parentID && !forkSessionID`) receive cached `SKILL.md` guidance (with YAML frontmatter stripped) directly from memory in Node.js via `session.hook("context")` (zero Python subprocess spawns per prompt), and `shell.hook("create.before")` binds `OPENCODE_SESSION_ID` into session shells.
   - On `session.idle`, `opencode_host.mjs` checks community sharing consent (`mindie-community.json` + `mindie-consent.json`) and authorized `project_roots` in pure Node.js before spawning `capture_stop.py`: when sharing is off or outside authorized `project_roots`, zero subprocesses are spawned and zero `admission.sqlite3` files are created; when authorized and in scope, `capture_stop.py` auto-provisions the session lease (`not_before=0.0`) and spools sanitized public turns read-only from `opencode.db`.

## 2. Installation & Upgrades

```bash
# Channel 1: GitHub repository / release
opencode plugin add github:mindie-agent/mindie-agent-oc

# Check and apply plugin updates
opencode plugin check
opencode plugin update
```

For local checkout provisioning (without touching `~/.config/opencode` or `~/.local/share/opencode`):

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 \
  python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"
```

## 3. Community Sharing & Privacy Notice

Community sharing and sanitized fault reporting stay off by default; see the [user notice](docs/install/user-notice.md), [workspace setup guide](docs/install/workspace-setup.md), and [GitHub sign-in guide](docs/install/github-authentication.md) for initial configuration.

## 4. Verification

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
```
