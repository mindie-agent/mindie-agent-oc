"""MCP surface, OC defaults, and knowledge fallback Receipt Honesty tests.

[Role]
Verifies the `oc` remote profile defaults, default-on `knowledge_*` and
`mindie_doctor` tools, zero `_meta` / `_mindie_bind_id` schemas, and Receipt
Honesty when the knowledge fallback's `Admission.finish` raises `OSError`.

[Invariants]
- `REMOTE_DEV_HARNESS_PROFILE` defaults to `oc` and `REMOTE_DEV_STATE_DIR` to
  `<state_dir>/remote` unless the caller overrides them.
- A succeeded fallback result keeps `isError=False` and gains
  `receipt_unconfirmed=True` + `receipt_warning`.

[Timeouts & Locks]
- Pure in-process adapter inspection; zero service starts.

[Side-Effect Policy]
- Reads harness metadata; writes nothing outside `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"


def test_remote_and_tools_surfaces_expose_default_tools(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMOTE_DEV_HARNESS_PROFILE", raising=False)
    monkeypatch.delenv("REMOTE_DEV_STATE_DIR", raising=False)
    adapter = mcp_server.OCManagedAdapter("oc", plugin_root=PLUGIN_ROOT)
    profile = adapter.resolve_profile()
    assert __import__("os").environ["REMOTE_DEV_HARNESS_PROFILE"] == "oc"
    assert Path(__import__("os").environ["REMOTE_DEV_STATE_DIR"]) == profile.state_dir / "remote"

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


def test_knowledge_fallback_receipt_honesty(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = mcp_server.OCManagedAdapter("oc", plugin_root=PLUGIN_ROOT)
    profile = adapter.resolve_profile()

    monkeypatch.setattr(
        mcp_server.ManagedMCPAdapter,
        "dispatch_tool",
        lambda *_a, **_k: {"content": [{"type": "text", "text": "unavailable"}], "isError": True, "structuredContent": {"reason": "knowledge_unavailable"}},
    )

    import mindie_knowledge.loop.activation as activation
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.transport as transport

    monkeypatch.setattr(loop_cli, "connect", lambda *_a, **_k: {"url": "http://127.0.0.1:1/rpc", "token": "t"})
    monkeypatch.setattr(transport, "rpc", lambda *_a, **_k: {"cards": [{"id": "unit-1"}], "foundation_cards": []})

    class FakeAdmission:
        def __init__(self, *_a, **_k):
            pass

        def active_lease(self, *_a, **_k):
            return {"token": "tok"}

        def finish(self, *_a, **_k):
            raise OSError("simulated local disk failure")

    monkeypatch.setattr(activation, "Admission", FakeAdmission)
    ctx = SimpleNamespace(tool_name="knowledge_query", session_id="ses_fallback01", cwd=None)
    result = adapter.dispatch_tool(ctx, {"query": "ACL 507015"}, profile)
    assert result["isError"] is False
    assert result["structuredContent"]["cards"] == [{"id": "unit-1"}]
    assert result["receipt_unconfirmed"] is True and "receipt_warning" in result
    assert "_mindie_bind_id" not in json.dumps(result) and '"_meta"' not in json.dumps(result)
