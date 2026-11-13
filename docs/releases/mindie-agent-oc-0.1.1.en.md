# mindie-agent-oc 0.1.1

[简体中文](mindie-agent-oc-0.1.1.md)

Maintenance and reliability hardening release of `mindie-agent-oc` (`v0.1.1`) for OpenCode v2 (`@opencode-ai/plugin >= 2.2.0`), closing audit findings across transcript capture, multi-session concurrency isolation, cold-start bootstrap, and paired generation upgrades.

- **Transcript Capture Contract & Pre-Disk Redaction**: Complete `read_material` pages keep `coverage=[]`, preserve single messages exceeding `48 KiB` / `256 KiB` without truncation, stream `> 1 MiB` initial records in `capture_source()`, and report unreadable/corrupt records via `discarded_records` so cursors never silently advance past damaged data; `opencode_spool.py` enforces cross-process OS file locking, torn-tail recovery, 500-row SQLite pagination, in-flight streaming `assistant` cursor holdback, `not_before` lease-boundary filtering, linear `>= 4 KiB` opaque-blob pre-masking, and credential/private-path redaction before disk persistence.
- **C6 Distillation & CLI Fallback Isolation**: Fixes `distill.decision_schema()` invocation in `organizer.py`, supports `opencode.jsonc` model discovery, and seeds read-only `opencode.db` auth tables into ephemeral `0600` scratch SQLite storage while isolating `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `OPENCODE_DB` and stripping inherited `OPENCODE_CONFIG*` and `OPENCODE_SESSION_ID` variables.
- **Concurrent Session Correlation, Cold-Start Bootstrap, Atomic Upgrades & Diagnostics v2**: Declares optional `_mindie_bind_id` across all 34 public MCP schemas (compatible with OpenCode `v2.2.7` post-hook Zod decoding) and correlates concurrent shell spawns via stripped inline comment tokens; awaits `ctx.event.subscribe()` during `setup(ctx)` and passes `--event-json` to detached capture workers across `session.execution.succeeded` / `failed` / `interrupted` (ignoring `reason === "shutdown"`) and `session.idle`; activates `/mindie-agent` leases immediately on cold start (including `-S` stdlib-only hosts) while running `uv sync --locked` asynchronously with lease promotion and live MCP reload; ensures `index.mjs` + `component_update.py` prefer newly installed plugin versions, prevent older `--source` downgrades, and validate/prepare candidates before atomically flipping `oc.json`; and integrates `diagnostics_v2` operational filtering (`diagnostic_support.py`).

Verification covers 32 contract and audit regression tests, `mindie_knowledge` `Engine` capture + C6 distillation, POSIX process-group reaping, sanitization microbenchmarks, and live OpenCode `v2.2.7` (`deepseek/deepseek-flash#max`) end-to-end verification; no user production host or NPU model accuracy claim is made.

## Verified Dependency Combination

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.14 | `ac4a89e993c89deba5a4a629e1cd89d61e57c843` |
| remote-dev | 0.11.6 | `991ef7863b2b208f94f54a29db4ac5be50884ea3` |
| diagnostics | 0.5.8 | `40ce12019354c26ef9006c0d4abc6ffade96afbd` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.4 | `6f1d5a871a7b1ef20ff7a8cab33aaf4637fb3f66` |
| coordinator | 0.7.5 | `819b0deb7bea994d4dff9c009200a48dd9300cc4` |
| npu-top | 0.3.1 | `8f0cb3ec66393812e90e118cd73704070c6027a2` |
