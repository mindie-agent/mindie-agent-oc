## 变更摘要 (Summary)

- 

## 设计原则 Review (Pre-PR Checklist)

> 对照 `AGENTS.md` 与 `host-plugin` 仓契约完成核验（不适用项须填写 `not-applicable: <具体原因>`）：

- [ ] **HOST-NATIVE** — OpenCode v2 ESM 插件入口（`index.mjs` / `opencode_host.mjs`）、只读 `opencode.db`（`?mode=ro`）投影及环回摘要端点契约兼容；所有 JS `spawn`/`spawnSync` 具备 `windowsHide: true` 与 `detached: process.platform !== "win32"`，Python Windows 子进程具备 `CREATE_NO_WINDOW` 且零 `pywin32` 依赖。
- [ ] **HOST-WIRE** — 常驻并发 stdio MCP 全部委托 `mindie_harness.api.serve_mcp`（兼容 `Content-Length` 与 JSONL 帧、读写/控制快慢池隔离、支持 `notifications/cancelled`），`OCManagedAdapter` 仅补 `oc` 远端默认值与知识 RPC 回退。
- [ ] **HOST-ADMISSION** — 全量工具（含默认启用的 5 项 `knowledge_*`）彻底消除 `_mindie_bind_id` 预绑定硬门禁（`P2.7` / `AD-01`：无 `_meta`、无 `OPENCODE_SESSION_ID`、无前置 JS Hook 亦可 100% 调用）且共享 `REMOTE_DEV_STATE_DIR`（`AD-02`）；`session.idle` 先在纯 Node.js 校验社区共享授权与 `project_roots` 再按需派生 `mindie_launch.py hook stop`，本地记账失败保持 Receipt Honesty（Principle 14）。
- [ ] **HOST-UPGRADE** — 种子包校验（`assets/seed-lock.json` 流式 SHA-256）、`oc.json` 原子切换、`.inuse` 运行时租约保护及历史版本平滑升级兼容性已验证，全仓文本保持 Unix `LF`（`\n`）且单模块 `< 800` 行。

## 自查证据 (Evidence)

- **HOST-NATIVE**: 
- **HOST-WIRE**: 
- **HOST-ADMISSION**: 
- **HOST-UPGRADE**: 

## 验证结果 (Verification)

- 
