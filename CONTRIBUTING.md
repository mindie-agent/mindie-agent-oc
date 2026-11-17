# 贡献指南 (`mindie-agent-oc`)

英文版请参阅 [CONTRIBUTING.en.md](CONTRIBUTING.en.md)。

`mindie-agent-oc`（`@mindie-agent/oc`）是 MindIE Agent 面向 OpenCode v2 的原生宿主插件仓（仓库类别：`host-plugin`）。

## 1. 本地工作区与分支拓扑

推荐使用扁平同级检出或独立 `.worktrees/` 工作树布局：

```text
code/
  mindie-agent/           # 架构文档、发布清单与跨仓门禁
  harness/                # mindie-harness 公共底座
  mindie-agent-oc/        # OpenCode v2 宿主插件仓
  .worktrees/             # 隔离开发分支工作树
```

## 2. 宿主插件核心契约（不可破坏）

1. **确定性工具零预绑定门禁（`P2.7` / `AD-01`）**：`mindie-remote-dev` 与 `mindie-tools` 的所有确定性工具严禁注入或要求 `_mindie_bind_id`；在无 `_meta`、无 `OPENCODE_SESSION_ID`、无前置 JS `execute.before` 钩子时，标准 MCP `tools/call` 必须 100% 成功执行。
2. **共享远端任务目录与并发 stdio MCP（`AD-02` / `AD-03`）**：`REMOTE_DEV_STATE_DIR` 统一落于 profile 级 `state_dir()/remote` 目录，确保 `task` 子智能体可轮询与停止父会话发起的 `job_id`；MCP 服务支持 `Content-Length` 与 JSONL 双帧并发调度。
3. **纯标准库 Windows 进程监管与零黑框弹窗（`P1.5`）**：严禁引入未安装的 `pywin32`（`win32api`/`win32job`），统一通过 `mindie_harness.process` 与标准库 `ctypes` 管理 Windows Job Object；所有 JS `spawn`/`spawnSync` 必须设置 `windowsHide: true` 且禁止在 Windows 下设置 `detached: true`（使用 `detached: process.platform !== "win32"`），所有 Python Windows 子进程必须携带 `CREATE_NO_WINDOW`。
4. **只读宿主数据库与 Unix LF**：对宿主 `opencode.db` 严格只读访问（`?mode=ro`），绝不篡改 `~/.config/opencode` 或 `~/.local/share/opencode`；全仓文本文件统一采用 Unix `LF`（`\n`）换行。

## 3. 测试隔离与提交前自查

所有测试必须隔离 `HOME`、`USERPROFILE`、`XDG_*`、`OPENCODE_TEST_HOME`、`MINDIE_CONFIG_HOME`、`MINDIE_DATA_HOME`、`MINDIE_DIAGNOSTICS_ROOT` 与 `UV_CACHE_DIR`。

提交 PR 前请完成 [`.github/pull_request_template.md`](.github/pull_request_template.md) 中的 `host-plugin` 四项门禁自查（`HOST-NATIVE`、`HOST-WIRE`、`HOST-ADMISSION`、`HOST-UPGRADE`）并运行：

```bash
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
node --check index.mjs && node --check plugins/mindie-agent/paths.mjs && node --check plugins/mindie-agent/opencode_host.mjs
pytest -q -m "not live_npu and not live_opencode"
```
