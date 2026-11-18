---
name: mindie-agent
description: "Ascend NPU, CANN, MindIE, and vLLM-Ascend engineering with default-enabled graded L1-L4 knowledge, remote NPU development, profiling, simulation, debugging, cluster leases, and diagnostics."
---

# MindIE Agent (OpenCode Unified Entry)

**Keep the user's task moving.** MindIE tools (`remote-dev`, `coordinator`, `knowledge`, `mindie_doctor`) are accelerators, never roadblocks. If any tool fails, report that specific failure once, reconcile an uncertain remote write before repeating it, and continue immediately through native `ssh`/`scp` or local scripts. Never bypass identity, permissions, or actual device/port ownership; coordinator-managed work keeps its leases.

1. **Default-On Surfaces**: `mindie-remote-dev` exposes the 14-tool `oc` remote profile; `mindie-tools` exposes deterministic component tools, `mindie_status`, `mindie_doctor`, diagnostics, and the 5 `knowledge_*` tools with zero manual activation and zero `_mindie_bind_id`.
2. **Readiness First**: Run `mindie_doctor` when remote execution, diagnostics, or knowledge retrieval looks unavailable. It is read-only and reports the exact prerequisite instead of retrying blindly.
3. **Progressive Knowledge**: `knowledge_query` first (observed `conditions`, bounded `token_budget`), then `knowledge_explain` with `refs` and `known_refs`. Use `mode="explore"` only when standard `L3` attempts stagnate on a structural bottleneck; verify `L4` changes with clean leave-one-out ablations.
4. **Remote NPU Discipline**: Acquire NPU and port leases through `coordinator` before device workloads, address background jobs by `job_id` / `execution_id`, and inspect live handles before replacing or stopping them.

## Progressive Context Disclosure (Read Only on Demand)

- **Tool schemas, the 14-tool `oc` profile, `remote_auth(..., save_default=True, discover=True)`, and Ascend NPU timing/profiler/lease discipline**: read [references/domain-skills.md](references/domain-skills.md).
- **Consent-gated capture, sharing tiers, and session isolation**: read [references/activation-lifecycle.md](references/activation-lifecycle.md).
