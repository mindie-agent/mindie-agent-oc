"""Lifecycle, on-demand session lease, MCP bind, and consent-first `session.idle` tests (`test_oc_lifecycle.py`).

[Role]
Tests `bridge.py`, `admission.py`, `identity.py`, and `capture_stop.py` across
top-level vs. subagent/forked sessions, case-sensitive OpenCode session leases,
one-shot MCP tool binds, and consent-gated read-only SQLite `session.idle`
capture handoff.

[Invariants]
- Unconfigured/disabled community sharing, out-of-scope workspaces, and
  subagent/forked sessions never create `admission.sqlite3` or capture transcripts.
- Authorized in-scope `session.idle` auto-provisions a default lease (`not_before=0.0`)
  and spools only post-`enabled_at` public turns.
- One-shot binds in `mcp-binds.sqlite3` reject replays and forged identity args.

[Timeouts & Locks]
- Pure local SQLite WAL operations (`< 5s`).

[Side-Effect Policy]
- Mutates only `isolated_oc_env` temporary directories.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from admission import deactivate, ensure_default_lease, knowledge_lease, status_facts
from bridge import handle_pre_tool_use, handle_session_state, handle_stop
from identity import claim_bind, finish_bind
from paths import admission_path, config_path, engine_config_path, transcript_spool_dir


def _seed_opencode_v2_db(db_path: Path, session_id: str, directory: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_v2 ("
                "id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_message ("
                "id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_part ("
                "message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            db.execute(
                "INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, 1700000000000, 1700000001000, '{}')",
                (session_id, directory),
            )
            db.execute(
                "INSERT INTO session_message VALUES (?, ?, 0, 'user', 1700000000100, 1700000000100, ?)",
                (
                    "msg_u1",
                    session_id,
                    json.dumps({"time": {"created": 1700000000100}, "text": "How do I decode ACL 507015?"}),
                ),
            )
            db.execute(
                "INSERT INTO session_message VALUES (?, ?, 1, 'assistant', 1700000000500, 1700000000900, ?)",
                (
                    "msg_a1",
                    session_id,
                    json.dumps(
                        {
                            "time": {"created": 1700000000500, "completed": 1700000000900},
                            "content": [
                                {"type": "reasoning", "text": "internal chain of thought"},
                                {"type": "text", "text": "ACL error 507015 indicates a kernel execution failure."},
                            ],
                        }
                    ),
                ),
            )
    finally:
        db.close()


def test_passive_session_state_and_subagent_non_inheritance(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    state = handle_session_state({"sessionID": "ses_passive01", "directory": ws})
    assert state["activated"] is True
    assert state["eligible"] is True
    assert not admission_path().exists()

    # Subagent (`parentID`) and forked (`forkSessionID`) sessions are rejected without touching SQLite.
    sub = handle_session_state(
        {
            "sessionID": "ses_sub001",
            "parentID": "ses_passive01",
            "directory": ws,
        }
    )
    assert sub["activated"] is False
    assert sub["eligible"] is False
    assert sub["reason"] == "subagent_session"
    assert not admission_path().exists()

    forked = handle_session_state(
        {
            "sessionID": "ses_fork001",
            "forkSessionID": "ses_passive01",
            "directory": ws,
        }
    )
    assert forked["activated"] is False
    assert forked["eligible"] is False
    assert forked["reason"] == "forked_session"
    assert not admission_path().exists()


def test_on_demand_case_sensitive_lease_and_deactivation(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    lease = ensure_default_lease("ses_CaseSensitive_01", ws, not_before=0.0)
    assert lease["session"] == "ses_CaseSensitive_01"
    assert float(lease["activated_at"]) == 0.0
    assert knowledge_lease("ses_CaseSensitive_01") is not None
    # Case-sensitive OpenCode IDs must NOT match lowercased variants.
    assert knowledge_lease("ses_casesensitive_01") is None

    facts = status_facts("ses_CaseSensitive_01")
    assert facts["harness"] == "opencode"
    assert facts["activated"] is True

    deact = deactivate("ses_CaseSensitive_01")
    assert deact["status"] == "inactive"
    assert knowledge_lease("ses_CaseSensitive_01") is None
    assert status_facts("ses_CaseSensitive_01")["activated"] is False


def test_pre_tool_use_binds_mcp_call_and_rejects_replay(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    args = {"argv": ["decode-code", "507015"]}
    bound = handle_pre_tool_use(
        {
            "sessionID": "ses_toolbind01",
            "id": "call_abc123",
            "tool": "mindie-tools_debugger",
            "input": args,
            "directory": ws,
        }
    )
    assert bound["bound"] is True
    assert bound["surface"] == "tools"
    assert bound["local_tool"] == "debugger"

    claimed = claim_bind("tools", "debugger", args)
    assert claimed["session"] == "ses_toolbind01"
    finish_bind(claimed["bind_id"], True)

    # Replaying the consumed bind must fail immediately.
    with pytest.raises(ValueError, match="no active execute.before bind"):
        claim_bind("tools", "debugger", args)

    # Forged session identity keys inside model-controlled arguments must be rejected.
    with pytest.raises(ValueError, match="forbidden in tool arguments"):
        handle_pre_tool_use(
            {
                "sessionID": "ses_toolbind01",
                "id": "call_forged",
                "tool": "mindie-tools_mindie_status",
                "input": {"sessionID": "ses_victim"},
                "directory": ws,
            }
        )


def test_stop_checks_consent_before_sqlite_and_auto_leases_when_enabled(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = str(isolated_oc_env["workspace"])
    outside_ws = str(isolated_oc_env["root"] / "unscoped-dir")
    Path(outside_ws).mkdir(parents=True, exist_ok=True)
    db_file = isolated_oc_env["opencode_db"]
    _seed_opencode_v2_db(db_file, "ses_capture01", ws)

    # 1. With community sharing unconfigured, stop returns inert and creates ZERO admission.sqlite3 or spool!
    inert = handle_stop({"sessionID": "ses_capture01", "directory": ws, "opencodeDb": str(db_file)})
    assert inert == {"stage": "inert", "reason": "community_unconfigured"}
    assert not admission_path().exists()
    assert not (transcript_spool_dir() / "ses_capture01.jsonl").exists()

    config_path().parent.mkdir(parents=True, exist_ok=True)
    if not config_path().is_file():
        config_path().write_text(json.dumps({"harness": "opencode"}) + "\n", encoding="utf-8")
    if not engine_config_path().is_file():
        engine_config_path().write_text(json.dumps({"harness": "opencode"}) + "\n", encoding="utf-8")

    import setup as oc_setup

    # 2. Community sharing explicitly disabled -> still ZERO admission.sqlite3 created!
    oc_setup.configure_community(enabled=False, project_root=ws)
    disabled_res = handle_stop({"sessionID": "ses_capture01", "directory": ws, "opencodeDb": str(db_file)})
    assert disabled_res["stage"] == "inert"
    assert not admission_path().exists()

    # 3. Enable community sharing for `ws`, then check out-of-scope directory -> still ZERO admission.sqlite3!
    oc_setup.configure_community(
        enabled=True,
        fork="test-user/knowledge-vllm-ascend",
        account="test-user",
        project_root=ws,
    )
    out_of_scope = handle_stop({"sessionID": "ses_capture01", "directory": outside_ws, "opencodeDb": str(db_file)})
    assert out_of_scope == {"stage": "inert", "reason": "out_of_scope"}
    assert not admission_path().exists()

    captured_calls: list[tuple[str, dict]] = []
    import mindie_knowledge.loop.cli as loop_cli

    def _fake_capture_hook(engine_cfg: str, payload: dict) -> dict:
        captured_calls.append((engine_cfg, payload))
        return {"stage": "queued", "capture_id": "cap_001", "duplicate": False}

    monkeypatch.setattr(loop_cli, "capture_hook", _fake_capture_hook)

    # 4. Simulate an earlier zero-cwd MCP call creating a lease at an out-of-scope dir; Stop MUST re-bind to verified `ws`!
    ensure_default_lease("ses_capture01", outside_ws, not_before=0.0)
    assert Path(knowledge_lease("ses_capture01")["project_root"]).resolve() == Path(outside_ws).resolve()
    # Event without verified directory AND session missing from opencode.db -> missing_project_root (never guesses from stale lease!).
    no_dir_res = handle_stop({"sessionID": "ses_unknown_nodir", "opencodeDb": str(db_file)})
    assert no_dir_res == {"stage": "inert", "reason": "missing_project_root"}

    # In-scope authorized stop re-activates lease with verified `ws` (`not_before=0.0`), while pre-consent turns (`1700000000100`) are skipped!
    pre_auth_receipt = handle_stop(
        {
            "sessionID": "ses_capture01",
            "eventID": "evt_idle_pre",
            "opencodeDb": str(db_file),
        }
    )
    assert pre_auth_receipt == {"stage": "inert", "reason": "transcript_unavailable"}
    assert len(captured_calls) == 0
    auto_lease = knowledge_lease("ses_capture01")
    assert auto_lease is not None and float(auto_lease["activated_at"]) == 0.0
    assert Path(auto_lease["project_root"]).resolve() == Path(ws).resolve()

    # 5. Append a post-consent turn (`now_ms`) and verify only authorized public text is spooled (no reasoning).
    import time as _time

    now_ms = int(_time.time() * 1000) + 100
    with sqlite3.connect(db_file) as db:
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, 2, 'user', ?, ?, ?)",
            ("msg_u2", "ses_capture01", now_ms, now_ms, json.dumps({"time": {"created": now_ms}, "text": "Verify post-auth ACL 507015"})),
        )
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, 3, 'assistant', ?, ?, ?)",
            (
                "msg_a2",
                "ses_capture01",
                now_ms + 10,
                now_ms + 20,
                json.dumps(
                    {
                        "time": {"created": now_ms + 10, "completed": now_ms + 20},
                        "content": [
                            {"type": "reasoning", "text": "internal chain of thought"},
                            {"type": "text", "text": "ACL error 507015 indicates a kernel execution failure."},
                        ],
                    }
                ),
            ),
        )

    receipt = handle_stop(
        {
            "sessionID": "ses_capture01",
            "eventID": "evt_idle_01",
            "opencodeDb": str(db_file),
        }
    )
    assert receipt["stage"] == "queued"
    assert len(captured_calls) == 1
    spooled = (transcript_spool_dir() / "ses_capture01.jsonl").read_text(encoding="utf-8")
    assert "Verify post-auth ACL 507015" in spooled
    assert "ACL error 507015 indicates a kernel execution failure." in spooled
    assert "internal chain of thought" not in spooled
    assert "How do I decode ACL 507015?" not in spooled


def test_first_opted_in_stop_prepares_store_schema_without_prior_query(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First opted-in `Stop` turn before any `knowledge_query` must not drop with `store-not-ready`."""
    import setup as oc_setup
    import mindie_knowledge.loop.handoff as handoff_mod
    import time as _time

    ws = str(isolated_oc_env["workspace"])
    py = str(isolated_oc_env["python"])
    bin_dir = isolated_oc_env["root"] / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gl_sh = bin_dir / "gitleaks"
    gl_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gl_sh.chmod(0o755)
    gl_cmd = bin_dir / "gitleaks.cmd"
    gl_cmd.write_bytes(b"@exit /b 0\r\n")
    import os as _os

    monkeypatch.setenv("PATH", f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}")
    pathext = _os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    if ".CMD" not in pathext.upper():
        pathext = f"{pathext};.CMD"
    monkeypatch.setenv("PATHEXT", pathext)

    cfg = config_path()
    oc_setup.ensure_default_profile(config=cfg, workspace_root=ws, python_exe=py, prepare_store=False)
    eng_doc = json.loads(engine_config_path(cfg).read_text(encoding="utf-8"))
    store_db = handoff_mod._store_path(eng_doc)
    store_db.unlink(missing_ok=True)
    assert not store_db.exists()

    oc_setup.configure_community(
        config=cfg,
        enabled=True,
        fork="test-user/knowledge-vllm-ascend",
        account="test-user",
        project_root=ws,
    )
    # Remove uninitialized domain directory cleanly to test `capture_stop.py`'s `_ensure_capture_store_ready` guard directly!
    import shutil as _shutil

    _shutil.rmtree(store_db.parent.parent, ignore_errors=True)
    assert not store_db.exists()

    monkeypatch.setattr(
        handoff_mod,
        "request_wake",
        lambda *_a, **_kw: {"wake": "requested", "runtime": "starting", "reason": "waking"},
    )
    db_file = isolated_oc_env["opencode_db"]
    _seed_opencode_v2_db(db_file, "ses_first_turn_01", ws)
    now_ms = int(_time.time() * 1000) + 200
    with sqlite3.connect(db_file) as db:
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, 2, 'user', ?, ?, ?)",
            ("msg_ft_u", "ses_first_turn_01", now_ms, now_ms, json.dumps({"time": {"created": now_ms}, "text": "First opted-in turn"})),
        )
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, 3, 'assistant', ?, ?, ?)",
            ("msg_ft_a", "ses_first_turn_01", now_ms + 5, now_ms + 10, json.dumps({"time": {"created": now_ms + 5, "completed": now_ms + 10}, "text": "Resolved cleanly."})),
        )

    real_receipt = handle_stop(
        {"sessionID": "ses_first_turn_01", "workspace_root": ws, "eventID": "evt_first_turn", "opencodeDb": str(db_file)},
        config=cfg,
    )
    assert real_receipt["stage"] in {"accepted-local", "accepted-runtime"}, real_receipt
    assert real_receipt.get("reason") != "store-not-ready"
    assert store_db.is_file()

    # Also verify pending cold lease promotion in `ensure_default_lease` once `mindie_knowledge` becomes available!
    import admission as adm_mod

    with monkeypatch.context() as mp:
        mp.setattr(adm_mod, "_has_mindie_knowledge", lambda: False)
        pending_lease = ensure_default_lease("ses_pending_promo", ws, not_before=123.0, config=cfg)
        assert not pending_lease.get("capture_schema")
    promoted_lease = ensure_default_lease("ses_pending_promo", None, not_before=0.0, config=cfg)
    assert promoted_lease.get("capture_schema") is True
    assert float(promoted_lease["activated_at"]) == 123.0
    assert Path(promoted_lease["project_root"]).resolve() == Path(ws).resolve()
