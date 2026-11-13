---
name: mindie-agent
description: Manually enable MindIE knowledge, domain skills, feedback, distillation and background capture for the current OpenCode session through /mindie-agent or $mindie-agent. Configured deterministic MCP tools remain available without this entry.
---

# MindIE Agent (OpenCode Unified Entry)

**Keep the user's task moving.** MindIE knowledge, setup, onboarding, diagnostics and local ledgers support the task. If one is unavailable, report that specific failure and continue independent work or use equivalent authorized host-native tools. Defer optional setup and sharing questions when they would interrupt the task. Retain existing job IDs and confirmed results. Reconcile an uncertain remote write before repeating that operation; do not bypass identity, permissions or actual device/port ownership. Coordinator-managed work keeps its leases; an existing verified host reservation may support equivalent native work. If ownership cannot be established, pause only that device action.

Use this skill **only** after the user explicitly invokes `/mindie-agent` or `$mindie-agent` in the current top-level OpenCode session. Do not activate it from topic matching.

1. **Manual Session Activation**:
   - The native OpenCode `/mindie-agent` command and `session.hook("prompt")` activate the session lease (`entry_enabled = 1`) in `admission.sqlite3` for `sessionID`, injecting this `SKILL.md` into `session.hook("context")` and injecting `OPENCODE_SESSION_ID` into session shell commands via `shell.hook("create.before")`.
   - Verify or inspect the private knowledge tool catalog via the paired Python runtime supplied in `<mindie-agent-skill>` (`OPENCODE_SESSION_ID` is auto-injected into session shells):
     ```bash
     "<python_runtime>" -I "<plugin_root>/scripts/knowledge_cli.py" describe
     ```
2. **Private Knowledge Catalog (`knowledge_cli.py`)**:
   - Knowledge tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) are intentionally absent from default MCP discovery (`mindie-remote-dev` and `mindie-tools`).
   - Execute any knowledge operation via `knowledge_cli.py` with JSON arguments on `stdin` (using the exact Python runtime and `knowledge_cli.py` paths supplied in the `<mindie-agent-skill>` header):
     ```bash
     "<python_runtime>" -I "<plugin_root>/scripts/knowledge_cli.py" knowledge_query <<'EOF'
     {"query": "Ascend A3 ACLGraph capture", "conditions": {"soc": "Ascend910C"}}
     EOF
     ```
   - Preserve the returned `isError` and nonzero exit status; never fabricate a session ID.
3. **Always-On Deterministic MCP Tools (`mindie-remote-dev` & `mindie-tools`)**:
   - `remote_*` (20 canonical remote development tools), `debugger`, `profiler`, `sim`, `coordinator`, `npu_top` (plus 6 live observation tools), `diagnostics_status`, `diagnostics_bundle`, and `mindie_status` are available in every session via MCP without requiring `/mindie-agent`.
4. **Two-Step Progressive Knowledge Loading & De-Biased Exploration (`mode="explore"`)**:
   - **Step 1 (`knowledge_query`)**: Query with observed `conditions` (`matched` | `mismatched` | `condition_unknown`; never treat `condition_unknown` as a match) and `token_budget` (`1..64000`, default `8000` in standard/reuse mode and `24000` in `mode="explore"`).
   - **Step 2 (`knowledge_explain`)**: Batch-read selected `refs=[...]` with `known_refs=[...]` and `token_budget=16000` (`1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`).
   - **De-Biased Exploration**: When 3 consecutive attempts stagnate (`assess_stagnation`), or when a baseline probe / prior attempt log shows standard `L3` heuristics stall due to a structural bottleneck, call `knowledge_query(..., mode="explore", conditions={...}, token_budget=24000, native_subcontext=True, ledger_entries=[...], bottleneck_summary={...}, goal=...)` and spawn an isolated OpenCode `task` subagent seeded **only** with `exploration_context.subagent_fact_pack.prompt_brief` (`L1` constraints + `L2` bottleneck evidence + `L4` mutators + `conditional_avoid`, with `L3` workflows stripped). When implementing a multi-factor `L4` mutator, expose optional keyword-only boolean ablation flags (defaulting to the full optimized candidate) directly on the primary operator function returning `(out_tensor, metrics_dict)`, and invoke that primary operator function directly across `full_candidate` and each leave-one-out arm so each arm executes a distinct tensor-level computation path. Use `<plugin_root>/scripts/experiment_ledger.py` and `<plugin_root>/scripts/rsi_loop.py` to record 4-gate RSI outcomes in `experiment-ledger.jsonl` and `PROGRESS.md`.

## Progressive Context Disclosure (Read Only on Demand)

- **Detailed tool schemas (`knowledge_*`, `remote_*`), dual-layer ledger (`PROGRESS.md` + `experiment-ledger.jsonl`), and NPU timing / profiler discipline**: read [references/domain-skills.md](references/domain-skills.md).
- **Session lifecycle, `/mindie-agent deactivate`, and `session.idle` capture rules**: read [references/activation-lifecycle.md](references/activation-lifecycle.md).
- **Installation, first-run onboarding (`question` prompt), workspace setup, and GitHub sign-in**:
  - Read [references/user-notice.md](references/user-notice.md), [references/workspace-setup.md](references/workspace-setup.md), and [references/github-authentication.md](references/github-authentication.md).
  - Right after installation or when `<python> -I "<plugin_root>/scripts/setup.py" sharing --status` reports `needs_onboarding_prompt: true` in an interactive session, use OpenCode's native **`question`** tool in the user's language (skipping interactive onboarding automatically in headless `--auto` runs) to ask:
    1. Local Development Workspace `vllm-ascend-workspace` (`Initialize vllm-ascend-workspace in current directory` vs `Keep current directory as workspace` vs `Use an existing workspace directory`)
    2. Community Knowledge Sharing & Automated Fault Reporting (`Enable Community Sharing & Fault Reporting` vs `Keep Local-Only Mode`)
    3. `Would you like to give mindie-agent a Star on GitHub ⭐?` (`Sure, star it ⭐` vs `Maybe later`)
  - After the user answers, resolve `WORKSPACE_ROOT` first, then run `<python> -I "<plugin_root>/scripts/setup.py" sharing --enable --project-root "$WORKSPACE_ROOT"` (or `--disable`), and if Star was chosen, run `gh api --hostname github.com --silent -X PUT /user/starred/mindie-agent/mindie-agent`.
