# mindie-agent-oc 0.1.7

[简体中文](mindie-agent-oc-0.1.7.md)

Release-metadata recovery, storage lifecycle governance, and component upgrades.

## Release cache and error-classification recovery

- Moves the shared knowledge baseline to formal 0.13.20, so an invalid HTTP 200 JSON body can no longer be reused from the TTL/ETag cache, and truncated responses are rejected.
- Removes the duplicate Catalog `JSONDecodeError` translation; the shared HTTP boundary now confirms remote faults.
- Release metadata, candidate preparation, and the actually loaded host payload are verified separately; no active session is restarted and no user consent or product history channel is modified.

Related diagnosis: [mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354). The historical response body is unavailable, so this release does not infer the original cause of that corruption.

## Storage lifecycle governance (new `storage_gc.py`)

- After a committed switch (and during background maintenance) the adapter silently reclaims excess `oc-state/component-runtimes` generations, failed builds, and `.trash-*` leftovers, plus historical `oc-state/oc-source/<ver>-<digest>` payloads. The active `oc.json` pointer, the live interpreter, interpreters borrowed by another plugin, an external user's Git `source_root`, live leases, and live processes always win over any retention count.
- `generations/oc.engine.*.json` now uses bounded retention aligned with the shared legacy sweep (`VERIFIED_RETENTION = 2`): the snapshot the active (or persisted `previous_generation`) `engine_config` points at, snapshots any config home still references, and the two most recent historical snapshots are kept. A snapshot whose ancestor is a symlink/junction, that lives inside an external Git checkout, or that lies under a user `source_root`/`package_dir` is never unlinked, and an incompletely read reference set aborts deletion instead of ranking a partial table.
- Every reclamation entry point returns an accounting dictionary and degrades failures into `warnings`. After a successful switch, contention during cleanup only attaches `cleanup_warning`: it never sets `receipt_unconfirmed`, never rolls `oc.json` back, and never writes a one-way `fault`.
- One absolute `deadline` bounds each scan: component runtimes `2.5s`, legacy baggage `3.0s`, engine snapshots `0.5s`, session projections `1.0s`. An exhausted budget reports `partial` and stops instead of ranking a truncated table.

## Silent reclamation of historical baggage

- Reuses the shared `mindie_knowledge.storage` two-phase retirement and global protection roots: historical generations under managed directories such as `oc-state/component-runtimes`, `oc-source`, `generations`, `update/*`, and `releases` are reclaimed by silent maintenance, while Git checkouts, user source trees, leases, and build locks are always kept.
- Protection is collected across every config home (`<config>/oc.json` plus the default `~/.config/mindie-agent`), including config files reached through symlinks, and Windows extended-length (`\\?\`, `\\?\UNC\`) spellings are normalized before identity/containment checks. When any existing config document cannot be enumerated or parsed, the legacy and engine sweeps are skipped with `partial=True` instead of deleting under incomplete protection; disposable interpreter generations are additionally held under `acquire_runtime_lease(owning_dir(...))` across both reclamation phases, and an unleased pin also skips the legacy sweep.
- A cold-start `uv sync` now pins `UV_CACHE_DIR` to a same-volume `<data_home>/.uv-cache` when the caller set none and uv's default cache crosses volumes, so generations stop paying full copies.

## Upgrade builds and resident processes

- A `native_candidate` upgrade no longer builds a redundant baseline runtime first: when the interpreter recorded in `oc.json` can still run the component inventory probe, discovery and the merged build use it directly and produce exactly one `component-runtimes` `.venv`. The baseline bootstrap is used only when `prepare()` returns `None` (target equals the baseline and the current interpreter is not aligned) or when a cold start has no healthy interpreter.
- `oc.json` now persists `previous_python` and `previous_generation` (the last successfully active interpreter and engine snapshot) with every atomic switch, and both are pinned by `keep_paths` plus a runtime lease during post-switch reclamation and background maintenance, so mtime ranking can no longer lose the rollback generation when newer unactivated candidates exist.
- A post-commit cleanup failure of any kind (`PermissionError`, `OSError`, `TimeoutError`, a raising `storage_gc` including its path resolution) is caught and reduced to `cleanup_warning`; a committed switch always reports `status="updated"` and never `receipt_unconfirmed`.
- The resident MCP server and the `mindie_launch.py` MCP supervisor hold a shared `mindie_knowledge.storage.acquire_runtime_lease(owning_dir(sys.executable))` runtime lease for their lifetime, so the generation they are serving is never retired underneath them.
- `transcripts/<session_id>.jsonl` and its session lock are no longer append-only: a complete bundle (`<session>.jsonl` + `<session>.seq` + `.<session>.lock`) older than 14 days, not admitted in `admission.sqlite3`, and without pending activation state is evicted as a unit, and the matching idle `_THREAD_LOCKS` entries are dropped. Deleting the projection with its cursor keeps a resumed session's transcript identity consistent, so it can never recreate the file with a new inode and trigger `transcript_cursor_identity_mismatch`; when the bundle is fully gone the sequence restarts cleanly from 0.
- Lock acquisition in `opencode_spool.py` and the spool reclaimer now re-verifies `fstat(fd) == lstat(path)` after `flock`, retrying onto the live path when a concurrent eviction unlinked the inode it locked; a held `.<session>.lock` is unlinked before it is unlocked, eliminating the POSIX two-writer race on old-versus-new lock inodes.
- `classify_update_exception` now maps DNS resolver failures, `LockTimeout`/lock contention, `ENOSPC` and `PermissionError` to non-reportable `resolver`/`busy`/`deferred`/`prerequisite` outcomes, so a local environment failure is never filed upstream as an `update` bug (issue #360 class).
- Extracted `runtime_bootstrap.py`, `contract_validation.py`, `host_process.py`, `mcp_stdio.py`, `storage_gc.py`, `storage_spool.py`, `storage_support.py`, and `session_support.mjs`; `opencode_host.mjs`, `mcp_server.py`, and `component_update.py` are all safely below 785 lines.

## Component pins

`mindie-knowledge` 0.13.22, `remote-dev` 0.11.12, `mindie-diagnostics` 0.5.15, `mindie-coordinator` 0.7.11, `mindie-debugger` 0.1.3, `mindie-profiler` 0.5.15, `mindie-sim` 0.9.15, `npu-top` 0.3.4. `pyproject.toml` `[tool.uv.sources]` and `override-dependencies` pin the matching formal-release commit SHAs, and `uv.lock` is regenerated.

Verification scope: the full `pytest tests` suite (including the R1 storage-lifecycle regressions for cross-config-root pins, rollback-pointer retention, `TimeoutError` propagation, engine-snapshot link/Git guards, POSIX lock-inode ordering, resumable spool cursors and post-switch exception isolation in `tests/test_storage_lifecycle_oc.py`), Node syntax checks, and the LF line-ending gate. All of it ran on macOS; no NPU or complete model-task result on another platform is inferred.
