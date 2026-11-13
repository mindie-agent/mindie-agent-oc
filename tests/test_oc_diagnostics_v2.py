"""Tier 1 crash-gateway and MCP operational-exception filtering tests for OpenCode (`mindie-agent-oc`)."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnostic_support  # noqa: E402
import mcp_server  # noqa: E402


def test_oc_diagnostics_v2_privacy_and_mcp_operational_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    cfg_dir = base / "config" / "mindie-agent"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    oc_cfg = cfg_dir / "oc.json"
    oc_cfg.write_text(json.dumps({"host": "opencode"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(base / "diag"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_dir / "diagnostics.json"))
    monkeypatch.setenv("OPENCODE_SESSION_ID", "oc-test-session-01")
    (base / "home").mkdir(parents=True, exist_ok=True)
    (base / "diag").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mcp_server, "_resolve_session", lambda *_a, **_k: ("oc-test-session-01", None, None))
    monkeypatch.setattr(
        mcp_server,
        "status_facts",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("operational_denied")),
    )
    perm_res = mcp_server.call_tools_surface("mindie_status", {}, config=oc_cfg)
    assert perm_res["isError"] is True
    assert "diagnostic" not in perm_res["structuredContent"]

    frames, chain = diagnostic_support._extract_owned_traceback(
        'Traceback (most recent call last):\n'
        '  File "/opt/v/lib/python3.12/site-packages/npu_top/__init__.py", line 8, in <module>\n'
        'AttributeError: err\nPrivateCustomerName: secret\n',
        "npu_top",
    )
    assert chain == ["AttributeError"]
    assert frames == [{"module": "npu_top", "function": "<module>", "line": 8}]
    assert diagnostic_support.record_component_crash(
        "npu_top", ["snapshot"], 1, f"Traceback...\nmindie-diagnostic-incident: {'2' * 32}\n", config=oc_cfg
    ) == {"incident_id": "2" * 32, "logging_failed": False}
