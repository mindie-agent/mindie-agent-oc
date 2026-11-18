"""MCP surface boundary, Receipt Honesty, and default-enabled `knowledge_*` tests (`test_oc_mcp_and_knowledge.py`).

[Role]
Verifies that `mcp_server.py` exposes all 20 `remote_*` tools on `mindie-remote-dev`
and all 19 tools (14 deterministic + 5 default-on `knowledge_*` tools) on
`mindie-tools` on startup, routes `knowledge_query` & `knowledge_explain` to
`read_pool`, preserves primary results when local SQLite accounting fails
(Receipt Honesty), and allows `knowledge_cli.py` without prior session activation.

[Invariants]
- All 5 `knowledge_*` tools appear in `tools/list` on `mindie-tools` by default and
  work with zero `_mindie_bind_id`, zero `OPENCODE_SESSION_ID`, and zero prior
  manual activation (`_CONNECTION_SESSION_ID` / `"oc-cli-default"` fallback +
  on-demand `not_before=0.0` lease).
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

import admission
from bridge import handle_pre_tool_use
import host_adapter
import knowledge_cli
import mcp_server
from paths import admission_path

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


def test_public_mcp_exposes_default_on_knowledge_and_remote_surfaces(isolated_oc_env: dict[str, Path]) -> None:
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
    tools_list = tools_res[1]["result"]["tools"]
    tool_by_name = {t["name"]: t for t in tools_list}
    assert len(tools_list) == 19
    for expected in (
        "mindie_status",
        "debugger",
        "profiler",
        "sim",
        "coordinator",
        "npu_top",
        "diagnostics_status",
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    ):
        assert expected in tool_by_name
        assert "_mindie_bind_id" not in tool_by_name[expected]["inputSchema"].get("properties", {})

    assert tool_by_name["knowledge_query"]["annotations"]["readOnlyHint"] is True
    assert tool_by_name["knowledge_explain"]["annotations"]["readOnlyHint"] is True
    assert tool_by_name["knowledge_feedback"]["annotations"]["readOnlyHint"] is False
    assert tool_by_name["knowledge_distill"]["annotations"]["readOnlyHint"] is False
    assert tool_by_name["knowledge_sync"]["annotations"]["openWorldHint"] is True
    assert host_adapter.is_fast_lane_tool("tools", "knowledge_query", {}) is True
    assert host_adapter.is_fast_lane_tool("tools", "knowledge_explain", {}) is True
    assert host_adapter.is_fast_lane_tool("tools", "knowledge_distill", {}) is False
    assert not admission_path().exists()


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


def test_default_on_knowledge_mcp_and_cli_without_prior_activation(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cli = str(REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "knowledge_cli.py")
    ws = str(isolated_oc_env["workspace"])

    # 1. `knowledge_cli.py describe` works with zero OPENCODE_SESSION_ID & creates zero SQLite files.
    env_no_sid = dict(os.environ)
    env_no_sid.pop("OPENCODE_SESSION_ID", None)
    p1 = subprocess.run([py, "-I", cli, "describe"], env=env_no_sid, capture_output=True, text=True, timeout=10.0)
    assert p1.returncode == 0, p1.stderr
    payload = json.loads(p1.stdout)
    assert payload["isError"] is False
    names = [t["name"] for t in payload["tools"]]
    assert names == [
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    ]
    assert not admission_path().exists()

    # 2. Direct MCP `knowledge_query` with zero OPENCODE_SESSION_ID & zero prior hook auto-provisions
    # default lease (`not_before=0.0`) on `_CONNECTION_SESSION_ID` and preserves Receipt Honesty!
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.transport as loop_transport

    monkeypatch.setattr(loop_cli, "ensure_service", lambda _cfg: "fake-conn")
    seen_rpc: list[tuple[str, dict]] = []

    def _fake_rpc(_conn, method: str, rpc_payload: dict, timeout: float = 30.0):
        seen_rpc.append((method, dict(rpc_payload)))
        return {"cards": [{"ref": "mindie://vllm-ascend/units/u1@1"}], "foundation_cards": []}

    monkeypatch.setattr(loop_transport, "rpc", _fake_rpc)
    monkeypatch.setattr(
        mcp_server.RemoteReceipts,
        "finish",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("simulated receipt finish disk full")),
    )

    mcp_res = mcp_server.call_tools_surface("knowledge_query", {"query": "ACLGraph", "mode": "standard"})
    assert mcp_res["isError"] is False
    assert mcp_res["structuredContent"]["cards"][0]["ref"] == "mindie://vllm-ascend/units/u1@1"
    assert mcp_res["structuredContent"]["receipt_unconfirmed"] is True
    assert "receipt_warning" in mcp_res["structuredContent"]
    assert seen_rpc[0][0] == "query"
    assert seen_rpc[0][1]["mode"] == "reuse"
    assert seen_rpc[0][1]["_session_id"] == mcp_server._CONNECTION_SESSION_ID

    auto_lease = admission.knowledge_lease(mcp_server._CONNECTION_SESSION_ID)
    assert auto_lease is not None
    assert float(auto_lease["activated_at"]) == 0.0

    # 3. `knowledge_sync` list RPC return value is wrapped in a dict (`{"feeds": ...}`) for MCP structuredContent!
    def _fake_sync_rpc(_conn, method: str, rpc_payload: dict, timeout: float = 30.0):
        seen_rpc.append((method, dict(rpc_payload)))
        return [{"domain": "vllm-ascend", "status": "synced"}]

    monkeypatch.setattr(loop_transport, "rpc", _fake_sync_rpc)
    sync_res = mcp_server.call_tools_surface("knowledge_sync", {})
    assert sync_res["isError"] is False
    assert sync_res["structuredContent"]["feeds"] == [{"domain": "vllm-ascend", "status": "synced"}]
    monkeypatch.setattr(loop_transport, "rpc", _fake_rpc)

    # 4. Direct `knowledge_cli.invoke` without session arg falls back to `"oc-cli-default"`, never seeds
    # `project_roots` on auto-profile init, and preserves existing lease's verified `project_root` when `project_root=None`!
    cli_res = knowledge_cli.invoke("knowledge_query", {"query": "HCCL"}, project_root=ws)
    assert cli_res["isError"] is False
    from paths import community_config_path, read_json

    comm_auto = read_json(community_config_path())
    assert comm_auto["enabled"] is False
    assert comm_auto["project_roots"] == []
    cli_lease = admission.knowledge_lease("oc-cli-default")
    assert cli_lease is not None and float(cli_lease["activated_at"]) == 0.0
    assert Path(cli_lease["project_root"]).resolve() == Path(ws).resolve()
    other_cwd = isolated_oc_env["root"] / "other-cwd"
    other_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other_cwd)
    assert knowledge_cli.invoke("knowledge_query", {"query": "HCCL"}, project_root=None)["isError"] is False
    assert Path(admission.knowledge_lease("oc-cli-default")["project_root"]).resolve() == Path(ws).resolve()

    # 5. `knowledge_sync` TimeoutError records `"unknown"` in RemoteReceipts!
    monkeypatch.setattr(
        loop_transport,
        "rpc",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("sync timed out")),
    )
    monkeypatch.undo() if False else None
    t_receipts = mcp_server.RemoteReceipts(isolated_oc_env["root"] / "sync-timeout.sqlite3")
    monkeypatch.setattr(mcp_server.RemoteReceipts, "finish", mcp_server.RemoteReceipts.finish)
    # Restore real finish on a subclass to bypass earlier monkeypatch on RemoteReceipts.finish!
    class _RealReceipts(mcp_server.RemoteReceipts):
        def finish(self, call_id: str, status: str) -> None:
            db = self._connect()
            try:
                with db:
                    db.execute("UPDATE receipts SET status=? WHERE call_id=?", (status, call_id))
            finally:
                db.close()

    real_rec = _RealReceipts(t_receipts.path)
    t_out = mcp_server.call_tools_surface("knowledge_sync", {}, receipts=real_rec)
    assert t_out["isError"] is True
    with sqlite3.connect(real_rec.path) as db:
        assert [r[0] for r in db.execute("SELECT status FROM receipts")] == ["unknown"]
    monkeypatch.setattr(loop_transport, "rpc", _fake_rpc)

    # 6. Explicit programmatic deactivation blocks subsequent calls on that session (`knowledge_inactive`).
    admission.deactivate("ses_explicit_off")
    off_res = mcp_server.call_tools_surface(
        "knowledge_query", {"query": "blocked"}, config=None
    )
    assert off_res["isError"] is False  # `_CONNECTION_SESSION_ID` stays independent and active!
    with pytest.raises(knowledge_cli.KnowledgeInactive):
        knowledge_cli.invoke("knowledge_query", {"query": "blocked"}, session="ses_explicit_off")


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
    assert status_res["structuredContent"]["activated"] is True

    dbg_res = responses[1]["result"]
    assert dbg_res["isError"] is False
    assert "507015" in dbg_res["content"][0]["text"]
