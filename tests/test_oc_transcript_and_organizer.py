"""SQLite v2 transcript spooling and organizer worker tests (`test_oc_transcript_and_organizer.py`).

[Role]
Verifies `opencode_transcript.py` incremental read-only projection from
OpenCode's `opencode.db` (`session_v2`, `session_message`, `session_part`) and
`organizer.py` single-call loopback execution + `outcome_unknown` non-retry policy.

[Invariants]
- Appending new rows to `opencode.db` preserves the immutable 512-byte anchor of
  `<state_dir>/transcripts/<sessionID>.jsonl` (`identify(...).same_as(...)` stays True).
- `reasoning`, `tool`, and `synthetic` parts never enter captured public material.
- `organizer.py` never retries after transmitting a request (`model_calls <= 1`).

[Timeouts & Locks]
- Local SQLite and `127.0.0.1:0` loopback HTTP server (`< 5s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
import threading
import pytest

from mindie_knowledge.materials import summarizer

import opencode_transcript
import organizer


def test_sqlite_v2_spool_and_public_transcript_filtering(isolated_oc_env: dict[str, Path]) -> None:
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
            # Main session + subagent session.
            db.execute(
                "INSERT INTO session_v2 VALUES ('ses_main_tx', ?, NULL, NULL, 1710000000000, 1710000000000, '{}')",
                (ws,),
            )
            db.execute(
                "INSERT INTO session_v2 VALUES ('ses_child_tx', ?, 'ses_main_tx', NULL, 1710000000000, 1710000000000, '{}')",
                (ws,),
            )
            # Turn 1: user + synthetic reminder + assistant with reasoning/tool/text.
            db.execute(
                "INSERT INTO session_message VALUES ('m1', 'ses_main_tx', 0, 'user', 1710000001000, 1710000001000, ?)",
                (json.dumps({"time": {"created": 1710000001000}, "text": "<USER_REQUEST>Tune A3 AllReduce</USER_REQUEST>"}),),
            )
            db.execute(
                "INSERT INTO session_message VALUES ('m2', 'ses_main_tx', 1, 'user', 1710000001500, 1710000001500, ?)",
                (json.dumps({"time": {"created": 1710000001500}, "synthetic": True, "text": "hidden system synthetic"}),),
            )
            db.execute(
                "INSERT INTO session_message VALUES ('m3', 'ses_main_tx', 2, 'assistant', 1710000002000, 1710000003000, ?)",
                (
                    json.dumps(
                        {
                            "time": {"created": 1710000002000, "completed": 1710000003000},
                            "content": [
                                {"type": "reasoning", "text": "secret reasoning trace"},
                                {"type": "tool", "tool": "bash", "state": {"output": "secret tool output"}},
                                {"type": "text", "text": "Set HCCL_BUFFSIZE=512 on Ascend A3."},
                            ],
                        }
                    ),
                ),
            )
    finally:
        db.close()

    # Subagent session must be rejected by sync_session_spool.
    with pytest.raises(ValueError, match="subagent or forked"):
        opencode_transcript.sync_session_spool("ses_child_tx", db_path=db_file, spool_dir=spool_dir)

    spool_file = opencode_transcript.sync_session_spool("ses_main_tx", db_path=db_file, spool_dir=spool_dir)
    assert spool_file is not None and spool_file.is_file()
    first_id = opencode_transcript.identify(spool_file)

    mat1 = opencode_transcript.read_material(spool_file, 0, session_id="ses_main_tx")
    assert mat1["status"] == "ok"
    assert mat1["records"] == 2
    assert "[user]\nTune A3 AllReduce" in mat1["text"]
    assert "[assistant]\nSet HCCL_BUFFSIZE=512 on Ascend A3." in mat1["text"]
    assert "secret" not in mat1["text"]
    assert "synthetic" not in mat1["text"]
    assert "secret" not in spool_file.read_text(encoding="utf-8")

    # Append a second turn in SQLite (which mutates SQLite's header change counter!)
    # and verify the JSONL spool preserves its FileIdentity prefix and supports [start, end) slicing.
    db = sqlite3.connect(db_file)
    try:
        with db:
            db.execute(
                "INSERT INTO session_message VALUES ('m4', 'ses_main_tx', 3, 'user', 1710000004000, 1710000004000, ?)",
                (json.dumps({"time": {"created": 1710000004000}, "text": "Verify latency gain"}),),
            )
            db.execute(
                "INSERT INTO session_message VALUES ('m5', 'ses_main_tx', 4, 'assistant', 1710000005000, 1710000006000, '{}')",
            )
            db.execute(
                "INSERT INTO session_part VALUES ('m5', 'ses_main_tx', 0, 'text', 1710000005500, 1710000006000, ?)",
                (json.dumps({"type": "text", "text": "Latency dropped from 4.2ms to 2.8ms."}),),
            )
    finally:
        db.close()

    opencode_transcript.sync_session_spool("ses_main_tx", db_path=db_file, spool_dir=spool_dir)
    second_id = opencode_transcript.identify(spool_file)
    assert first_id is not None and second_id is not None
    assert second_id.size > first_id.size

    mat_delta = opencode_transcript.read_material(
        spool_file,
        mat1["end"],
        session_id="ses_main_tx",
        expected=first_id,
    )
    assert mat_delta["status"] == "ok"
    assert mat_delta["records"] == 2
    assert "Verify latency gain" in mat_delta["text"]
    assert "Latency dropped from 4.2ms to 2.8ms." in mat_delta["text"]


def test_organizer_loopback_and_outcome_unknown_no_retry(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_id = "b" * 64
    task_id = "a" * 64
    calls: list[dict] = []
    mode = {"behavior": "ok"}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append({"auth": self.headers.get("Authorization"), "body": body})
            if mode["behavior"] == "502":
                self.send_response(502)
                self.end_headers()
                self.wfile.write(b'{"error":"upstream timeout"}')
                return
            reply = {
                "result": json.dumps(
                    {
                        "blocks": [
                            {
                                "block_id": block_id,
                                "title": "HCCL AllReduce tuning",
                                "summary": "Observed A3 AllReduce latency drop after setting HCCL_BUFFSIZE=512.",
                            }
                        ],
                        "navigation": {
                            "title": "A3 HCCL AllReduce tuning",
                            "summary": "Tuned A3 HCCL AllReduce buffer size to 512 MB.",
                        },
                    }
                ),
                "usage": {"input_tokens": 120, "output_tokens": 45},
            }
            encoded = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("MINDIE_SUMMARY_ENDPOINT", f"http://127.0.0.1:{port}/v1/summary")
        monkeypatch.setenv("MINDIE_SUMMARY_TOKEN", "sec_tok_123")
        monkeypatch.setenv("MINDIE_SUMMARY_MODEL", "deepseek/deepseek-flash#max")

        worker_id = organizer.identity("deepseek/deepseek-flash#max")
        summarizer.validate_identity(worker_id)

        req = summarizer.make_request(
            task_id=task_id,
            body_version="v1",
            prior_navigation=None,
            blocks=[
                {
                    "block_id": block_id,
                    "text": "[user]\nTune A3 AllReduce\n\n[assistant]\nSet HCCL_BUFFSIZE=512 on Ascend A3.",
                    "source_range": {"start": 0, "end": 80},
                }
            ],
            identity=worker_id,
        )

        # 1. Valid loopback response -> status == "returned", single call.
        out_ok = organizer.execute_request(req)
        assert out_ok["status"] == "returned"
        assert out_ok["model_calls"] == 1
        assert len(calls) == 1
        assert calls[0]["auth"] == "Bearer sec_tok_123"

        # 2. Upstream 502 after transmission -> status == "outcome_unknown", NEVER retried!
        calls.clear()
        mode["behavior"] = "502"
        out_unk = organizer.execute_request(req)
        assert out_unk["status"] == "outcome_unknown"
        assert out_unk["model_calls"] == 1
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()

    # 3. Connection refused before transmission -> status == "failed", billing_status == "not_called".
    out_refused = organizer.execute_request(req)
    assert out_refused["status"] == "failed"
    assert out_refused["model_calls"] == 0
    assert out_refused["billing_status"] == "not_called"
