# mindie-agent-oc 0.1.2

[简体中文](mindie-agent-oc-0.1.2.md)

Design-principles and end-to-end reliability hardening release of `mindie-agent-oc` (`v0.1.2`) aligned with the `mindie-agent#332` audit gate across state honesty, timeout budget arithmetic, nested process-group cleanup, corrupt-state visibility, and formal shared component baselines.

- **State Honesty & Anti-Silent-Fallback Hardening (Category A)**: Removes silent `{}` + `exit 0` hook exception masking in `mindie_launch.py` and fails closed on `pre-tool-use` bind or timeout errors; emits `-32700 Parse error` on malformed UTF-8 / JSON / `Content-Length` stdio frames and `-32600 Invalid Request` on invalid envelopes in `mcp_server.py`, rejects empty `npu-top` responses, distinguishes post-success receipt/bind `TimeoutError` (`isError=False` + `receipt_unconfirmed=True`) from post-failure propagation, and isolates optional background updater timeouts during MCP initialization so independent MCP calls remain operational; isolates post-completion pipe draining from process-tree cleanup failures in `bounded.py` and `organizer.py` (`status="returned"`, `cleanup_failed=True`) so completed child output is never discarded; moves optional `prepare_schema` out of the critical hook path into `setup.py` / `component_update.py` while scheduling bounded deferred `--prepare-schema` recovery after fast activation and persisting/clearing `schema_warning` across `bridge.py` and `admission.py`; and surfaces corrupt/unreadable config, cursor, and SQLite JSON records across `setup.py`, `opencode_spool.py`, `capture_stop.py`, and `paths.mjs` (`repair_required=True` / `stage="unavailable"`).
- **Timeout Arithmetic & Nested Process-Tree Supervision (Categories B & C)**: Enforces `HOOK_NODE_TIMEOUT_MS = 15000 >= HOOK_TOTAL (10.0s) + 5.0s` startup margin (`HOOK_TOTAL = 8.0s + 2.0s`); propagates `--timeout` end-to-end across `setup.py`; derives `knowledge_cli.py` re-execution budgets from the active operation (`summary_budget.distill_rpc_timeout() + 60.0s` for `knowledge_distill` and `90.0s` for standard RPCs); tracks nested `bounded.run()` process groups across re-execution boundaries and closes Windows Job Object handles on attachment failure or exit so zero descendants or handles leak on timeout; and reclaims stale dead-PID `in_progress` update markers while reporting `read_inventory` failures honestly (`status="failed"`).
- **Formal Shared Component Baseline Upgrade**: Pins all 8 shared runtime components to their formal `#332`-hardened GitHub Releases.

Verification covers local contract and `#332` fault-injection regression suites, POSIX nested process-tree termination probes, LF line-ending checks, and release metadata validation; no user production host or NPU model accuracy claim is made.

## Verified Dependency Combination

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.16 | `99d65f11d904bb945d33cf3a9e006609702d8dda` |
| remote-dev | 0.11.9 | `6e9882e0fa495bf2feeef29ec86492498d364531` |
| diagnostics | 0.5.10 | `37ed7f04a47eae8097cc286335186c3fcd9584b3` |
| debugger | 0.1.1 | `28c558bc545680cdbad60771d1eeb902955ddd3f` |
| profiler | 0.5.11 | `f7dbca5e643db52807e12649e02291069f3d9d82` |
| sim | 0.9.11 | `f0b19acdab6f5f4e952561c7ff92e4bf9c0b0c3a` |
| coordinator | 0.7.8 | `db9b0c6432001d6049056eabd37422d0bfb9f75a` |
| npu-top | 0.3.2 | `8a68947e730b21b5ed65c50c34aec0e923b3a14c` |
