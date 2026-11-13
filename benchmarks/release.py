"""[Role] Measure repeatable OpenCode v2 SQLite public-transcript spool & parse latency.
[Invariants] Assert reasoning/tool/synthetic sanitization before reporting latency; no productivity claim.
[Timeouts & Locks] Bounded in-tempdir SQLite iterations; zero network or background services.
[Side-Effect Policy] Temporary synthetic SQLite DB, spool file, and explicit result JSON only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "mindie-agent" / "scripts"))
import opencode_transcript  # noqa: E402

SESSION_ID = "ses_benchmark0123456789abcdef"

with tempfile.TemporaryDirectory(prefix="mindie-oc-bench-") as tmp:
    tmp_dir = Path(tmp)
    db_path = tmp_dir / "opencode.db"
    spool_dir = tmp_dir / "spool"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE session_v2 (
                id TEXT PRIMARY KEY,
                directory TEXT,
                parent_id TEXT,
                fork_session_id TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT
            );
            CREATE TABLE session_message (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                seq INTEGER,
                type TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT
            );
            CREATE TABLE session_part (
                message_id TEXT,
                session_id TEXT,
                ordinal INTEGER,
                type TEXT,
                time_created INTEGER,
                time_updated INTEGER,
                data TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, ?, ?, '{}')",
            (SESSION_ID, "/workspace/ascend-infer", 1730000000000, 1730000000000),
        )
        for i in range(100):
            msg_id = f"msg_{i:06d}"
            role = "user" if i % 2 == 0 else "assistant"
            ts = 1730000000000 + i * 100
            conn.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, SESSION_ID, i, role, ts, ts, json.dumps({"time": {"created": ts}})),
            )
            if role == "assistant":
                conn.execute(
                    "INSERT INTO session_part VALUES (?, ?, 0, 'reasoning', ?, ?, ?)",
                    (msg_id, SESSION_ID, ts, ts, json.dumps({"text": "HIDDEN_REASONING_SENTINEL"})),
                )
                conn.execute(
                    "INSERT INTO session_part VALUES (?, ?, 1, 'tool', ?, ?, ?)",
                    (msg_id, SESSION_ID, ts, ts, json.dumps({"tool": "bash", "output": "HIDDEN_TOOL_SENTINEL"})),
                )
            conn.execute(
                "INSERT INTO session_part VALUES (?, ?, 2, 'text', ?, ?, ?)",
                (msg_id, SESSION_ID, ts, ts, json.dumps({"text": f"Public Ascend ACLGraph observation {i}"})),
            )
        conn.commit()

    spool_file = opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_path, spool_dir=spool_dir)
    assert spool_file is not None and spool_file.is_file()
    raw_spool = spool_file.read_text(encoding="utf-8")
    assert "HIDDEN_REASONING_SENTINEL" not in raw_spool
    assert "HIDDEN_TOOL_SENTINEL" not in raw_spool

    iterations = 100
    started = time.perf_counter()
    result: dict = {}
    for _ in range(iterations):
        result = opencode_transcript.read_material(str(spool_file), 0, session_id=SESSION_ID)
    elapsed = time.perf_counter() - started
    assert result.get("status") == "ok" and "observation 99" in result.get("text", "")
    assert "HIDDEN_REASONING_SENTINEL" not in result["text"]
    assert "HIDDEN_TOOL_SENTINEL" not in result["text"]

Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(
    json.dumps(
        {
            "status": "pass",
            "metrics": [
                {
                    "name": "public_100_message_spool_parse_latency",
                    "unit": "us/op",
                    "value": elapsed * 1_000_000 / iterations,
                    "iterations": iterations,
                }
            ],
        }
    )
    + "\n",
    encoding="utf-8",
    newline="\n",
)
