# mindie-agent-oc 0.1.8：移除 `_mindie_bind_id` 硬拦截反模式、接入 `mindie-harness` 底座与纳入 `0.1.13` 产品通道

[English](mindie-agent-oc-0.1.8.en.md)

- **移除确定性工具 `_mindie_bind_id` 硬拦截反模式（反转 `AD-01`，贯彻 `P2.7`）**：彻底删除对 `remote` 与 `tools` 确定性工具调用强制要求 `_mindie_bind_id` 的阻断门禁，标准 MCP `tools/call` 零前置参数即可直接执行；仅 `knowledge_*` 保留显式 `/mindie-agent` 会话准入校验（`P2.1`）。
- **接入 `mindie-harness v0.1.0` 共享底座与清理越界依赖**：内置确定性冷启动包 `plugins/mindie-agent/assets/mindie-harness-seed.pyz` + `seed-lock.json`，复用 `mindie_harness` 并发双帧 stdio 路由、无窗子进程监管（移除冗余第三方 `pywin32` / 评测运行器越界残留）与单状态源 RSI 闭环。
- **正式纳入 `0.1.13` 产品通道并锁定全量组件**：在 `pyproject.toml`、`uv.lock` 与 `product-contract.json` 中同步锁定 `mindie-harness v0.1.0`、`mindie-knowledge v0.13.23`、`remote-dev v0.11.13`、`mindie-diagnostics v0.5.16`、`mindie-debugger v0.1.4`、`mindie-profiler v0.5.16`、`mindie-sim v0.9.18`、`mindie-coordinator v0.7.13` 与 `npu-top v0.3.5`，对齐 `--profile host-plugin` PR 设计原则门禁。
