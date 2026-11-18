# Domain Tooling & NPU Discipline

## 1. Graded Knowledge Tools (`mindie-tools`)

- `knowledge_query`: searches graded `L1-L4` units and task blocks (`token_budget` `1..64000`). Pass observed `conditions` (`matched` / `mismatched` / `condition_unknown`; never treat unknown as a match).
- `knowledge_explain`: reads exact `mindie://<domain>/units/<id>@<rev>` references and expands `requires` once with `known_refs`.
- `knowledge_feedback`: records `discovered`, `loaded`, `applied`, or `evaluated` (`supported` / `refuted` / `inconclusive`).
- `knowledge_distill`: distills verified milestone or completed evidence into graded units; `L4` mutators need a verified leave-one-out ablation receipt.
- `knowledge_sync`: refreshes the public graded feed for the selected domain.
- `mindie_doctor`: read-only readiness check for runtime pins, prerequisites, remote connectivity, and diagnostics; run it before blaming a model or device.

## 2. Remote NPU Execution (`mindie-remote-dev`, 14-tool `oc` profile)

- `remote_auth(action="setup", save_default=True, discover=True)` stores shared SSH access and discovers endpoints in one step; `action="status"` is read-only.
- Core tools: `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_list`, `remote_probe`, `remote_artifact_pull`, and `remote_artifact_push`.
- Support and jobs: `remote_auth`, `remote_job_status`, `remote_job_output`, and `remote_job_stop`; always retain the `job_id` and inspect live status before replacing or stopping a run.
- Acquire NPU and port leases through `coordinator` before device workloads (`vram_free_mb >= expected VRAM`; respect Ascend A3 910C dual-die `Phy-ID 0..15` package boundaries).
- Prefer non-destructive profiling (`profiler`), roofline simulation (`sim`), and `debugger` triage before restarts.

## 3. Continuity Contract

If a MindIE tool is unavailable, report the exact failure once (after reconciling any uncertain write), then continue with native `ssh`/`scp`, local scripts, or another authorized path. Never stall the user's task on an accelerator.
