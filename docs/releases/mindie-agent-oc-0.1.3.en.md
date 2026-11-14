# mindie-agent-oc 0.1.3

[简体中文](mindie-agent-oc-0.1.3.md)

`mindie-agent-oc` `0.1.3` integrates Diagnostics 2.0 unified observability, child-crash invocation provenance verification, post-promotion receipt honesty, and refreshed shared runtime component baselines.

- **Diagnostics 2.0 Provenance & Unified Reporting**: Injects and verifies per-invocation `MINDIE_DIAGNOSTICS_INVOCATION_TOKEN` across `mcp_server.py` and `diagnostic_support.py` to reject forged or stale `incident_id` stderr markers while centralizing opt-in automatic issue filing into `mindie-agent/mindie-agent`.
- **Post-Promotion Receipt Honesty**: Once `oc.json` has been atomically promoted, post-promotion `TimeoutError` or `OSError` faults in legacy mirror writes, initial community config creation, store schema preparation, feed sync, or status-file persistence preserve `status="updated"` and `needs_host_reload=True` with `receipt_warning` and `receipt_unconfirmed=True`.
- **Formal Shared Runtime Component Upgrade**: Upgrades and pins `knowledge 0.13.17`, `remote-dev 0.11.10`, `diagnostics 0.5.11`, `coordinator 0.7.9`, `debugger 0.1.2`, and `npu-top 0.3.3`.

## Verified Dependency Combination

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.17 | `315fc4bc51ee9a7afef32a3666599d608ff59f9d` |
| remote-dev | 0.11.10 | `3556df571450dab481a25e0157167f1c3727dc6c` |
| diagnostics | 0.5.11 | `171623584aa812677df907518dcb55b06493241b` |
| debugger | 0.1.2 | `d40a250e4048ac63d4a478a5dbfa993702ceddd0` |
| profiler | 0.5.11 | `f7dbca5e643db52807e12649e02291069f3d9d82` |
| sim | 0.9.11 | `f0b19acdab6f5f4e952561c7ff92e4bf9c0b0c3a` |
| coordinator | 0.7.9 | `d8977114e8cd0e5b7befa59a5ff5e53f40a74521` |
| npu-top | 0.3.3 | `848d74da3cba383c07a0eb372a17baf35b6b944b` |
