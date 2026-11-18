"""Read-only SQLite spool projection, byte accounting, and release benchmark tests.

[Role]
Verifies `sync_session_spool` projects only public text from a read-only
`opencode.db`, rejects subagent/forked sessions, recovers torn tails, caps cold
spools at `64` files, and that `read_material` returns exact `[start, end)`
SHA-256 accounting. Also runs `benchmarks/release.py`.

[Invariants]
- `reasoning`/`tool`/`synthetic`/`ignored` sentinels never reach the spool.
- Incremental reads resume at a byte boundary and never re-read the prefix.

[Timeouts & Locks]
- Local SQLite spools and one bounded benchmark (`< 20s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import opencode_transcript

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_ID = "ses_transcript_a001"


def _seed(path: Path, *, stamp_ms: int = 1730000000000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        db.execute("INSERT INTO session_v2 VALUES (?, '/workspace', NULL, NULL, ?, ?, '{}')", (SESSION_ID, stamp_ms, stamp_ms))
        db.execute("INSERT INTO session_v2 VALUES ('ses_child', '/workspace', ?, NULL, ?, ?, '{}')", (SESSION_ID, stamp_ms, stamp_ms))
        for index in range(2):
            role = "user" if index == 0 else "assistant"
            message_id = f"msg_{index}"
            stamp = stamp_ms + index * 100
            db.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)", (message_id, SESSION_ID, index, role, stamp, stamp, json.dumps({"time": {"created": stamp}})))
            db.execute("INSERT INTO session_part VALUES (?, ?, 0, 'text', ?, ?, ?)", (message_id, SESSION_ID, stamp, stamp, json.dumps({"type": "text", "text": "PUBLIC_SENTINEL"})))
            if role == "assistant":
                for ordinal, (ptype, text, extra) in enumerate((("reasoning", "HIDDEN_REASONING_SENTINEL", {}), ("tool", "HIDDEN_TOOL_SENTINEL", {"tool": "bash"}), ("text", "HIDDEN_IGNORED_SENTINEL", {"ignored": True})), start=1):
                    db.execute("INSERT INTO session_part VALUES (?, ?, ?, ?, ?, ?, ?)", (message_id, SESSION_ID, ordinal, ptype, stamp, stamp, json.dumps({"type": ptype, "text": text, **extra})))
        db.execute("INSERT INTO session_message VALUES ('msg_syn', ?, 2, 'user', ?, ?, ?)", (SESSION_ID, stamp_ms + 300, stamp_ms + 300, json.dumps({"text": "HIDDEN_SYNTHETIC_SENTINEL"})))
        db.execute("INSERT INTO session_part VALUES ('msg_syn', ?, 0, 'text', ?, ?, ?)", (SESSION_ID, stamp_ms + 300, stamp_ms + 300, json.dumps({"type": "text", "text": "HIDDEN_SYNTHETIC_SENTINEL", "synthetic": True})))


def test_sqlite_v2_spool_and_public_transcript_filtering(isolated_oc_env: dict[str, Path]) -> None:
    db_file, spool_dir = isolated_oc_env["opencode_db"], isolated_oc_env["root"] / "spool"
    _seed(db_file)

    with pytest.raises(ValueError, match="subagent or forked"):
        opencode_transcript.sync_session_spool("ses_child", db_path=db_file, spool_dir=spool_dir)

    before = db_file.stat().st_mtime_ns
    spool = opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert spool is not None and spool.is_file()
    assert db_file.stat().st_mtime_ns == before
    raw = spool.read_text(encoding="utf-8")
    for sentinel in ("HIDDEN_REASONING_SENTINEL", "HIDDEN_TOOL_SENTINEL", "HIDDEN_IGNORED_SENTINEL", "HIDDEN_SYNTHETIC_SENTINEL"):
        assert sentinel not in raw
    first = opencode_transcript.identify(spool)
    assert first is not None and first.size == len(raw.encode("utf-8"))

    material = opencode_transcript.read_material(spool, 0, session_id=SESSION_ID)
    assert material["status"] == "ok" and material["records"] == 2
    assert "[user]\nPUBLIC_SENTINEL" in material["text"] and "[assistant]\nPUBLIC_SENTINEL" in material["text"]
    payload = spool.read_bytes()[material["start"] : material["end"]]
    assert material["digest"] == hashlib.sha256(payload).hexdigest()

    with sqlite3.connect(db_file) as db:
        db.execute("INSERT INTO session_message VALUES ('msg_3', ?, 3, 'user', 1730000004000, 1730000004000, ?)", (SESSION_ID, json.dumps({"time": {"created": 1730000004000}, "text": "SECOND_TURN_SENTINEL"})))
    opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert opencode_transcript.identify(spool).size > first.size
    delta = opencode_transcript.read_material(spool, material["end"], session_id=SESSION_ID, expected=first)
    assert delta["status"] == "ok" and delta["records"] == 1 and "SECOND_TURN_SENTINEL" in delta["text"]
    assert delta["digest"] == hashlib.sha256(spool.read_bytes()[delta["start"] : delta["end"]]).hexdigest()
    assert opencode_transcript.read_increment is opencode_transcript.read_material


def test_spool_torn_tail_cold_cap_and_release_benchmark(isolated_oc_env: dict[str, Path]) -> None:
    db_file, spool_dir = isolated_oc_env["opencode_db"], isolated_oc_env["root"] / "spool"
    _seed(db_file)
    spool = opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert spool is not None
    with spool.open("ab") as stream:
        stream.write(b'{"schema":"mindie-oc-spool/1","seq":')
    assert not spool.read_bytes().endswith(b"\n")
    opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert spool.read_bytes().endswith(b"\n")
    assert all(json.loads(line)["seq"] >= 0 for line in spool.read_text(encoding="utf-8").splitlines())

    for index in range(70):
        cold = spool_dir / f"ses_cold_{index:03d}.jsonl"
        cold.write_text("{}\n", encoding="utf-8")
        os.utime(cold, (1_600_000_000 + index, 1_600_000_000 + index))
    opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert len(list(spool_dir.glob("*.jsonl"))) <= 64

    result_path = isolated_oc_env["root"] / "bench.json"
    env = dict(os.environ, MINDIE_BENCH_RESULT_PATH=str(result_path))
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "benchmarks" / "release.py")], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "pass" and payload["metrics"][0]["iterations"] == 50
