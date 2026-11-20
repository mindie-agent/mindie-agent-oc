"""MCP surface, OC defaults, and `mindie_harness` Receipt Honesty tests.

[Role]
Verifies the `oc` remote profile defaults, default-on `knowledge_*` and
`mindie_doctor` tools, zero `_meta` / `_mindie_bind_id` schemas, pure
delegation from `OCManagedAdapter` to `mindie_harness` (zero shadow `_rpc`),
and Receipt Honesty when `Admission.finish` raises `OSError`.

[Invariants]
- `REMOTE_DEV_HARNESS_PROFILE` defaults to `oc` and `REMOTE_DEV_STATE_DIR` to
  `<state_dir>/remote` unless the caller overrides them.
- `mcp_server.py` stays `< 50` lines with zero `_rpc` or `"knowledge_unavailable"`.
- A succeeded `mindie_harness` knowledge operation keeps `isError=False` and
  attaches `receipt_unconfirmed=True` + `receipt_warning` on `Admission.finish` failure.

[Timeouts & Locks]
- Pure in-process adapter dispatch; zero external services.

[Side-Effect Policy]
- Reads harness metadata and writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server
import setup as oc_setup

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"


def test_remote_and_tools_surfaces_expose_default_tools(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert len(mcp_source.splitlines()) < 50
    assert not hasattr(mcp_server, "_rpc")
    assert "knowledge_unavailable" not in mcp_source

    monkeypatch.delenv("REMOTE_DEV_HARNESS_PROFILE", raising=False)
    monkeypatch.delenv("REMOTE_DEV_STATE_DIR", raising=False)
    adapter = mcp_server.OCManagedAdapter("oc", plugin_root=PLUGIN_ROOT)
    profile = adapter.resolve_profile()
    assert os.environ["REMOTE_DEV_HARNESS_PROFILE"] == "oc"
    assert Path(os.environ["REMOTE_DEV_STATE_DIR"]) == profile.state_dir / "remote"

    tools = adapter.mcp_tools("tools", profile)
    names = {item["name"] for item in tools}
    assert {"mindie_status", "mindie_doctor", "diagnostics_status", "diagnostics_bundle", "knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill", "knowledge_sync"} <= names
    for item in tools:
        assert "_mindie_bind_id" not in (item.get("inputSchema") or {}).get("properties", {})
        assert "_meta" not in (item.get("inputSchema") or {}).get("properties", {})

    remote = adapter.mcp_tools("remote", profile)
    if remote:
        assert len(remote) == 14
        assert "remote_auth" in {item["name"] for item in remote}


def test_knowledge_fallback_receipt_honesty(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert oc_setup.main(["init"]) == 0
    capsys.readouterr()

    adapter = mcp_server.OCManagedAdapter("oc", plugin_root=PLUGIN_ROOT)
    profile = adapter.resolve_profile()

    import mindie_knowledge.graded.distill as distill_mod
    import mindie_knowledge.loop.activation as activation
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.transport as transport

    monkeypatch.setattr(loop_cli, "ensure_service", lambda *_a, **_k: {"url": "http://127.0.0.1:1/rpc", "token": "t"})
    monkeypatch.setattr(transport, "rpc", lambda _conn, method, payload, **_k: {"cards": [{"id": "unit-1"}], "method": method, "session": payload.get("_session_id")})

    query_ctx = SimpleNamespace(tool_name="knowledge_query", session_id="oc-conn-zero00000001", cwd=None, deadline=None, cancel=None, env=dict(os.environ))
    query_out = adapter.dispatch_tool(query_ctx, {"query": "ACL 507015", "domain": "operators"}, profile)
    assert query_out["isError"] is False
    assert query_out["structuredContent"]["cards"] == [{"id": "unit-1"}]
    assert not profile.admission_db.exists()

    class FakeAdmission:
        def __init__(self, *_a, **_k):
            pass

        def active_lease(self, *_a, **_k):
            return {"token": "tok_distill", "entry_enabled": True}

        def finish(self, *_a, **_k):
            raise OSError("simulated local disk failure")

    monkeypatch.setattr(activation, "Admission", FakeAdmission)
    monkeypatch.setattr(distill_mod, "result_view", lambda raw: (raw, False))
    monkeypatch.setattr(transport, "rpc", lambda *_a, **_k: {"status": "distilled", "unit_id": "vllm-ascend:L2:acl-507015"})

    distill_ctx = SimpleNamespace(tool_name="knowledge_distill", session_id="ses_honesty01", cwd=None, deadline=None, cancel=None, env=dict(os.environ))
    result = adapter.dispatch_tool(distill_ctx, {"inputs": [{"kind": "observation", "text": "Verified ACL 507015 fix"}], "task_state": "completed"}, profile)
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "distilled"
    assert result["receipt_unconfirmed"] is True and "receipt_warning" in result
    assert "_mindie_bind_id" not in json.dumps(result) and '"_meta"' not in json.dumps(result)
