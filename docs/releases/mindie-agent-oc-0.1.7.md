# mindie-agent-oc 0.1.7

[English](mindie-agent-oc-0.1.7.en.md)

发布元数据恢复、存储生命周期治理与组件升级。

## 发布缓存与错误分类恢复

- 将共享 knowledge 基线更新至正式 0.13.20，防止无效 HTTP 200 JSON 在 TTL/ETag 缓存中反复复用，并拒绝截断响应。
- 移除重复的 Catalog JSONDecodeError 转换，由共享 HTTP 边界确认远端故障。
- 发布、候选环境准备和正在运行的宿主实际加载分别核验；不重启活跃会话，不修改用户授权或产品历史通道。

关联诊断：[mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354)。历史响应正文不可用，本版本不推断当时的初始损坏原因。

## 存储生命周期治理（新增 `storage_gc.py`）

- 切换成功后（以及后台维护时）静默回收 `oc-state/component-runtimes` 超量代、失败构建与 `.trash-*` 残骸，并回收 `oc-state/oc-source/<ver>-<digest>` 历史载荷。当前 `oc.json` 指针、活跃解释器、跨插件借用解释器、外部用户 Git `source_root`、活租约与活进程始终优先于任何保留数量。
- `generations/oc.engine.*.json` 采用与共享 legacy 回收一致的有界保留（`VERIFIED_RETENTION = 2`）：当前（或已持久化 `previous_generation`）`engine_config` 指向的活跃文件、任一 config home 仍引用的快照，加上最近 2 份历史快照；祖先含 symlink/junction、位于外部 Git 检出内或落在用户 `source_root`/`package_dir` 之下的快照一律不删除，引用集合读取不完整时直接放弃删除而不是按被截断的表排名。
- 回收入口一律返回账目字典并把失败降级为 `warnings`。主切换成功后若回收遇占用，结果只附 `cleanup_warning`，不产生 `receipt_unconfirmed`，不回退 `oc.json` 主状态，也不写入单向 `fault`。
- 单一绝对 `deadline` 约束每次扫描：组件运行时 `2.5s`、历史包袱 `3.0s`、引擎快照 `0.5s`、会话投影 `1.0s`；预算耗尽记录 `partial` 并停止，不按被截断的表排名。

## 存量历史包袱静默回收

- 复用共享 `mindie_knowledge.storage` 的两阶段退役与全局保护根：`oc-state/component-runtimes`、`oc-source`、`generations`、`update/*`、`releases` 等受管目录中的历史代在静默维护中被回收，Git 检出、用户源码树、租约与构建锁一律保留。
- 保护集合跨所有 config home（`<config>/oc.json` 与默认 `~/.config/mindie-agent`）收集，包含经符号链接引用的配置文件；Windows 扩展长度路径（`\\?\`、`\\?\UNC\`）在身份与包含判断前归一化。任一已存在配置无法枚举或解析时，legacy 与引擎回收均以 `partial=True` 跳过，绝不在保护不完整时删除；可回收解释器代还在两个回收阶段全程持有 `acquire_runtime_lease(owning_dir(...))`，任一租约不可得同样跳过 legacy 回收。
- 冷启动 `uv sync` 在未显式设置 `UV_CACHE_DIR` 且默认缓存跨卷时，改用同卷 `<data_home>/.uv-cache`，避免逐代整份复制。

## 升级构建与常驻进程

- `native_candidate` 升级不再先构建一套冗余 baseline 运行时：当 `oc.json` 记录的既有解释器仍可执行组件清单探测时，直接用它做依赖发现与合并构建，只产生 1 套 `component-runtimes` `.venv`；仅当 `prepare()` 返回 `None`（目标等于 baseline 且当前解释器未对齐）或冷启动无健康解释器时才回退 baseline 引导。
- 每次原子切换都会在 `oc.json` 中持久化 `previous_python` 与 `previous_generation`（上一次成功激活的解释器与引擎快照），并在切换后回收与后台维护中同时以 `keep_paths` 与运行租约钉住，因此即使存在 2 个更新的未激活候选构建，mtime 排名也不会丢失回滚代。
- 提交后清理的任意异常（`PermissionError`、`OSError`、`TimeoutError`，以及包含路径解析在内抛错的 `storage_gc`）都被收敛为 `cleanup_warning`；已提交的切换始终返回 `status="updated"`，绝不产生 `receipt_unconfirmed`。
- 常驻 MCP 服务与 `mindie_launch.py` 的 MCP 监督进程在生命周期内持有 `mindie_knowledge.storage.acquire_runtime_lease(owning_dir(sys.executable))` 共享运行租约，使正在服务的代不会被并发回收。
- `transcripts/<session_id>.jsonl` 与会话锁不再只增不减：仅当完整 bundle（`<session>.jsonl` + `<session>.seq` + `.<session>.lock`）超过 14 天、未在 `admission.sqlite3` 中处于已准入状态且无待处理激活时，才作为整体淘汰，同时清理进程内 `_THREAD_LOCKS` 中已淘汰会话的空闲锁引用。投影与游标一起退休，恢复的会话不会以新 inode 重建文件并触发 `transcript_cursor_identity_mismatch`；两者都不存在时序号从 0 干净重启。
- `opencode_spool.py` 与投影回收器的加锁在 `flock` 之后重新校验 `fstat(fd) == lstat(path)`，若并发淘汰已把所锁 inode 解除链接则重试到活跃路径；已持有的 `.<session>.lock` 先解除链接再解锁，彻底消除 POSIX 上两个写者分别持有新旧锁 inode 的竞态。
- `classify_update_exception` 将 DNS 解析失败、`LockTimeout`/锁竞争、`ENOSPC` 与 `PermissionError` 归类为不可上报的 `resolver`/`busy`/`deferred`/`prerequisite`，本地环境故障不再作为 `update` 缺陷上报（#360 类）。
- 抽出 `runtime_bootstrap.py`、`contract_validation.py`、`host_process.py`、`mcp_stdio.py`、`storage_gc.py`、`storage_spool.py`、`storage_support.py` 与 `session_support.mjs`；`opencode_host.mjs`、`mcp_server.py`、`component_update.py` 均安全低于 785 行。

## 组件版本锁定

`mindie-knowledge` 0.13.22、`remote-dev` 0.11.12、`mindie-diagnostics` 0.5.15、`mindie-coordinator` 0.7.11、`mindie-debugger` 0.1.3、`mindie-profiler` 0.5.15、`mindie-sim` 0.9.15、`npu-top` 0.3.4；`pyproject.toml` 的 `[tool.uv.sources]` 与 `override-dependencies` 锁定对应正式版 commit SHA，`uv.lock` 同步更新。

验证范围：`pytest tests` 全量（含 `tests/test_storage_lifecycle_oc.py` 中针对跨 config home 保护、回滚指针保留、`TimeoutError` 传播、引擎快照链接/Git 守卫、POSIX 锁 inode 顺序、可恢复会话游标与切换后异常隔离的 R1 回归）、Node 语法检查与 LF 换行门禁。以上在本机 macOS 完成；不推断其他平台 NPU 或完整模型任务的验证结论。
