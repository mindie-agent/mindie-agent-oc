"""Hermetic isolation fixtures for `mindie-agent-oc` Tier 0 & Tier 1 tests.

[Role]
Isolates `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`,
`MINDIE_OPENCODE_DB`, every `XDG_*` root, `HOME`, and the sibling harness homes
(`CODEX_HOME`, `CLAUDE_CONFIG_DIR`/`CLAUDE_PLUGIN_DATA`, `KIMI_SHARE_DIR`,
`KIMI_CODE_HOME`, `DSH_HOME`, `OPENCODE_CONFIG_DIR`, `WORKBUDDY_HOME`) inside
`tmp_path`, so no test enumerates the host's cross-harness borrowed runtimes.

[Invariants]
- Adds `plugins/mindie-agent/scripts` to `sys.path` once and activates
  `ensure_oc_native_support()`.
- Every storage/config root a test may enumerate is redirected into `tmp_path`;
  a regression test never reads host `~/.config/*` or `~/.local/share/*`.

[Timeouts & Locks]
- Pure filesystem setup per test (`< 10ms`).

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from oc_native_compat import ensure_oc_native_support  # noqa: E402

ensure_oc_native_support()


@pytest.fixture()
def isolated_oc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    mindie_cfg = tmp_path / "mindie-config"
    mindie_data = tmp_path / "mindie-data"
    mindie_diag = tmp_path / "mindie-diag"
    xdg_cfg = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    xdg_state = tmp_path / "xdg-state"
    xdg_cache = tmp_path / "xdg-cache"
    workspace = tmp_path / "workspace"
    opencode_db = xdg_data / "opencode" / "opencode.db"

    appdata = tmp_path / "appdata" / "Roaming"
    localappdata = tmp_path / "appdata" / "Local"
    for directory in (
        home,
        appdata,
        localappdata,
        mindie_cfg,
        mindie_data,
        mindie_diag,
        xdg_cfg / "opencode",
        xdg_data / "opencode",
        xdg_state,
        xdg_cache,
        workspace,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    venv_py = REPO_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    env_map = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(localappdata),
        "MINDIE_CONFIG_HOME": str(mindie_cfg),
        "MINDIE_DATA_HOME": str(mindie_data),
        "MINDIE_DIAGNOSTICS_ROOT": str(mindie_diag),
        "MINDIE_DIAGNOSTICS_CONFIG": str(mindie_cfg / "diagnostics.json"),
        "MINDIE_DIAGNOSTICS_SERVICE_DIR": str(mindie_diag / "services"),
        "MINDIE_OPENCODE_DB": str(opencode_db),
        "MINDIE_OC_PYTHON": str(venv_py if venv_py.is_file() else Path(sys.executable)),
        "OC_PLUGIN_ROOT": str(REPO_ROOT / "plugins" / "mindie-agent"),
        "XDG_CONFIG_HOME": str(xdg_cfg),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_RUNTIME_DIR": str(xdg_state / "runtime"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_PLUGIN_DATA": str(home / ".claude" / "plugins" / "data"),
        "KIMI_SHARE_DIR": str(home / ".kimi-share"),
        "KIMI_CODE_HOME": str(home / ".kimi-code"),
        "DSH_HOME": str(home / ".dsh"),
        "OPENCODE_CONFIG_DIR": str(xdg_cfg / "opencode"),
        "WORKBUDDY_HOME": str(home / ".workbuddy"),
    }
    for key, val in env_map.items():
        monkeypatch.setenv(key, val)
    for drop_key in (
        "MINDIE_AGENT_CONFIG",
        "MINDIE_OC_FORCE_CLEANUP",
        "CLAUDE_PLUGIN_ROOT",
        "MINDIE_OC_ALLOW_BG_UPDATE",
        "MINDIE_OC_INSTALLED_ROOT",
        "OPENCODE_SESSION_ID",
        "MINDIE_SUMMARY_ENDPOINT",
        "MINDIE_SUMMARY_TOKEN",
        "_MINDIE_OC_PGID_FILE",
        "_MINDIE_OC_PGID_FILES",
        "_MINDIE_OC_BG_NO_BREAKAWAY",
        "_MINDIE_OC_REEXEC",
    ):
        monkeypatch.delenv(drop_key, raising=False)

    return {
        "root": tmp_path,
        "home": home,
        "mindie_cfg": mindie_cfg,
        "mindie_data": mindie_data,
        "mindie_diag": mindie_diag,
        "xdg_cfg": xdg_cfg,
        "xdg_data": xdg_data,
        "xdg_state": xdg_state,
        "workspace": workspace,
        "opencode_db": opencode_db,
        "python": venv_py if venv_py.is_file() else Path(sys.executable),
    }
