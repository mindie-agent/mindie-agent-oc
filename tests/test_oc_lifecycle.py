"""Consent-first Stop capture, lease boundary, revocation, and idle dedup tests.

[Role]
Verifies that disabled/out-of-scope/subagent Stop events spawn zero subprocesses
and create zero SQLite files; authorized turns capture only post-consent public
text; explicit `Admission` revocation is preserved; and coalesced idle events
spawn at most one capture helper.

[Invariants]
- `auth_boundary = max(enabled_at, activated_at)` filters pre-consent turns.
- Deterministic `eventID` is forwarded as `turn_id`.
- Node `event.subscribe` rejects parent/fork sessions and deduplicates within `250ms`.

[Timeouts & Locks]
- Local SQLite WAL and one bounded Node driver (`< 15s`).

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

import pytest

import mindie_launch
import setup as oc_setup

REPO_ROOT = Path(__file__).resolve().parent.parent


def _seed_db(path: Path, session_id: str, directory: str, *, stamp_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        db.execute("INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, ?, ?, '{}')", (session_id, directory, stamp_ms, stamp_ms))
        db.execute("INSERT INTO session_message VALUES (?, ?, 0, 'user', ?, ?, ?)", ("m1", session_id, stamp_ms, stamp_ms, json.dumps({"time": {"created": stamp_ms}, "text": "PRE_CONSENT_SENTINEL"})))


def _append_turn(path: Path, session_id: str, *, stamp_ms: int) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO session_message VALUES (?, ?, 2, 'user', ?, ?, ?)", ("m3", session_id, stamp_ms, stamp_ms, json.dumps({"time": {"created": stamp_ms}, "text": "POST_CONSENT_SENTINEL"})))
        db.execute("INSERT INTO session_message VALUES (?, ?, 3, 'assistant', ?, ?, ?)", ("m4", session_id, stamp_ms + 10, stamp_ms + 20, json.dumps({"time": {"created": stamp_ms + 10, "completed": stamp_ms + 20}, "content": [{"type": "reasoning", "text": "HIDDEN_REASONING_SENTINEL"}, {"type": "text", "text": "POST_CONSENT_SENTINEL"}]})))
        db.execute("INSERT INTO session_part VALUES (?, ?, 0, 'text', ?, ?, ?)", ("m4", session_id, stamp_ms + 10, stamp_ms + 20, json.dumps({"type": "text", "text": "PART_SENTINEL"})))


def _configure(isolated_oc_env: dict[str, Path], workspace: str, *, choice: str = "contribute") -> None:
    cfg = isolated_oc_env["mindie_cfg"]
    consent = cfg / "mindie-consent.json"
    consent.write_text(json.dumps({"schema": "mindie-consent/1", "choice": choice, "updated_at": 1}), encoding="utf-8")
    (cfg / "mindie-community.json").write_text(
        json.dumps({"schema": "mindie-community-config/1", "enabled": True, "generation": "gen_test", "enabled_at": time.time() - 5, "project_roots": [workspace], "consent_config": str(consent)}),
        encoding="utf-8",
    )


def test_stop_consent_gate_and_capture_boundary(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert oc_setup.main(["init"]) == 0
    capsys.readouterr()
    workspace = str(isolated_oc_env["workspace"])
    outside = str(isolated_oc_env["root"] / "outside")
    Path(outside).mkdir()
    db_file = isolated_oc_env["opencode_db"]
    now_ms = int(time.time() * 1000)
    _seed_db(db_file, "ses_capture_a001", workspace, stamp_ms=now_ms - 60000)

    event = {"sessionID": "ses_capture_a001", "directory": workspace, "eventID": "evt_pre", "opencodeDb": str(db_file)}
    admission = mindie_launch.state_dir() / "admission.sqlite3"
    spool = mindie_launch.state_dir() / "transcripts" / "ses_capture_a001.jsonl"

    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps(event)]) == 0
    assert json.loads(capsys.readouterr().out.strip() or "{}") == {}
    assert not admission.exists() and not spool.exists()

    _configure(isolated_oc_env, workspace)
    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "directory": outside})]) == 0
    capsys.readouterr()
    assert not admission.exists() and not spool.exists()

    _append_turn(db_file, "ses_capture_a001", stamp_ms=now_ms + 1000)
    captured: list[dict] = []
    import mindie_knowledge.loop.cli as loop_cli

    monkeypatch.setattr(mindie_launch, "_prepare_schema", lambda *_: None)
    monkeypatch.setattr(loop_cli, "capture_hook", lambda *_a, **kwargs: captured.append(kwargs) or {"stage": "accepted-local", "capture_id": "cap_1"})
    monkeypatch.setenv("_MINDIE_OC_REEXEC", "1")
    receipt = mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "eventID": "evt_post"})])
    capsys.readouterr()
    assert receipt == 0 and len(captured) == 1
    forwarded = captured[0]
    assert forwarded.get("timeout") == 6.0
    body = (spool).read_text(encoding="utf-8")
    assert "POST_CONSENT_SENTINEL" in body and "PART_SENTINEL" in body
    assert "PRE_CONSENT_SENTINEL" not in body and "HIDDEN_REASONING_SENTINEL" not in body

    from mindie_knowledge.loop.activation import Admission

    store = Admission(admission)
    lease = store.active_lease("ses_capture_a001")
    assert lease is not None and float(lease["activated_at"]) == 0.0
    store.deactivate("ses_capture_a001")
    captured.clear()
    replay = mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "eventID": "evt_revoked"})])
    capsys.readouterr()
    assert replay == 0 and captured == []


def test_admission_revocation_and_idle_deduplication(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    node = shutil.which("node")
    assert node is not None
    if os.name == "nt":
        pytest.skip("spy executable requires a POSIX shebang")
    workspace = str(isolated_oc_env["workspace"])
    _configure(isolated_oc_env, workspace, choice="local-only")
    log = isolated_oc_env["root"] / "capture_spawns.jsonl"
    spy = isolated_oc_env["root"] / "capture_spy.py"
    spy.write_text(f"#!{isolated_oc_env['python']}\nimport pathlib, sys\nwith pathlib.Path({str(log)!r}).open('a', encoding='utf-8') as f:\n    f.write(sys.argv[-1] + '\\n')\n", encoding="utf-8")
    spy.chmod(0o755)
    driver = isolated_oc_env["root"] / "event_driver.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import path from "node:path";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").as_uri())};
const events = [];
let resolveEvent = null;
const push = (value) => {{ if (resolveEvent) {{ const r = resolveEvent; resolveEvent = null; r({{ value, done: false }}); }} else events.push(value); }};
const sessions = {{
  ses_main_a001: {{ id: "ses_main_a001", directory: process.env.TEST_WORKSPACE }},
  ses_sub_b002: {{ id: "ses_sub_b002", directory: process.env.TEST_WORKSPACE, parentID: "ses_main_a001" }},
  ses_out_c003: {{ id: "ses_out_c003", directory: process.env.TEST_OUTSIDE }},
}};
const ctx = {{
  location: {{ directory: process.env.TEST_WORKSPACE }},
  mcp: {{ async transform(fn) {{}} }},
  session: {{ hook() {{}}, async get({{ sessionID }}) {{ return sessions[sessionID] || null; }} }},
  tool: {{ hook() {{}} }},
  shell: {{ hook() {{}} }},
  event: {{ async subscribe() {{ return {{ [Symbol.asyncIterator]() {{ return {{ next: () => events.length ? Promise.resolve({{ value: events.shift(), done: false }}) : new Promise((r) => {{ resolveEvent = r; }}) }}; }} }}; }} }},
}};
const cleanup = await plugin.setup(ctx);
push({{ type: "session.idle", id: "evt_out", data: {{ sessionID: "ses_out_c003" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_sub_b002" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_main_a001" }} }});
await new Promise((resolve) => setTimeout(resolve, 2000));
await cleanup();
""",
        encoding="utf-8",
    )
    outside = str(isolated_oc_env["root"] / "outside-scope"); Path(outside).mkdir()
    env = dict(os.environ, TEST_WORKSPACE=workspace, TEST_OUTSIDE=outside, MINDIE_OC_ALLOW_BG_UPDATE="0", MINDIE_OC_PYTHON=str(spy))
    result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.is_file() else []
    assert len(lines) == 1, lines
    payload = json.loads(lines[0])
    assert payload["sessionID"] == "ses_main_a001" and payload["eventID"] == "evt_same"
