# mindie-agent-oc 0.1.5

[简体中文](mindie-agent-oc-0.1.5.md)

`mindie-agent-oc` `0.1.5` hardens cold Windows bootstrap interpreter discovery, UTF-8 BOM handling across JS and Python hooks/configs, Windows hook-timeout process-tree cleanup, WSL2 `/mnt/<drive>` binary isolation, and update-channel TLS/HTTP transport exception classification (`mindie-agent#350` parity).

- **Cold Windows Bootstrap Python Discovery & Hook Tree Cleanup (`paths.mjs`, `opencode_host.mjs`, `paths.py`, `mindie_launch.py`)**: `resolveBootstrapPython()` probes `%LOCALAPPDATA%\Programs\uv-python`, `uv`-managed interpreters, and non-`WindowsApps` `PATH` candidates (`windowsHide: true`, cached in-memory) so Microsoft Store `WindowsApps\python.exe` stubs (`exit 9009`) and Smart App Control blocks (`os error 4551`) never break cold OpenCode startup; `same_interpreter` normalizes `\\?\` prefixes and drive casing, and `killHookTree` terminates full Windows child trees via `taskkill /F /T /PID` (`windowsHide: true`).
- **UTF-8 BOM Tolerance & WSL2 `/mnt/<drive>` Binary Isolation (`paths.mjs`, `opencode_host.mjs`, `paths.py`, `identity.py`, `setup.py`, `component_update.py`)**: Strips leading UTF-8 BOM (`\ufeff`) in `read_json`, `readJsonSafe`, `parse_hook`, and `/mindie-agent` gesture parsing; adds `in_wsl`, `is_windows_host_binary`, `which_host_binary`, and `is_usable_venv_python` so WSL2 `/mnt/c/...` Windows `.exe` and PE shims never shadow Linux host CLIs (`opencode`, `uv`, `gh`, `gitleaks`, `python`).
- **Update Transport Exception Classification Parity (`diagnostic_support.py`, `component_update.py`)**: Adds `classify_update_exception` to classify transient `ssl.SSLError`, `http.client` stream drops, `urllib.error.URLError`, DNS errors, and malformed upstream release JSON as non-reportable `temporary_network` / `certificate` / `rate_limited` / `timeout`, while keeping `ReleaseResponseTooLarge`, contract validation errors, and local `http.client` programmer bugs reportable.

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
