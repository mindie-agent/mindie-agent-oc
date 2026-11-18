# MindIE Agent for OpenCode `v0.1.11` — Great Subtraction + UX Upgrade

`v0.1.11` 将 OpenCode 适配器收敛为 **6 个瘦宿主生产文件**，把运行时、进程监督、MCP 并发、更新与代际租约全部交给 `mindie-harness v0.1.3`，并重做安装与首次配置体验。

## 1. Great Subtraction

- 删除 `paths.mjs`、`session_support.mjs` 以及 23 个历史 Python 模块（`admission.py`、`bounded.py`、`bridge.py`、`capture_stop.py`、`component_update.py`、`contract_validation.py`、`diagnostic_support.py`、`experiment_ledger.py`、`host_process.py`、`host_adapter.py`、`identity.py`、`knowledge_cli.py`、`mcp_stdio.py`、`oc_native_compat.py`、`opencode_spool.py`、`organizer.py`、`paths.py`、`release_metadata.py`、`rsi_loop.py`、`runtime_bootstrap.py`、`storage_gc.py`、`storage_spool.py`、`storage_support.py`）。
- 生产面仅保留 `index.mjs`、`opencode_host.mjs`、`mindie_launch.py`、`mcp_server.py`、`opencode_transcript.py`、`setup.py`，合计 `< 1,200` 行。
- `.mjs` + `.py` 总行数由静态 `storage_lifecycle_gate.py` 直接校验；删除文件不再被任何 workflow、`code_paths` 或 `test_ids` 引用。

## 2. Thin Host Behavior

- **MCP**：`mindie-remote-dev` 使用 14 项 `oc` 原生远端档案，`mindie-tools` 提供确定性组件、`mindie_status`、`mindie_doctor`、诊断与 5 项默认启用的 `knowledge_*` 工具；`OCManagedAdapter` 统一共享 `state/oc/remote`。
- **Context**：`session.hook("context")` 继续在纯 Node.js 中注入缓存 `SKILL.md`，仅限顶层会话（非 Subagent、非 Fork），每轮零 Python 子进程。
- **Capture**：终态事件在纯 Node.js 中检查 `mindie-community.json`、`mindie-consent.json`（`contribute` / `local-only`）和规范化 `project_roots`；未授权时零子进程、零 SQLite 文件，授权时以 `250ms` 去重并派生 `mindie_launch.py hook stop`。
- **Transcript**：`opencode_transcript.py` 严格 `?mode=ro`，过滤 `reasoning`/`tool`/`synthetic`/`ignored`，恢复撕裂尾部，按 `[start, end)` SHA-256 记账，并将冷 spool 上限压到 `64` 个 JSONL。
- **Launcher**：`mindie_launch.py` 提供纯 stdlib 快路径、seed 完整性校验、活动解释器 re-exec、`summary` HTTP 桥与 `mcp` / `setup` 分发。
- **Setup**：`setup.py` 写入 `oc.engine.json` / `oc.json`，将 `sharing` / `doctor` / `update` 委托给 `mindie-harness`，并新增 `release-metadata` 子命令替代 `release_metadata.py`。

## 3. UX Upgrade

- 一键安装：

  ```bash
  uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
  ```

- `gh` 完全可选：已登录推荐 `contribute`，否则推荐 `local-only`；缺失或未登录都不会阻塞。
- 三个动态 `question`：工作区选择、社区共享层级、单行 Star 后记。
- 终端模式一步完成 SSH NPU 连接；命令末尾追加 `--password` 可隐藏输入。
- 安装后运行只读 `mindie_doctor` 就绪检查。
- 新增 Non-Blocking Task Continuity Rule：MindIE 工具是加速器而非路障，失败时报告一次（先核对不确定写入）并立即改用原生 `ssh`/`scp` 或本地脚本继续。

## 4. Safety & Verification

- Node 子进程全部 `windowsHide: true` + `detached: process.platform !== "win32"`；Python 子进程全部 `creationflags=CREATE_NO_WINDOW`。
- 临时文件在 `finally` 清理；`except TimeoutError: raise` 始终先于 `OSError` / 宽泛异常。
- 全新隔离测试：`tests/test_oc_plugin.py`、`tests/test_oc_lifecycle.py`、`tests/test_oc_mcp_and_knowledge.py`、`tests/test_oc_transcript.py`。
- `benchmarks/release.py` 直接调用 `sync_session_spool` 与 `read_material` 并通过。
