"""Read-only SQLite spool projection, `scan_until`, `unserialize`, and lock tests.

[Role]
Verifies `sync_session_spool` projects only public text from a read-only
`opencode.db`, rejects subagent/forked sessions, recovers torn tails, caps cold
spools at `64` files under interprocess `.prune.lock` without deleting active
locked spools, supports `FileIdentity.unserialize()`, and honors `scan_until`
in `read_material` / `read_increment`. Also runs `benchmarks/release.py`.

[Invariants]
- `reasoning`/`tool`/`synthetic`/`ignored` sentinels never reach the spool.
- Incremental reads resume at a byte boundary, clamp at `scan_until`, and never
  re-read the prefix.
- Active session spools held by another process are never pruned mid-sync.

[Timeouts & Locks]
- Local SQLite spools, one child lock holder, and one bounded benchmark (`< 20s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

from dataclasses import asdict
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
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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

    assert opencode_transcript.FileIdentity.unserialize(first.serialize()) == first
    assert opencode_transcript.FileIdentity.unserialize(asdict(first)) == first
    cursor_reloaded = opencode_transcript.FileIdentity.unserialize(first.serialize(), str(spool))
    assert cursor_reloaded is not None and cursor_reloaded.anchor_digest == first.anchor_digest
    legacy_json = json.dumps({"dev": first.dev, "ino": first.ino, "anchor_len": first.anchor_len, "anchor_digest": first.anchor_digest})
    legacy_reloaded = opencode_transcript.FileIdentity.unserialize(legacy_json, str(spool))
    assert legacy_reloaded is not None and legacy_reloaded.path == first.path
    assert opencode_transcript.FileIdentity.unserialize("") is None
    assert opencode_transcript.FileIdentity.unserialize("{}", str(spool)) is None

    empty_spool = spool_dir / "ses_empty_a001.jsonl"
    empty_spool.write_bytes(b"")
    empty_id = opencode_transcript.identify(empty_spool)
    assert empty_id is not None and empty_id.size == 0 and empty_id.anchor_len == 0
    empty_reloaded = opencode_transcript.FileIdentity.unserialize(empty_id.serialize(), str(empty_spool))
    assert empty_reloaded is not None and empty_reloaded.size == 0 and empty_reloaded.anchor_len == 0
    empty_read = opencode_transcript.read_material(empty_spool, 0, session_id="ses_empty_a001", expected=empty_reloaded)
    assert empty_read["status"] == "unchanged" and empty_read["more"] is False and empty_read["end"] == 0
    empty_spool.unlink()

    material = opencode_transcript.read_material(spool, 0, session_id=SESSION_ID)
    assert material["status"] == "ok" and material["records"] == 2
    assert "[user]\nPUBLIC_SENTINEL" in material["text"] and "[assistant]\nPUBLIC_SENTINEL" in material["text"]
    payload = spool.read_bytes()[material["start"] : material["end"]]
    assert material["digest"] == hashlib.sha256(payload).hexdigest()

    with sqlite3.connect(db_file) as db:
        db.execute("INSERT INTO session_message VALUES ('msg_3', ?, 3, 'user', 1730000004000, 1730000004000, ?)", (SESSION_ID, json.dumps({"time": {"created": 1730000004000}, "text": "SECOND_TURN_SENTINEL"})))
    opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
    assert opencode_transcript.identify(spool).size > first.size

    clamped = opencode_transcript.read_material(spool, 0, scan_until=material["end"], session_id=SESSION_ID, expected=cursor_reloaded)
    assert clamped["status"] == "ok" and clamped["end"] == material["end"] and clamped["more"] is False
    assert "SECOND_TURN_SENTINEL" not in clamped["text"]

    clamped_mid = opencode_transcript.read_material(spool, 0, scan_until=material["end"] + 10, session_id=SESSION_ID, expected=cursor_reloaded)
    assert clamped_mid["status"] == "ok" and clamped_mid["end"] == material["end"] and clamped_mid["partial"] is True and clamped_mid["more"] is False

    mid_cut = opencode_transcript.read_increment(spool, start_offset=material["end"], scan_until=material["end"] + 10, session_id=SESSION_ID)
    assert mid_cut["status"] == "unchanged" and mid_cut["end"] == material["end"] and mid_cut["partial"] is True and mid_cut["more"] is False

    with pytest.raises(ValueError, match="scan_until"):
        opencode_transcript.read_material(spool, 10, scan_until=5)

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

    os.utime(spool, (1_500_000_000, 1_500_000_000))
    for index in range(70):
        cold = spool_dir / f"ses_cold_{index:03d}.jsonl"
        cold.write_text("{}\n", encoding="utf-8")
        os.utime(cold, (1_600_000_000 + index, 1_600_000_000 + index))

    with sqlite3.connect(db_file) as db:
        db.execute("INSERT INTO session_v2 VALUES ('prune', '/workspace', NULL, NULL, 1730000000000, 1730000000000, '{}')")
        db.execute("INSERT INTO session_message VALUES ('msg_prune', 'prune', 0, 'user', 1730000000000, 1730000000000, ?)", (json.dumps({"time": {"created": 1730000000000}, "text": "PRUNE_SESSION_OK"}),))

    holder = isolated_oc_env["root"] / "hold_lock.py"
    holder.write_text(
        f"import pathlib, sys, time\nsys.path.insert(0, {str(REPO_ROOT / 'plugins' / 'mindie-agent' / 'scripts')!r})\n"
        f"import opencode_transcript as t\n"
        f"with t._session_lock(pathlib.Path({str(spool_dir / '.ses_cold_000.lock')!r}), deadline=time.monotonic() + 5.0):\n"
        f"    sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); sys.stdin.readline()\n",
        encoding="utf-8",
    )
    child = subprocess.Popen([sys.executable, "-u", str(holder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=CREATE_NO_WINDOW)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "LOCKED"
        refreshed = opencode_transcript.sync_session_spool(SESSION_ID, db_path=db_file, spool_dir=spool_dir)
        assert refreshed == spool and spool.is_file()
        assert (spool_dir / "_spool_gc.lock").is_file() and not (spool_dir / ".prune.lock").exists()
        prune_spool = opencode_transcript.sync_session_spool("prune", db_path=db_file, spool_dir=spool_dir)
        assert prune_spool is not None and prune_spool.is_file()
        assert spool.is_file() and (spool_dir / ".prune.lock").is_file()
        assert (spool_dir / "ses_cold_000.jsonl").is_file()
        assert len(list(spool_dir.glob("*.jsonl"))) <= 64
    finally:
        if child.stdin is not None:
            try:
                child.stdin.write("\n")
                child.stdin.flush()
            except OSError:
                pass
        child.wait(timeout=5)

    result_path = isolated_oc_env["root"] / "bench.json"
    env = dict(os.environ, MINDIE_BENCH_RESULT_PATH=str(result_path))
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "benchmarks" / "release.py")], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "pass" and payload["metrics"][0]["iterations"] == 50
