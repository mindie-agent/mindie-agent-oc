<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>OpenCode (<code>v2.2.x+</code>) 原生客户端适配器：支持 GitHub 插件安装与本地目录加载，启动即默认启用 39 项昇腾远端、诊断与 L1–L4 分级知识 MCP 工具，并通过纯 Node.js 授权门禁实现零开销按需采集</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.10-0F766E.svg" alt="Release v0.1.10"></a>
  &nbsp;
  <a href="./README.en.md">English</a>
</p>

---

## 1. 架构特性与暴露边界

`mindie-agent-oc`（npm 包标识 `@mindie-agent/oc`，当前正式版本为 `v0.1.10`）基于 OpenCode v2 Promise Plugin API（`@opencode-ai/plugin`）原生构建，启动即默认开放全部能力且不暴露额外用户命令：

1. **GitHub / 本地安装与无感升级**：同时支持 GitHub 仓库通道（`opencode plugin add github:mindie-agent/mindie-agent-oc`）（OpenCode 2.2.7 不接受直接 tarball URL，Release tarball 仅作为分发产物）以及本地开发目录引用（`"plugin": ["file:///abs/path/to/mindie-agent-oc"]`）；支持 `opencode plugin update` 原生升级，并在后台按 `300s` 周期单调发现并原子切换共享组件独立更新（`max(installed, baseline, latest)`）。
2. **启动默认全量开放确定性与分级知识 MCP 工具（共 39 项）**：
   - `mindie-remote-dev`（20 项 `remote_*` 远端开发与异步任务工具，`timeout.execution = 660000ms`）
   - `mindie-tools`（19 项工具：`mindie_status`、`profiler`、`sim`、`coordinator`、`debugger`、`npu_top` 及 6 项实时观测工具、`diagnostics_status`、`diagnostics_bundle`，以及默认启用的 5 项 `knowledge_*` 分级知识工具）
3. **静默语义指引与授权优先 `session.idle` 采集**：
   - 顶层主会话（非 Subagent、非 Fork 会话）在 `session.hook("context")` 中由纯 Node.js 直接从内存缓存注入剥离 YAML Frontmatter 后的 `SKILL.md` 指引（每轮对话零 Python 子进程派生），并通过 `shell.hook("create.before")` 自动为会话 Shell 绑定 `OPENCODE_SESSION_ID`。
   - `session.idle` 在纯 Node.js 中优先校验社区共享开关（`mindie-community.json` + `mindie-consent.json`）与授权 `project_roots`：未开启或目录越界时零子进程派生、零 `admission.sqlite3` 创建；已开启且命中授权目录时自动签发会话租约并从只读 `opencode.db` 增量投影脱敏对话完成采集。

## 2. MCP 与命令行工具矩阵

| 通道 | 表面 | 数量 | 工具列表 |
| :--- | :--- | :---: | :--- |
| **默认公开 MCP** | `mindie-remote-dev` | **20** | `remote_auth`, `remote_read`, `remote_write`, `remote_edit`, `remote_multi_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_ls`, `remote_apply_patch`, `remote_job_status`, `remote_job_tail`, `remote_job_output`, `remote_job_stop`, `remote_job_stdin`, `remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`, `remote_context_snapshot`, `remote_probe` |
| **默认公开 MCP** | `mindie-tools` | **19** | `mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`, `diagnostics_status`, `diagnostics_bundle`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |
| **本地知识 CLI** | `knowledge_cli.py` | **5 (+describe)** | `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`（支持无预激活直调并按需自动签发默认会话租约） |

## 3. 安装、升级与初始化

### 方式 A：通过 OpenCode CLI 安装与升级（GitHub）

```bash
# 通道 1：GitHub 仓库源码 / Release
opencode plugin add github:mindie-agent/mindie-agent-oc

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

## 4. 社区共享与隐私边界

社区共享与脱敏故障上报默认关闭；首次配置详见 [用户须知](docs/install/user-notice.md)、[工作区配置](docs/install/workspace-setup.md) 与 [GitHub 登录引导](docs/install/github-authentication.md)。

## 5. 测试与核验

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
```
