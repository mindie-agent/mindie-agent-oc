"""Tier 1 provenance, MCP operational filter, and post-promotion Receipt Honesty tests for OpenCode (`mindie-agent-oc`)."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mindie_diagnostics.integration import configure_reporting, record_failure  # noqa: E402
import component_update  # noqa: E402
import diagnostic_support  # noqa: E402
import mcp_server  # noqa: E402
from paths import community_config_path, config_path, data_home  # noqa: E402
import setup as oc_setup  # noqa: E402


def test_oc_diagnostics_v2_provenance_mcp_and_inventory_timeout_honesty(
    isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    py = str(isolated_oc_env["python"])
    oc_cfg = config_path()
    gitleaks_stub = isolated_oc_env["home"] / ".cache" / "mindie-agent" / "gitleaks" / "gitleaks"
    gitleaks_stub.parent.mkdir(parents=True, exist_ok=True)
    gitleaks_stub.write_bytes(b"stub")
    oc_setup.ensure_default_profile(config=oc_cfg, python_exe=py)
    diag_cfg = oc_cfg.parent / "diagnostics.json"
    diag_root = isolated_oc_env["root"] / "diag"
    diag_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(diag_root))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(diag_cfg))
    monkeypatch.setenv("OPENCODE_SESSION_ID", "oc-test-session-01")
    configure_reporting(True, roots=[str(diag_root)], config=diag_cfg)

    def _wrapped_timeout(*_a, **_k):
        try:
            raise TimeoutError("deadline")
        except TimeoutError as err:
            raise RuntimeError("wrapper") from err

    monkeypatch.setattr(mcp_server, "_resolve_session", lambda *_a, **_k: ("oc-test-session-01", None, None))
    monkeypatch.setattr(mcp_server, "status_facts", _wrapped_timeout)
    perm_res = mcp_server.call_tools_surface("mindie_status", {}, config=oc_cfg)
    assert perm_res["isError"] is True
    assert "diagnostic" not in perm_res["structuredContent"]

    tok = "6" * 32
    ev_dir = diag_root / "events" / "mindie-coordinator"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "component": "mindie-coordinator",
                "event": "operation.end",
                "status": "success",
                "reportable": False,
                "operation_id": "f" * 32,
                "invocation_token": tok,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        diagnostic_support.record_component_crash(
            "coordinator", ["hosts"], 1, f"mindie-diagnostic-incident: {'f' * 32}\n", config=oc_cfg, invocation_token=tok
        )
        is None
    )
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_INVOCATION_TOKEN", tok)
    rec = record_failure(
        "mindie-coordinator", "mindie.hosts", stage="cli_dispatch", category="internal_exception", exception=KeyError("k")
    )
    assert diagnostic_support.record_component_crash(
        "coordinator", ["hosts"], 1, f"mindie-diagnostic-incident: {rec['incident_id']}\n", config=oc_cfg, invocation_token=tok
    ) == {"incident_id": rec["incident_id"], "logging_failed": False}

    # Post-success TimeoutError in OC receipt_store.finish preserves completed result!
    import remote_dev.mcp.outcome as rd_outcome
    import remote_dev.mcp.tools as rd_tools

    class _TimeoutFinishReceipts:
        def begin(self, *_a, **_k):
            return None

        def finish(self, *_a, **_k):
            raise TimeoutError("oc_finish_timeout")

    monkeypatch.setattr(rd_tools, "call_tool", lambda *_a, **_k: {"status": "succeeded", "stdout": "ok"})
    monkeypatch.setattr(rd_outcome, "operation_outcome", lambda *_a, **_k: "succeeded")
    rem_ok = mcp_server.call_remote_surface(
        "remote_bash", {"command": "true"}, config=oc_cfg, receipts=_TimeoutFinishReceipts()
    )
    assert rem_ok["isError"] is False
    assert rem_ok["structuredContent"]["receipt_unconfirmed"] is True
    assert "TimeoutError" in rem_ok["structuredContent"]["receipt_warning"]

    # Non-promoted inventory failure fails/exits 1 and TimeoutError clears in_progress & re-raises
    monkeypatch.setattr(component_update, "_candidate_plugin_is_newer", lambda *_a, **_k: False)
    monkeypatch.setattr(component_update, "prepare", lambda **_k: None)
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad inv")))
    assert component_update.main(["--config", str(oc_cfg), "--check"]) == 1

    status_file = data_home() / "oc-component-update-status.json"
    status_file.write_text(json.dumps({"checked_at": 1.0, "status": "in_progress"}), encoding="utf-8")
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("inv_timeout")))
    with pytest.raises(TimeoutError):
        component_update.check(config=oc_cfg)
    assert json.loads(status_file.read_text(encoding="utf-8"))["status"] == "failed"

    # AFTER atomic promotion (`oc.json` switched), post-promotion schema/stat TimeoutError or PermissionError preserves status="updated"!
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_k: {"mindie-diagnostics": {"version": "0.5.11"}})
    monkeypatch.setattr(component_update, "_candidate_plugin_is_newer", lambda *_a, **_k: True)
    monkeypatch.setattr(oc_setup, "ensure_runtime_python", lambda **_k: py)
    monkeypatch.setattr(
        component_update,
        "sync_store_schema",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("post_promote_schema_timeout")),
    )
    promoted = component_update.check(config=oc_cfg)
    assert promoted["status"] == "updated"
    assert promoted["needs_host_reload"] is True
    assert promoted["receipt_unconfirmed"] is True
    assert "TimeoutError" in promoted["receipt_warning"]

    comm_p = community_config_path(oc_cfg)
    comm_p.write_text("{}", encoding="utf-8")
    orig_stat = Path.stat
    orig_atomic = component_update.atomic_write_json
    for exc_cls in (TimeoutError, PermissionError):
        with monkeypatch.context() as mp:
            mp.setattr(component_update, "_candidate_plugin_is_newer", lambda *_a, **_k: False)
            mp.setattr(component_update, "sync_store_schema", lambda *_a, **_k: (True, None))
            mp.setattr(
                component_update,
                "prepare",
                lambda **_k: {
                    "python": py,
                    "components": {"runtime": {"mindie-diagnostics": {"version": "0.5.11", "revision": "1" * 40}}},
                    "installed": {"mindie-diagnostics": {"version": "0.5.11"}},
                },
            )
            promoted_flag = {"done": False}

            def _tracking_write(path, payload, *, _orig=orig_atomic, _flag=promoted_flag):
                _orig(path, payload)
                if Path(path) == oc_cfg:
                    _flag["done"] = True

            def _faulty_stat(self, *args, _e=exc_cls, _flag=promoted_flag, **kwargs):
                if _flag["done"] and self == comm_p:
                    raise _e("oc_post_promote_stat_fault")
                return orig_stat(self, *args, **kwargs)

            mp.setattr(component_update, "atomic_write_json", _tracking_write)
            mp.setattr(Path, "stat", _faulty_stat)
            res_r6 = component_update.check(config=oc_cfg, sync_feeds=True)
            assert res_r6["status"] == "updated"
            assert res_r6["needs_host_reload"] is True
            assert res_r6["receipt_unconfirmed"] is True
            assert exc_cls.__name__ in res_r6["receipt_warning"]
            saved_status = json.loads(status_file.read_text(encoding="utf-8"))
            assert saved_status["status"] == "updated"
            assert saved_status["receipt_unconfirmed"] is True
