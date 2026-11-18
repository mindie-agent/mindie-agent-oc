"""Hermetic isolation fixtures for `mindie-agent-oc` tests.

[Role]
Redirects `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, every `XDG_*` root,
`MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`,
`OPENCODE_TEST_HOME`, `REMOTE_DEV_AUTH_DIR`, `UV_CACHE_DIR`, and `TMPDIR` inside
`tmp_path`, and exposes the repo/plugin/scripts paths plus an isolated environment.

[Invariants]
- No test reads host `~/.config/*`, `~/.local/share/*`, or host OpenCode state.
- `plugins/mindie-agent/scripts` is on `sys.path` once, without importing deleted modules.

[Timeouts & Locks]
- Filesystem-only setup per test (`< 10ms`).

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    return PLUGIN_ROOT


@pytest.fixture()
def isolated_oc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    mindie_cfg = tmp_path / "mindie-config"
    mindie_data = tmp_path / "mindie-data"
    mindie_diag = tmp_path / "mindie-diag"
    xdg_cfg, xdg_data, xdg_state, xdg_cache = (tmp_path / name for name in ("xdg-config", "xdg-data", "xdg-state", "xdg-cache"))
    workspace = tmp_path / "workspace"
    opencode_db = xdg_data / "opencode" / "opencode.db"
    appdata, localappdata = tmp_path / "appdata" / "Roaming", tmp_path / "appdata" / "Local"
    for directory in (home, appdata, localappdata, mindie_cfg, mindie_data, mindie_diag, xdg_cfg / "opencode", xdg_data / "opencode", xdg_state, xdg_cache, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    env_map = {
        "HOME": home, "USERPROFILE": home, "APPDATA": appdata, "LOCALAPPDATA": localappdata,
        "MINDIE_CONFIG_HOME": mindie_cfg, "MINDIE_DATA_HOME": mindie_data,
        "MINDIE_DIAGNOSTICS_ROOT": mindie_diag, "MINDIE_DIAGNOSTICS_CONFIG": mindie_cfg / "diagnostics.json",
        "MINDIE_OPENCODE_DB": opencode_db, "MINDIE_OC_PYTHON": python,
        "OC_PLUGIN_ROOT": PLUGIN_ROOT,
        "XDG_CONFIG_HOME": xdg_cfg, "XDG_DATA_HOME": xdg_data, "XDG_STATE_HOME": xdg_state, "XDG_CACHE_HOME": xdg_cache,
        "XDG_RUNTIME_DIR": xdg_state / "runtime", "OPENCODE_TEST_HOME": home,
        "REMOTE_DEV_AUTH_DIR": tmp_path / "remote-auth", "UV_CACHE_DIR": tmp_path / "uv-cache",
        "TMPDIR": tmp_path / "tmp", "TEMP": tmp_path / "tmp", "TMP": tmp_path / "tmp",
    }
    for key, value in env_map.items():
        Path(value).mkdir(parents=True, exist_ok=True) if key in {"REMOTE_DEV_AUTH_DIR", "UV_CACHE_DIR", "TMPDIR"} else None
        monkeypatch.setenv(key, str(value))
    for key in ("MINDIE_AGENT_CONFIG", "MINDIE_OC_ALLOW_BG_UPDATE", "MINDIE_OC_INSTALLED_ROOT", "OPENCODE_SESSION_ID", "MINDIE_SUMMARY_ENDPOINT", "MINDIE_SUMMARY_TOKEN", "MINDIE_SUMMARY_MODEL", "_MINDIE_OC_REEXEC", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "KIMI_SHARE_DIR", "DSH_HOME", "WORKBUDDY_HOME"):
        monkeypatch.delenv(key, raising=False)
    return {
        "root": tmp_path, "home": home, "mindie_cfg": mindie_cfg, "mindie_data": mindie_data,
        "mindie_diag": mindie_diag, "xdg_cfg": xdg_cfg, "xdg_data": xdg_data, "xdg_state": xdg_state,
        "workspace": workspace, "opencode_db": opencode_db, "python": python,
    }
