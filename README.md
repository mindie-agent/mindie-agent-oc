<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>OpenCode (<code>v2.2.x+</code>) 原生客户端适配器：支持 GitHub 与 npm 双通道插件分发、默认暴露 34 项确定性昇腾远端与诊断 MCP 工具，并通过 <code>/mindie-agent</code> 按会话隔离激活分级知识飞轮</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.2-0F766E.svg" alt="Release v0.1.2"></a>
  &nbsp;
  <a href="./README.en.md">English</a>
</p>

---

## 1. 架构特性与暴露边界

`mindie-agent-oc`（npm 包标识 `@mindie-agent/oc`，当前正式版本为 `v0.1.2`）基于 OpenCode v2 Promise Plugin API（`@opencode-ai/plugin`）原生构建，与其他手动入口 Harness（`mindie-agent-agy` / `mindie-agent-codex`）保持严格的边界一致性：

1. **双通道原生安装与无感升级**：同时支持 GitHub 仓库通道（`opencode plugin add github:mindie-agent/mindie-agent-oc`）、GitHub Release 标准 npm Tarball（`opencode plugin add https://github.com/mindie-agent/mindie-agent-oc/releases/download/v0.1.2/mindie-agent-oc-0.1.2.tgz`）以及本地开发目录引用（`"plugin": ["file:///abs/path/to/mindie-agent-oc"]`）；支持 `opencode plugin update` 原生升级，并在后台按 `300s` 周期单调发现并原子切换共享组件独立更新（`max(installed, baseline, latest)`）。
2. **默认全量开放确定性 MCP 工具（共 34 项，零 `knowledge_*` 污染）**：
   - `mindie-remote-dev`（20 项 `remote_*` 远端开发与异步任务工具，`timeout.execution = 660000ms`）
   - `mindie-tools`（14 项确定性工具：`mindie_status`、`profiler`、`sim`、`coordinator`、`debugger`、`npu_top` 及 6 项实时观测工具、`diagnostics_status`、`diagnostics_bundle`）
3. **显式 `/mindie-agent` 按会话隔离激活知识飞轮**：
   - 普通会话零额外子进程开销、零 System Prompt 注入、零后台对话采集。
   - 当且仅当用户在顶层主会话（非 Subagent、非 Fork 会话）执行 `/mindie-agent`（或 `$mindie-agent`）后，`session.hook("context")` 才向该 `sessionID` 注入 `SKILL.md` 指引，`shell.hook("create.before")` 自动为该会话的 Shell 子进程绑定 `OPENCODE_SESSION_ID` 以解锁私有 `knowledge_cli.py`（`knowledge_query` / `knowledge_explain` / `knowledge_feedback` / `knowledge_distill` / `knowledge_sync`），并在 `session.idle` 时从只读 `opencode.db` 过滤 `reasoning` / `tool` / `synthetic` 后增量投影公开对话至独立 Spool 完成采集闭环。

## 2. MCP 与私有知识工具矩阵

| 通道 | 表面 | 数量 | 工具列表 |
| :--- | :--- | :---: | :--- |
| **默认公开 MCP** | `mindie-remote-dev` | **20** | `remote_auth`, `remote_read`, `remote_write`, `remote_edit`, `remote_multi_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_ls`, `remote_apply_patch`, `remote_job_status`, `remote_job_tail`, `remote_job_output`, `remote_job_stop`, `remote_job_stdin`, `remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`, `remote_context_snapshot`, `remote_probe` |
| **默认公开 MCP** | `mindie-tools` | **14** | `mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`, `diagnostics_status`, `diagnostics_bundle` |
| **`/mindie-agent` 私有 CLI** | `knowledge_cli.py` | **5 (+describe)** | `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`（受 `OPENCODE_SESSION_ID` + `admission.sqlite3` 租约双重门控） |

## 3. 安装、升级与初始化

### 方式 A：通过 OpenCode CLI 安装与升级（GitHub / npm Tarball 双通道）

```bash
# 通道 1：GitHub 仓库源码 / Release
opencode plugin add github:mindie-agent/mindie-agent-oc

# 通道 2：GitHub Release 签名 npm Tarball（无需外部 npm Registry）
opencode plugin add https://github.com/mindie-agent/mindie-agent-oc/releases/download/v0.1.2/mindie-agent-oc-0.1.2.tgz

# 检查并升级插件版本
opencode plugin check
opencode plugin update
```

### 方式 B：本地源码开发挂载

1. 在源码目录初始化受管 Python 运行环境与 MindIE Profile（不会修改 `~/.config/opencode` 或 `~/.local/share/opencode`）：
   ```bash
   UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 \
     python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"
   ```
2. 在项目的 `opencode.json`（或指定配置目录的 `opencode.json`）中挂载目录 URI：
   ```json
   {
     "plugin": ["file:///abs/path/to/mindie-agent-oc"]
   }
   ```

## 4. 会话内常用命令

- `/mindie-agent` 或 `/mindie-agent <任务描述>`：为当前 OpenCode 主会话激活知识飞轮（并直接携带任务进入执行）。
- `/mindie-agent status`：查看当前会话租约、Python 环境与组件健康状态。
- `/mindie-agent deactivate`：关闭当前会话的知识飞轮（确定性 MCP 工具保持可用）。

社区共享与故障上报默认关闭；首次配置详见 [用户须知](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md)、[工作区配置](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md) 与 [GitHub 登录引导](plugins/mindie-agent/skills/mindie-agent/references/github-authentication.md)。

## 5. 测试与核验

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
```
