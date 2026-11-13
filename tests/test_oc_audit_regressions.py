"""Comprehensive regression suite for Codex `gpt-6-astra` (`max`) audit findings.

[Role]
Exercises real `mindie_knowledge`, `opencode_host.mjs` (Node), SQLite WAL, and
POSIX process-group boundaries across all 19 identified P1/P2 & contract items.

[Invariants]
- Zero `MagicMock` hardware fabrication; uses real `mindie_knowledge` (`Engine`,
  `MaterialStore`, `distill`, `releases`), real SQLite databases, and real child
  processes strictly inside `isolated_oc_env`.

[Timeouts & Locks]
- Tier 0/1 local boundary execution (`< 15s` total).

[Side-Effect Policy]
- Mutates only `isolated_oc_env` temporary directories.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import pytest

import bounded
import bridge
import component_update
import identity
import knowledge_cli
import mcp_server
import opencode_transcript
import organizer
import paths
import setup as oc_setup

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_real_transcript_capture_ingest_and_oversize_messages(isolated_oc_env: dict[str, Path]) -> None:
    """Items #1 & #10a: `coverage` stays empty on complete pages; >48 KiB and >256 KiB messages keep tail sentinels."""
    from mindie_knowledge.loop import settings as settings_mod
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.store import Store

    ws = str(isolated_oc_env["workspace"])
    session_id = "ses_real_ingest_01"
    bridge.handle_gesture({"sessionID": session_id, "directory": ws, "prompt": "/mindie-agent"})
    oc_setup.ensure_default_profile(workspace_root=ws, python_exe=sys.executable)
    oc_setup.configure_community(
        enabled=True,
        fork="test-user/knowledge-vllm-ascend",
        account="test-user",
        project_root=ws,
    )

    now_ms = int(time.time() * 1000) + 200
    tail_64k = "TAIL_SENTINEL_64KB_PRESERVED"
    tail_320k = "TAIL_SENTINEL_320KB_PRESERVED"
    msg_64k = ("A3 HCCL tuning observation. " * 2500) + tail_64k
    msg_320k = ("Ascend ACLGraph stream capture detail. " * 9000) + tail_320k
    assert len(msg_64k.encode("utf-8")) > 64 * 1024 and len(msg_320k.encode("utf-8")) > 300 * 1024

    db_file = isolated_oc_env["opencode_db"]
    with sqlite3.connect(db_file) as db:
        db.executescript(
            """
            CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            """
        )
        db.execute("INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, ?, ?, '{}')", (session_id, ws, now_ms, now_ms))
        for seq, (role, body) in enumerate(
            [
                ("user", "Short 112-byte prompt checking normal transcript capture into MaterialStore."),
                ("assistant", msg_64k),
                ("assistant", msg_320k),
            ]
        ):
            mid, ts = f"m_{seq}", now_ms + seq * 10
            db.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, session_id, seq, role, ts, ts, json.dumps({"time": {"created": ts}})),
            )
            db.execute(
                "INSERT INTO session_part VALUES (?, ?, 0, 'text', ?, ?, ?)",
                (mid, session_id, ts, ts, json.dumps({"type": "text", "text": body})),
            )

    spool_file = opencode_transcript.sync_session_spool(session_id, db_path=db_file, spool_dir=paths.transcript_spool_dir())
    assert spool_file is not None and spool_file.is_file()

    eng_cfg = paths.read_json(paths.engine_config_path())
    redactor_exe = eng_cfg.get("redactor_executable")
    store = Store(isolated_oc_env["mindie_data"] / "domains" / "vllm-ascend", domain="vllm-ascend")
    try:
        engine = Engine(
            store,
            settings_path=paths.community_config_path(),
            admission=Admission(paths.admission_path()),
            state_dir=paths.state_dir() / "engine",
            transcript_adapter=opencode_transcript,
            redactor_executable=redactor_exe,
            summary_command=[sys.executable, "-I", str(REPO_ROOT / "plugins/mindie-agent/scripts/organizer.py")],
        )
        if not redactor_exe:
            import mindie_knowledge.materials.ingest as ingest_mod

            orig_redact = ingest_mod.redact_increment
            ingest_mod.redact_increment = lambda text, **kw: (text, [], kw.get("state") or {})
        else:
            orig_redact = None
        try:
            cap = engine.capture(session_id=session_id, turn_id="turn_real_01", transcript_path=str(spool_file), harness="opencode")
            assert cap["status"] in {"queued", "pending"}
            for _ in range(4):
                due = store.due_capture()
                if not due:
                    break
                engine._process(due)

            rows = store.db.execute("SELECT status, detail FROM captures WHERE id=?", (cap["id"],)).fetchall()
            assert rows and rows[0][0] in {"succeeded", "organized", "captured"}, f"unexpected capture state: {rows}"

            stream_rows = store.db.execute("SELECT entry_id, source_cursor FROM material_streams").fetchall()
            assert len(stream_rows) == 1
            entry_id, finish_offset = stream_rows[0]
            assert int(finish_offset) == spool_file.stat().st_size
            exported = store.materials.export_task(entry_id, source="draft")["files"]
            combined = "".join(exported.values())
            assert "Short 112-byte prompt" in combined and tail_64k in combined and tail_320k in combined

            with spool_file.open("ab") as f:
                f.write(b"{corrupt_json_line\n")
            bad_inc = opencode_transcript.read_material(spool_file, int(finish_offset), session_id=session_id)
            assert bad_inc.get("discarded_records")
            cap2 = engine.capture(session_id=session_id, turn_id="turn_corrupt_02", transcript_path=str(spool_file), harness="opencode")
            engine._process(cap2["id"])
            st2 = store.db.execute("SELECT status, detail FROM captures WHERE id=?", (cap2["id"],)).fetchone()
            assert st2[0] != "organized" and ("incomplete" in st2[1] or "projection" in st2[1] or "discarded" in st2[1])
            finish_after = store.db.execute("SELECT source_cursor FROM material_streams WHERE entry_id=?", (entry_id,)).fetchone()[0]
            assert int(finish_after) == int(finish_offset)
        finally:
            if orig_redact is not None:
                ingest_mod.redact_increment = orig_redact
    finally:
        store.close()
        assert settings_mod.load(paths.community_config_path()).allows_capture()


def test_organizer_c6_distill_request_executes_and_validates_schema(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item #2: `organizer.py` calls `distill.decision_schema()` with 0 args and completes C6 distillation."""
    from mindie_knowledge.graded import distill
    import mindie_knowledge.loop.transcript_redaction as tr_redact

    observed_schemas: list[dict] = []
    decision_holder: list[dict] = []

    class _DistillHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            observed_schemas.append(body["response_schema"])
            encoded = json.dumps(
                {"result": json.dumps(decision_holder[0]), "usage": {"input_tokens": 90, "output_tokens": 20}}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = HTTPServer(("127.0.0.1", 0), _DistillHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("MINDIE_SUMMARY_ENDPOINT", f"http://127.0.0.1:{port}/v1/summary")
        monkeypatch.setenv("MINDIE_SUMMARY_TOKEN", "distill_tok")
        monkeypatch.setenv("MINDIE_SUMMARY_MODEL", "deepseek/deepseek-flash#max")
        monkeypatch.setattr(tr_redact, "redact_tree", lambda tree, **_kw: (tree, []))

        worker_id = organizer.identity("deepseek/deepseek-flash#max")
        assert organizer.DISTILL_REQUEST_SCHEMA in worker_id["capabilities"]

        raw_req = distill.prepare_request(
            [
                {
                    "kind": "ablation_receipt",
                    "outcome": "verified",
                    "content": "Verified HCCL_BUFFSIZE=512 reduced A3 16-die TP AllReduce p99 from 4.2ms to 2.8ms.",
                    "source_locator": "mindie-bench/run_01.json",
                    "evidence_refs": ["mindie-bench/run_01.json"],
                }
            ],
            session_digest="d" * 64,
            task_state="verified_milestone",
            redactor_executable=None,
            redaction_key=b"0" * 32,
        )
        decision_holder.append(distill.noop_decision(raw_req))
        req = distill.with_worker_identity(raw_req, worker_id)
        outcome = organizer.execute_request(req)
        assert outcome["status"] == "returned", f"unexpected C6 outcome: {outcome}"
        assert outcome["model_calls"] == 1
        assert outcome["result"]["action"] == "noop"
        assert len(observed_schemas) == 1
        assert observed_schemas[0] == distill.decision_schema()
    finally:
        server.shutdown()
        server.server_close()


def test_organizer_cli_fallback_isolates_host_opencode_db_and_env(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item #4: CLI fallback isolates XDG_DATA_HOME, XDG_CACHE_HOME, OPENCODE_DB, and strips inherited OPENCODE_*."""
    from mindie_knowledge.materials import summarizer

    host_db = isolated_oc_env["opencode_db"]
    host_db.parent.mkdir(parents=True, exist_ok=True)
    host_db.write_bytes(b"HOST_SQLITE_SENTINEL")

    env_dump_file = isolated_oc_env["root"] / "cli_env_dump.json"
    fake_cli = isolated_oc_env["root"] / "fake-opencode"
    block_id = "c" * 64
    fake_cli.write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib
db_target = pathlib.Path(os.environ["OPENCODE_DB"])
db_target.parent.mkdir(parents=True, exist_ok=True)
db_target.write_bytes(b"SCRATCH_DB_WRITE")
pathlib.Path({json.dumps(str(env_dump_file))}).write_text(json.dumps({{
    "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
    "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
    "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
    "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
    "OPENCODE_DB": os.environ.get("OPENCODE_DB"),
    "OPENCODE_CONFIG": os.environ.get("OPENCODE_CONFIG"),
    "OPENCODE_CONFIG_CONTENT": os.environ.get("OPENCODE_CONFIG_CONTENT"),
    "OPENCODE_SESSION_ID": os.environ.get("OPENCODE_SESSION_ID"),
    "MINDIE_OPENCODE_DB": os.environ.get("MINDIE_OPENCODE_DB"),
}}), encoding="utf-8")
print(json.dumps({{
    "blocks": [{{"block_id": {json.dumps(block_id)}, "title": "Isolated", "summary": "CLI fallback isolated DB."}}],
    "navigation": {{"title": "Isolated", "summary": "CLI fallback isolated DB."}}
}}))
""",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    monkeypatch.delenv("MINDIE_SUMMARY_ENDPOINT", raising=False)
    monkeypatch.setenv("MINDIE_OC_BIN", str(fake_cli))
    monkeypatch.setenv("OPENCODE_CONFIG", "/leak/opencode.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"plugin":["leak"]}')
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_leak_parent")

    worker_id = organizer.identity("deepseek/deepseek-flash#max")
    req = summarizer.make_request(
        task_id="e" * 64,
        body_version="v1",
        prior_navigation=None,
        blocks=[{"block_id": block_id, "text": "[user]\nTest isolation", "source_range": {"start": 0, "end": 22}}],
        identity=worker_id,
    )
    out = organizer.execute_request(req)
    assert out["status"] == "returned", f"unexpected CLI fallback outcome: {out}"
    assert host_db.read_bytes() == b"HOST_SQLITE_SENTINEL"

    dumped = json.loads(env_dump_file.read_text(encoding="utf-8"))
    assert dumped["OPENCODE_CONFIG"] is None
    assert dumped["OPENCODE_CONFIG_CONTENT"] is None
    assert dumped["OPENCODE_SESSION_ID"] is None
    assert dumped["MINDIE_OPENCODE_DB"] is None
    assert dumped["XDG_DATA_HOME"] != str(isolated_oc_env["xdg_data"])
    assert dumped["OPENCODE_DB"].startswith(dumped["XDG_DATA_HOME"])
    assert not Path(dumped["XDG_DATA_HOME"]).exists(), "ephemeral scratch data dir must be cleaned up"


def test_concurrent_same_args_mcp_binds_and_shell_commands_never_cross_bind(
    isolated_oc_env: dict[str, Path],
) -> None:
    """Item #5: Out-of-order concurrent identical MCP calls and shell commands bind strictly 1-to-1."""
    ws_a = isolated_oc_env["root"] / "ws_alpha"
    ws_b = isolated_oc_env["root"] / "ws_bravo"
    ws_a.mkdir(parents=True, exist_ok=True)
    ws_b.mkdir(parents=True, exist_ok=True)

    same_args = {"argv": ["decode-code", "507015"]}
    res_a = bridge.handle_pre_tool_use(
        {
            "sessionID": "ses_alpha01",
            "id": "call_alpha",
            "tool": "mindie-tools_debugger",
            "input": same_args,
            "directory": str(ws_a),
        }
    )
    res_b = bridge.handle_pre_tool_use(
        {
            "sessionID": "ses_bravo02",
            "id": "call_bravo",
            "tool": "mindie-tools_debugger",
            "input": same_args,
            "directory": str(ws_b),
        }
    )
    assert res_a["bound"] and res_b["bound"] and res_a["bind_id"] != res_b["bind_id"]

    # Ambiguous claim without `bind_id` across 2 active sessions MUST fail closed instead of guessing!
    with pytest.raises(ValueError, match="ambiguous concurrent"):
        identity.claim_bind("tools", "debugger", same_args)

    # Exact correlation via `_mindie_bind_id` preserves session + workspace even when claimed A-then-B or B-then-A!
    claim_a = identity.claim_bind("tools", "debugger", same_args, bind_id=res_a["bind_id"])
    claim_b = identity.claim_bind("tools", "debugger", same_args, bind_id=res_b["bind_id"])
    assert claim_a["session"] == "ses_alpha01" and claim_a["workspace_root"] == str(ws_a.resolve())
    assert claim_b["session"] == "ses_bravo02" and claim_b["workspace_root"] == str(ws_b.resolve())
    identity.finish_bind(claim_a["bind_id"], True)
    identity.finish_bind(claim_b["bind_id"], True)

    # Verify Node host (`opencode_host.mjs`) for concurrent identical shell commands + terminal events!
    node_bin = shutil.which("node")
    assert node_bin is not None
    capture_marker = isolated_oc_env["root"] / "captured_events.jsonl"
    driver = isolated_oc_env["root"] / "concurrent_host_driver.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").as_uri())};

const toolHooks = new Map();
const shellHooks = new Map();
const eventsToEmit = [
    {{ id: "ev_ok_1", type: "session.execution.succeeded", data: {{ sessionID: "ses_alpha01" }} }},
    {{ id: "ev_ok_1", type: "session.idle", data: {{ sessionID: "ses_alpha01" }} }},
    {{ id: "ev_shut_1", type: "session.execution.interrupted", data: {{ sessionID: "ses_bravo02", reason: "shutdown" }} }},
    {{ id: "ev_int_1", type: "session.execution.interrupted", data: {{ sessionID: "ses_bravo02", reason: "user" }} }},
];

let releaseStream;
const streamDone = new Promise((r) => {{ releaseStream = r; }});

const mockCtx = {{
    location: {{ directory: {json.dumps(str(ws_a))} }},
    session: {{
        async get({{ sessionID }}) {{
            return {{ id: sessionID, directory: sessionID === "ses_alpha01" ? {json.dumps(str(ws_a))} : {json.dumps(str(ws_b))} }};
        }},
        hook() {{}},
    }},
    tool: {{ hook(stage, fn) {{ toolHooks.set(stage, fn); }} }},
    shell: {{ hook(stage, fn) {{ shellHooks.set(stage, fn); }} }},
    event: {{
        async subscribe() {{
            return (async function* () {{
                for (const ev of eventsToEmit) {{
                    yield ev;
                }}
                releaseStream();
            }})();
        }},
    }},
}};

const cleanup = await plugin.setup(mockCtx);

// 1. Concurrent identical shell commands executed OUT OF ORDER (`B` spawns before `A`).
const inputA = {{ command: "python knowledge_cli.py describe" }};
const inputB = {{ command: "python knowledge_cli.py describe" }};
await toolHooks.get("execute.before")({{ tool: "bash", sessionID: "ses_alpha01", id: "c1", input: inputA }});
await toolHooks.get("execute.before")({{ tool: "bash", sessionID: "ses_bravo02", id: "c2", input: inputB }});

const shellB = {{ command: `zsh -lc '${{inputB.command}}'`, env: {{}} }};
shellHooks.get("create.before")(shellB);
assert.equal(shellB.env.OPENCODE_SESSION_ID, "ses_bravo02");
assert.equal(shellB.command, "zsh -lc 'python knowledge_cli.py describe'");

const shellA = {{ command: `zsh -lc '${{inputA.command}}'`, env: {{}} }};
shellHooks.get("create.before")(shellA);
assert.equal(shellA.env.OPENCODE_SESSION_ID, "ses_alpha01");
assert.equal(shellA.command, "zsh -lc 'python knowledge_cli.py describe'");

// 2. Concurrent MCP tool `execute.before` injects distinct `_mindie_bind_id` on same-arg inputs.
const mcpInA = {{ argv: ["status"] }};
const mcpInB = {{ argv: ["status"] }};
await toolHooks.get("execute.before")({{ tool: "mindie-tools_mindie_status", sessionID: "ses_alpha01", id: "m1", input: mcpInA }});
await toolHooks.get("execute.before")({{ tool: "mindie-tools_mindie_status", sessionID: "ses_bravo02", id: "m2", input: mcpInB }});
assert.equal(typeof mcpInA._mindie_bind_id, "string");
assert.equal(typeof mcpInB._mindie_bind_id, "string");
assert.notEqual(mcpInA._mindie_bind_id, mcpInB._mindie_bind_id);

await streamDone;
await cleanup();
console.log("CONCURRENT_HOST_OK");
""",
        encoding="utf-8",
    )
    proc = subprocess.run([node_bin, str(driver)], capture_output=True, text=True, timeout=15.0, check=False)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "CONCURRENT_HOST_OK" in proc.stdout
    assert not capture_marker.exists()


def test_stage_source_baseline_includes_uv_lock_and_keeps_snapshot_immutable(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item #6: `stage_source_baseline` stages `uv.lock`; `ensure_runtime_python` builds outside `src_root` with `--locked`."""
    staged = oc_setup.stage_source_baseline(REPO_ROOT)
    assert (staged / "uv.lock").is_file()
    digest_before = oc_setup._payload_digest(staged)

    uv_calls: list[list[str]] = []
    fake_venv_py = isolated_oc_env["mindie_data"] / "oc-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _fake_bounded_run(argv, stdin_text="", **kwargs):
        uv_calls.append(list(argv))
        if "--project" in argv:
            proj_dir = Path(argv[argv.index("--project") + 1])
            (proj_dir / ".uv-mutated-marker").write_text("touched\n", encoding="utf-8")
        fake_venv_py.parent.mkdir(parents=True, exist_ok=True)
        if not fake_venv_py.exists():
            fake_venv_py.symlink_to(sys.executable)
        return bounded.ProcessResult(0, "", "", 1)

    monkeypatch.setattr(oc_setup, "_current_python_has_runtime", lambda: False)
    monkeypatch.setattr(oc_setup, "_discover_existing_mindie_python", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(oc_setup, "bounded_run", _fake_bounded_run)
    monkeypatch.setattr(component_update, "compatible_runtime", lambda py, **_kw: str(py))

    resolved_py = oc_setup.ensure_runtime_python(source_root=REPO_ROOT)
    assert Path(resolved_py).name.startswith("python")
    sync_calls = [c for c in uv_calls if len(c) >= 2 and c[1] == "sync"]
    assert len(sync_calls) == 1
    assert {"--locked", "--no-editable", "--no-install-project"}.issubset(sync_calls[0])
    proj_arg = Path(sync_calls[0][sync_calls[0].index("--project") + 1]).resolve()
    assert proj_arg != staged.resolve()
    assert oc_setup._payload_digest(staged) == digest_before
    assert oc_setup.stage_source_baseline(REPO_ROOT) == staged


def test_cold_start_gesture_activates_lease_immediately_and_spawns_detached_bootstrap(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item #7: Cold-start `/mindie-agent` activates lease in <1s without blocking on 540s `uv sync`."""
    ws = str(isolated_oc_env["workspace"])
    spawned: list[list[str]] = []

    class _DummyPopen:
        pid = 99999

    monkeypatch.setattr(oc_setup, "_current_python_has_runtime", lambda: False)
    monkeypatch.setattr(oc_setup, "_discover_existing_mindie_python", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_kw: (spawned.append(list(argv)), _DummyPopen())[1])

    t0 = time.monotonic()
    res = bridge.handle_gesture({"sessionID": "ses_coldstart01", "directory": ws, "prompt": "/mindie-agent"})
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0 and res["activated"] is True and res["bootstrapping"] is True and res["status"] == "bootstrapping"
    admission_lease = bridge.knowledge_lease("ses_coldstart01")
    assert admission_lease is not None and admission_lease["project_root"] == str(Path(ws).resolve())
    assert len(spawned) == 1 and "setup.py" in spawned[0][2] and "init" in spawned[0]


def test_upgraded_plugin_discovery_and_atomic_rollback_on_prepare_failure(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items #8 & #9: Installed `0.1.1` overrides active `0.1.0` in `index.mjs`; failed `prepare()` keeps `0.1.0` untouched."""
    def _make_plugin_tree(dest: Path, version: str) -> Path:
        oc_setup.stage_source_baseline(REPO_ROOT, dest_dir=dest)
        for rel, key in (
            ("package.json", "version"),
            ("plugins/mindie-agent/plugin.json", "version"),
            ("product-contract.json", "native_version"),
        ):
            p = dest / rel
            d = json.loads(p.read_text(encoding="utf-8"))
            d[key] = version
            p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        return dest

    v010 = _make_plugin_tree(isolated_oc_env["root"] / "src-0.1.0", "0.1.0")
    v011 = _make_plugin_tree(isolated_oc_env["root"] / "src-0.1.1", "0.1.1")

    oc_setup.ensure_default_profile(source_root=v010, workspace_root=isolated_oc_env["workspace"], python_exe=sys.executable)
    active_before = paths.read_json(paths.config_path())
    eng_before = paths.engine_config_path()
    assert active_before["product_validation"]["native_version"] == "0.1.0"

    node_bin = shutil.which("node")
    assert node_bin is not None
    probe_js = isolated_oc_env["root"] / "probe_upgrade.mjs"
    probe_js.write_text(
        f"""
import assert from "node:assert/strict";
import plugin, {{ resolveActiveHostModule }} from {json.dumps((v011 / "index.mjs").as_uri())};
import {{ pluginRoot }} from {json.dumps((v011 / "plugins/mindie-agent/paths.mjs").as_uri())};
assert.equal(resolveActiveHostModule(), {json.dumps(str((v011 / "plugins/mindie-agent/opencode_host.mjs").resolve()))});
await plugin.setup({{}});
assert.equal(process.env.MINDIE_OC_INSTALLED_ROOT, {json.dumps(str(v011.resolve()))});
assert.equal(pluginRoot(), {json.dumps(str((v011 / "plugins/mindie-agent").resolve()))});
console.log("UPGRADE_INDEX_OK");
""",
        encoding="utf-8",
    )
    clean_js_env = {k: v for k, v in os.environ.items() if k not in {"OC_PLUGIN_ROOT", "MINDIE_OC_INSTALLED_ROOT"}}
    js_res = subprocess.run([node_bin, str(probe_js)], env=clean_js_env, capture_output=True, text=True, timeout=10.0, check=False)
    assert js_res.returncode == 0, f"STDOUT:\n{js_res.stdout}\nSTDERR:\n{js_res.stderr}"
    assert "UPGRADE_INDEX_OK" in js_res.stdout

    monkeypatch.setenv("MINDIE_OC_INSTALLED_ROOT", str(v011))
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: (_ for _ in ()).throw(RuntimeError("injected prepare failure")))
    with pytest.raises(RuntimeError, match="injected prepare failure"):
        component_update.check()

    after_fail = paths.read_json(paths.config_path())
    assert after_fail["product_validation"]["native_version"] == "0.1.0"
    assert after_fail["source_root"] == active_before["source_root"]
    assert paths.engine_config_path() == eng_before

    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    promoted = component_update.check()
    assert promoted["status"] == "updated" and promoted["needs_host_reload"] is True
    after_ok = paths.read_json(paths.config_path())
    assert after_ok["product_validation"]["native_version"] == "0.1.1"
    assert "/generations/" in after_ok["engine_config"].replace("\\", "/")
    assert Path(after_ok["engine_config"]).is_file()
    assert paths.read_json(after_ok["engine_config"])["summary_command"][0] == after_ok["python"]


def test_spool_concurrency_torn_tail_recovery_redaction_and_pagination(
    isolated_oc_env: dict[str, Path],
) -> None:
    """Items #10b, #10c & #11: Concurrent spool writers never duplicate; torn tail + missing `.seq` recover; >500 rows paginate; secrets/paths redact."""
    db_file = isolated_oc_env["opencode_db"]
    spool_dir = paths.transcript_spool_dir()
    ws = str(isolated_oc_env["workspace"])
    sid = "ses_concurrent_600"
    secret_tok = "sk-liveSecretKey9876543210abcdefghijklmnop"

    with sqlite3.connect(db_file) as db:
        db.executescript(
            """
            CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            """
        )
        db.execute("INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, 1730000000000, 1730000000000, '{}')", (sid, ws))
        for i in range(620):
            mid, ts, role = f"m_{i:04d}", 1730000001000 + i * 10, ("user" if i % 2 == 0 else "assistant")
            db.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, sid, i, role, ts, ts, json.dumps({"time": {"created": ts}})))
            db.execute("INSERT INTO session_part VALUES (?, ?, 0, 'reasoning', ?, ?, ?)", (mid, sid, ts, ts, json.dumps({"text": "HIDDEN_REASONING_NEVER_LOADED"})))
            txt = f"Row {i} in {ws} using token {secret_tok}" if i == 0 else f"Row {i} public observation"
            db.execute("INSERT INTO session_part VALUES (?, ?, 1, 'text', ?, ?, ?)", (mid, sid, ts, ts, json.dumps({"type": "text", "text": txt})))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [
            f.result()
            for f in [
                pool.submit(opencode_transcript.sync_session_spool, sid, db_path=db_file, spool_dir=spool_dir, private_paths=(ws,), max_seconds=5.0)
                for _ in range(4)
            ]
        ]

    spool_file = results[0]
    assert spool_file is not None and spool_file.is_file()
    raw_text = spool_file.read_text(encoding="utf-8")
    assert secret_tok not in raw_text and ws not in raw_text and "HIDDEN_REASONING_NEVER_LOADED" not in raw_text
    assert [json.loads(line)["seq"] for line in raw_text.splitlines()] == list(range(620))

    with spool_file.open("ab") as f:
        f.write(b'{"schema":"mindie-oc-spool/1","seq":999,"broken":')
    (spool_dir / f"{sid}.seq").unlink()

    recovered = opencode_transcript.sync_session_spool(sid, db_path=db_file, spool_dir=spool_dir, max_seconds=5.0)
    assert recovered == spool_file
    assert [json.loads(line)["seq"] for line in spool_file.read_text(encoding="utf-8").splitlines()] == list(range(620))


def test_bounded_run_streams_oversized_stdout_preserves_exit0_and_kills_process_group(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item #12: Streaming stdout cap preserves exit 0 (`isError=False` in MCP); POSIX `killpg` kills grandchildren after parent exits."""
    res = bounded.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('X' * 131072)"],
        "",
        timeout=10.0,
        max_output=16384,
        strict_output_limit=False,
    )
    assert res.returncode == 0 and res.stdout_truncated is True and len(res.stdout) == 16384

    ws = str(isolated_oc_env["workspace"])
    bind = bridge.handle_pre_tool_use(
        {"sessionID": "ses_mcp_trunc", "id": "call_trunc", "tool": "mindie-tools_npu_top", "input": {"argv": ["--snapshot"]}, "directory": ws}
    )
    monkeypatch.setattr(mcp_server, "COMPONENT_CAPTURE_LIMIT_BYTES", 32768)
    monkeypatch.setattr(
        mcp_server,
        "_resolve_component_command",
        lambda _comp, config=None: [sys.executable, "-c", "import sys; sys.stdout.write('Y' * 200000)"],
    )
    mcp_res = mcp_server.call_tools_surface("npu_top", {"argv": ["--snapshot"], "_mindie_bind_id": bind["bind_id"]})
    assert mcp_res["isError"] is False
    assert mcp_res["structuredContent"]["exit_code"] == 0
    assert mcp_res["structuredContent"]["output_truncated"] is True

    if os.name != "nt":
        def _assert_dead(pid: int) -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.03)
                except ProcessLookupError:
                    return
            os.kill(pid, signal.SIGKILL)
            pytest.fail(f"grandchild {pid} escaped process-group cleanup")

        # 2a. Parent exits 0 immediately while grandchild holds inherited pipes open: must NOT hang or leak grandchild!
        pid_early = isolated_oc_env["root"] / "grandchild_early.pid"
        code_early = (
            "import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({json.dumps(str(pid_early))}, 'w').write(str(p.pid)); "
            "sys.exit(0)"
        )
        early_res = bounded.run([sys.executable, "-c", code_early], "", timeout=3.0)
        assert early_res.returncode == 0 and early_res.cleanup_failed is False
        _assert_dead(int(pid_early.read_text(encoding="utf-8").strip()))

        # 2b. Parent + grandchild exceed timeout: raises TimeoutError and reaps entire process group!
        pid_timeout = isolated_oc_env["root"] / "grandchild_timeout.pid"
        code_timeout = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({json.dumps(str(pid_timeout))}, 'w').write(str(p.pid)); "
            "time.sleep(60)"
        )
        with pytest.raises(TimeoutError):
            bounded.run([sys.executable, "-c", code_timeout], "", timeout=0.5)
        _assert_dead(int(pid_timeout.read_text(encoding="utf-8").strip()))


def test_contract_and_parity_gaps_standard_mode_db_path_org_fork_and_compat_validation(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tables #1, #2, #3, #4: `mode='standard'`->`'reuse'`, JS/Python DB path parity, `--org` fork verification, and strict `validate_contracts`."""
    ws = str(isolated_oc_env["workspace"])
    bridge.handle_gesture({"sessionID": "ses_std_mode", "directory": ws, "prompt": "/mindie-agent"})
    oc_setup.ensure_default_profile(workspace_root=ws, python_exe=sys.executable)
    captured_rpc: list[tuple[str, dict]] = []
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.transport as loop_transport

    monkeypatch.setattr(loop_cli, "ensure_service", lambda _eng: ("fake_conn",))
    monkeypatch.setattr(
        loop_transport,
        "rpc",
        lambda _conn, method, payload, timeout=30.0: (captured_rpc.append((method, dict(payload))), {"hits": []})[1],
    )
    res_q = knowledge_cli.invoke("knowledge_query", {"query": "HCCL", "mode": "standard"}, session="ses_std_mode")
    assert res_q["isError"] is False and captured_rpc[-1][0] == "query" and captured_rpc[-1][1]["mode"] == "reuse"

    node_bin = shutil.which("node")
    assert node_bin is not None
    custom_cfg_db = isolated_oc_env["root"] / "from_oc_json" / "custom.db"
    cfg_data = paths.read_json(paths.config_path())
    cfg_data["opencode_db"] = str(custom_cfg_db)
    paths.atomic_write_json(paths.config_path(), cfg_data)
    monkeypatch.delenv("MINDIE_OPENCODE_DB", raising=False)
    monkeypatch.setenv("OPENCODE_DB", "channel-dev.db")

    def _js_db_path() -> str:
        out = subprocess.check_output(
            [
                node_bin,
                "--input-type=module",
                "-e",
                f"import {{ opencodeDbPath }} from {json.dumps((REPO_ROOT / 'plugins/mindie-agent/paths.mjs').as_uri())}; console.log(opencodeDbPath());",
            ],
            text=True,
            timeout=5.0,
        )
        return out.strip()

    assert str(paths.opencode_db_path()) == str(custom_cfg_db.resolve())
    assert _js_db_path() == str(custom_cfg_db.resolve())

    del cfg_data["opencode_db"]
    paths.atomic_write_json(paths.config_path(), cfg_data)
    expected_rel_db = (isolated_oc_env["xdg_data"] / "opencode" / "channel-dev.db").resolve()
    assert paths.opencode_db_path() == expected_rel_db and _js_db_path() == str(expected_rel_db)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def _fake_gh_unverified(argv, _stdin="", **_kw):
        gh_calls.append(list(argv))
        if "user" in argv:
            return bounded.ProcessResult(0, "personal-user\n", "", 1)
        if "fork" in argv:
            return bounded.ProcessResult(0, "", "", 1)
        return bounded.ProcessResult(1, "", "404 Not Found", 1)

    monkeypatch.setattr(oc_setup, "bounded_run", _fake_gh_unverified)
    acct, fork_slug = oc_setup._resolve_github_fork("mindie-agent/knowledge-vllm-ascend", "target-org")
    assert acct == "target-org" and fork_slug is None
    fork_invocations = [c for c in gh_calls if "fork" in c]
    assert len(fork_invocations) == 1 and "--org" in fork_invocations[0] and "target-org" in fork_invocations[0]

    bad_root = isolated_oc_env["root"] / "bad-contracts"
    oc_setup.stage_source_baseline(REPO_ROOT, dest_dir=bad_root)
    compat_file = bad_root / "harness-compatibility.json"
    bad_compat = json.loads(compat_file.read_text(encoding="utf-8"))
    bad_compat["harness"] = "antigravity"
    compat_file.write_text(json.dumps(bad_compat) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="harness mismatch"):
        oc_setup.validate_contracts(bad_root)

    bad_compat["harness"] = "opencode"
    bad_compat["supported"] = []
    compat_file.write_text(json.dumps(bad_compat) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="supported intervals"):
        oc_setup.validate_contracts(bad_root)


def test_recheck_cold_stdlib_schema_bind_older_source_streaming_and_1mb_head(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recheck P1/P2 fixes: `-S` cold activation, 34-tool `_mindie_bind_id` schema, older `--source` monotonicity, in-flight streaming row, and >1 MiB head record."""
    import admission

    # 1. Cold `-S` activation (site-packages disabled -> zero `mindie_knowledge`) activates pending lease & promotes!
    ws = str(isolated_oc_env["workspace"])
    with monkeypatch.context() as mp:
        mp.setattr(admission, "_has_mindie_knowledge", lambda: False)
        mp.setattr(oc_setup, "_current_python_has_runtime", lambda: False)
        mp.setattr(oc_setup, "_discover_existing_mindie_python", lambda: None)
        mp.setattr(subprocess, "Popen", lambda *_a, **_kw: type("_P", (), {"pid": 4242})())
        cold_res = bridge.handle_gesture({"sessionID": "ses_stdlib_cold", "directory": ws, "prompt": "/mindie-agent"})
        assert cold_res["activated"] is True and cold_res["bootstrapping"] is True
        assert admission.knowledge_lease("ses_stdlib_cold") is not None
    assert admission.flush_pending_cold_activations() == 1
    assert admission.knowledge_lease("ses_stdlib_cold")["project_root"] == str(Path(ws).resolve())

    # 2. All 34 MCP schemas declare optional `_mindie_bind_id` (including closed `mindie_status`) so OpenCode never strips/rejects it.
    all_tools = mcp_server.tools_catalog() + mcp_server.remote_catalog()
    assert len(all_tools) == 34
    for tool in all_tools:
        props = tool["inputSchema"]["properties"]
        assert props.get("_mindie_bind_id") == {"type": "string"}
        assert "_mindie_bind_id" not in (tool["inputSchema"].get("required") or [])

    # 3. `component_update.check(source=<older_0.1.0>)` never downgrades active `0.1.1` payload when a component updates.
    v010 = isolated_oc_env["root"] / "older-0.1.0"
    oc_setup.stage_source_baseline(REPO_ROOT, dest_dir=v010)
    for rel, key in (("package.json", "version"), ("plugins/mindie-agent/plugin.json", "version"), ("product-contract.json", "native_version")):
        p = v010 / rel
        d = json.loads(p.read_text(encoding="utf-8"))
        d[key] = "0.1.0"
        p.write_text(json.dumps(d) + "\n", encoding="utf-8")
    oc_setup.ensure_default_profile(source_root=REPO_ROOT, workspace_root=ws, python_exe=sys.executable)
    with monkeypatch.context() as mp:
        mp.setattr(
            component_update,
            "prepare",
            lambda **_kw: {"python": sys.executable, "components": {"runtime": sys.executable}, "installed": {}},
        )
        mp.setenv("OC_PLUGIN_ROOT", str(v010 / "plugins" / "mindie-agent"))
        upd = component_update.check(source=v010, force=True)
    assert upd["status"] == "updated"
    active_cfg = paths.read_json(paths.config_path())
    assert active_cfg["product_validation"]["native_version"] == "0.1.1"
    assert Path(active_cfg["source_root"]).resolve() != v010.resolve()

    # 4. In-flight streaming assistant row stops cursor BEFORE `seq`, then captures full response once `completed` is set; >1 MiB first record parses!
    db_file = isolated_oc_env["opencode_db"]
    sid = "ses_stream_1mb"
    now_ms = int(time.time() * 1000)
    huge_user = ("Ascend A3 HCCL ring trace event ok. " * 32000) + ("U" * (128 * 1024)) + " _HEAD_OK"
    with sqlite3.connect(db_file) as db:
        db.executescript(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        db.execute("INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, ?, ?, '{}')", (sid, ws, now_ms, now_ms))
        db.execute("INSERT INTO session_message VALUES ('m1', ?, 1, 'user', ?, ?, ?)", (sid, now_ms, now_ms, json.dumps({"time": {"created": now_ms}, "text": huge_user})))
        db.execute("INSERT INTO session_message VALUES ('m2', ?, 2, 'assistant', ?, ?, ?)", (sid, now_ms + 5, now_ms + 5, json.dumps({"time": {"created": now_ms + 5, "streamed": now_ms + 6}, "agent": "build", "content": [{"type": "text", "text": "PARTIAL"}]})))
    spool = opencode_transcript.sync_session_spool(sid, db_path=db_file, spool_dir=paths.transcript_spool_dir())
    assert spool is not None
    assert "PARTIAL" not in spool.read_text(encoding="utf-8")
    src_meta = opencode_transcript.capture_source(spool, sid, binding={"session": sid, "enabled": 1, "project_root": ws, "activated_at": 1.0})
    assert src_meta["session_id"] == sid
    with sqlite3.connect(db_file) as db:
        db.execute("UPDATE session_message SET data=? WHERE id='m2'", (json.dumps({"time": {"created": now_ms + 5, "completed": now_ms + 20}, "finish": "stop", "content": [{"type": "text", "text": "FINAL_COMPLETE_REPLY"}]}),))
    opencode_transcript.sync_session_spool(sid, db_path=db_file, spool_dir=paths.transcript_spool_dir())
    assert "FINAL_COMPLETE_REPLY" in spool.read_text(encoding="utf-8")


def test_round3_deactivation_tombstone_boundary_overlap_custom_adm_and_stale_endpoint(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-3 fixes: cold deactivate tombstone, preserved `activated_at`, 1800s bind TTL, overlap union redaction, custom `admission_path`, and stale endpoint CLI fallback."""
    import admission
    import opencode_spool

    ws = str(isolated_oc_env["workspace"])
    # 1. Cold activation + cold deactivation leaves tombstone so promotion NEVER re-enables; preserved `activated_at` survives promotion!
    with monkeypatch.context() as mp:
        mp.setattr(admission, "_has_mindie_knowledge", lambda: False)
        a_keep = admission.activate("ses_keep_boundary", ws)
        admission.activate("ses_cold_deact", ws)
        d_res = admission.deactivate("ses_cold_deact")
        assert d_res["status"] == "inactive"
    time.sleep(0.05)
    assert admission.flush_pending_cold_activations() == 1
    assert admission.knowledge_lease("ses_cold_deact") is None
    promoted_lease = admission.knowledge_lease("ses_keep_boundary")
    assert promoted_lease is not None
    assert abs(float(promoted_lease["activated_at"]) - float(a_keep["activated_at"])) < 1e-3

    # 2. Bind TTL covers queued long-running MCP calls (`>= 1800s >= 2 * 660s`).
    assert identity.BIND_TTL_SECONDS >= 1800.0

    # 3. Overlapping redaction spans merge via union (`include_overlaps=True`) leaving zero customer username/suffix fragments.
    overlap_raw = "path=/Users/z00123456/customer_secret_repo password=ghp_1234567890abcdefghij123456!tailSecret"
    redacted = opencode_spool._redact_spool_text(overlap_raw, private_paths=())
    assert "z00123456" not in redacted
    assert "customer_secret_repo" not in redacted
    assert "tailSecret" not in redacted

    # 4. JS `admissionPath()` & host fast-path honor custom `oc.json.admission_path`.
    custom_adm = isolated_oc_env["root"] / "custom-adm" / "custom_leases.sqlite3"
    paths.atomic_write_json(paths.config_path(), {"harness": "opencode", "admission_path": str(custom_adm)})
    admission.activate("ses_custom_db", ws)
    assert custom_adm.is_file()
    node_probe = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import host from './plugins/mindie-agent/opencode_host.mjs'; "
            "import { admissionPath } from './plugins/mindie-agent/paths.mjs'; "
            "let ctxFn; const ctx = { session: { hook(n, fn){ if(n==='context') ctxFn=fn; }, get: async ()=>({directory: process.env.TEST_WS}) } }; "
            "await host.setup(ctx); const sys = []; await ctxFn({ sessionID: 'ses_custom_db', system: sys }); "
            "console.log(JSON.stringify({ adm: admissionPath(), injected: sys.length }));",
        ],
        cwd=str(REPO_ROOT),
        env={**os.environ, "TEST_WS": ws},
        capture_output=True,
        text=True,
        timeout=15.0,
        check=True,
    )
    probe_out = json.loads(node_probe.stdout.strip().splitlines()[-1])
    assert Path(probe_out["adm"]).resolve() == custom_adm.resolve()
    assert probe_out["injected"] == 1

    # 5. Stale `summary-endpoint.json` (`ConnectionRefusedError` after unclean exit) falls back cleanly to standalone CLI.
    sock_probe = __import__("socket").socket()
    sock_probe.bind(("127.0.0.1", 0))
    dead_port = sock_probe.getsockname()[1]
    sock_probe.close()
    ep_file = paths.state_dir() / "summary-endpoint.json"
    paths.atomic_write_json(ep_file, {"url": f"http://127.0.0.1:{dead_port}/v1/summary", "token": "stale"})
    fake_oc = isolated_oc_env["root"] / "fake-opencode-stale"
    fake_oc.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ok\":true}'\n", encoding="utf-8")
    fake_oc.chmod(0o755)
    monkeypatch.setenv("MINDIE_OC_BIN", str(fake_oc))
    monkeypatch.delenv("MINDIE_SUMMARY_ENDPOINT", raising=False)
    model = organizer.NativeSummaryModel("deepseek/deepseek-flash")
    assert json.loads(model._call_once("Return ok json", {"type": "object"})) == {"ok": True}
    assert not ep_file.exists()
