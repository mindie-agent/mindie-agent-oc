"""Auto-update, paired generation cutover, Windows P1.5, and task-continuity tests (`test_oc_update_and_continuity.py`).

[Role]
Verifies OpenCode (`oc`) component/plugin auto-update invariants (`300s` cooldown,
`60s` failure backoff, OS file lock, atomic `oc.json` + `oc.engine.json` rebind,
paired `(python, runtime_scripts)` cutover across `opencode plugin update`),
Windows zero-popup `CREATE_NO_WINDOW` / `windowsHide` invariants (`P1.5`), large
output capture decoupling (`P3.1`), `remote_bash` input repair / `remote_auth`
safe error hints / `"unknown"` receipt persistence / corrupt SQLite resilience,
`npu_status` observation dispatch, and `release_metadata.py` AST verification.

[Invariants]
- Tests run strictly inside `isolated_oc_env` (`< 15s`), never touching host
  `~/.config/opencode`, `~/.local/share/opencode`, or `~/.config/mindie-agent`.
- Auto-update and profile promotion never create `admission.sqlite3` or activate
  a session lease.

[Timeouts & Locks]
- Subprocess invocations bounded by `<= 15.0s`; OS lock contention verified
  deterministically via `_update_lock`.

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import pytest

from bridge import handle_pre_tool_use
import component_update
import mcp_server
from paths import admission_path, atomic_write_json, config_path, data_home, engine_config_path, read_json, remote_receipts_path
import release_metadata
import setup

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"


def _copy_plugin_checkout(dst: Path, *, version: str) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for item_name in (
        "package.json",
        "index.mjs",
        "pyproject.toml",
        "product-contract.json",
        "harness-compatibility.json",
        "plugins",
    ):
        src = REPO_ROOT / item_name
        target = dst / item_name
        if src.is_dir():
            shutil.copytree(
                src,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        elif src.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

    for rel in ("package.json", "plugins/mindie-agent/plugin.json"):
        p = dst / rel
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["version"] = version
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")

    contract_path = dst / "product-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["native_version"] = version
    contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8", newline="\n")

    pyproject_path = dst / "pyproject.toml"
    txt = pyproject_path.read_text(encoding="utf-8").replace('version = "0.1.0"', f'version = "{version}"', 1)
    pyproject_path.write_text(txt, encoding="utf-8", newline="\n")
    return dst


def test_component_update_cooldown_backoff_lock_and_atomic_rebind(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=py)

    # 1. Suppressed by default in isolated MINDIE_CONFIG_HOME unless explicitly enabled.
    assert component_update.trigger_background_update_if_due(config=cfg) is False

    spawned: list[list[str]] = []

    class _DummyPopen:
        def __init__(self, cmd, **_kwargs):
            spawned.append([str(c) for c in cmd])

    monkeypatch.setenv("MINDIE_OC_ALLOW_BG_UPDATE", "1")
    monkeypatch.setattr(component_update.subprocess, "Popen", _DummyPopen)

    # 2. First due check triggers background spawn and writes in_progress status.
    assert component_update.trigger_background_update_if_due(config=cfg) is True
    assert len(spawned) == 1
    assert "--non-blocking" in spawned[0]
    # Immediate second call within 300s cooldown is suppressed.
    assert component_update.trigger_background_update_if_due(config=cfg) is False

    # 3. Failed status uses 60s backoff instead of 300s cooldown.
    status_file = data_home() / "oc-component-update-status.json"
    atomic_write_json(status_file, {"checked_at": time.time() - 30.0, "status": "failed"})
    assert component_update.trigger_background_update_if_due(config=cfg) is False
    atomic_write_json(status_file, {"checked_at": time.time() - 65.0, "status": "failed"})
    assert component_update.trigger_background_update_if_due(config=cfg) is True
    assert len(spawned) == 2

    # 4. Concurrent lock contention returns status="locked" immediately when non_blocking=True.
    lock_file = data_home() / "oc-component-update.lock"
    with component_update._update_lock(lock_file) as acquired:
        assert acquired is True
        locked_res = component_update.check(config=cfg, non_blocking=True)
        assert locked_res["status"] == "locked"
        assert locked_res["needs_host_reload"] is False

    # 5. Atomic rebinding updates oc.json + oc.engine.json summary_command without touching admission.sqlite3.
    fake_artifact = {
        "python": py,
        "components": {"runtime": {"mindie-knowledge": {"version": "0.13.99", "revision": "a" * 40}}},
        "installed": {"mindie-knowledge": {"version": "0.13.99", "revision": "a" * 40}},
    }
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: fake_artifact)
    updated_res = component_update.check(config=cfg)
    assert updated_res["status"] == "updated"
    assert updated_res["needs_host_reload"] is True

    rebound_adapter = read_json(cfg)
    rebound_engine = read_json(engine_config_path(cfg))
    expected_org = str(Path(rebound_adapter["runtime_scripts"]) / "organizer.py")
    assert rebound_engine["summary_command"] == [rebound_adapter["python"], "-I", expected_org]
    assert rebound_adapter["component_runtime"] == fake_artifact["components"]
    assert not admission_path(cfg).exists()


def test_paired_generation_continuity_across_plugin_overwrite_and_failed_cutover(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    live_plugin_dir = isolated_oc_env["root"] / "live-opencode-plugin"
    _copy_plugin_checkout(live_plugin_dir, version="0.1.0")

    monkeypatch.setenv("OC_PLUGIN_ROOT", str(live_plugin_dir / "plugins" / "mindie-agent"))
    adapter_v0 = setup.ensure_default_profile(config=cfg, source_root=live_plugin_dir, python_exe=py)
    assert adapter_v0["product_validation"]["native_version"] == "0.1.0"
    retained_v0 = Path(adapter_v0["source_root"])
    assert retained_v0.is_dir()

    # Simulate `opencode plugin update` overwriting the live checkout on disk to 0.1.1 while OpenCode is running.
    _copy_plugin_checkout(live_plugin_dir, version="0.1.1")

    # Active hooks invoked through the overwritten 0.1.1 launcher must keep serving the paired 0.1.0 snapshot!
    launch_script = live_plugin_dir / "plugins" / "mindie-agent" / "scripts" / "mindie_launch.py"
    hook_proc = subprocess.run(
        [py, "-I", str(launch_script), "hook", "session-state"],
        input=json.dumps({"sessionID": "ses_cutover01", "directory": str(isolated_oc_env["workspace"])}),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10.0,
    )
    assert hook_proc.returncode == 0
    assert json.loads(hook_proc.stdout)["activated"] is False
    assert read_json(cfg)["product_validation"]["native_version"] == "0.1.0"

    # Simulate a broken 0.1.1 runtime candidate preparation: oc.json must stay on 0.1.0 and status records "failed".
    monkeypatch.setattr(
        component_update,
        "compatible_runtime",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("simulated uv build failure")),
    )
    with pytest.raises(RuntimeError, match="simulated uv build failure"):
        component_update.check(config=cfg, source=live_plugin_dir)
    assert read_json(cfg)["product_validation"]["native_version"] == "0.1.0"
    assert read_json(data_home() / "oc-component-update-status.json")["status"] == "failed"

    # Restore compatible_runtime and promote 0.1.1 cleanly.
    monkeypatch.setattr(component_update, "compatible_runtime", lambda interpreter, **_kw: str(Path(interpreter).resolve()))
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    promoted = component_update.check(config=cfg, source=live_plugin_dir)
    assert promoted["status"] == "updated"
    assert promoted["needs_host_reload"] is True
    adapter_v1 = read_json(cfg)
    assert adapter_v1["product_validation"]["native_version"] == "0.1.1"
    assert "0.1.1-" in Path(adapter_v1["source_root"]).name


def test_windows_zero_console_popup_ast_and_node_spawn_invariants() -> None:
    # 1. Every Python script invoking subprocess.Popen / subprocess.run in plugins/mindie-agent/scripts
    # must set CREATE_NO_WINDOW on Windows and NEVER use DETACHED_PROCESS (P1.5).
    checked_py = 0
    for py_file in sorted(SCRIPTS_DIR.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        calls_subprocess = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in {"Popen", "run"}
            for node in ast.walk(tree)
        )
        if not calls_subprocess:
            continue
        checked_py += 1
        assert "CREATE_NO_WINDOW" in source, f"{py_file.name} spawns subprocesses without CREATE_NO_WINDOW"
        referenced_attrs = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert "DETACHED_PROCESS" not in referenced_attrs, f"{py_file.name} must not combine DETACHED_PROCESS"
    assert checked_py >= 4

    # 2. Every Node `spawn(` call in opencode_host.mjs must pass `windowsHide: true`.
    host_mjs = (REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs").read_text(encoding="utf-8")
    spawn_calls = host_mjs.count("spawn(")
    windows_hide = host_mjs.count("windowsHide: true")
    assert spawn_calls >= 2
    assert windows_hide >= spawn_calls


def test_large_component_stdout_truncates_cleanly_without_sigkill(isolated_oc_env: dict[str, Path]) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    adapter = setup.ensure_default_profile(config=cfg, python_exe=py)
    adapter["component_commands"] = {
        "debugger": [py, "-I", "-c", "import sys; sys.stdout.write('A' * (1024 * 1024))"],
    }
    atomic_write_json(cfg, adapter)

    call_args = {"argv": ["--help"], "max_output_tokens": 1024}
    handle_pre_tool_use(
        {
            "sessionID": "ses_large_out",
            "id": "call_large_1",
            "tool": "mindie-tools_debugger",
            "input": call_args,
            "directory": ws,
        }
    )
    res = mcp_server.call_tools_surface("debugger", call_args, config=cfg)
    assert res["isError"] is False
    assert res["structuredContent"]["exit_code"] == 0
    assert res["structuredContent"]["output_truncated"] is True
    assert len(res["structuredContent"]["stdout"].encode("utf-8")) <= 4096


def test_debugger_action_shorthand_and_npu_top_observation_dispatch(isolated_oc_env: dict[str, Path]) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    setup.ensure_default_profile(config=cfg, python_exe=py)

    shorthand = {"action": "decode-code", "code": "507015"}
    handle_pre_tool_use(
        {
            "sessionID": "ses_short01",
            "id": "call_short_1",
            "tool": "mindie-tools_debugger",
            "input": shorthand,
            "directory": ws,
        }
    )
    dbg = mcp_server.call_tools_surface("debugger", shorthand, config=cfg)
    assert dbg["isError"] is False
    assert "507015" in dbg["content"][0]["text"]

    # npu_status observation tool must dispatch through VawsTopClient / handle_request without ImportError.
    handle_pre_tool_use(
        {
            "sessionID": "ses_short01",
            "id": "call_npu_status",
            "tool": "mindie-tools_npu_status",
            "input": {},
            "directory": ws,
        }
    )
    npu_res = mcp_server.call_tools_surface("npu_status", {}, config=cfg)
    assert isinstance(npu_res["isError"], bool)
    assert "ImportError" not in npu_res["content"][0]["text"]


def test_remote_continuity_input_repair_auth_redaction_uncertain_receipt_and_corrupt_sqlite(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    setup.ensure_default_profile(config=cfg, python_exe=py)

    # 1. remote_bash missing port -> structured input_repair without crashing MCP server (P4.3).
    bad_bash = {"host": "192.0.2.10", "user": "root", "command": "npu-smi info"}
    handle_pre_tool_use(
        {
            "sessionID": "ses_rem_cont",
            "id": "call_rb_1",
            "tool": "mindie-remote-dev_remote_bash",
            "input": bad_bash,
            "directory": ws,
        }
    )
    rb_res = mcp_server.call_remote_surface("remote_bash", bad_bash, config=cfg)
    assert rb_res["isError"] is True
    text_blob = json.dumps(rb_res, ensure_ascii=False)
    assert "port" in text_blob

    # 2. remote_auth with missing password_env -> safe auth guidance without secret value leakage.
    secret_sentinel = "SUPER_SECRET_OC_PW_99182"
    monkeypatch.setenv("UNRELATED_SECRET_ENV", secret_sentinel)
    auth_args = {
        "action": "enroll",
        "host": "192.0.2.10",
        "port": 22,
        "user": "root",
        "password_env": "MISSING_OC_SSH_PASSWORD_ENV",
    }
    handle_pre_tool_use(
        {
            "sessionID": "ses_rem_cont",
            "id": "call_ra_1",
            "tool": "mindie-remote-dev_remote_auth",
            "input": auth_args,
            "directory": ws,
        }
    )
    ra_res = mcp_server.call_remote_surface("remote_auth", auth_args, config=cfg)
    assert ra_res["isError"] is True
    assert secret_sentinel not in json.dumps(ra_res, ensure_ascii=False)

    # 3. Uncertain SSH submission records status="unknown" in RemoteReceipts (Principle 9 / P4.1).
    from remote_dev.core.errors import RemoteExecutionError
    import remote_dev.mcp.tools as remote_tools

    monkeypatch.setattr(
        remote_tools,
        "call_tool",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            RemoteExecutionError("connection reset after command send", submission_state="uncertain")
        ),
    )
    probe_args = {"host": "192.0.2.10", "port": 22, "user": "root"}
    handle_pre_tool_use(
        {
            "sessionID": "ses_rem_cont",
            "id": "call_uncertain_1",
            "tool": "mindie-remote-dev_remote_probe",
            "input": probe_args,
            "directory": ws,
        }
    )
    unc_res = mcp_server.call_remote_surface("remote_probe", probe_args, config=cfg)
    assert unc_res["isError"] is True
    assert unc_res["structuredContent"]["error_details"]["submission_state"] == "uncertain"
    with sqlite3.connect(remote_receipts_path(cfg)) as conn:
        statuses = [row[0] for row in conn.execute("SELECT status FROM receipts ORDER BY updated DESC LIMIT 1")]
    assert statuses == ["unknown"]

    # 4. Corrupt remote-receipts.sqlite3 returns structured admission_unavailable (execution="not_started").
    corrupt_receipts = isolated_oc_env["root"] / "corrupt-receipts.sqlite3"
    corrupt_receipts.write_bytes(b"NOT_A_VALID_SQLITE_HEADER_BYTES_XXXX")
    handle_pre_tool_use(
        {
            "sessionID": "ses_rem_cont",
            "id": "call_corrupt_1",
            "tool": "mindie-remote-dev_remote_probe",
            "input": probe_args,
            "directory": ws,
        }
    )
    corrupt_res = mcp_server.call_remote_surface(
        "remote_probe",
        probe_args,
        config=cfg,
        receipts=mcp_server.RemoteReceipts(corrupt_receipts),
    )
    assert corrupt_res["isError"] is True
    assert corrupt_res["structuredContent"]["code"] == "admission_unavailable"
    assert corrupt_res["structuredContent"]["execution"] == "not_started"
    assert corrupt_res["structuredContent"]["automatic_retry"] is False


def test_release_metadata_validates_interface_registry_and_ast_tests() -> None:
    checked = release_metadata.check_registry(REPO_ROOT, REPO_ROOT / "tests")
    assert checked["harness"] == "opencode"
    assert len(checked["used_interfaces"]) == 10
    payload = release_metadata.metadata(REPO_ROOT, "a" * 40)
    assert payload["schema"] == "mindie-plugin-release/1"
    assert payload["version"] == "0.1.0"
    assert payload["compatibility"]["harness"] == "opencode"
