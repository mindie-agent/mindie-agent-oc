# mindie-agent-oc 0.1.4

[English](mindie-agent-oc-0.1.4.en.md)

`mindie-agent-oc` `0.1.4` 补齐 Diagnostics 2.0 归属发现有界顶堆扫描、非晋升态 Schema 故障显式传播、晋升后返回态 `schema_warning` 收据字段归一化、README 安装链接对齐，并将 `mindie-profiler` 升级至正式版 `0.5.12`。

- **Diagnostics 2.0 归属发现有界顶堆（Top-8 Heap over 4096 Entries）**：`_verified_failure_ids` 在 `O(1)` 内存内遍历最多 `4096` 个事件目录项维护最新 8 个 `.jsonl` 文件，消除无序 64 项截断导致的归属漏检，并清理遗留未引用符号。
- **Schema 故障可见性与晋升后收据归一化**：未发生晋升（`status == "up_to_date"`）时 Schema 检查的 `OSError` / `ValueError` 显式持久化 `schema_warning` 并向上传播异常，严禁静默返回无告警的干净 `up_to_date`；原子晋升后（`status == "updated"`）`sync_store_schema()` 正常返回的 `(False, warning)` 同步归一化填充 `receipt_warning` 与 `receipt_unconfirmed=True`。
- **正式共享运行时组件与文档对齐**：升级并锁定 `profiler 0.5.12`，同步对齐中英文 README 的版本标识与 Release Tarball 安装命令。

## 正式依赖组合

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.17 | `315fc4bc51ee9a7afef32a3666599d608ff59f9d` |
| remote-dev | 0.11.10 | `3556df571450dab481a25e0157167f1c3727dc6c` |
| diagnostics | 0.5.11 | `171623584aa812677df907518dcb55b06493241b` |
| debugger | 0.1.2 | `d40a250e4048ac63d4a478a5dbfa993702ceddd0` |
| profiler | 0.5.12 | `1c0f28941d6adfb6579cf7b3477a1a28964be97b` |
| sim | 0.9.11 | `f0b19acdab6f5f4e952561c7ff92e4bf9c0b0c3a` |
| coordinator | 0.7.9 | `d8977114e8cd0e5b7befa59a5ff5e53f40a74521` |
| npu-top | 0.3.3 | `848d74da3cba383c07a0eb372a17baf35b6b944b` |
