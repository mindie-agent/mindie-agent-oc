# MindIE Agent — OpenCode Native Host Plugin (`mindie-agent-oc`)

`mindie-agent-oc` (`@mindie-agent/oc`) is a **`host-plugin`** repository implementing the OpenCode v2 native host plugin (`index.mjs`, `plugins/mindie-agent/opencode_host.mjs`, `plugins/mindie-agent/paths.mjs`, and `plugins/mindie-agent/scripts/`), backed by `mindie-harness` (`v0.1.0`) and the shared MindIE runtime components.

## 1. Core Architectural & Contract Rules

- **Zero Pre-Call Session-Binding Gates & Default-Enabled Knowledge (`P2.7` / `AD-01`)**:
  - Standard MCP `tools/call` on `mindie-remote-dev` (`remote_*`) and `mindie-tools` (`mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `diagnostics_*`, `knowledge_*`) MUST succeed with **zero `_meta`, zero `_mindie_bind_id`, zero `OPENCODE_SESSION_ID`, and zero prior `execute.before` JS hook**, falling back cleanly to `_CONNECTION_SESSION_ID` and auto-provisioning a default session lease (`not_before=0.0`) on demand.
  - Never inject `_mindie_bind_id` into tool schemas (`tools_catalog`, `remote_catalog`) and never throw from `tool.hook("execute.before")` on `pre-tool-use` failures. Forged session keys inside model-supplied `arguments` remain rejected.
- **Consent-First Idle Capture & Session Context Isolation**:
  - `session.hook("context")` injects cached `SKILL.md` guidance directly in pure Node.js (zero Python subprocess per prompt) for top-level sessions (`!parentID && !forkSessionID`), with zero user-facing command registration.
  - `event.subscribe` (`session.idle`) checks `mindie-community.json`, `mindie-consent.json`, and `project_roots` in pure Node.js before spawning `capture_stop.py`; when sharing is off or out of scope, zero subprocesses are spawned and zero `admission.sqlite3` files are created. Subagent (`parentID`) and forked (`forkSessionID`) sessions never capture transcripts.
- **Shared Remote State & Concurrent Stdio MCP (`AD-02` & `AD-03`)**:
  - `REMOTE_DEV_STATE_DIR` resolves to the shared profile-level `state_dir()/remote` directory so OpenCode `task` subagents can inspect and stop parent `job_id`s.
  - `mcp_server.py` and `host_adapter.py` (`OCHostAdapter`) serve `Content-Length` and JSONL frames concurrently across separate read/control (`16` workers) and mutation (`8` workers) pools while holding `.inuse` generation leases (`hold_combined_service_lease`).
- **Pure-Stdlib Windows Supervision & Zero Console Popups (`P1.5`)**:
  - `bounded.py` delegates to `mindie_harness.process.supervisor.run_owned` and uses pure-stdlib `ctypes` (`kernel32` Job Objects) with zero `pywin32` (`win32api`/`win32job`) dependencies.
  - Every Node.js `spawn`/`spawnSync` call MUST pass `windowsHide: true` and `detached: process.platform !== "win32"`, and every Windows Python subprocess call MUST include `CREATE_NO_WINDOW` (never `DETACHED_PROCESS` or `CREATE_NEW_CONSOLE`).
- **Symlink Compatibility, Clean Skill Prose & Receipt Honesty (`AD-07`, `AD-10`, Principle 14)**:
  - Never use blanket `O_NOFOLLOW` in `diagnostic_support.py` or `fs.lstatSync` config rejection in `paths.mjs` (`AD-07`).
  - Keep `SKILL.md` and `references/domain-skills.md` free of benchmark tuples (`(out_tensor, metrics_dict)`) and `experiment_ledger.py` / `PROGRESS.md` ceremonies (`AD-10`).
  - Keep all JS/Python source modules `< 800` lines with `[Role]`, `[Invariants]`, `[Timeouts & Locks]`, and `[Side-Effect Policy]` in the top 30 lines; always precede `except OSError:` with `except TimeoutError: raise`; preserve `isError=False` + `receipt_warning` + `receipt_unconfirmed=True` when local SQLite accounting fails after a primary operation succeeds.

## 2. Plugin Module Map

| Module | Role |
| :--- | :--- |
| `index.mjs` / `opencode_host.mjs` / `paths.mjs` / `session_support.mjs` | OpenCode v2 ESM plugin entry, pure-Node cached `SKILL.md` context hook, non-blocking tool correlation, loopback summary server, and consent-gated `session.idle` capture trigger. |
| `scripts/host_adapter.py` | `OCHostAdapter` (`mindie_harness.api.HostAdapter`), `verify_seed_bundle()`, and concurrent dual-framing stdio loop (`serve_concurrent_stdio`). |
| `scripts/mcp_server.py` / `scripts/knowledge_cli.py` | Zero-binding MCP surfaces (`remote`, `tools` including default-on `knowledge_*`) and on-demand lease-backed knowledge CLI. |
| `scripts/bridge.py` / `scripts/admission.py` / `scripts/identity.py` | On-demand session lease provisioning (`ensure_default_lease`) and optional one-shot `mcp-binds.sqlite3` correlation. |
| `scripts/opencode_spool.py` / `scripts/opencode_transcript.py` / `scripts/capture_stop.py` | Read-only SQLite `opencode.db` (`mode=ro`) incremental transcript spooling and turn-end capture handoff. |
| `scripts/setup.py` / `scripts/component_update.py` / `scripts/storage_gc.py` | Atomic `oc.json` / `oc-source` provisioning, single-venv candidate upgrades, and bounded generation/spool GC. |
| `scripts/bounded.py` / `scripts/diagnostic_support.py` / `scripts/rsi_loop.py` | `mindie_harness.process` + pure-stdlib `ctypes` Windows Job runner, symlink-safe sanitized fault recording, and `mindie_harness.rsi` entrypoints. |

## 3. Local Verification

```bash
python3 .github/scripts/check_lf_eol.py
node --check index.mjs
node --check plugins/mindie-agent/paths.mjs
node --check plugins/mindie-agent/opencode_host.mjs
pytest -q -m "not live_npu and not live_opencode"
```
