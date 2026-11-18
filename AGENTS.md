# MindIE Agent — OpenCode Native Host Plugin (`mindie-agent-oc`)

`mindie-agent-oc` (`@mindie-agent/oc`) is a **`host-plugin`** repository implementing the OpenCode v2 native host plugin, backed by `mindie-harness` (`v0.1.3`) and the shared MindIE runtime components. `v0.1.11` is the **thin host** release: the entire production surface is six files (`index.mjs`, `plugins/mindie-agent/opencode_host.mjs`, and `scripts/{mindie_launch,mcp_server,opencode_transcript,setup}.py`) totaling `< 1,200` lines.

## 1. Core Architectural & Contract Rules

- **Zero Pre-Call Session-Binding Gates & Default-Enabled Knowledge (`P2.7` / `AD-01`)**:
  - Standard MCP `tools/call` on `mindie-remote-dev` (`remote_*`) and `mindie-tools` (`mindie_status`, `mindie_doctor`, component, diagnostics, and `knowledge_*` tools) MUST succeed with **zero `_meta`, zero `_mindie_bind_id`, zero `OPENCODE_SESSION_ID`, and zero prior JS hook**; `mindie_harness` owns the connection-scoped fallback and on-demand default lease (`not_before=0.0`).
  - `opencode_host.mjs` strips forged `_mindie_bind_id` from MindIE MCP inputs in pure Node.js and never spawns a pre-tool-use subprocess.
- **Consent-First Idle Capture & Session Context Isolation**:
  - `session.hook("context")` injects cached `SKILL.md` in pure Node.js (zero Python subprocess per prompt) for top-level sessions (`!parentID && !forkSessionID`), with zero user-facing command registration.
  - Terminal events (`session.idle`, `session.execution.*`) are gated in pure Node.js on `mindie-community.json` (`enabled === true`), `mindie-consent.json` (`contribute` or `local-only`), and canonical `project_roots`; disabled/out-of-scope/subagent/forked sessions spawn zero subprocesses and create zero SQLite files. Duplicate idle events coalesce within `250ms`, and the deterministic `eventID` becomes `turn_id`.
- **Shared Remote State & Resident MCP (`AD-02` / `AD-03`)**:
  - `OCManagedAdapter` defaults `REMOTE_DEV_HARNESS_PROFILE="oc"` (14 native remote tools) and `REMOTE_DEV_STATE_DIR=<state_dir>/remote`, so subagents can inspect/stop parent jobs.
  - `serve_mcp` provides concurrent dual-framing stdio, deadlines, and generation leases; a `knowledge_unavailable` fallback uses `mindie_knowledge.loop.transport.rpc` with Receipt Honesty on `Admission.finish` `OSError`/`sqlite3.Error`.
- **Pure-Stdlib Windows Supervision & Zero Console Popups (`P1.5`)**:
  - Every Node.js `spawn` call passes `windowsHide: true` and `detached: process.platform !== "win32"`; every Python `subprocess.run` passes `creationflags=CREATE_NO_WINDOW`.
  - Fast paths (`hook session-start`, `hook pre-tool-use`, passive `hook stop`) are pure stdlib and import no runtime packages or SQLite.
- **Read-Only Transcript Projection & Receipt Honesty (`AD-10`, Principle 14)**:
  - `opencode_transcript.py` opens `opencode.db` strictly `?mode=ro`, rejects subagent/forked sessions, projects only public `text`/`input_text`/`output_text`, redacts secrets and private paths, recovers torn tails, and bounds cold spools to `64` files.
  - Keep runtime `skills/` free of benchmark tuples, onboarding files, user-facing activation commands, and `disable-model-invocation`.

## 2. Plugin Module Map

| Module | Role |
| :--- | :--- |
| `index.mjs` | Root OpenCode v2 entry shim; resolves the recorded active host module (`source_root` as plugin dir or outer repo root) with clean local fallback. |
| `plugins/mindie-agent/opencode_host.mjs` | Windowless MCP registration, background update check, cached `SKILL.md` context, loopback `/v1/summary`, MCP input sanitization, shell `OPENCODE_SESSION_ID` binding, consent-first capture spawn. |
| `plugins/mindie-agent/scripts/mindie_launch.py` | Pure-stdlib fast paths, seed verification/cold provisioning, active-interpreter re-exec, in-runtime `hook stop` capture, `summary` bridge, and `mcp`/`setup` dispatch. |
| `plugins/mindie-agent/scripts/mcp_server.py` | Thin `OCManagedAdapter` gateway with `oc` remote defaults and the knowledge RPC fallback. |
| `plugins/mindie-agent/scripts/opencode_transcript.py` | Read-only SQLite spool projector, `FileIdentity`, `sync_session_spool`, `capture_source`, `read_material` / `read_increment`. |
| `plugins/mindie-agent/scripts/setup.py` | `init` profile writer, `sharing`/`doctor`/`update` harness delegation, `--dry-run`, and `release-metadata`. |

## 3. Local Verification

```bash
python3 .github/scripts/check_lf_eol.py
node --check index.mjs
node --check plugins/mindie-agent/opencode_host.mjs
python3 -m pytest -q -m "not live_npu and not live_opencode"
wc -l index.mjs plugins/mindie-agent/*.mjs plugins/mindie-agent/scripts/*.py
```

`benchmarks/release.py` calls `sync_session_spool` and `read_material` directly and must always pass.
