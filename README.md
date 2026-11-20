<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for OpenCode (<code>mindie-agent-oc</code>)</h1>

<p align="center">
  <b>OpenCode（<code>v2.2.x+</code>）原生客户端适配器：六个瘦宿主文件、启动即默认开放 14 项昇腾远端工具与全量诊断/分级知识 MCP、纯 Node.js 授权门禁与零开销按需采集</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-oc/releases"><img src="https://img.shields.io/badge/release-v0.1.12-0F766E.svg" alt="Release v0.1.12"></a>
  &nbsp;
  <a href="./README.en.md">English</a>
</p>

---

- [1. 架构特性与暴露边界](#1-架构特性与暴露边界)
- [2. MCP 与工具矩阵](#2-mcp-与工具矩阵)
- [3. 安装、升级与初始化](#3-安装升级与初始化)
- [4. 社区共享与隐私边界](#4-社区共享与隐私边界)
- [5. 测试与核验](#5-测试与核验)

## 1. 架构特性与暴露边界

`mindie-agent-oc`（npm 包标识 `@mindie-agent/oc`，当前正式版本为 `v0.1.12`）基于 OpenCode v2 Promise Plugin API（`@opencode-ai/plugin`）原生构建，`v0.1.12` 是 **Great Subtraction** 瘦宿主版本：生产面仅 6 个文件（`index.mjs`、`opencode_host.mjs`、`mindie_launch.py`、`mcp_server.py`、`opencode_transcript.py`、`setup.py`），合计 `< 1,200` 行，全部重逻辑下沉到 `mindie-harness` 共享组件。

1. **启动默认开放全部工具且零手工激活**：
   - `mindie-remote-dev`（14 项 `oc` 原生 `remote_*` 工具，`timeout.execution = 660000ms`）
   - `mindie-tools`（确定性组件与 RSI 工具、`mindie_status`、`mindie_doctor`、`diagnostics_status`/`diagnostics_bundle`，以及默认启用的 5 项 `knowledge_*` 工具）
2. **零子进程/提示词与授权优先采集**：顶层主会话（非 Subagent、非 Fork）由纯 Node.js 从内存缓存注入剥离 YAML Frontmatter 的 `SKILL.md`；`session.idle` / `session.execution.*` 在纯 Node.js 中校验 `mindie-community.json`、`mindie-consent.json`（`contribute` 或 `local-only`）与规范化 `project_roots`，未开启或越界时零子进程、零 SQLite 文件；命中时按 `250ms` 去重并派生 `mindie_launch.py hook stop`。
3. **无感升级与共享运行时**：后台按需执行 `mindie_launch.py update --background`，冷启动经 `mindie-harness-seed.pyz` 校验后提供运行时，所有组件更新与代际租约交由 `mindie-harness`。

## 2. MCP 与工具矩阵

| 通道 | 表面 | 工具列表 |
| :--- | :--- | :--- |
| **默认公开 MCP** | `mindie-remote-dev` | `remote_auth`, `remote_probe`, `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_list`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_output`, `remote_job_stop`（14 项 `oc` 档案工具） |
| **默认公开 MCP** | `mindie-tools` | `mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, `diagnostics_bundle`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |

## 3. 安装、升级与初始化

### 一键安装（推荐）

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
```

`gh` 完全可选：未安装或未登录时，`install.py --host oc` 自动在终端输出的 JSON 回执中给出 `recommended_tier: "local-only"` 与结构化 `next_questions`（`workspace_roots`、`privacy_tier`、`optional_ssh_npu_host`），绝不阻塞共享运行时预置。随后在 OpenCode 会话中，Agent 按安装指引调用原生 **`question`** 工具依次确认：

1. **工作区**：在当前目录初始化 Ascend 工作区，或继续使用当前目录，或指定已有目录。
2. **社区共享**：根据回执中的 `recommended_tier` 推荐 `contribute`（`gh` 已登录时）或 `local-only`（两档均启用本地脱敏沉淀，仅 `contribute` 外发脱敏结果）。
3. **可选 SSH NPU 连接与单行 Star 后记**：按需一步式配置 SSH NPU 连接（终端模式下可在命令末尾追加 `--password` 隐藏输入），并在收尾时附带单行后记（如果这个插件帮到了你，欢迎给 `mindie-agent` 点一个 Star ⭐）。

安装完成后可调用只读就绪检查工具：

```text
mindie_doctor
```

### 手动升级与本地开发

```bash
opencode plugin check
opencode plugin update
```

本地源码挂载时，在项目 `opencode.json` 中使用 `file:///abs/path/to/mindie-agent-oc`，并运行 `uv run --no-project python plugins/mindie-agent/scripts/setup.py init --workspace-root "$PWD"`。

首次配置详见 [工作区配置](docs/install/workspace-setup.md)、[GitHub 登录（可选）](docs/install/github-authentication.md) 与 [用户须知](docs/install/user-notice.md)。

## 4. 社区共享与隐私边界

社区共享与脱敏故障上报默认关闭；`setup.py sharing --enable | --local-only | --disable` 显式切换，`--status` 只读。所有状态位于 MindIE 自有目录，绝不修改 `~/.config/opencode` 或 `~/.local/share/opencode`。

## 5. 测试与核验

```bash
UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv sync --extra test --python 3.13
.venv/bin/pytest tests -q
python3 .github/scripts/check_lf_eol.py
node --check index.mjs && node --check plugins/mindie-agent/opencode_host.mjs
```
