"""Hermetic isolation fixtures for `mindie-agent-oc` Tier 0 & Tier 1 tests.

[Role]
Isolates `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`,
`MINDIE_OPENCODE_DB`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, and
`HOME` inside `tmp_path` so tests never read or mutate host `~/.config/opencode`,
`~/.local/share/opencode`, or `~/.config/mindie-agent`.

[Invariants]
- Adds `plugins/mindie-agent/scripts` to `sys.path` once and activates
  `ensure_oc_native_support()`.
- Preserves host `XDG_CACHE_HOME` when available so standalone OpenCode CLI checks
  do not redownload cached runtime metadata.

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
    workspace = tmp_path / "workspace"
    opencode_db = xdg_data / "opencode" / "opencode.db"

    for directory in (
        home,
        mindie_cfg,
        mindie_data,
        mindie_diag,
        xdg_cfg / "opencode",
        xdg_data / "opencode",
        xdg_state,
        workspace,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    venv_py = REPO_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    env_map = {
        "HOME": str(home),
        "MINDIE_CONFIG_HOME": str(mindie_cfg),
        "MINDIE_DATA_HOME": str(mindie_data),
        "MINDIE_DIAGNOSTICS_ROOT": str(mindie_diag),
        "MINDIE_OPENCODE_DB": str(opencode_db),
        "MINDIE_OC_PYTHON": str(venv_py if venv_py.is_file() else Path(sys.executable)),
        "OC_PLUGIN_ROOT": str(REPO_ROOT / "plugins" / "mindie-agent"),
        "XDG_CONFIG_HOME": str(xdg_cfg),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
    }
    for key, val in env_map.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("MINDIE_AGENT_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("MINDIE_SUMMARY_ENDPOINT", raising=False)
    monkeypatch.delenv("MINDIE_SUMMARY_TOKEN", raising=False)

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
