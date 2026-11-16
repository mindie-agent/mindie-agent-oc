"""Round-3 Astra P1 regressions for storage lifecycle and update classification.

[Role]
Proves the six `0.1.7` Astra R2 P1 fixes: cursor-safe spool eviction against the
real `mindie_knowledge` transcript consumer, caller-configured external
`admission_path` support, immutable rollback-engine snapshots, symlink-alias
engine reference identity, full ancestor link/Git guards, and exception
`.category`/child-subprocess classification.

[Invariants]
- Eviction never unlinks a projection whose knowledge cursor is partially
  consumed or whose knowledge store cannot be queried/deleted from.
- A resumed session after a consumed-cursor eviction re-reads sequence 0 with no
  `transcript_cursor_identity_mismatch`.
- Every test runs inside `isolated_oc_env`; no host `~/.config` state is read.

[Timeouts & Locks]
- Spool/GC calls use `<= 5s` budgets; no test waits on a network or a real
  service. Subprocess leases are not started here.

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path` (including an isolated `TMPDIR`
  and `UV_CACHE_DIR`); never touches `opencode.db` outside the fixture.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import pytest

import component_update
import diagnostic_support
import opencode_spool
import opencode_transcript
import setup
import storage_gc
import storage_spool
from paths import config_path, engine_config_path, read_json, transcript_spool_dir

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def r3_env(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    scratch = isolated_oc_env["root"] / "scratch"
    temp = scratch / "tmp"
    cache = scratch / "uv-cache"
    for directory in (temp, cache):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.delenv("MINDIE_OC_FORCE_CLEANUP", raising=False)
    return dict(isolated_oc_env, scratch=scratch, temp=temp, uv_cache=cache)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_engine(path: Path, root: Path, domain: str = "vllm-ascend", *, python: str | None = None) -> None:
    _write_json(
        path,
        {
            "schema": "mindie-engine-config/1",
            "harness": "opencode",
            "domain": domain,
            "root": str(root),
            "state_dir": str(root.parent / "engine-state"),
            "admission_path": str(path.parent / "admission.sqlite3"),
            "summary_command": [python or sys.executable, "-I", "organizer.py"],
        },
    )


def _write_adapter(cfg: Path, payload: dict) -> None:
    _write_json(cfg, {"schema": "mindie-adapter-config/1", "harness": "opencode", **payload})


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - float(seconds)
    os.utime(path, (stamp, stamp))


def _create_opencode_db(path: Path, session: str, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT,"
            " time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT,"
            " time_created INTEGER, time_updated INTEGER, data TEXT);"
            "CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT,"
            " time_created INTEGER, time_updated INTEGER, data TEXT);"
        )
        created = 1730000000000
        db.execute("INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, ?, ?, '{}')", (session, str(path.parent), created, created))
        _insert_rows(db, session, 0, rows, created)


def _insert_rows(db: sqlite3.Connection, session: str, start: int, count: int, created: int = 1730000000000) -> None:
    for seq in range(start, start + count):
        stamp = created + seq
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, 'user', ?, ?, ?)",
            (f"m_{seq}", session, seq, stamp, stamp, json.dumps({"time": {"created": stamp}, "text": f"turn {seq}"})),
        )


def _append_opencode_rows(path: Path, session: str, start: int, count: int) -> None:
    with sqlite3.connect(path) as db:
        _insert_rows(db, session, start, count)


def _consume_spool(store, spool_file: Path, session: str, capture_id: str):
    """Drive the real `mindie_knowledge` transcript consumer for one read."""
    from mindie_knowledge.loop.transcript_increment import transcript_cursor

    identity = opencode_transcript.identify(spool_file)
    key, cursor = transcript_cursor(store, str(spool_file), opencode_transcript, identity)
    start = int(cursor["finish"]) if cursor else 0
    observed = {"identity": cursor.get("identity") or "", "finish": start} if cursor else None
    inc = opencode_transcript.read_material(spool_file, start, session_id=session, expected=None)
    assert inc["status"] == "ok", inc
    region = store.reserve_region(
        capture_id=capture_id, file_identity=key, identity=inc["identity"],
        start=inc["start"], finish=inc["end"], region_digest=inc["digest"],
        status="attempted", observed_cursor=observed,
    )
    assert region is not None, inc
    store.finish_region(region, "succeeded")
    return key, inc


def _make_knowledge_db(path: Path, *, finish: int, key: str, pending_capture: bool = False, trigger: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE cursors(file_identity TEXT PRIMARY KEY, identity TEXT NOT NULL,"
            " finish INTEGER NOT NULL, digest TEXT NOT NULL, ok_finish INTEGER NOT NULL, updated REAL NOT NULL)"
        )
        db.execute(
            "CREATE TABLE captures(id TEXT PRIMARY KEY, transcript TEXT, status TEXT)"
        )
        db.execute(
            "INSERT INTO cursors VALUES (?, '{}', ?, 'd', 0, 0)", (key, int(finish))
        )
        if pending_capture:
            db.execute("INSERT INTO captures VALUES ('c1', ?, 'pending')", (key,))
        if trigger:
            db.execute(
                "CREATE TRIGGER hold_cursor BEFORE DELETE ON cursors "
                "BEGIN SELECT RAISE(ABORT, 'cursor delete refused'); END"
            )


def _stage_candidate(dst: Path, *, version: str) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for item in ("package.json", "index.mjs", "pyproject.toml", "product-contract.json", "harness-compatibility.json", "plugins"):
        source, target = REPO_ROOT / item, dst / item
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for rel in ("package.json", "plugins/mindie-agent/plugin.json"):
        manifest = json.loads((dst / rel).read_text(encoding="utf-8"))
        manifest["version"] = version
        (dst / rel).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    contract = json.loads((dst / "product-contract.json").read_text(encoding="utf-8"))
    contract["native_version"] = version
    (dst / "product-contract.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return dst


def _snapshot(generations: Path, name: str, *, age_seconds: float) -> Path:
    generations.mkdir(parents=True, exist_ok=True)
    target = generations / name
    target.write_text(json.dumps({"schema": "mindie-engine-config/1"}) + "\n", encoding="utf-8")
    _age(target, age_seconds)
    return target


def test_consumed_cursor_is_retired_with_the_spool_and_resume_is_clean(r3_env: dict[str, Path]) -> None:
    """P1 #1: real consumer cursor is deleted before unlink; a resume never mismatches."""
    from mindie_knowledge.loop.store import Store

    session = "ses_r3_consume_01"
    db_file = r3_env["opencode_db"]
    _create_opencode_db(db_file, session, 2)
    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    spool_file = opencode_spool.sync_session_spool(session, db_path=db_file, spool_dir=spool, max_seconds=5.0)
    assert spool_file is not None and spool_file.is_file()

    store_root = r3_env["root"] / "knowledge-real"
    store = Store(store_root, "vllm-ascend")
    cfg = config_path()
    engine = engine_config_path(cfg)
    try:
        key, inc = _consume_spool(store, spool_file, session, "capture-r3-1")
        size = spool_file.stat().st_size
        row = store.db.execute("SELECT finish FROM cursors WHERE file_identity=?", (key,)).fetchone()
        assert row is not None and int(row[0]) == size, (row, size)

        _write_engine(engine, store_root)
        _write_adapter(
            cfg,
            {
                "python": str(Path(sys.executable)),
                "state_dir": str(spool.parent),
                "admission_path": str(spool.parent / "admission.sqlite3"),
                "engine_config": str(engine),
                "source_root": str(r3_env["mindie_data"] / "oc-source" / "r3"),
            },
        )
        _age(spool_file, 20 * 24 * 3600)
        report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
        assert session in report["removed_sessions"], report
        assert not (spool / f"{session}.jsonl").exists()
        assert not (spool / f"{session}.seq").exists()
        assert store.db.execute("SELECT finish FROM cursors WHERE file_identity=?", (key,)).fetchone() is None

        _append_opencode_rows(db_file, session, 2, 2)
        resumed = opencode_spool.sync_session_spool(session, db_path=db_file, spool_dir=spool, max_seconds=5.0)
        assert resumed is not None and resumed.is_file()
        key2, inc2 = _consume_spool(store, resumed, session, "capture-r3-2")
        assert key2 == key
        assert inc2["start"] == 0 and inc2["records"] == 4, inc2
        assert "turn 3" in inc2["text"], inc2["text"]
        assert store.db.execute("SELECT finish FROM cursors WHERE file_identity=?", (key,)).fetchone()[0] == resumed.stat().st_size
    finally:
        store.close()


def test_unconsumed_unknown_or_undeletable_cursor_keeps_the_projection(r3_env: dict[str, Path]) -> None:
    """P1 #1: partial offsets, pending captures, corrupt stores and DELETE failures keep the file."""
    session = "ses_r3_guard_01"
    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    cfg = config_path()
    engine = engine_config_path(cfg)
    knowledge_root = r3_env["root"] / "knowledge-manual"
    _write_engine(engine, knowledge_root)
    _write_adapter(
        cfg,
        {
            "python": str(Path(sys.executable)),
            "state_dir": str(spool.parent),
            "admission_path": str(spool.parent / "admission.sqlite3"),
            "engine_config": str(engine),
        },
    )
    database = knowledge_root / "vllm-ascend" / "state-v4" / "state-v4.sqlite3"
    projection = spool / f"{session}.jsonl"
    projection.write_text('{"schema":"mindie-oc-spool/1"}\n', encoding="utf-8")
    _age(projection, 20 * 24 * 3600)
    size = projection.stat().st_size

    # Raw (unresolved) spelling and a short offset must protect the projection.
    _make_knowledge_db(database, finish=size - 1, key=str(projection))
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["removed_sessions"] == [] and projection.is_file(), report
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 1

    # A pending capture for the same projection also protects it.
    _make_knowledge_db(database, finish=size, key=str(projection), pending_capture=True)
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["removed_sessions"] == [] and projection.is_file(), report
    assert not (spool / f"{session}.seq").exists()

    # A failing DELETE aborts retirement and reports partial.
    _make_knowledge_db(database, finish=size, key=str(projection), trigger=True)
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["status"] == "partial" and projection.is_file(), report

    # An unreadable store is unknown state: partial and no unlink.
    database.write_bytes(b"not-a-sqlite-database")
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["status"] == "partial" and projection.is_file(), report

    # A non-regular file at the configured DB path is also unknown state.
    database.unlink()
    database.mkdir(parents=True)
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["status"] == "partial" and projection.is_file(), report
    database.rmdir()

    # Restore a fully consumed store: the cursor is removed before the unlink.
    _make_knowledge_db(database, finish=size, key=str(projection))
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert session in report["removed_sessions"] and not projection.exists(), report
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0


def test_external_admission_path_is_honored_by_prune_and_spool_maintenance(r3_env: dict[str, Path]) -> None:
    """P1 #2: an admission store outside `state_dir` still protects live sessions."""
    state = r3_env["root"] / "state-custom"
    spool = state / "transcripts"
    spool.mkdir(parents=True, exist_ok=True)
    external = r3_env["root"] / "external-admission" / "admission.sqlite3"
    external.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(external) as db:
        db.execute(
            "CREATE TABLE leases(session TEXT PRIMARY KEY, token TEXT NOT NULL, enabled INTEGER NOT NULL,"
            " failures INTEGER NOT NULL, project_root TEXT, root_session TEXT, activated_at REAL,"
            " entry_enabled INTEGER NOT NULL DEFAULT 0)"
        )
        db.executemany("INSERT INTO leases (session, token, enabled, failures) VALUES (?, 't', ?, 0)",
                       (("ses_r3_live", 1), ("ses_r3_dead", 0)))
    cfg = r3_env["root"] / "custom-config" / "oc.json"
    _write_adapter(cfg, {"python": str(Path(sys.executable)), "state_dir": str(state), "admission_path": str(external)})
    for session in ("ses_r3_live", "ses_r3_dead", "ses_r3_other"):
        projection = spool / f"{session}.jsonl"
        projection.write_text("{}\n", encoding="utf-8")
        _age(projection, 20 * 24 * 3600)

    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert "ses_r3_live" not in report["removed_sessions"]
    assert (spool / "ses_r3_live.jsonl").is_file()
    assert "ses_r3_dead" in report["removed_sessions"], report
    assert not (spool / "ses_r3_dead.jsonl").exists()

    # The maintenance path driven by `opencode_spool` must pass the caller config.
    marker = spool / ".spool-maintenance.json"
    marker.unlink(missing_ok=True)
    cold = spool / "ses_r3_cold.jsonl"
    cold.write_text("{}\n", encoding="utf-8")
    _age(cold, 20 * 24 * 3600)
    opencode_spool._run_spool_maintenance(spool, "ses_r3_active", config=cfg)
    assert (spool / "ses_r3_live.jsonl").is_file(), "opencode_spool dropped the external admission pin"
    assert not cold.exists(), "the unadmitted cold projection should be retired"

    # A corrupt adapter config is unknown admission state, never an empty one.
    broken = r3_env["root"] / "broken-config" / "oc.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{corrupt", encoding="utf-8")
    survivor = spool / "ses_r3_survivor.jsonl"
    survivor.write_text("{}\n", encoding="utf-8")
    _age(survivor, 20 * 24 * 3600)
    guarded = storage_spool.prune_spool_transcripts(spool, config=broken, budget_seconds=5.0)
    assert guarded["status"] == "partial" and guarded["removed_sessions"] == [], guarded
    assert survivor.is_file(), "an unreadable adapter config pruned a live projection"


def test_first_upgrade_snapshots_the_mutable_rollback_engine(r3_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 #3: the default `oc.engine.json` payload survives the first upgrade."""
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=str(r3_env["python"]), prepare_store=False)
    before = read_json(cfg)
    default_engine = engine_config_path(cfg)
    old_summary = read_json(default_engine)["summary_command"]
    candidate = _stage_candidate(r3_env["root"] / "candidate-r3-0.1.9", version="0.1.9")
    fake_python = str(r3_env["root"] / "runtime-alt" / "bin" / "python")

    monkeypatch.setattr(component_update, "_active_python_is_reusable", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        component_update, "prepare",
        lambda **_kw: {
            "python": fake_python,
            "installed": {"mindie-knowledge": {"version": "0.13.22"}},
            "components": {"runtime": {}, "source": ""},
        },
    )
    monkeypatch.setattr(component_update, "sync_store_schema", lambda *_a, **_kw: (True, None))
    monkeypatch.setattr(storage_gc, "post_switch_reclaim", lambda **_kw: {"status": "completed", "warnings": [], "partial": False})

    result = component_update.check(config=cfg, source=candidate)
    assert result["status"] == "updated", result
    switched = read_json(cfg)
    assert switched["previous_python"] == before["python"]
    snapshot = Path(switched["previous_generation"])
    assert snapshot.parent == cfg.parent / "generations" and snapshot.is_file(), switched
    assert read_json(snapshot)["summary_command"] == old_summary
    assert read_json(default_engine)["summary_command"][0] == fake_python
    assert read_json(switched["engine_config"])["summary_command"][0] == fake_python

    # The persisted rollback snapshot stays pinned across engine snapshot GC.
    assert storage_gc.prune_engine_snapshots(cfg, keep_recent=0)["status"] in {"completed", "partial"}
    assert snapshot.is_file(), "GC reclaimed the persisted rollback engine snapshot"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory symlink")
def test_engine_snapshot_referenced_through_a_symlinked_alias_is_kept(r3_env: dict[str, Path]) -> None:
    """P1 #4: unresolved and resolved config spellings both pin a snapshot."""
    cfg_dir = r3_env["root"] / "alias-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "oc.json"
    generations = cfg_dir / "generations"
    old = _snapshot(generations, "oc.engine.old00000.json", age_seconds=90_000)
    newer = [_snapshot(generations, f"oc.engine.new{index}0000.json", age_seconds=80_000 - index) for index in range(2)]
    alias = r3_env["root"] / "alias-parent"
    alias.symlink_to(cfg_dir, target_is_directory=True)
    _write_adapter(
        cfg,
        {
            "python": str(Path(sys.executable)),
            "state_dir": str(cfg_dir / "state"),
            "engine_config": str(alias / "generations" / old.name),
        },
    )

    report = storage_gc.prune_engine_snapshots(cfg, keep_recent=0)
    assert report["status"] == "completed", report
    assert old.is_file(), "an older snapshot referenced through a symlinked parent was reclaimed"
    assert all(not item.is_file() for item in newer), "unreferenced snapshots must still be reclaimed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory symlink")
def test_ancestor_symlink_and_resolved_git_root_protect_reclamation(
    r3_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #5: a link above `state_dir` and a resolved Git ancestor are both keeps."""
    real = r3_env["root"] / "real-tree"
    (real / "state" / "component-runtimes").mkdir(parents=True, exist_ok=True)
    generation = real / "state" / "component-runtimes" / "gen-r3-cold"
    (generation / "runtime" / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (generation / "runtime" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    _age(generation, 90_000)
    linked_parent = r3_env["root"] / "linked-parent"
    linked_parent.symlink_to(real, target_is_directory=True)
    cfg = r3_env["root"] / "link-config" / "oc.json"
    _write_adapter(cfg, {"python": str(Path(sys.executable)), "state_dir": str(linked_parent / "state")})

    report = storage_gc.prune_component_generations(cfg, keep_recent_verified=0)
    assert report["status"] == "protected", report
    assert generation.is_dir(), "a symlinked ancestor above state_dir was ignored"

    # Isolate the resolved-Git guard: pretend the link walk passed, then a parent
    # symlink into an external Git checkout must still be caught by the resolved path.
    repo = r3_env["root"] / "external-git"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / "sub" / "state" / "component-runtimes").mkdir(parents=True, exist_ok=True)
    git_gen = repo / "sub" / "state" / "component-runtimes" / "gen-r3-git"
    git_gen.mkdir(parents=True, exist_ok=True)
    _age(git_gen, 90_000)
    git_link = r3_env["root"] / "git-link"
    git_link.symlink_to(repo / "sub", target_is_directory=True)
    git_cfg = r3_env["root"] / "git-config" / "oc.json"
    _write_adapter(git_cfg, {"python": str(Path(sys.executable)), "state_dir": str(git_link / "state")})
    monkeypatch.setattr(storage_gc.guards, "link_free", lambda *_a, **_kw: True)
    guarded = storage_gc.prune_component_generations(git_cfg, keep_recent_verified=0)
    assert guarded["status"] == "protected", guarded
    assert git_gen.is_dir(), "a Git checkout reached through a parent symlink was ignored"

    # macOS `/var`, `/tmp`, `/etc` are root firmlinks and must not false-positive.
    if os.name != "nt" and Path("/tmp").exists():
        import storage_support

        assert storage_support.link_free("/tmp") is True


def test_exception_categories_and_child_subprocess_phrases_are_non_reportable(
    r3_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #6: explicit `.category` and bounded child stderr map to non-reportable codes."""
    expected = {
        "resolver": "release_transport_unavailable",
        "busy": "update_resource_busy",
        "deferred": "update_deferred",
        "prerequisite": "update_environment_unavailable",
        "temporary_network": "release_transport_unavailable",
        "rate_limited": "release_rate_limited",
        "certificate": "release_certificate_error",
        "authentication": "update_authentication_required",
        "permission": "update_permission_denied",
        "hook_trust": "update_hook_trust_rejected",
        "cancelled": "update_cancelled",
        "deadline": "update_deadline_exceeded",
    }

    class Categorized(RuntimeError):
        pass

    for category, code in expected.items():
        direct = Categorized(category)
        direct.category = category
        assert diagnostic_support.classify_update_exception(direct) == (category, False, code)
        if category == "deadline":
            deadline_exc = TimeoutError("deadline")
            deadline_exc.category = "deadline"
            assert diagnostic_support.classify_update_exception(deadline_exc) == ("deadline", False, code)
        wrapped = RuntimeError("wrapper")
        wrapped.__cause__ = Categorized(category)
        wrapped.__cause__.category = category
        assert diagnostic_support.classify_update_exception(wrapped) == (category, False, code)
        contextual = RuntimeError("context wrapper")
        contextual.__context__ = Categorized(category)
        contextual.__context__.category = category
        contextual.__suppress_context__ = False
        assert diagnostic_support.classify_update_exception(contextual) == (category, False, code)

    assert diagnostic_support.classify_update_exception(RuntimeError("genuine bug")) == ("update", True, None)

    class Proc:
        returncode = 1
        stdout = ""

        def __init__(self, stderr: str):
            self.stderr = stderr

    cases = {
        "resolver": "No solution found when resolving dependencies: conflicting versions",
        "prerequisite": "error: No space left on device (os error 28)",
        "permission": "Permission denied: could not write oc-venv",
        "temporary_network": "Could not resolve host: github.com",
        "certificate": "SSL certificate problem: certificate verify failed",
    }
    for category, stderr in cases.items():
        with pytest.raises(RuntimeError) as caught:
            setup._raise_process_failure("uv sync failed while bootstrapping oc-venv", Proc(stderr))
        assert caught.value.category == category
        assert stderr not in str(caught.value), "raw child stderr leaked into the public message"
        assert diagnostic_support.classify_update_exception(caught.value)[1] is False

    source = r3_env["root"] / "uv-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "pyproject.toml").write_text("[project]\nname='r3'\n", encoding="utf-8")
    monkeypatch.setattr(setup, "_discover_existing_mindie_python", lambda: None)
    monkeypatch.setattr(setup, "_current_python_has_runtime", lambda: False)
    monkeypatch.setattr(setup, "_probe_runtime_python", lambda *_a, **_kw: False)
    monkeypatch.setattr(setup, "stage_source_baseline", lambda *_a, **_kw: source)
    monkeypatch.setattr(setup, "which_host_binary", lambda *_a, **_kw: "/usr/bin/uv")
    monkeypatch.setattr(setup, "apply_enterprise_transport_env", lambda env: dict(env))
    monkeypatch.setattr(setup, "bounded_run", lambda *_a, **_kw: Proc("version solving failed: unsatisfiable dependency"))
    with pytest.raises(RuntimeError) as caught:
        setup.ensure_runtime_python(config=r3_env["root"] / "no-profile.json", timeout=30.0)
    assert getattr(caught.value, "category", None) == "resolver"
    assert caught.value.reason_code == "update_resolver_conflict"
    assert diagnostic_support.classify_update_exception(caught.value) == ("resolver", False, "release_transport_unavailable")


def test_missing_explicit_domain_config_aborts_spool_eviction(r3_env: dict[str, Path]) -> None:
    """R4 P1 #1: a missing/dangling `domain_configs` target marks DB discovery incomplete."""
    session = "ses_r4_offline_domain"
    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    cfg = config_path()
    engine = engine_config_path(cfg)
    knowledge_root = r3_env["root"] / "knowledge-primary"
    _write_engine(engine, knowledge_root)
    missing_domain_cfg = r3_env["root"] / "offline-mount" / "sglang.engine.json"
    _write_adapter(
        cfg,
        {
            "python": str(Path(sys.executable)),
            "state_dir": str(spool.parent),
            "admission_path": str(spool.parent / "admission.sqlite3"),
            "engine_config": str(engine),
            "domain_configs": {"sglang": str(missing_domain_cfg)},
        },
    )
    projection = spool / f"{session}.jsonl"
    seq_file = spool / f"{session}.seq"
    projection.write_text('{"schema":"mindie-oc-spool/1"}\n', encoding="utf-8")
    seq_file.write_text("1\n", encoding="utf-8")
    _age(projection, 20 * 24 * 3600)
    _age(seq_file, 20 * 24 * 3600)
    primary_db = knowledge_root / "vllm-ascend" / "state-v4" / "state-v4.sqlite3"
    _make_knowledge_db(primary_db, finish=projection.stat().st_size, key=str(projection))

    dbs, complete = storage_spool.knowledge_databases(cfg)
    assert complete is False, (dbs, complete)
    report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
    assert report["status"] == "partial" and report["partial"] is True, report
    assert report["removed_sessions"] == []
    assert projection.is_file() and seq_file.is_file()

    # Non-string explicit engine keys (list/int/dict/empty) must also fail closed.
    for malformed in (["/offline/old.engine.json"], 42, {"path": "/offline/old.engine.json"}, ""):
        _write_adapter(
            cfg,
            {
                "python": str(Path(sys.executable)),
                "state_dir": str(spool.parent),
                "admission_path": str(spool.parent / "admission.sqlite3"),
                "engine_config": str(engine),
                "previous_generation": malformed,
            },
        )
        _dbs, ok = storage_spool.knowledge_databases(cfg)
        assert ok is False, malformed
        malformed_report = storage_spool.prune_spool_transcripts(spool, config=cfg, budget_seconds=5.0)
        assert malformed_report["status"] == "partial" and malformed_report["removed_sessions"] == [], malformed
        assert projection.is_file() and seq_file.is_file()


def test_custom_config_forbidden_source_root_without_git_protects_component_runtimes(
    r3_env: dict[str, Path],
) -> None:
    """R4 P1 #2: custom `--config` passes its non-Git `source_root` as `forbidden_roots`."""
    custom_source = r3_env["root"] / "exported-source-no-git"
    gen = custom_source / "state" / "component-runtimes" / "gen-r4-cold"
    (gen / "runtime" / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (gen / "runtime" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (gen / "verified.json").write_text('{"status":"verified"}\n', encoding="utf-8")
    _age(gen, 90_000)

    custom_cfg = r3_env["root"] / "isolated-custom-cfg" / "oc.json"
    _write_adapter(
        custom_cfg,
        {
            "python": str(Path(sys.executable)),
            "source_root": str(custom_source),
            "state_dir": str(custom_source / "state"),
        },
    )

    report = storage_gc.prune_component_generations(
        custom_cfg, keep_recent_verified=0, live_paths=set(),
    )
    assert report["status"] == "protected_root", report
    assert report["reclaimed_dirs"] == 0
    assert gen.is_dir(), "generation inside configured non-Git source_root was reclaimed"
