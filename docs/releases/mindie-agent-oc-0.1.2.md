# mindie-agent-oc 0.1.2

[English](mindie-agent-oc-0.1.2.en.md)

`mindie-agent-oc` `0.1.2` 设计原则与全链路稳健性加固版本发布（对齐 `mindie-agent#332` 审计门禁），围绕状态诚实性、超时预算算术、嵌套进程组清理、损坏状态显式暴露与正式共享组件基线升级完成系统收敛。

- **状态诚实性与反静默回退（Category A）**：移除 `mindie_launch.py` Hook 分发的 `{}` + `exit 0` 静默吞错并在 `pre-tool-use` 绑定或超时失败时严格拒绝放行；`mcp_server.py` 对非法 UTF-8 / JSON / `Content-Length` 帧显式返回 `-32700 Parse error`、对非对象请求返回 `-32600 Invalid Request`、拒绝空的 `npu-top` 响应，并在本地回执/绑定收尾发生 `TimeoutError` 时按主调用成败严格区分收据告警（`isError=False` + `receipt_unconfirmed=True`）与异常上抛，同时隔离 `initialize` 阶段可选后台更新检查超时以保障独立 MCP 请求连续性；`bounded.py` 与 `organizer.py` 将子进程完成后进程树清理与管道排空解耦，即使收尾清理超时仍完整保留已输出内容并标记 `cleanup_failed=True`；`setup.py` / `component_update.py` / `bridge.py` 将可选 `prepare_schema` 迁出 Hook 关键路径、在快速激活后异步调度有界 `--prepare-schema` 恢复，并持久化透传与自动清除 `schema_warning`；`setup.py`、`opencode_spool.py`、`capture_stop.py` 与 `paths.mjs` 显式区分缺失文件与损坏/不可读配置、游标及消息记录（`repair_required=True` / `stage="unavailable"`）。
- **超时算术与跨层进程树收敛（Category B & C）**：对齐外层宿主 Hook 预算（`HOOK_NODE_TIMEOUT_MS = 15000 >= HOOK_TOTAL 10.0s + 5.0s` 启动裕量，`HOOK_TOTAL = 8.0s + 2.0s`）；`setup.py` 全路径透传 `--timeout` 预算；`knowledge_cli.py` 重执行外层预算按操作动态推导（`knowledge_distill` 为 `summary_budget.distill_rpc_timeout() + 60.0s`，常规 RPC 为 `90.0s`）；`bounded.py` 与重执行入口通过受管进程组登记与 Windows Job Object 句柄防泄漏清理确保外层超时不遗留嵌套子进程；`component_update.py` 与 `opencode_host.mjs` 支持残留死进程 `in_progress` 状态立即回收，并在 `read_inventory` 失败时诚实记录 `status="failed"`。
- **正式依赖基线同步升级**：将全部 8 个共享运行时组件升级并锁定至已完成 `#332` 收敛的正式 GitHub Release 版本。

验证覆盖本地契约回归测试、`#332` 专项故障注入与进程树清理测试、LF 行尾与发布元数据校验，不声称用户业务主机或 NPU 模型精度已重新验收。

## 正式依赖组合

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
