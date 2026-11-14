# mindie-agent-oc 0.1.5

[English](mindie-agent-oc-0.1.5.en.md)

`mindie-agent-oc` `0.1.5` 补齐 Windows 冷启动解释器探测、无控制台 PowerShell 5.1 UTF-8 BOM 兼容、Windows 钩子超时子进程树清理、WSL2 `/mnt/<drive>` 宿主二进制隔离，以及更新通道 TLS/HTTP 瞬时传输异常分类（`mindie-agent#350` 对齐）。

- **Windows 冷启动解释器探测与宿主进程树清理（`paths.mjs`、`opencode_host.mjs`、`paths.py`、`mindie_launch.py`）**：`resolveBootstrapPython()` 在 Windows 未生成 `oc-venv` 的冷启动阶段优先按版本探测 `%LOCALAPPDATA%\Programs\uv-python`、`uv` 托管解释器与非 `WindowsApps` `PATH` 候选并跳过 Microsoft Store `WindowsApps\python.exe` 空壳（`exit 9009`）及 Smart App Control 拦截项（`os error 4551`）；支持 `\\?\` 扩展路径与盘符大小写无关的 `same_interpreter` 判等，并在 Windows 钩子超时回收时调用 `taskkill /F /T /PID`（`windowsHide: true`）清理完整后代进程树。
- **UTF-8 BOM 兼容与 WSL2 `/mnt/<drive>` 宿主二进制隔离（`paths.mjs`、`opencode_host.mjs`、`paths.py`、`identity.py`、`setup.py`、`component_update.py`）**：`read_json`、`readJsonSafe`、`parse_hook` 与 `/mindie-agent` 手势解析统一剥离前导 UTF-8 BOM（`\ufeff`）；新增 `in_wsl`、`is_windows_host_binary`、`which_host_binary` 与 `is_usable_venv_python`，防止 WSL2 `PATH` 中的 `/mnt/c/...` Windows `.exe` 或 PE shim 遮蔽 Linux 原生 `opencode` / `uv` / `gh` / `gitleaks` / `python`。
- **更新通道传输异常分类对齐（`diagnostic_support.py`、`component_update.py`）**：新增 `classify_update_exception`，将组件目录拉取与更新期间的瞬时 `ssl.SSLError`、`http.client` 响应流中断、`urllib.error.URLError`、DNS 异常与上游损坏 JSON 归类为非上报的 `temporary_network` / `certificate` / `rate_limited` / `timeout`，同时保留 `ReleaseResponseTooLarge`、本地契约校验异常与本地 HTTP 状态机编程错误的可上报属性。

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
