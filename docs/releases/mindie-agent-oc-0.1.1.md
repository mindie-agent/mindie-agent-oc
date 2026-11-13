# mindie-agent-oc 0.1.1

[English](mindie-agent-oc-0.1.1.en.md)

`mindie-agent-oc` `0.1.1` 维护与质量加固版本发布：围绕 OpenCode v2（`@opencode-ai/plugin >= 2.2.0`）生命周期事件、对话增量采集契约、多会话并发隔离、冷启动非阻塞引导与跨代际升级连续性完成全面审计修复。

- **对话采集契约与落盘脱敏加固**：完整页 `read_material` 保持 `coverage=[]`、支持超过 `48 KiB` / `256 KiB` 超长消息不截断投影与 `> 1 MiB` 首条记录流式头解析，损坏或越界记录写入 `discarded_records` 阻止游标静默越过；`opencode_spool.py` 增加进程级文件锁、残尾自愈、500 行分批 SQLite 查询、流式未完成 `assistant` 消息游标驻留保护、`not_before` 授权边界过滤，以及 `>= 4 KiB` 连续内联二进制块线性预掩码、凭据与私密路径先脱敏后落盘。
- **C6 蒸馏与 CLI 回退宿主隔离**：修复 `organizer.py` 中 `distill.decision_schema()` 无参调用契约；无回环摘要服务回退至 `opencode run --standalone` 时支持 `opencode.jsonc` 配置解析与只读复制 `opencode.db` 认证表至 `0600` 临时沙箱数据库，隔离临时 `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `OPENCODE_DB` 并剥离继承的 `OPENCODE_CONFIG*` / `OPENCODE_SESSION_ID`，杜绝污染宿主数据库或重入插件。
- **多会话并发隔离、冷启动引导、原子升级与诊断隐私保护**：34 个公开 MCP 工具 Schema 显式声明可选 `_mindie_bind_id`（兼容 OpenCode `v2.2.7` 后置参数解码）并在执行前一次性认领剥离，并发 Shell 子进程通过内联注释令牌实现严格一对一绑定；`setup(ctx)` 同步挂载事件流并通过 `--event-json` 投递 `session.execution.succeeded` / `failed` / `interrupted`（过滤进程退出时的 `shutdown`）与 `session.idle` 去重汇聚；冷启动（含 `-S` 纯标准库环境）`/mindie-agent` 秒级激活会话租约并后台异步完成 `uv sync --locked` 引导、租约晋升与 MCP 热重载；`index.mjs` 与 `component_update.py` 支持新装插件根自动优先发现、防旧版 `--source` 降级与先准备后原子切换 `oc.json`；同步接入 `diagnostics_v2` 操作诊断与隐私过滤（`diagnostic_support.py`）。

验证覆盖 32 项本地契约与审计回归测试、`mindie_knowledge` `Engine` 端到端采集与 C6 蒸馏、进程组清理、微基准以及 OpenCode `v2.2.7`（`deepseek/deepseek-flash#max`）实机会话核验，不声称用户业务主机或 NPU 模型精度已重新验收。

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
