# MindIE Agent — OpenCode Native Harness Plugin (`mindie-agent-oc`)

This repository implements the **OpenCode (`oc`) Native Harness Plugin** (`index.mjs` + `plugins/mindie-agent/`) for MindIE Agent (`v2.2.0 <= opencode <= 2.99.99`, `@opencode-ai/plugin` Promise API). It bridges OpenCode v2's native plugin domains (`ctx.mcp.transform`, `ctx.command.transform`, `ctx.session.hook`, `ctx.tool.hook`, `ctx.shell.hook`, `ctx.event.subscribe`, `ctx.generate.text`, and read-only SQLite `opencode.db` v2 transcripts) to the shared MindIE runtime components (`mindie_knowledge`, `remote_dev`, `mindie_diagnostics`, `mindie_coordinator`, `mindie_debugger`, `mindie_profiler`, `mindie_sim`, and `npu_top`).

---

## 1. Plugin Module Navigation & Call Graph

All JavaScript and Python modules enforce `< 800` lines and carry standardized top-30-line Agent Contract headers (`[Role]`, `[Invariants]`, `[Timeouts & Locks]`, `[Side-Effect Policy]`):

| Layer | Module | Role & Key Responsibilities |
| :--- | :--- | :--- |
| **OpenCode v2 Host Entry** | `index.mjs` | Root `@mindie-agent/oc` plugin shim (`{ id: "mindie-agent", setup(ctx) }`) resolving the paired active `source_root` from `oc.json` or `./plugins/mindie-agent/opencode_host.mjs`. |
| | `plugins/mindie-agent/opencode_host.mjs` | Registers `mindie-remote-dev` and `mindie-tools` (`timeout.execution = 660000`), `/mindie-agent` slash command, `session.hook("prompt" \| "context")`, `tool.hook("execute.before")`, `shell.hook("create.before")`, `event.subscribe()` (`session.idle`), and `127.0.0.1:0` Bearer summary endpoint backed by `ctx.generate.text`. |
| | `plugins/mindie-agent/paths.mjs` | JS path resolver byte-for-byte aligned with `scripts/paths.py`; never mutates `~/.config/opencode` or `~/.local/share/opencode`. |
| **Native Launcher & Setup** | `scripts/mindie_launch.py` | Stable dispatcher (`hook \| mcp \| setup \| update`) enforcing `HOOK_TOTAL=10.0s`, `HOOK_LOCK_BUDGET=2.0s`, `BOOTSTRAP_TOTAL=540.0s`, and paired `.venv` + payload selection. |
| | `scripts/setup.py` / `scripts/paths.py` | Provisions `oc-venv` (`uv sync`), validates `product-contract.json` & `harness-compatibility.json`, writes `oc.json` / `oc.engine.json` atomically (`os.replace()`), and manages community sharing consent. |
| | `scripts/component_update.py` / `scripts/oc_native_compat.py` | Checks/applies channel updates under file lock and registers `"opencode"` in `mindie_knowledge.releases`. |
| **Lifecycle & Identity** | `scripts/bridge.py` / `scripts/admission.py` | Handles `gesture`, `session-state`, `pre-tool-use`, and `stop` against `admission.sqlite3` without revoking active leases if default profile init degrades. |
| | `scripts/identity.py` | Validates `ses_...` / UUID session IDs, rejects forged identity arguments in tool calls, parses `/mindie-agent` & `$mindie-agent`, and manages one-shot WAL binds in `mcp-binds.sqlite3`. |
| **MCP & Private Knowledge CLI** | `scripts/mcp_server.py` | Public stdio MCP server (`remote` = 20 `remote_*` tools, `tools` = 14 deterministic tools, **0** `knowledge_*` tools) with Receipt Honesty (`isError=False` + `receipt_warning` + `receipt_unconfirmed=True` if local receipt/bind accounting fails after remote success). |
| | `scripts/knowledge_cli.py` | Private CLI (`describe`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) gated strictly by `OPENCODE_SESSION_ID` + active `Admission.knowledge_lease`. |
| **Transcript Spool & Summary** | `scripts/opencode_transcript.py` / `scripts/capture_stop.py` | Projects public `user` & visible `assistant` text parts read-only (`?mode=ro`) from OpenCode's `opencode.db` (`session_v2`, `session_message`, `session_part`) into immutable-prefix `<state_dir>/transcripts/<sessionID>.jsonl` and hands off `session.idle` receipts to `capture_hook`. |
| | `scripts/organizer.py` / `scripts/bounded.py` | Isolated summary & C6 distillation worker (`mindie-summary-outcome/1`) with zero retries on `outcome_unknown`, plus POSIX process-group / Windows Job Object (`CREATE_NO_WINDOW`) bounded execution. |

### Call Graph

```text
OpenCode Host (`setup(ctx)`)
  ├─► ctx.mcp.transform ──► mindie_launch.py mcp (remote | tools) ──► mcp_server.py
  ├─► ctx.command.transform ("/mindie-agent") & ctx.session.hook("prompt")
  │     └─► mindie_launch.py hook gesture ──► bridge.py ──► admission.activate / deactivate / status_facts
  ├─► ctx.session.hook("context")
  │     └─► (!admission.sqlite3 ? 0ms no-op : mindie_launch.py hook session-state) ──► inject SKILL.md
  ├─► ctx.tool.hook("execute.before")
  │     ├─► MindIE MCP tool ──► mindie_launch.py hook pre-tool-use ──► identity.publish_bind
  │     └─► Shell tool ──► enqueue (command, sessionID) ──► ctx.shell.hook("create.before") sets OPENCODE_SESSION_ID
  └─► ctx.event.subscribe ("session.idle")
        └─► detached capture_stop.py ──► opencode_transcript.sync_session_spool ──► capture_hook
```

---

## 2. Core Guardrails

- **Zero Host Config Mutation**: Never mutate `~/.config/opencode` or `~/.local/share/opencode/opencode.db` from MindIE runtime hooks (`opencode.db` is opened strictly `?mode=ro&immutable=0`).
- **Zero Console Popup (`P1.5`)**: Every Node `spawn` sets `windowsHide: true` (never combined with `detached: true` on `win32`), and every Python subprocess on Windows uses `CREATE_NO_WINDOW`.
- **State & Receipt Honesty**: Never swallow `TimeoutError` inside `except OSError:`, never retry `outcome_unknown` model calls, and never turn a succeeded remote operation into `isError=True` on local SQLite accounting failure.

---

## 3. Verification Commands

```bash
# Run full Tier 0 + Tier 1 unit, host-isolation, SQLite spool, and OpenCode v2 loading tests
.venv/bin/pytest tests -q
```
