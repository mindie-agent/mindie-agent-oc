# mindie-agent-oc 0.1.8: Removal of `_mindie_bind_id` Hard Block, `mindie-harness` Substrate Adoption, and `0.1.13` Channel Inclusion

[中文](mindie-agent-oc-0.1.8.md)

- **Remove `_mindie_bind_id` Hard Block on Deterministic Tools (`AD-01` Reversal, `P2.7`)**: Eliminates mandatory `_mindie_bind_id` admission checks on `remote` and `tools` MCP calls so standard zero-metadata `tools/call` requests execute immediately, reserving `/mindie-agent` session admission strictly for `knowledge_*` (`P2.1`).
- **Shared `mindie-harness v0.1.0` Substrate & Dependency Cleanup**: Bundles verified `plugins/mindie-agent/assets/mindie-harness-seed.pyz` + `seed-lock.json`, delegates concurrent dual-framing stdio MCP routing, windowless process supervision, and RSI helpers to `mindie_harness`, and removes extraneous `pywin32`/benchmark spillover.
- **`0.1.13` Product Channel Inclusion & Formal Component Pins**: Locks `mindie-harness v0.1.0`, `mindie-knowledge v0.13.23`, `remote-dev v0.11.13`, `mindie-diagnostics v0.5.16`, `mindie-debugger v0.1.4`, `mindie-profiler v0.5.16`, `mindie-sim v0.9.18`, `mindie-coordinator v0.7.13`, and `npu-top v0.3.5` across `pyproject.toml`, `uv.lock`, and `product-contract.json`, aligned with `--profile host-plugin` governance.
