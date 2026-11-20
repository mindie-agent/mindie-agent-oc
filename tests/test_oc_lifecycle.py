"""Consent-first Stop capture, lease boundary, revocation, and terminal-only event tests.

[Role]
Verifies that disabled/out-of-scope/subagent Stop events spawn zero subprocesses
and create zero SQLite files; authorized turns capture only post-consent public
text via `run_stop_capture` (`identity_kind="turn"` vs `"notification"`);
explicit `Admission` revocation is never revived; and non-idle events
(`session.execution.started`/`running`/`progress`) spawn zero capture helpers.

[Invariants]
- `auth_boundary = max(enabled_at, activated_at)` filters pre-consent turns.
- Deterministic `eventID` uses `identity_kind="turn"`; absent turn ID uses `"notification"`.
- Node `event.subscribe` ignores non-terminal execution events, rejects parent/fork
  sessions, and deduplicates within `250ms`.

[Timeouts & Locks]
- Local SQLite WAL and one bounded cross-platform Node driver (`< 15s`).

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
    consent.write_text(
        json.dumps({"schema": "mindie-consent/1", "choice": choice, "choice_revision": "0" * 32, "updated_at": 1}),
        encoding="utf-8",
    )
    (cfg / "mindie-community.json").write_text(
        json.dumps(
            {
                "schema": "mindie-community-config/1",
                "enabled": True,
                "local_only": choice == "local-only",
                "repository": "mindie-agent/knowledge-vllm-ascend" if choice == "contribute" else None,
                "branch": "main",
                "idle_seconds": 300,
                "generation": "a" * 32,
                "enabled_at": time.time() - 5,
                "project_roots": [workspace],
                "consent_config": str(consent),
            }
        ),
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

    assert mindie_launch.main(["hook", "user-prompt-submit"]) == 0
    assert json.loads(capsys.readouterr().out.strip() or "{}") == {}
    assert mindie_launch.main(["hook"]) == 0
    assert json.loads(capsys.readouterr().out.strip() or "{}") == {}

    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps(event)]) == 0
    assert json.loads(capsys.readouterr().out.strip() or "{}") == {}
    assert not admission.exists() and not spool.exists()

    _configure(isolated_oc_env, workspace)
    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "directory": outside})]) == 0
    capsys.readouterr()
    assert not admission.exists() and not spool.exists()
    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "eventType": "session.execution.started"})]) == 0
    capsys.readouterr()
    assert not admission.exists() and not spool.exists()
    assert mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "stop_hook_active": True})]) == 0
    capsys.readouterr()
    assert not admission.exists() and not spool.exists()

    _append_turn(db_file, "ses_capture_a001", stamp_ms=now_ms + 1000)
    captured_events: list[dict] = []
    import mindie_knowledge.loop.handoff as handoff_mod

    orig_accept = handoff_mod.accept_stop
    monkeypatch.setattr(handoff_mod, "request_wake", lambda *_a, **_k: {"wake": "coalesced", "runtime": "unavailable", "reason": "test_no_daemon"})

    def _spy_accept(cfg_path, stop_event, **kwargs):
        captured_events.append(dict(stop_event))
        return orig_accept(cfg_path, stop_event, **kwargs)

    monkeypatch.setattr(handoff_mod, "accept_stop", _spy_accept)
    monkeypatch.setenv("_MINDIE_OC_REEXEC", "1")
    receipt = mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "eventID": "evt_post"})])
    capsys.readouterr()
    assert receipt == 0 and len(captured_events) == 1
    assert captured_events[0]["identity_kind"] == "notification" and captured_events[0]["event_id"] == "evt_post"
    assert "turn_id" not in captured_events[0]

    orig_log = mindie_launch._log
    monkeypatch.setattr(mindie_launch, "_log", lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated log full")))
    turn_out = mindie_launch._stop({**event, "eventID": "evt_bus_2", "turnID": "turn_native_01"})
    monkeypatch.setattr(mindie_launch, "_log", orig_log)
    assert len(captured_events) == 2
    assert captured_events[1]["identity_kind"] == "turn" and captured_events[1]["turn_id"] == "turn_native_01"
    assert "event_id" not in captured_events[1]
    assert turn_out.get("receipt_unconfirmed") is True and "receipt_warning" in turn_out

    body = spool.read_text(encoding="utf-8")
    assert "POST_CONSENT_SENTINEL" in body and "PART_SENTINEL" in body
    assert "PRE_CONSENT_SENTINEL" not in body and "HIDDEN_REASONING_SENTINEL" not in body

    from mindie_knowledge.loop.activation import Admission

    store = Admission(admission)
    lease = store.active_lease("ses_capture_a001")
    assert lease is not None and float(lease["activated_at"]) == 0.0
    store.deactivate("ses_capture_a001")
    captured_events.clear()
    replay = mindie_launch.main(["hook", "stop", "--event-json", json.dumps({**event, "eventID": "evt_revoked"})])
    capsys.readouterr()
    assert replay == 0 and captured_events == []


def test_admission_revocation_and_idle_deduplication(isolated_oc_env: dict[str, Path]) -> None:
    node = shutil.which("node")
    assert node is not None
    workspace = str(isolated_oc_env["workspace"])
    _configure(isolated_oc_env, workspace, choice="local-only")
    isolated_oc_env["opencode_db"].write_bytes(b"")

    spawns_dir = isolated_oc_env["root"] / "capture_spawns"
    spawns_dir.mkdir()
    spy_plugin_root = isolated_oc_env["root"] / "spy-plugin"
    (spy_plugin_root / "scripts").mkdir(parents=True)
    (spy_plugin_root / "scripts" / "mindie_launch.py").write_text(
        f"import os, pathlib, sys, time\n"
        f"out = pathlib.Path({str(spawns_dir)!r}) / f'{{os.getpid()}}_{{time.time_ns()}}.json'\n"
        f"out.write_text(sys.argv[-1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    driver = isolated_oc_env["root"] / "event_driver.mjs"
    driver.write_text(
        f"""
import fs from "node:fs";
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
push({{ type: "session.execution.started", id: "evt_started", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.execution.running", id: "evt_running", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.execution.progress", id: "evt_progress", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.execution.interrupted", id: "evt_int", data: {{ sessionID: "ses_main_a001", reason: "shutdown" }} }});
push({{ type: "session.idle", id: "evt_out", data: {{ sessionID: "ses_out_c003" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_sub_b002" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.idle", id: "evt_same", data: {{ sessionID: "ses_main_a001" }} }});
push({{ type: "session.turn.completed", id: "evt_t1", data: {{ sessionID: "ses_main_a001", turnID: "turn_01" }} }});
push({{ type: "session.turn.completed", id: "evt_t2", data: {{ sessionID: "ses_main_a001", turnID: "turn_02" }} }});
push({{ type: "session.turn.completed", id: "evt_t2_dup", data: {{ sessionID: "ses_main_a001", turnID: "turn_02" }} }});
const deadline = Date.now() + 8000;
while (Date.now() < deadline && fs.readdirSync({json.dumps(str(spawns_dir))}).length < 3) {{
  await new Promise((resolve) => setTimeout(resolve, 50));
}}
await new Promise((resolve) => setTimeout(resolve, 250));
await cleanup();
""",
        encoding="utf-8",
    )
    outside = str(isolated_oc_env["root"] / "outside-scope")
    Path(outside).mkdir()
    env = dict(
        os.environ,
        TEST_WORKSPACE=workspace,
        TEST_OUTSIDE=outside,
        MINDIE_OC_ALLOW_BG_UPDATE="0",
        OC_PLUGIN_ROOT=str(spy_plugin_root),
        MINDIE_OC_PYTHON=str(isolated_oc_env["python"]),
    )
    result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    files = sorted(spawns_dir.glob("*.json"))
    assert len(files) == 3, [f.read_text(encoding="utf-8") for f in files]
    payloads = {item["eventID"]: item for item in (json.loads(f.read_text(encoding="utf-8")) for f in files)}
    assert set(payloads) == {"evt_same", "evt_t1", "evt_t2"}
    assert payloads["evt_same"]["sessionID"] == "ses_main_a001" and "turnID" not in payloads["evt_same"]
    assert payloads["evt_t1"]["turnID"] == "turn_01" and payloads["evt_t2"]["turnID"] == "turn_02"
