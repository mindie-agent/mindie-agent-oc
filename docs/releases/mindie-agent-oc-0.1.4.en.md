# mindie-agent-oc 0.1.4

[简体中文](mindie-agent-oc-0.1.4.md)

`mindie-agent-oc` `0.1.4` hardens Diagnostics 2.0 provenance lookup with a bounded top-8 heap scan, preserves pre-promotion schema error propagation, normalizes post-promotion `schema_warning` receipt fields, aligns bilingual README install URLs, and upgrades `mindie-profiler` to formal release `0.5.12`.

- **Bounded Top-8 Provenance Discovery (4096 Directory Entries)**: `_verified_failure_ids` maintains an `O(1)` min-heap of the 8 newest `.jsonl` files across up to `4096` directory entries so unsorted directory enumeration order never drops valid invocation receipts, while removing unused legacy symbols.
- **Schema Error Honesty & Post-Promotion Receipt Normalization**: Pre-promotion (`status == "up_to_date"`) `OSError` / `ValueError` faults during schema inspection persist `schema_warning` and propagate instead of silently returning a clean `up_to_date` payload; post-promotion (`status == "updated"`) `(False, warning)` returns from `sync_store_schema()` populate `receipt_warning` and `receipt_unconfirmed=True`.
- **Shared Runtime Component & README Alignment**: Upgrades and pins `profiler 0.5.12` and updates bilingual README release labels and tarball installation commands to `v0.1.4`.

## Verified Dependency Combination

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
