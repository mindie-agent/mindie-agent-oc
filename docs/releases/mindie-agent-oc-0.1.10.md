# mindie-agent-oc 0.1.10：默认启用全量 `knowledge_*` 工具、纯 Node 静默技能注入与授权优先 `session.idle` 捕获

[English](mindie-agent-oc-0.1.10.en.md)

- **默认启用 `mindie-tools` 上的 5 项 `knowledge_*` 工具与无门禁 `knowledge_cli.py`**：在 `mcp_server.py` 与 `host_adapter.py` 中直接通过 `mindie-tools` 暴露 `knowledge_query`、`knowledge_explain`（均带 `readOnlyHint=True` 并路由至并发 `read_pool`）、`knowledge_feedback`、`knowledge_distill`、`knowledge_sync`，并在缺失宿主会话元数据时自动回退至 `_CONNECTION_SESSION_ID` / `"oc-cli-default"` 按需签发默认会话租约（`not_before=0.0`），严格遵循 Principle 14 收据诚实性。
- **静默化 `opencode_host.mjs` 与零子进程 `SKILL.md` 注入**：彻底移除 `ctx.command.transform` 与 `ctx.session.hook("prompt")` 手动命令注册；`ctx.session.hook("context")` 在纯 Node.js 中直接从内存缓存读取并剥离 YAML Frontmatter 后向顶层会话注入 `<mindie-agent-skill>`（每轮对话零 Python 子进程派生），并按需惰性启动 `ensureSummaryServer()`；同时移除 `SKILL.md` 的 `disable-model-invocation: true`（精简至 21 行）并清理全仓面向用户的命令暴露。
- **纯 Node.js 授权优先 `session.idle` 捕获**：在 `ctx.event.subscribe()` 与 `capture_stop.py` 中优先校验社区共享授权（`mindie-community.json` + `mindie-consent.json`）及 `project_roots` 范围；未开启或目录越界时零子进程派生（`0ms` 冷路径）、零 `admission.sqlite3` 创建；已开启且命中授权目录时自动签发会话租约并执行脱敏对话投影采集。
