# mindie-agent-oc 0.1.0

[English](mindie-agent-oc-0.1.0.en.md)

`mindie-agent-oc` 首个正式版本发布：提供 OpenCode v2（`@opencode-ai/plugin >= 2.2.0`）原生客户端插件适配（npm 包名 `@mindie-agent/oc`），支持 GitHub 与 npm 双通道分发。

- **双通道安装与升级连续性**：支持 `opencode plugin add github:mindie-agent/mindie-agent-oc`、`opencode plugin add @mindie-agent/oc` 与本地 `"file:///..."` 挂载；插件升级与共享运行时组件后台自动更新采用 `(python, runtime_scripts)` 成对代际切换与单实例文件锁，候选准备失败时保留原有效代际与用户配置。
- **默认全量开放确定性 MCP 工具（零 `knowledge_*` 污染）**：通过 `ctx.mcp.transform` 注册 `mindie-remote-dev`（20 项远端开发与后台作业工具，660s 执行上限）与 `mindie-tools`（14 项确定性性能剖析、建模仿真、故障解码、集群租约与诊断工具）。
- **显式 `/mindie-agent` 按会话隔离激活分级知识飞轮**：仅在顶层主会话显式触发 `/mindie-agent` 或 `$mindie-agent` 后注入 `SKILL.md` 指引、为该会话 Shell 绑定 `OPENCODE_SESSION_ID` 解锁私有 `knowledge_cli.py`，并在 `session.idle` 时从只读 `opencode.db` 过滤 `reasoning` / `tool` / `synthetic` 分片后增量落盘公开对话。

验证覆盖 OpenCode v2 Promise 插件契约、stdio MCP 收据诚实性、跨代际升级保持、公开对话脱敏微基准与实机 OpenCode `v2.2.7` 会话端到端核验，不声称用户业务主机或 NPU 模型精度已重新验收。

## 正式依赖组合

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
