---
name: mindie-agent
description: "Ascend NPU, CANN, MindIE, and vLLM-Ascend engineering guidance with default-enabled graded L1-L4 knowledge retrieval, remote NPU development, profiling, roofline simulation, debugging, and cluster lease coordination."
---

# MindIE Agent (OpenCode Unified Entry)

**Keep the user's task moving.** MindIE knowledge, diagnostics, and local ledgers support the task. If one is unavailable, report that specific failure and continue independent work or use equivalent authorized host-native tools. Retain existing job IDs and confirmed results. Reconcile an uncertain remote write before repeating that operation; do not bypass identity, permissions, or actual device/port ownership. Coordinator-managed work keeps its leases; if ownership cannot be established, pause only that device action.

1. **Default-Enabled MCP Surfaces (`mindie-remote-dev` & `mindie-tools`)**:
   - `mindie-remote-dev` exposes 20 canonical `remote_*` tools; `mindie-tools` exposes `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`, `debugger`, `profiler`, `sim`, `coordinator`, `npu_top` (plus 6 observation tools), `diagnostics_status`, `diagnostics_bundle`, and `mindie_status` on startup without manual activation.
   - `knowledge_cli.py` is also available directly via the host-verified Python runtime in `<mindie-agent-skill>` without prior session activation.
2. **Two-Step Progressive Knowledge Loading & De-Biased Exploration (`mode="explore"`)**:
   - **Step 1 (`knowledge_query`)**: Query with observed `conditions` (`matched` | `mismatched` | `condition_unknown`; never treat `condition_unknown` as a match) and `token_budget` (`1..64000`, default `8000` in standard/reuse mode and `24000` in `mode="explore"`).
   - **Step 2 (`knowledge_explain`)**: Batch-read selected `refs=[...]` with `known_refs=[...]` and `token_budget=16000` (`1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`).
   - **De-Biased Exploration**: When repeated `L3` attempts stagnate (`assess_stagnation`) or standard `L3` heuristics stall on a structural bottleneck, call `knowledge_query(..., mode="explore", conditions={...}, token_budget=24000, native_subcontext=True, bottleneck_summary={...}, goal=...)` and spawn an isolated OpenCode `task` subagent seeded **only** with `exploration_context.subagent_fact_pack.prompt_brief` (`L1` constraints + `L2` bottleneck evidence + `L4` mutators + `conditional_avoid`, with `L3` workflows stripped). Verify multi-factor `L4` changes with clean leave-one-out ablations so each factor's causal contribution is measured independently.

## Progressive Context Disclosure (Read Only on Demand)

- **Detailed tool schemas (`knowledge_*`, `remote_*`) and Ascend NPU timing / profiler / lease discipline**: read [references/domain-skills.md](references/domain-skills.md).
- **Consent-gated `session.idle` capture and session isolation rules**: read [references/activation-lifecycle.md](references/activation-lifecycle.md).
