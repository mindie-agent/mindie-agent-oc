"""MCP surface boundary, Receipt Honesty, and private `knowledge_cli.py` tests (`test_oc_mcp_and_knowledge.py`).

[Role]
Verifies that `mcp_server.py` exposes the exact deterministic `remote` and
`tools` surfaces with zero `knowledge_*` tools, preserves primary results when
local SQLite receipt/bind accounting fails (Receipt Honesty), executes real
`mindie_status` and `debugger` tools, and gates `knowledge_cli.py` by active
`OPENCODE_SESSION_ID` lease.

[Invariants]
- `knowledge_*` never appears in `tools/list` on either MCP surface, even when
  a session is activated.
- Succeeded primary operations never flip to `isError=True` on local SQLite
  accounting errors.

[Timeouts & Locks]
- Local in-process and bounded subprocess tests (`< 10s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import pytest

from bridge import handle_gesture, handle_pre_tool_use
import mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_jsonrpc(surface: str, messages: list[dict], *, content_length: bool = False) -> list[dict]:
    raw_in = io.BytesIO()
    for msg in messages:
        encoded = json.dumps(msg).encode("utf-8")
        if content_length:
            raw_in.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
        else:
            raw_in.write(encoded + b"\n")
    raw_in.seek(0)
    raw_out = io.BytesIO()
    rc = mcp_server.serve(surface, stdin=raw_in, stdout=raw_out)
    assert rc == 0
    data = raw_out.getvalue()
    if content_length:
        responses = []
        idx = 0
        while idx < len(data):
            hdr_end = data.index(b"\r\n\r\n", idx)
            hdr = data[idx:hdr_end].decode("ascii")
            length = int(hdr.split(":", 1)[1].strip())
            body_start = hdr_end + 4
            responses.append(json.loads(data[body_start : body_start + length].decode("utf-8")))
            idx = body_start + length
        return responses
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def test_public_mcp_excludes_knowledge_even_when_session_is_active(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    handle_gesture({"sessionID": "ses_active_mcp", "directory": ws, "prompt": "/mindie-agent"})

    remote_res = _run_jsonrpc(
        "remote",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    remote_names = [t["name"] for t in remote_res[1]["result"]["tools"]]
    assert len(remote_names) == 20
    assert "remote_bash" in remote_names
    assert all(not name.startswith("knowledge_") for name in remote_names)

    tools_res = _run_jsonrpc(
        "tools",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
        content_length=True,
    )
    tool_names = [t["name"] for t in tools_res[1]["result"]["tools"]]
    assert len(tool_names) == 14
    for expected in ("mindie_status", "debugger", "profiler", "sim", "coordinator", "npu_top", "diagnostics_status"):
        assert expected in tool_names
    assert all(not name.startswith("knowledge_") for name in tool_names)


def test_receipt_honesty_preserves_succeeded_remote_result_on_local_sqlite_error(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = str(isolated_oc_env["workspace"])
    args = {"host": "127.0.0.1"}
    handle_pre_tool_use(
        {
            "sessionID": "ses_receipt01",
            "id": "call_r1",
            "tool": "mindie-remote-dev_remote_probe",
            "input": args,
            "directory": ws,
        }
    )

    import remote_dev.mcp.tools as remote_tools

    monkeypatch.setattr(
        remote_tools,
        "call_tool",
        lambda name, a, profile="canonical": {"outcome": "success", "probe": "ok"},
    )

    def _broken_finish_bind(*_a, **_kw):
        raise sqlite3.OperationalError("simulated local disk full on mcp-binds.sqlite3")

    monkeypatch.setattr(mcp_server, "finish_bind", _broken_finish_bind)
    monkeypatch.setattr(
        mcp_server.RemoteReceipts,
        "finish",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("simulated local disk full on remote-receipts.sqlite3")),
    )

    res = _run_jsonrpc(
        "remote",
        [{"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "remote_probe", "arguments": args}}],
    )[0]["result"]

    assert res["isError"] is False
    assert res["structuredContent"]["probe"] == "ok"
    assert res["structuredContent"]["receipt_unconfirmed"] is True
    assert "receipt_warning" in res["structuredContent"]
    assert "bind_warning" in res["structuredContent"]


def test_knowledge_cli_requires_active_lease_and_executes_when_active(isolated_oc_env: dict[str, Path]) -> None:
    py = str(isolated_oc_env["python"])
    cli = str(REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "knowledge_cli.py")
    ws = str(isolated_oc_env["workspace"])

    # 1. Missing OPENCODE_SESSION_ID -> nonzero exit + isError=True.
    env_no_sid = dict(os.environ)
    env_no_sid.pop("OPENCODE_SESSION_ID", None)
    p1 = subprocess.run([py, "-I", cli, "describe"], env=env_no_sid, capture_output=True, text=True, timeout=10.0)
    assert p1.returncode == 1
    assert json.loads(p1.stdout)["isError"] is True

    # 2. Unactivated OPENCODE_SESSION_ID -> nonzero exit + knowledge_inactive.
    env_unact = dict(os.environ, OPENCODE_SESSION_ID="ses_unactivated_99")
    p2 = subprocess.run([py, "-I", cli, "describe"], env=env_unact, capture_output=True, text=True, timeout=10.0)
    assert p2.returncode == 1
    assert json.loads(p2.stdout)["structuredContent"]["code"] == "knowledge_inactive"

    # 3. Activated OPENCODE_SESSION_ID -> exit 0 + private 5-tool catalog returned.
    handle_gesture({"sessionID": "ses_know_ok", "directory": ws, "prompt": "/mindie-agent"})
    env_act = dict(os.environ, OPENCODE_SESSION_ID="ses_know_ok")
    p3 = subprocess.run([py, "-I", cli, "describe"], env=env_act, capture_output=True, text=True, timeout=10.0)
    assert p3.returncode == 0
    payload = json.loads(p3.stdout)
    assert payload["isError"] is False
    names = [t["name"] for t in payload["tools"]]
    assert names == [
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    ]


def test_deterministic_debugger_and_status_mcp_tools(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    handle_pre_tool_use(
        {
            "sessionID": "ses_det01",
            "id": "call_status",
            "tool": "mindie-tools_mindie_status",
            "input": {},
            "directory": ws,
        }
    )
    dbg_args = {"argv": ["decode-code", "507015"]}
    handle_pre_tool_use(
        {
            "sessionID": "ses_det01",
            "id": "call_dbg",
            "tool": "mindie-tools_debugger",
            "input": dbg_args,
            "directory": ws,
        }
    )

    responses = _run_jsonrpc(
        "tools",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mindie_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "debugger", "arguments": dbg_args}},
        ],
    )
    status_res = responses[0]["result"]
    assert status_res["isError"] is False
    assert status_res["structuredContent"]["harness"] == "opencode"
    assert status_res["structuredContent"]["activated"] is False

    dbg_res = responses[1]["result"]
    assert dbg_res["isError"] is False
    assert "507015" in dbg_res["content"][0]["text"]
