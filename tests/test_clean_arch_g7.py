"""Clean-architecture contract tests for `mindie-agent-oc` (Group G7).

[Role]
Verifies `AD-01` (`P2.7` zero-binding deterministic MCP execution), `AD-02` (shared
profile-level `REMOTE_DEV_STATE_DIR`), `AD-03` (concurrent dual-framing MCP serving),
pure-stdlib `ctypes` Windows Job supervision with zero `pywin32` dependency,
`mindie-harness` (`v0.1.0`) adapter/seed/RSI integration, `AD-07` symlink support,
`AD-10` `SKILL.md` hygiene, and `v0.1.7` baseline upgrade compatibility.

[Invariants]
- Deterministic tools succeed with zero `_meta`, zero `_mindie_bind_id`, zero
  `OPENCODE_SESSION_ID`, and zero prior `execute.before` JS hook.
- Model-supplied session keys in `params.arguments` remain rejected.

[Timeouts & Locks]
- Bounded in-process and local Node/Git subprocess checks (`<= 15s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import ast
import ctypes
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"


def test_ad01_zero_binding_deterministic_mcp_succeeds_without_hook_or_env(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bridge
    import mcp_server

    # Ensure zero OPENCODE_SESSION_ID in env and zero prior JS hook!
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "remote_dev.mcp.tools.call_tool",
        lambda name, args, profile="canonical": {"outcome": "success", "status": "ok", "summary": "ok", "tool": name},
    )

    # 1. Cold zero-binding call on BOTH mindie-tools and mindie-remote-dev succeeds 100%
    raw_tools_in = io.BytesIO(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mindie_status", "arguments": {}}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "debugger", "arguments": {"argv": ["decode-code", "507015"]}}})
            + "\n"
        ).encode("utf-8")
    )
    raw_tools_out = io.BytesIO()
    assert mcp_server.serve("tools", stdin=raw_tools_in, stdout=raw_tools_out) == 0
    tool_replies = {m["id"]: m for m in (json.loads(ln) for ln in raw_tools_out.getvalue().decode("utf-8").splitlines() if ln.strip())}
    assert tool_replies[1]["result"]["isError"] is False
    assert tool_replies[2]["result"]["isError"] is False

    raw_rem_in = io.BytesIO(
        (
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "remote_probe", "arguments": {"host": "127.0.0.1"}}})
            + "\n"
        ).encode("utf-8")
    )
    raw_rem_out = io.BytesIO()
    assert mcp_server.serve("remote", stdin=raw_rem_in, stdout=raw_rem_out) == 0
    rem_reply = json.loads(raw_rem_out.getvalue().decode("utf-8"))
    assert rem_reply["result"]["isError"] is False

    # 2. Even when mcp-binds.sqlite3 exists with unrelated or ambiguous concurrent binds, zero-binding MCP still succeeds!
    ws = str(isolated_oc_env["workspace"])
    for sid, cid in (("ses_other_a", "c_a"), ("ses_other_b", "c_b")):
        bridge.handle_pre_tool_use(
            {"sessionID": sid, "id": cid, "tool": "mindie-remote-dev_remote_probe", "input": {"host": "127.0.0.1"}, "directory": ws}
        )
    ambig_res = mcp_server.call_remote_surface("remote_probe", {"host": "127.0.0.1"})
    assert ambig_res["isError"] is False

    # 3. Forged identity key inside arguments is STILL rejected!
    forged_res = mcp_server.call_remote_surface("remote_probe", {"host": "127.0.0.1", "sessionID": "ses_forged"})
    assert forged_res["isError"] is True
    assert forged_res["structuredContent"]["code"] == "admission_unavailable"


def test_ad01_js_execute_before_non_blocking_when_hook_fails(isolated_oc_env: dict[str, Path]) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None
    driver = isolated_oc_env["root"] / "nonblocking_hook.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").as_uri())};

const toolHooks = new Map();
await plugin.setup({{
    mcp: {{ add() {{}} }},
    command: {{ add() {{}} }},
    session: {{ hook() {{}}, async get() {{ return {{ directory: "/nonexistent/relative/../bad" }}; }} }},
    tool: {{ hook(name, fn) {{ toolHooks.set(name, fn); }} }},
    shell: {{ hook() {{}} }},
    event: {{ stream() {{ return (async function* () {{}})(); }} }},
}});

const input = {{ host: "127.0.0.1" }};
// Even with an invalid sessionID ("x") that causes pre-tool-use to fail, execute.before MUST NOT throw!
await toolHooks.get("execute.before")({{ tool: "mindie-remote-dev_remote_probe", sessionID: "x", id: "1", input }});
assert.equal("_mindie_bind_id" in input, false);
console.log("NON_BLOCKING_OK");
""",
        encoding="utf-8",
    )
    proc = subprocess.run([node_bin, str(driver)], capture_output=True, text=True, timeout=10.0, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "NON_BLOCKING_OK" in proc.stdout


def test_ad02_shared_remote_dev_state_dir_across_parent_and_subagent(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_server
    import paths

    monkeypatch.delenv("REMOTE_DEV_STATE_DIR", raising=False)
    seen: list[tuple[str, str]] = []

    def fake_call_tool(name: str, args: dict, profile: str = "canonical"):
        seen.append((os.environ["REMOTE_DEV_SESSION_ID"], os.environ["REMOTE_DEV_STATE_DIR"]))
        return {"outcome": "success", "status": "ok", "summary": "ok", "job_id": "job-parent-oc"}

    monkeypatch.setattr("remote_dev.mcp.tools.call_tool", fake_call_tool)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_parent_main")
    assert mcp_server.call_remote_surface("remote_job_status", {"job_id": "job-parent-oc"})["isError"] is False

    # Subagent call with zero OPENCODE_SESSION_ID sees the exact same shared REMOTE_DEV_STATE_DIR!
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    assert mcp_server.call_remote_surface("remote_job_status", {"job_id": "job-parent-oc"})["isError"] is False

    expected_shared = str(paths.state_dir() / "remote")
    assert seen[0] == ("ses_parent_main", expected_shared)
    assert seen[1][0] == mcp_server._CONNECTION_SESSION_ID
    assert seen[1][1] == expected_shared


def test_zero_pywin32_imports_and_pure_stdlib_ctypes_job_supervision(monkeypatch: pytest.MonkeyPatch) -> None:
    import bounded

    for py_file in SCRIPTS_DIR.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("win32"), f"Forbidden pywin32 import {alias.name} in {py_file.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("win32"), f"Forbidden pywin32 import {node.module} in {py_file.name}"

    calls: list[str] = []

    class FakeK32:
        def CreateJobObjectW(self, *_a):
            calls.append("create")
            return 101

        def QueryInformationJobObject(self, _h, _cls, info_ptr, _sz, _ret):
            calls.append("query")
            return 1

        def SetInformationJobObject(self, _h, _cls, _info_ptr, _sz):
            calls.append("set")
            return 1

        def OpenProcess(self, _acc, _inh, _pid):
            calls.append("open")
            return 202

        def AssignProcessToJobObject(self, _hj, _hp):
            calls.append("assign")
            return 1

        def TerminateJobObject(self, _hj, _rc):
            calls.append("terminate")
            return 1

        def CloseHandle(self, _h):
            calls.append("close")
            return 1

    monkeypatch.setattr(bounded.os, "name", "nt")
    monkeypatch.setattr(bounded, "_win32_kernel32", lambda: FakeK32())
    fake_proc = type("FakeProc", (), {"pid": 999999, "poll": lambda self: 0})()
    hjob = bounded._attach_windows_job(fake_proc)
    assert hjob == 101
    assert bounded._close_windows_job(hjob, disarm_kill_on_close=True) is False
    assert {"create", "set", "open", "assign", "query", "close"}.issubset(set(calls))


def test_ad03_concurrent_mcp_seed_lock_rsi_ad07_symlinks_and_ad10_skill(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mindie_harness.api import HostAdapter
    import diagnostic_support
    import experiment_ledger
    import host_adapter
    import mcp_server
    from mcp_stdio import iter_stdio_messages, write_rpc_frame
    import rsi_loop
    import setup as oc_setup

    # 1. Seed lock + binary .pyz staging + HostAdapter + RSI delegation
    lock = host_adapter.verify_seed_bundle(PLUGIN_ROOT)
    assert lock["schema"] == "mindie-harness-seed/1"
    orig_pyz = (PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz").read_bytes()
    staged = oc_setup.stage_source_baseline(REPO_ROOT, dest_dir=isolated_oc_env["root"] / "staged-oc")
    assert (staged / "plugins" / "mindie-agent" / "assets" / "mindie-harness-seed.pyz").read_bytes() == orig_pyz

    adapter = host_adapter.OCHostAdapter()
    assert isinstance(adapter, HostAdapter)
    assert adapter.resolve_profile().host == "oc"
    assert rsi_loop.__name__.startswith("mindie_harness.rsi")
    assert experiment_ledger.__name__.startswith("mindie_harness.rsi")

    # 2. AD-07 symlink support in diagnostic_support.py AND paths.mjs
    real_dir = isolated_oc_env["root"] / "real-pkg"
    real_dir.mkdir()
    (real_dir / "METADATA").write_text("Metadata-Version: 2.1\nName: npu-top\nVersion: 0.3.4\n", encoding="utf-8")
    dist_info = isolated_oc_env["root"] / "sp" / "npu_top-0.3.4.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").symlink_to(real_dir / "METADATA")
    assert diagnostic_support._inspect_dist_info(isolated_oc_env["root"] / "sp", "npu_top")[0] == "0.3.4"

    real_json = real_dir / "target.json"
    real_json.write_text('{"python": "/usr/bin/python3"}\n', encoding="utf-8")
    link_json = isolated_oc_env["root"] / "linked-oc.json"
    link_json.symlink_to(real_json)
    node_bin = shutil.which("node")
    assert node_bin is not None
    js_symlink = subprocess.run(
        [
            node_bin,
            "--input-type=module",
            "-e",
            f"import {{ readJsonSafe }} from {json.dumps((PLUGIN_ROOT / 'paths.mjs').as_uri())};"
            f"const d = readJsonSafe({json.dumps(str(link_json))});"
            "if (!d || d.python !== '/usr/bin/python3') process.exit(1);",
        ],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert js_symlink.returncode == 0, js_symlink.stderr

    # 3. AD-10 SKILL.md & domain-skills.md benchmark spillover purge
    skill_text = (PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md").read_text(encoding="utf-8")
    domain_text = (PLUGIN_ROOT / "skills" / "mindie-agent" / "references" / "domain-skills.md").read_text(encoding="utf-8")
    for forbidden in ("(out_tensor, metrics_dict)", "mindie-bench-ablation/1", "experiment_ledger.py", "PROGRESS.md"):
        assert forbidden not in skill_text, f"Forbidden benchmark spillover {forbidden!r} in SKILL.md"
        assert forbidden not in domain_text, f"Forbidden benchmark spillover {forbidden!r} in domain-skills.md"

    # 4. AD-03 concurrent MCP stdio dispatch + cancellation
    started = threading.Event()
    release = threading.Event()
    orig_tools = mcp_server.call_tools_surface

    def slow_or_fast(name: str, arguments=None, **kwargs):
        if name == "profiler":
            started.set()
            cancel_ev = kwargs.get("cancel")
            deadline = time.monotonic() + 3.0
            while not release.is_set() and time.monotonic() < deadline:
                if cancel_ev is not None and cancel_ev.is_set():
                    return {"content": [{"type": "text", "text": "cancelled"}], "structuredContent": {"cancelled": True}, "isError": True}
                time.sleep(0.01)
            return {"content": [{"type": "text", "text": "done"}], "structuredContent": {"ok": True}, "isError": False}
        return orig_tools(name, arguments, **kwargs)

    monkeypatch.setattr(mcp_server, "call_tools_surface", slow_or_fast)
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    srv = threading.Thread(
        target=lambda: mcp_server.serve("tools", stdin=os.fdopen(r_in, "rb", buffering=0), stdout=os.fdopen(w_out, "wb", buffering=0)),
        daemon=True,
    )
    srv.start()
    writer = os.fdopen(w_in, "wb", buffering=0)
    reader = os.fdopen(r_out, "rb", buffering=0)
    out_iter = iter_stdio_messages(reader)
    try:
        write_rpc_frame(writer, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, framed=True)
        framed, init_msg = next(out_iter)
        assert framed is True and init_msg["result"]["capabilities"]["tools"]["listChanged"] is True

        write_rpc_frame(
            writer,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "profiler", "arguments": {"argv": ["--version"]}}},
            framed=False,
        )
        assert started.wait(timeout=2.0)
        write_rpc_frame(
            writer,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mindie_status", "arguments": {}}},
            framed=False,
        )
        _, fast_msg = next(out_iter)
        assert fast_msg["id"] == 3 and fast_msg["result"]["isError"] is False

        write_rpc_frame(writer, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}, framed=False)
        _, cancel_msg = next(out_iter)
        assert cancel_msg["id"] == 2 and cancel_msg["result"]["structuredContent"]["cancelled"] is True
    finally:
        release.set()
        writer.close()
        srv.join(timeout=3.0)
        reader.close()


def test_baseline_v0_1_7_upgrade_preserves_consent_and_lease(isolated_oc_env: dict[str, Path]) -> None:
    import admission
    import bridge
    import paths
    import setup as oc_setup

    old_tree = isolated_oc_env["root"] / "oc-v0.1.7"
    old_tree.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "v0.1.7"], capture_output=True, timeout=10, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
        tf.extractall(old_tree, filter="data")

    ws = str(isolated_oc_env["workspace"])
    cfg = paths.config_path()
    oc_setup.ensure_default_profile(config=cfg, source_root=old_tree, workspace_root=ws, python_exe=sys.executable)
    bridge.handle_gesture({"sessionID": "ses_upgrade_v017", "directory": ws, "prompt": "/mindie-agent"})
    oc_setup.configure_community(enabled=False, config=cfg)
    consent_before = paths.consent_path(cfg).read_bytes()

    upgraded = oc_setup.ensure_default_profile(config=cfg, source_root=REPO_ROOT, workspace_root=ws, python_exe=sys.executable)
    assert upgraded["product_validation"]["native_version"] == json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert paths.consent_path(cfg).read_bytes() == consent_before
    assert admission.knowledge_lease("ses_upgrade_v017", config=cfg) is not None
