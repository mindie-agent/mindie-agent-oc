# mindie-agent-oc 0.1.3

[English](mindie-agent-oc-0.1.3.en.md)

`mindie-agent-oc` `0.1.3` 接入 Diagnostics 2.0 统一可观测体系，补齐子进程崩溃调用令牌归属核验、晋升后收据诚实性与正式共享运行时组件升级。

- **Diagnostics 2.0 归属核验与契约对齐**：在 `mcp_server.py` 与 `diagnostic_support.py` 中注入并校验每次子进程调用独立的 `MINDIE_DIAGNOSTICS_INVOCATION_TOKEN`，避免伪造或陈旧 `incident_id` 污染诊断上报，并将自动 Issue 投递统一收敛至 `mindie-agent/mindie-agent`。
- **晋升后收据诚实性（Post-Promotion Receipt Honesty）**：在 `oc.json` 完成原子晋升后，若遗留镜像同步、社区配置初始化、存储 Schema 准备、知识源同步或状态落盘发生 `TimeoutError` / `OSError`，严格保留 `status="updated"` 与 `needs_host_reload=True`，并附带 `receipt_warning` 与 `receipt_unconfirmed=True`。
- **正式共享运行时组件升级**：同步升级并锁定 `knowledge 0.13.17`、`remote-dev 0.11.10`、`diagnostics 0.5.11`、`coordinator 0.7.9`、`debugger 0.1.2` 与 `npu-top 0.3.3`。

## 正式依赖组合

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
