"""Lifecycle, manual `/mindie-agent` lease, MCP bind, and `session.idle` tests (`test_oc_lifecycle.py`).

[Role]
Tests `bridge.py`, `admission.py`, `identity.py`, and `capture_stop.py` across
ordinary prompts, subagent/forked sessions, `/mindie-agent` gestures, one-shot
MCP tool binds, and read-only SQLite `session.idle` capture handoff.

[Invariants]
- Ordinary prompts and subagent/forked sessions never create `admission.sqlite3`
  or capture transcripts.
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

from admission import knowledge_lease
from bridge import handle_gesture, handle_pre_tool_use, handle_session_state, handle_stop
from identity import claim_bind, finish_bind
from paths import admission_path, community_config_path, config_path, engine_config_path, transcript_spool_dir


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


def test_passive_ordinary_prompt_and_subagent_non_inheritance(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    res = handle_gesture({"sessionID": "ses_passive01", "directory": ws, "prompt": "Explain RMSNorm on Ascend"})
    assert res == {"matched": False}
    assert not admission_path().exists()

    state = handle_session_state({"sessionID": "ses_passive01", "directory": ws})
    assert state["activated"] is False

    # Subagent (`parentID`) and forked (`forkSessionID`) sessions cannot activate a lease.
    sub = handle_gesture(
        {
            "sessionID": "ses_sub001",
            "parentID": "ses_passive01",
            "directory": ws,
            "prompt": "/mindie-agent optimize matmul",
        }
    )
    assert sub["matched"] is True
    assert sub["activated"] is False
    assert sub["reason"] == "subagent_session"
    assert not admission_path().exists()

    forked = handle_gesture(
        {
            "sessionID": "ses_fork001",
            "forkSessionID": "ses_passive01",
            "directory": ws,
            "prompt": "$mindie-agent",
        }
    )
    assert forked["matched"] is True
    assert forked["activated"] is False
    assert forked["reason"] == "forked_session"


def test_explicit_mindie_agent_gesture_activates_and_deactivates_lease(isolated_oc_env: dict[str, Path]) -> None:
    ws = str(isolated_oc_env["workspace"])
    act = handle_gesture(
        {
            "sessionID": "ses_CaseSensitive_01",
            "directory": ws,
            "prompt": "/mindie-agent analyze Ascend A3 HCCL topology",
        }
    )
    assert act["matched"] is True
    assert act["activated"] is True
    assert act["action"] == "activate"
    assert act["task_prompt"] == "analyze Ascend A3 HCCL topology"
    assert knowledge_lease("ses_CaseSensitive_01") is not None
    # Case-sensitive OpenCode IDs must NOT match lowercased variants.
    assert knowledge_lease("ses_casesensitive_01") is None

    status = handle_gesture(
        {
            "sessionID": "ses_CaseSensitive_01",
            "directory": ws,
            "prompt": "/mindie-agent status",
        }
    )
    assert status["action"] == "status"
    assert status["facts"]["harness"] == "opencode"
    assert status["facts"]["activated"] is True

    deact = handle_gesture(
        {
            "sessionID": "ses_CaseSensitive_01",
            "directory": ws,
            "prompt": "/mindie-agent deactivate",
        }
    )
    assert deact["action"] == "deactivate"
    assert deact["activated"] is False
    assert knowledge_lease("ses_CaseSensitive_01") is None


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


def test_stop_requires_manual_lease_and_spools_public_transcript(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = str(isolated_oc_env["workspace"])
    db_file = isolated_oc_env["opencode_db"]
    _seed_opencode_v2_db(db_file, "ses_capture01", ws)

    # 1. Without manual lease, stop returns inert and creates no transcript spool.
    inert = handle_stop({"sessionID": "ses_capture01", "opencodeDb": str(db_file)})
    assert inert == {"stage": "inert", "reason": "no_manual_lease"}
    assert not (transcript_spool_dir() / "ses_capture01.jsonl").exists()

    # 2. Activate lease and seed community consent + engine config, then verify spool + handoff.
    handle_gesture({"sessionID": "ses_capture01", "directory": ws, "prompt": "/mindie-agent"})
    config_path().parent.mkdir(parents=True, exist_ok=True)
    if not config_path().is_file():
        config_path().write_text(json.dumps({"harness": "opencode"}) + "\n", encoding="utf-8")
    if not engine_config_path().is_file():
        engine_config_path().write_text(json.dumps({"harness": "opencode"}) + "\n", encoding="utf-8")

    import setup as oc_setup

    oc_setup.configure_community(
        enabled=True,
        fork="test-user/knowledge-vllm-ascend",
        account="test-user",
        project_root=ws,
    )

    captured_calls: list[tuple[str, dict]] = []
    import mindie_knowledge.loop.cli as loop_cli

    def _fake_capture_hook(engine_cfg: str, payload: dict) -> dict:
        captured_calls.append((engine_cfg, payload))
        return {"stage": "queued", "capture_id": "cap_001", "duplicate": False}

    monkeypatch.setattr(loop_cli, "capture_hook", _fake_capture_hook)

    receipt = handle_stop(
        {
            "sessionID": "ses_capture01",
            "eventID": "evt_idle_01",
            "opencodeDb": str(db_file),
        }
    )
    assert receipt["stage"] == "queued"
    assert len(captured_calls) == 1
    _, forwarded = captured_calls[0]
    assert forwarded["harness"] == "opencode"
    assert forwarded["session_id"] == "ses_capture01"
    spool_file = Path(forwarded["transcript_path"])
    assert spool_file.is_file()
    spool_text = spool_file.read_text(encoding="utf-8")
    assert "ACL error 507015" in spool_text
    assert "internal chain of thought" not in spool_text
