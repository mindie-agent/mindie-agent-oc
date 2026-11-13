# mindie-agent-oc 0.1.0

[简体中文](mindie-agent-oc-0.1.0.md)

Initial formal release of `mindie-agent-oc` (npm package `@mindie-agent/oc`), the native OpenCode v2 (`@opencode-ai/plugin >= 2.2.0`) client harness adapter for MindIE Agent.

- **Dual-Channel Distribution & Upgrade Continuity**: Supports `opencode plugin add github:mindie-agent/mindie-agent-oc`, `opencode plugin add @mindie-agent/oc`, and local `"file:///..."` plugin mounts. Plugin upgrades and independent 300s background component updates enforce paired `(python, runtime_scripts)` generation cutover under a single-instance OS file lock, preserving the active generation if candidate preparation fails.
- **Always-On Deterministic MCP Tools (Zero `knowledge_*` in Default Discovery)**: Registers `mindie-remote-dev` (20 canonical remote NPU development and async job tools, 660s execution ceiling) and `mindie-tools` (14 deterministic profiling, simulation, fault triage, cluster coordination, telemetry, and diagnostics tools) via `ctx.mcp.transform`.
- **Per-Session `/mindie-agent` Knowledge Flywheel**: Injects `SKILL.md` via `session.hook("context")`, binds `OPENCODE_SESSION_ID` into session shells via `shell.hook("create.before")` to unlock private `knowledge_cli.py`, and spools public turns read-only from `opencode.db` on `session.idle` (stripping `reasoning`, `tool`, and `synthetic` parts before disk persistence) strictly for top-level manually activated sessions.

Verification covers OpenCode v2 plugin contracts, stdio MCP receipt honesty, paired generation upgrade continuity, public-transcript sanitization microbenchmarks, and live OpenCode `v2.2.7` end-to-end verification; no user production host or NPU model accuracy claim is made.

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
