# MindIE Agent for OpenCode `v0.1.12` — Windows Path Unification, Transcript Cursor Integrity & `mindie-harness v0.1.7` Alignment

[English](mindie-agent-oc-0.1.12.en.md)

- **清除失效的本地 `_rpc` 回退**：删除 `scripts/mcp_server.py` 中传入错误参数类型的本地 `_rpc` 旁路（`mcp_server.py` 缩减至 `33` 行），5 项 `knowledge_*` 工具统一由 `mindie-harness v0.1.7` 正式接口承载。
- **消除跨平台路径脑裂与裸 `python` 启动隐患**：统一 `opencode_host.mjs`、`mindie_launch.py` 与 `setup.py` 路径解析规则至 `ProfileRef.resolve("oc")`（Windows 使用 `%APPDATA%` / `%LOCALAPPDATA%`，POSIX 使用 XDG）；将 `.mcp.json` 迁移至无假桩风险的受管启动方式，修正 `pending-update-check.json` 标准 Schema，并仅在会话终态（idle）触发采集。
- **Transcript 增量游标与并发回收锁补齐**：在 `opencode_transcript.py` 中补齐 `FileIdentity.unserialize()`（兼容空文件）、`read_material(..., scan_until=...)` 记录边界栅栏与跨进程 `_spool_gc.lock` 缓冲池回收锁；单锁接入 9 域 `ensure_knowledge_binding`，并上线三平台 `verify-host-plugin.yml` 门禁。
