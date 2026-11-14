"""Design-principles, mindie-agent#332, and Astra Max hardening tests (`test_oc_audit332_hardening.py`).

[Role]
Verifies Category A (no silent fallbacks / `TimeoutError` propagation / corrupt-state
visibility), Category B (timeout arithmetic `outer >= sum(inner) + grace`), Category C
(`5000ms` SQLite wait, receipt honesty, and single-instance PID reclamation), and
supervised nested process-group cleanup across OpenCode (`oc`) Python scripts and Node helpers.

[Invariants]
- Hook failures never emit `{}` with exit `0`; `pre-tool-use` fails closed (`decision="deny"`).
- Corrupt or unreadable config, cursor, or SQLite JSON rows surface explicit errors or
  `repair_required=True` rather than silently resetting to empty defaults.
- Post-success local receipt/bind `TimeoutError` or scratch-cleanup `TimeoutError` preserves
  completed primary outcomes with `receipt_unconfirmed=True` / `cleanup_failed=True`.

[Timeouts & Locks]
- Pure local boundary tests (`< 10s` total) inside `isolated_oc_env`.

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env` temporary directories.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import pytest

import admission
import bounded
from bridge import handle_gesture, handle_pre_tool_use
import capture_stop
import component_update
import knowledge_cli
import mcp_server
import mindie_launch
import opencode_transcript
import organizer
from paths import (
    atomic_write_json,
    community_config_path,
    config_path,
    consent_path,
    data_home,
    diagnostics_config_path,
    engine_config_path,
    read_json,
)
import setup

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hook_error_visibility_and_timeout_budget_arithmetic(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Timeout budget arithmetic: Node outer (15.0s) >= HOOK_TOTAL (10.0s) + startup grace (5.0s),
    # and HOOK_TOTAL (10.0s) == HOOK_EXEC_BUDGET (8.0s) + HOOK_CLEANUP_GRACE (2.0s).
    assert mindie_launch.HOOK_EXEC_BUDGET == 8.0
    assert mindie_launch.HOOK_CLEANUP_GRACE == 2.0
    assert mindie_launch.HOOK_TOTAL == 10.0
    assert mindie_launch.HOOK_OUTER_SECONDS == 15
    assert knowledge_cli._reexec_timeout_seconds(["describe"]) == 90.0
    assert knowledge_cli._reexec_timeout_seconds(["knowledge_query"]) == 90.0
    assert knowledge_cli._reexec_timeout_seconds(["knowledge_distill"]) >= 480.0

    # 2. Unhandled hook error exits non-zero and fails closed on pre-tool-use instead of `{}` + exit 0.
    py = str(isolated_oc_env["python"])
    launch_py = REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "mindie_launch.py"
    bad_proc = subprocess.run(
        [py, "-I", str(launch_py), "hook", "pre-tool-use"],
        input="{not-valid-json",
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10.0,
    )
    assert bad_proc.returncode != 0
    assert bad_proc.stdout.strip() != "{}"
    denied_stdout = json.loads(bad_proc.stdout.strip())
    assert denied_stdout["decision"] == "deny"
    assert denied_stdout["bound"] is False

    # 3. _emit_pre_tool_deny emits decision="deny" with non-zero exit code.
    buf = io.BytesIO()
    monkeypatch.setattr(
        mindie_launch.sys,
        "stdout",
        type("_Out", (), {"buffer": buf})(),
    )
    rc = mindie_launch._emit_pre_tool_deny("simulated timeout")
    assert rc == 2
    emitted = json.loads(buf.getvalue().decode("utf-8"))
    assert emitted["decision"] == "deny"
    assert emitted["bound"] is False
    assert "simulated timeout" in emitted["reason"]


def test_node_paths_read_json_safe_and_execute_before_fail_closed(isolated_oc_env: dict[str, Path]) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None
    corrupt_cfg = isolated_oc_env["mindie_cfg"] / "corrupt-oc.json"
    corrupt_cfg.write_text("{corrupt-json", encoding="utf-8")
    missing_cfg = isolated_oc_env["mindie_cfg"] / "missing-oc.json"

    paths_uri = (REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs").as_uri()
    script = f"""
import assert from "node:assert/strict";
import {{ readJsonSafe }} from {json.dumps(paths_uri)};

assert.equal(readJsonSafe({json.dumps(str(missing_cfg))}), null);
assert.equal(readJsonSafe({json.dumps(str(corrupt_cfg))}), null);
console.log("READ_JSON_OK");
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10.0,
    )
    assert proc.returncode == 0
    assert "READ_JSON_OK" in proc.stdout
    assert str(missing_cfg) not in proc.stderr
    assert str(corrupt_cfg) in proc.stderr


def test_sharing_status_corrupt_visibility_timeout_kwargs_and_schema_warning_forwarding(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    # Verify explicit `timeout=` kwarg works on both ensure_default_profile and configure_community.
    setup.ensure_default_profile(config=cfg, python_exe=py, timeout=30.0)
    setup.configure_community(config=cfg, enabled=False, project_root=ws, timeout=15.0)

    # 1. Corrupt community.json / diagnostics.json sets repair_required=True and sharing_enabled=None.
    community_config_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    community_config_path(cfg).write_text("{broken-community", encoding="utf-8")
    diagnostics_config_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    diagnostics_config_path(cfg).write_text(json.dumps({"reporting": "invalid-non-dict"}), encoding="utf-8")
    consent_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    consent_path(cfg).write_text(json.dumps({"choice": "contribute"}), encoding="utf-8")

    st = setup.sharing_status(cfg)
    assert st["community_state"] == "corrupt"
    assert st["diagnostics_state"] == "corrupt"
    assert st["repair_required"] is True
    assert st["sharing_enabled"] is None
    assert st["reporting_enabled"] is None

    facts = admission.status_facts("ses_status_01", config=cfg)
    assert facts["repair_required"] is True
    assert facts["community_state"] == "corrupt"
    assert facts["diagnostics_state"] == "corrupt"
    assert facts["community_sharing_enabled"] is None

    # 2. Missing redactor surfaces schema_warning without polluting update status; provisioned redactor recovers via --prepare-schema.
    import bridge as bridge_mod
    import mindie_knowledge.loop.handoff as handoff_mod

    community_config_path(cfg).unlink(missing_ok=True)
    monkeypatch.setattr(setup.shutil, "which", lambda _cmd: None)
    shutil.rmtree(isolated_oc_env["home"] / ".cache", ignore_errors=True)
    prof_warn = setup.ensure_default_profile(config=cfg, python_exe=py, timeout=30.0)
    assert prof_warn["schema_ready"] is False and "redactor_executable" in prof_warn["schema_warning"]
    assert "prepare_schema warning" in capsys.readouterr().err
    assert not (data_home() / "oc-component-update-status.json").exists()

    gitleaks_stub = isolated_oc_env["home"] / ".cache" / "mindie-agent" / "gitleaks" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    gitleaks_stub.parent.mkdir(parents=True, exist_ok=True)
    gitleaks_stub.write_bytes(b"stub")
    eng_data = read_json(engine_config_path(cfg))
    eng_data["redactor_executable"] = str(gitleaks_stub.resolve())
    atomic_write_json(engine_config_path(cfg), eng_data)

    act_timeouts: list[float] = []
    real_activate = admission.activate
    monkeypatch.setattr(
        handoff_mod, "prepare_schema",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("prepare_schema must not run on hook path")),
    )
    monkeypatch.setattr(
        bridge_mod, "activate",
        lambda *a, **kw: (act_timeouts.append(float(kw.get("timeout", 0.0))), real_activate(*a, **kw))[1],
    )
    gesture_res = handle_gesture(
        {"sessionID": "ses_schema_warn", "directory": ws, "prompt": "/mindie-agent inspect latency"}, config=cfg,
    )
    assert len(act_timeouts) == 1 and act_timeouts[0] >= 5.0
    assert gesture_res["activated"] is True and gesture_res["task_prompt"] == "inspect latency"
    assert "redactor_executable" in gesture_res["schema_warning"] and "redactor_executable" in gesture_res["message"]

    schema_prep_status = data_home() / "oc-schema-prepare.json"
    for attempt in range(2):
        if attempt == 1:
            cfg.unlink(missing_ok=True)
            engine_config_path(cfg).unlink(missing_ok=True)
            schema_prep_status.unlink(missing_ok=True)
            shutil.rmtree(data_home() / "domains", ignore_errors=True)
            cold_fast = handle_gesture({"sessionID": "ses_fast_cold", "directory": ws, "prompt": "/mindie-agent"}, config=cfg)
            assert cold_fast["activated"] is True and cold_fast["bootstrapping"] is False
        prep_dl, st_doc = time.monotonic() + 25.0, {"status": "in_progress"}
        while time.monotonic() < prep_dl and st_doc.get("status") == "in_progress":
            try:
                st_doc = read_json(schema_prep_status) if schema_prep_status.is_file() else {"status": "in_progress"}
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        assert st_doc["status"] == "ready", st_doc
        assert read_json(cfg)["schema_ready"] is True and "schema_warning" not in read_json(cfg)
    assert component_update.store_schema_needs_prepare(cfg) is False


def test_component_update_dead_pid_reclaim_and_inventory_error_honesty(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    py = str(isolated_oc_env["python"])
    ws = str(isolated_oc_env["workspace"])
    cfg = config_path()
    gitleaks_stub = isolated_oc_env["home"] / ".cache" / "mindie-agent" / "gitleaks" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    gitleaks_stub.parent.mkdir(parents=True, exist_ok=True)
    gitleaks_stub.write_bytes(b"stub")
    setup.ensure_default_profile(config=cfg, python_exe=py)
    monkeypatch.setenv("MINDIE_OC_ALLOW_BG_UPDATE", "1")

    # 1. Dead PID in `in_progress` status is reclaimed immediately; scope Popen mock strictly around trigger.
    status_file = data_home() / "oc-component-update-status.json"
    atomic_write_json(status_file, {"checked_at": time.time(), "status": "in_progress", "pid": 99999999})
    spawned: list[list[str]] = []
    with monkeypatch.context() as mp:
        mp.setattr(component_update.subprocess, "Popen", lambda cmd, **_kw: (spawned.append([str(x) for x in cmd]), type("_P", (), {"pid": 424242})())[1])
        assert component_update.trigger_background_update_if_due(config=cfg) is True
    assert len(spawned) == 1 and "reclaiming leftover in_progress" in capsys.readouterr().err

    # 2. read_inventory failure when prepare returns None marks status="failed" instead of faking {}.
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    monkeypatch.setattr(
        component_update, "read_inventory", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("broken venv metadata")),
    )
    res = component_update.check(config=cfg)
    assert res["status"] == "failed" and res["installed"] is None
    assert "broken venv metadata" in res["inventory_error"] and read_json(status_file)["status"] == "failed"

    # 3. Background check persists schema_warning across oc-component-update-status.json + oc.json, then clears on up_to_date recovery.
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_kw: {"mindie-knowledge": {"version": "0.13.16"}})
    domains_root = data_home() / "domains"
    shutil.rmtree(domains_root, ignore_errors=True)
    domains_root.write_text("file-blocker-not-a-dir\n", encoding="utf-8")
    warn_res = component_update.check(config=cfg)
    assert warn_res["status"] == "up_to_date" and bool(warn_res.get("schema_warning"))
    assert warn_res["schema_warning"] == read_json(status_file)["schema_warning"] == read_json(cfg)["schema_warning"]
    assert warn_res["schema_warning"] in admission.status_facts("ses_warn_01", config=cfg)["schema_warning"]
    monkeypatch.delenv("MINDIE_OC_ALLOW_BG_UPDATE", raising=False)
    domains_root.unlink()
    rec_res = component_update.check(config=cfg)
    assert rec_res["status"] == "up_to_date" and "schema_warning" not in rec_res
    assert "schema_warning" not in read_json(status_file) and "schema_warning" not in read_json(cfg)
    assert read_json(cfg)["schema_ready"] is True
    assert "schema_warning" not in handle_gesture({"sessionID": "ses_warn_01", "directory": ws, "prompt": "/mindie-agent"}, config=cfg)


def test_mcp_stdio_parse_errors_updater_timeout_isolation_and_receipt_honesty(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    setup.ensure_default_profile(config=cfg, python_exe=py)

    # 1. TimeoutError inside optional background updater during `initialize` is isolated;
    # malformed JSON / negative Content-Length / invalid requests emit -32700 / -32600, then `ping` succeeds.
    monkeypatch.setattr(
        mcp_server,
        "trigger_background_update_if_due",
        lambda **_kw: (_ for _ in ()).throw(TimeoutError("optional bg update lock timeout")),
    )
    bad_body = b"{broken"
    framed_bad = f"Content-Length: {len(bad_body)}\r\n\r\n".encode("ascii") + bad_body
    in_bytes = (
        b"not-json-at-all\n"
        + b"Content-Length: -5\r\n\r\n"
        + framed_bad
        + b"[1, 2, 3]\n"
        + b'{"jsonrpc":"2.0","id":6,"method":"initialize","params":{}}\n'
        + b'{"jsonrpc":"2.0","id":7}\n'
        + b'{"jsonrpc":"2.0","id":8,"method":"ping"}\n'
    )
    out_buf = io.BytesIO()
    rc = mcp_server.serve("tools", config=cfg, stdin=io.BytesIO(in_bytes), stdout=out_buf)
    assert rc == 0
    raw_out = out_buf.getvalue().decode("utf-8")
    assert '"code": -32700' in raw_out or '"code":-32700' in raw_out
    assert '"code": -32600' in raw_out or '"code":-32600' in raw_out
    assert '"id": 6' in raw_out or '"id":6' in raw_out
    assert '"id": 8' in raw_out or '"id":8' in raw_out

    # 2. _execute_npu_top_observation rejects empty `{}` / `{"result": {}}` instead of faking isError=False.
    import npu_top.client as npu_client
    import npu_top.mcp as npu_mcp

    monkeypatch.setattr(npu_client, "VawsTopClient", lambda *_a, **_kw: object())
    monkeypatch.setattr(npu_mcp, "handle_request", lambda *_a, **_kw: {"jsonrpc": "2.0", "id": 1, "result": {}})
    empty_obs = mcp_server._execute_npu_top_observation("npu_status", {})
    assert empty_obs["isError"] is True
    assert "empty MCP result" in empty_obs["content"][0]["text"]

    # 3. Receipt honesty on TimeoutError: post-success keeps isError=False + receipt_unconfirmed=True;
    # post-failure re-raises TimeoutError!
    import remote_dev.mcp.tools as remote_tools
    from remote_dev.core.errors import RemoteExecutionError

    probe_args = {"host": "192.0.2.55", "port": 22, "user": "root"}
    handle_pre_tool_use(
        {"sessionID": "ses_rcpt_ok", "id": "call_rcpt_ok", "tool": "mindie-remote-dev_remote_probe", "input": probe_args, "directory": ws}
    )
    monkeypatch.setattr(remote_tools, "call_tool", lambda *_a, **_kw: {"outcome": "success", "host": "192.0.2.55"})
    receipts = mcp_server.RemoteReceipts(isolated_oc_env["root"] / "timeout-receipts.sqlite3")
    monkeypatch.setattr(receipts, "finish", lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("local sqlite receipt lock timeout")))
    ok_res = mcp_server.call_remote_surface("remote_probe", probe_args, config=cfg, receipts=receipts)
    assert ok_res["isError"] is False and ok_res["structuredContent"]["receipt_unconfirmed"] is True
    assert "local sqlite receipt lock timeout" in ok_res["structuredContent"]["receipt_warning"]

    handle_pre_tool_use(
        {"sessionID": "ses_rcpt_fail", "id": "call_rcpt_fail", "tool": "mindie-remote-dev_remote_probe", "input": probe_args, "directory": ws}
    )
    monkeypatch.setattr(
        remote_tools, "call_tool", lambda *_a, **_kw: (_ for _ in ()).throw(RemoteExecutionError("ssh dial refused", submission_state="not_submitted")),
    )
    receipts_fail = mcp_server.RemoteReceipts(isolated_oc_env["root"] / "timeout-receipts-fail.sqlite3")
    monkeypatch.setattr(receipts_fail, "finish", lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("local sqlite receipt lock timeout")))
    with pytest.raises(TimeoutError, match="local sqlite receipt lock timeout"):
        mcp_server.call_remote_surface("remote_probe", probe_args, config=cfg, receipts=receipts_fail)


def test_organizer_scratch_cleanup_timeout_preserves_completed_response(
    isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_dir = isolated_oc_env["root"] / "org-scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(organizer.summary_scratch, "create_profile", lambda **_kw: (str(scratch_dir), False))
    monkeypatch.setattr(organizer, "_resolve_endpoint", lambda: {"url": "http://127.0.0.1:9/v1/summary", "token": "t"})

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, _n=-1):
            return json.dumps({"result": '{"ok":true}', "usage": {"input_tokens": 10, "output_tokens": 4}}).encode("utf-8")

    monkeypatch.setattr(organizer.urllib.request, "build_opener", lambda *_a: type("_O", (), {"open": lambda *_x, **_k: _FakeResp()})())
    monkeypatch.setattr(organizer.shutil, "rmtree", lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("scratch rmtree timed out")))
    sm = organizer.NativeSummaryModel()
    assert sm._call_once("summarize this", {"type": "object"}) == '{"ok":true}' and sm.cleanup_failed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX nested start_new_session PGID reaping test")
def test_supervised_reexec_reaps_nested_bounded_run_process_group(isolated_oc_env: dict[str, Path]) -> None:
    pid_file = isolated_oc_env["root"] / "nested-grandchild.pid"
    scripts_dir = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
    child_code = (
        f"import sys; sys.path.insert(0, {str(scripts_dir)!r}); import bounded; "
        "bounded.run([sys.executable, '-c', "
        "'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)', "
        "sys.argv[1]], '', timeout=25.0)"
    )
    with pytest.raises(TimeoutError, match="supervised process timed out"):
        bounded.run_supervised_reexec(
            [sys.executable, "-I", "-c", child_code, str(pid_file)],
            timeout=0.6,
            env=dict(os.environ),
        )
    assert pid_file.is_file()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 1.5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
            time.sleep(0.03)
        except ProcessLookupError:
            alive = False
            break
    if alive:
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            alive = False
    assert alive is False, f"nested bounded.run grandchild PID {grandchild_pid} survived supervised re-exec timeout"


def test_transcript_cursor_recovery_corrupt_rejection_and_capture_unavailable_visibility(
    isolated_oc_env: dict[str, Path],
) -> None:
    db_file = isolated_oc_env["opencode_db"]
    spool_dir = isolated_oc_env["mindie_data"] / "oc-state" / "transcripts"
    ws = str(isolated_oc_env["workspace"])

    db = sqlite3.connect(db_file)
    try:
        with db:
            db.execute(
                "CREATE TABLE session_v2 ("
                "id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "CREATE TABLE session_message ("
                "id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "CREATE TABLE session_part ("
                "message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "INSERT INTO session_v2 VALUES ('ses_cur_01', ?, NULL, NULL, 1710000000000, 1710000000000, '{}')",
                (ws,),
            )
            db.execute(
                "INSERT INTO session_message VALUES ('m1', 'ses_cur_01', 0, 'user', 1710000001000, 1710000001000, ?)",
                (json.dumps({"time": {"created": 1710000001000}, "text": "First turn question"}),),
            )
    finally:
        db.close()

    spool = opencode_transcript.sync_session_spool("ses_cur_01", db_path=db_file, spool_dir=spool_dir)
    assert spool is not None
    cursor_file = spool_dir / "ses_cur_01.seq"
    assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    # 1. Missing cursor file with intact spool recovers last_seq without duplicating lines!
    cursor_file.unlink()
    opencode_transcript.sync_session_spool("ses_cur_01", db_path=db_file, spool_dir=spool_dir)
    assert len(spool.read_text(encoding="utf-8").splitlines()) == 1

    # 2. Corrupt cursor file raises ValueError instead of resetting to -1!
    cursor_file.write_text("corrupt-seq\n", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt transcript cursor"):
        opencode_transcript.sync_session_spool("ses_cur_01", db_path=db_file, spool_dir=spool_dir)
    cursor_file.write_text(json.dumps({"seq": 0, "not_before": None}) + "\n", encoding="utf-8")

    # 3. Corrupt session_message JSON raises ValueError instead of silently substituting `{}`!
    db = sqlite3.connect(db_file)
    try:
        with db:
            db.execute(
                "INSERT INTO session_message VALUES ('m2', 'ses_cur_01', 1, 'assistant', 1710000002000, 1710000002000, '{bad-json')"
            )
    finally:
        db.close()
    with pytest.raises(ValueError, match="corrupt session_message JSON"):
        opencode_transcript.sync_session_spool("ses_cur_01", db_path=db_file, spool_dir=spool_dir)

    # 4. capture_stop propagates corrupt message JSON and marks missing DB for activated session as stage="unavailable".
    handle_gesture({"sessionID": "ses_cur_01", "directory": ws, "prompt": "/mindie-agent"})
    setup.ensure_default_profile(config=config_path(), python_exe=str(isolated_oc_env["python"]))
    setup.configure_community(
        enabled=True,
        fork="test-user/knowledge-vllm-ascend",
        account="test-user",
        project_root=ws,
    )
    with pytest.raises(ValueError, match="corrupt session_message JSON"):
        capture_stop.capture_event({"sessionID": "ses_cur_01", "opencodeDb": str(db_file)})

    missing_db = isolated_oc_env["root"] / "nonexistent-opencode.db"
    handle_gesture({"sessionID": "ses_no_db_01", "directory": ws, "prompt": "/mindie-agent"})
    unavail = capture_stop.capture_event({"sessionID": "ses_no_db_01", "opencodeDb": str(missing_db)})
    assert unavail == {"stage": "unavailable", "reason": "transcript_unavailable"}

    # 5. Corrupt community.json during capture_stop surfaces stage="unavailable" (authority-unavailable), never "inert".
    community_config_path().write_text("{corrupt-community", encoding="utf-8")
    fault_res = capture_stop.capture_event({"sessionID": "ses_no_db_01", "opencodeDb": str(missing_db)})
    assert fault_res["stage"] == "unavailable"
    assert fault_res["reason"] == "authority-unavailable"


def test_pyproject_sources_and_overrides_lockstep_and_diagnostics_isolation(
    isolated_oc_env: dict[str, Path],
) -> None:
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = data["tool"]["uv"]["sources"]
    overrides: dict[str, str] = {}
    for entry in data["tool"]["uv"]["override-dependencies"]:
        pkg, url = [part.strip() for part in entry.split("@", 1)]
        overrides[pkg] = url.rsplit("@", 1)[1]
    assert set(sources) == set(overrides)
    for pkg, spec in sources.items():
        assert overrides[pkg] == spec["rev"], f"pyproject.toml source vs override mismatch for {pkg}"
    assert diagnostics_config_path().is_relative_to(isolated_oc_env["root"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX nested supervisor self-signal & normal-exit reaping test")
def test_nested_supervised_reexec_does_not_signal_self_and_normal_exit_reaps_grandchild(
    isolated_oc_env: dict[str, Path],
) -> None:
    scripts_dir = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
    # 1. Inner supervisor inside outer supervisor must NOT send SIGTERM to itself when reaping a SIGTERM-ignoring child.
    gc_pid_file = isolated_oc_env["root"] / "sigterm-ignoring-gc.pid"
    inner_supervisor_code = (
        f"import sys; sys.path.insert(0, {str(scripts_dir)!r}); import bounded\n"
        "gc = 'import os, pathlib, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'\n"
        "try:\n"
        "    bounded.run_supervised_reexec([sys.executable, '-I', '-c', gc, sys.argv[1]], timeout=0.35, cleanup_grace=0.6)\n"
        "except TimeoutError:\n"
        "    raise SystemExit(42)\n"
        "raise SystemExit(0)\n"
    )
    rc = bounded.run_supervised_reexec(
        [sys.executable, "-I", "-c", inner_supervisor_code, str(gc_pid_file)],
        timeout=4.0,
        env=dict(os.environ),
    )
    assert rc == 42, f"inner supervisor exited {rc} instead of catching TimeoutError cleanly"
    gc_pid = int(gc_pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)

    # 2. Normal exit (rc == 0) of a supervised child also reaps nested start_new_session grandchildren.
    orphan_pid_file = isolated_oc_env["root"] / "normal-exit-orphan.pid"
    spawner_code = (
        f"import subprocess, sys, time; sys.path.insert(0, {str(scripts_dir)!r}); import bounded\n"
        "p = subprocess.Popen([sys.executable, '-I', '-c', "
        "'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)', "
        "sys.argv[1]], start_new_session=True)\n"
        "bounded._register_owned_pgid(p.pid, bounded._ancestor_pgid_files())\n"
        "deadline = time.monotonic() + 2.0\n"
        "import pathlib\n"
        "while not pathlib.Path(sys.argv[1]).is_file() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )
    assert (
        bounded.run_supervised_reexec(
            [sys.executable, "-I", "-c", spawner_code, str(orphan_pid_file)],
            timeout=4.0,
            env=dict(os.environ),
        )
        == 0
    )
    orphan_pid = int(orphan_pid_file.read_text(encoding="utf-8").strip())
    wait_end = time.monotonic() + 1.0
    reaped = False
    while time.monotonic() < wait_end:
        try:
            os.kill(orphan_pid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            reaped = True
            break
    assert reaped is True, f"orphan grandchild PID {orphan_pid} survived normal supervised exit"


def test_native_candidate_inventory_failure_does_not_flip_active_profile(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from test_oc_update_and_continuity import _copy_plugin_checkout

    py = str(isolated_oc_env["python"])
    cfg = config_path()
    v1_dir = isolated_oc_env["root"] / "plugin-0.1.2"
    v2_dir = isolated_oc_env["root"] / "plugin-0.1.9"
    _copy_plugin_checkout(v1_dir, version="0.1.2")
    _copy_plugin_checkout(v2_dir, version="0.1.9")

    setup.ensure_default_profile(config=cfg, source_root=v1_dir, python_exe=py)
    assert read_json(cfg)["product_validation"]["native_version"] == "0.1.2"

    monkeypatch.setattr(component_update, "compatible_runtime", lambda interpreter, **_kw: component_update._norm_python_path(interpreter))
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    monkeypatch.setattr(
        component_update,
        "read_inventory",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("candidate inventory corrupted")),
    )
    res = component_update.check(config=cfg, source=v2_dir)
    assert res["status"] == "failed"
    assert res["needs_host_reload"] is False
    assert read_json(cfg)["product_validation"]["native_version"] == "0.1.2"


def _parse_framed_responses(raw: bytes) -> list[dict]:
    out: list[dict] = []
    pos = 0
    while pos < len(raw):
        sep = raw.index(b"\r\n\r\n", pos)
        length = int(raw[pos:sep].split(b":", 1)[1].strip())
        body_start = sep + 4
        out.append(json.loads(raw[body_start : body_start + length].decode("utf-8")))
        pos = body_start + length
    return out


def test_oversized_mcp_frame_and_shared_deactivation_deadline(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=py)

    # 1. Oversized Content-Length frame is rejected with -32700 while draining payload in bounded chunks.
    monkeypatch.setattr(mcp_server, "MAX_STDIO_FRAME_BYTES", 64)
    big_body = b"x" * 128
    ping_body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode("utf-8")
    stream_in = io.BytesIO(
        f"Content-Length: {len(big_body)}\r\n\r\n".encode("ascii")
        + big_body
        + f"Content-Length: {len(ping_body)}\r\n\r\n".encode("ascii")
        + ping_body
    )
    stream_out = io.BytesIO()
    assert mcp_server.serve("tools", config=cfg, stdin=stream_in, stdout=stream_out) == 0
    frames = _parse_framed_responses(stream_out.getvalue())
    assert frames[0]["error"]["code"] == -32700
    assert "exceeds" in frames[0]["error"]["message"]
    assert frames[1]["id"] == 9 and frames[1]["result"] == {}

    # 2. admission.activate enforces caller budget / SQLite floor; deactivate shares one deadline.
    with pytest.raises(TimeoutError):
        admission.activate("ses_under_floor", isolated_oc_env["workspace"], config=cfg, timeout=0.5)
    assert admission.knowledge_lease("ses_under_floor", config=cfg) is None
    adm_res = admission.activate("ses_deadline_01", isolated_oc_env["workspace"], config=cfg)
    assert adm_res["status"] == "active"
    recorded_timeouts: list[float] = []
    real_gate = admission.gate

    class _SpyGate:
        def __init__(self, inner):
            self.path = inner.path
            self._inner = inner

        def deactivate(self, sid, *, timeout=5.0):
            recorded_timeouts.append(timeout)
            return self._inner.deactivate(sid, timeout=timeout)

    orig_lock = admission._pending_lock

    def _slow_lock(c=None, *, timeout=5.0):
        time.sleep(0.25)
        return orig_lock(c, timeout=timeout)

    monkeypatch.setattr(admission, "_pending_lock", _slow_lock)
    monkeypatch.setattr(admission, "gate", lambda c=None: _SpyGate(real_gate(c)))
    out = admission.deactivate("ses_deadline_01", config=cfg, timeout=6.0)
    assert out["status"] == "inactive"
    assert len(recorded_timeouts) == 1 and 5.5 <= recorded_timeouts[0] <= 5.8


def test_session_state_fault_continuity_supervisor_receipt_and_cleanup_bounds(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    ws = str(isolated_oc_env["workspace"])
    setup.ensure_default_profile(config=cfg, python_exe=py)

    # 1. Enclosing supervisor budget strictly exceeds child alarm budget and pops supervisor deadline env.
    assert mindie_launch.HOOK_SUPERVISOR_BUDGET >= mindie_launch.HOOK_EXEC_BUDGET + 2.0
    assert mindie_launch.HOOK_OUTER_SECONDS >= mindie_launch.HOOK_SUPERVISOR_BUDGET + mindie_launch.HOOK_CLEANUP_GRACE + 3.0
    monkeypatch.setenv("_MINDIE_OC_SUPERVISOR_DEADLINE", f"{time.monotonic() + 5.0:.6f}")
    with mindie_launch._setup_outer_budget(8.0):
        assert "_MINDIE_OC_SUPERVISOR_DEADLINE" not in os.environ

    # 2. Corrupt admission.sqlite3 surfaces unavailable diagnostics without blocking ordinary context or user task prompts.
    adm_db = admission.admission_path(cfg)
    adm_db.parent.mkdir(parents=True, exist_ok=True)
    adm_db.write_bytes(b"corrupt-sqlite-header")
    node_bin = shutil.which("node")
    assert node_bin is not None
    host_uri = (REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs").as_uri()
    status_dir_blocker = data_home() / "oc-component-update-status.json"
    status_dir_blocker.mkdir(parents=True, exist_ok=True)
    newer_root = isolated_oc_env["root"] / "fake-installed-9.9.9"
    (newer_root / "plugins" / "mindie-agent").mkdir(parents=True, exist_ok=True)
    (newer_root / "plugins" / "mindie-agent" / "plugin.json").write_text('{"version":"9.9.9"}\n', encoding="utf-8")
    js_probe = f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import hostMod from {json.dumps(host_uri)};
const cfgFile = {json.dumps(str(cfg))};
const cfgBak = cfgFile + ".bak";
fs.renameSync(cfgFile, cfgBak);
process.env.MINDIE_OC_ALLOW_BG_UPDATE = "1";
process.env.MINDIE_OC_INSTALLED_ROOT = {json.dumps(str(newer_root))};
process.env.MINDIE_OC_PYTHON = "/nonexistent-mindie-python-enoent";
const hooks = {{ session: {{}}, tool: {{}} }};
const ctx = {{
  location: {{ directory: {json.dumps(ws)} }},
  session: {{ get: async () => ({{ directory: {json.dumps(ws)} }}), hook: (n, fn) => {{ hooks.session[n] = fn; }} }},
  tool: {{ hook: (n, fn) => {{ hooks.tool[n] = fn; }} }},
}};
let dispose = await hostMod.setup(ctx);
await new Promise((r) => setTimeout(r, 40));
await dispose();
fs.renameSync(cfgBak, cfgFile);
process.env.MINDIE_OC_PYTHON = {json.dumps(py)};
dispose = await hostMod.setup(ctx);
const sysArr = [];
await hooks.session.context({{ sessionID: "ses_inactive_ordinary", system: sysArr }});
assert.equal(sysArr.length, 0);
const promptObj = {{ text: "/mindie-agent optimize decode kernel" }};
await hooks.session.prompt({{ sessionID: "ses_inactive_ordinary", prompt: promptObj }});
assert.match(promptObj.text, /optimize decode kernel/);
assert.match(promptObj.text, /MindIE Unavailable/);
await dispose();
console.log("CONTINUITY_OK");
"""
    probe = subprocess.run(
        [node_bin, "--input-type=module", "-e", js_probe],
        capture_output=True, text=True, env=dict(os.environ, MINDIE_OC_PYTHON=py), timeout=15.0,
    )
    shutil.rmtree(status_dir_blocker, ignore_errors=True)
    assert probe.returncode == 0, probe.stderr
    assert "CONTINUITY_OK" in probe.stdout and "unavailable" in probe.stderr.lower()
    adm_db.unlink(missing_ok=True)

    # 3. Post-completion cleanup TimeoutError preserves delayed pipe output; timeout cleanup runs once.
    kill_calls: list[float] = []
    real_kill = bounded._kill_process_tree
    monkeypatch.setattr(
        bounded, "_kill_process_tree",
        lambda *a, **kw: (kill_calls.append(float(kw.get("wait_timeout", 0.0))), real_kill(*a, **kw))[1],
    )
    with pytest.raises(TimeoutError):
        bounded.run_supervised_reexec([py, "-c", "import time; time.sleep(5)"], timeout=0.15, cleanup_grace=0.3)
    assert len(kill_calls) == 1 and kill_calls[0] <= 0.35

    real_drain = bounded._drain_pipe_bounded
    monkeypatch.setattr(bounded, "_drain_pipe_bounded", lambda p, m, s: (time.sleep(0.05), real_drain(p, m, s))[1])
    monkeypatch.setattr(
        bounded, "_kill_process_tree",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("simulated post-completion cleanup timeout")),
    )
    assert bounded.run_supervised_reexec([py, "-c", "raise SystemExit(0)"], timeout=5.0, cleanup_grace=0.3) == 0
    res_ok = bounded.run([py, "-c", "print('DONE_OK')"], timeout=5.0, cleanup_grace=0.5)
    assert res_ok.returncode == 0 and res_ok.stdout.splitlines() == ["DONE_OK"] and res_ok.cleanup_failed is True
    assert "post-completion cleanup warning" in capsys.readouterr().err

    # 4. Windows breakaway marker write TimeoutError propagates without spawning a doomed fallback child.
    import types

    marker_path = isolated_oc_env["root"] / "no-breakaway-marker"
    posix_path_cls = type(cfg)
    monkeypatch.setenv("MINDIE_OC_ALLOW_BG_UPDATE", "1")
    monkeypatch.setenv("_MINDIE_OC_BG_NO_BREAKAWAY", str(marker_path))
    monkeypatch.setattr(component_update, "os", types.SimpleNamespace(name="nt", environ=os.environ))
    spawn_calls = 0

    def _win_deny_breakaway(*_a, **_kw):
        nonlocal spawn_calls
        spawn_calls += 1
        err = OSError(5, "Access is denied")
        err.winerror = 5  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(component_update.subprocess, "Popen", _win_deny_breakaway)
    monkeypatch.setattr(
        posix_path_cls,
        "write_text",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("marker write timed out")),
    )
    with pytest.raises(TimeoutError, match="marker write timed out"):
        component_update.trigger_background_update_if_due(config=cfg)
    assert spawn_calls == 1
