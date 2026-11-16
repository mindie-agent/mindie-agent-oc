"""Cross-platform storage lifecycle regression tests (`test_storage_lifecycle_oc.py`).

[Role]
Covers the `0.1.7` storage governance work: silent reclamation of historical
`component-runtimes` / `oc-source` / `generations/oc.engine.*.json` /
`transcripts/*.jsonl` baggage, live-lease and borrowed-interpreter protection, the
single-`.venv` `native_candidate` upgrade path, post-switch cleanup warnings that
never roll back a committed switch, and the `< 800` line module cap.

[Invariants]
- Reclamation never touches the active `oc.json` pointer, the active interpreter,
  a generation leased by a real subprocess, a session projection whose
  `.lock` is held, or a `source_root` still referenced by a harness config.
- `native_candidate` upgrades reuse a healthy existing interpreter and build
  exactly one component runtime; the baseline bootstrap only runs when
  `prepare()` reports nothing to build.
- Every assertion stays inside `isolated_oc_env`, including `TMPDIR` and
  `UV_CACHE_DIR`; no test writes to the host `~`.

[Timeouts & Locks]
- Lease subprocesses are bounded (`<= 15s`) and always reaped; eviction uses
  `<= 2.5s` budgets and OS locks are probed non-blocking.

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`; the only external processes are
  the lease holders and the bounded `pkill`-free tree termination of those
  holders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

import component_update
import opencode_spool
import opencode_transcript
from paths import config_path, data_home, read_json, state_dir, transcript_spool_dir
import runtime_bootstrap
import setup
import storage_gc

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
_PYTHON_REL = "Scripts/python.exe" if os.name == "nt" else "bin/python"
CORE_MODULES = (
    REPO_ROOT / "index.mjs",
    REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs",
    REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs",
    REPO_ROOT / "plugins" / "mindie-agent" / "session_support.mjs",
    *sorted((REPO_ROOT / "plugins" / "mindie-agent" / "scripts").glob("*.py")),
)
TIGHT_MODULES = (
    REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs",
    REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "mcp_server.py",
    REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "component_update.py",
)


@pytest.fixture()
def storage_env(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """`isolated_oc_env` plus an isolated `TMPDIR` and `UV_CACHE_DIR`."""
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


def _touch_generation(root: Path, name: str, *, age_seconds: float, verified: bool = True) -> Path:
    """A synthetic component-runtimes generation with a real interpreter path."""
    generation = root / name
    venv = generation / "runtime" / ".venv"
    inner = venv / ("Scripts" if os.name == "nt" else "bin")
    inner.mkdir(parents=True, exist_ok=True)
    (inner / ("python.exe" if os.name == "nt" else "python")).write_text("#!/bin/sh\n", encoding="utf-8")
    manifest = generation / "components.json"
    if verified:
        manifest.write_text(json.dumps({"generation_key": name, "runtime": {}}) + "\n", encoding="utf-8")
    stamp = time.time() - float(age_seconds)
    targets = [generation, venv, inner, *([manifest] if verified else [])]
    for target in targets:
        os.utime(target, (stamp, stamp))
    return generation


def _write_adapter(config: Path, payload: dict) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def _hold_spool_lock(lock_file: Path):
    """Acquire `lock_file` exactly like a live projection writer would."""
    handle = lock_file.open("a+b")
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if os.stat(lock_file).st_size == 0:
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def _release_spool_lock(handle, lock_file: Path) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _write_engine_snapshot(generations: Path, name: str, *, age_seconds: float) -> Path:
    generations.mkdir(parents=True, exist_ok=True)
    snapshot = generations / name
    snapshot.write_text(json.dumps({"schema": "mindie-engine-config/1"}) + "\n", encoding="utf-8")
    stamp = time.time() - float(age_seconds)
    os.utime(snapshot, (stamp, stamp))
    return snapshot


def test_component_runtimes_silent_reclaim_protects_live_and_borrowed_interpreter(
    storage_env: dict[str, Path],
) -> None:
    cfg = config_path()
    runtimes = state_dir(cfg) / "component-runtimes"
    current = _touch_generation(runtimes, "gen-current", age_seconds=90_000)
    borrowed = _touch_generation(runtimes, "gen-borrowed", age_seconds=85_000)
    stale = [_touch_generation(runtimes, f"gen-stale-{index}", age_seconds=70_000 - index * 10_000) for index in range(2)]
    previous = _touch_generation(runtimes, "gen-previous", age_seconds=400)
    newest = _touch_generation(runtimes, "gen-newest", age_seconds=300)
    failed = _touch_generation(runtimes, "gen-failed", age_seconds=9_000, verified=False)
    staging = _touch_generation(runtimes, "gen-staging", age_seconds=5, verified=False)
    python_rel = ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _write_adapter(
        cfg,
        {
            "schema": "mindie-adapter-config/1",
            "harness": "opencode",
            "python": str(current / "runtime/.venv" / python_rel),
            "state_dir": str(state_dir(cfg)),
            "source_root": str(storage_env["mindie_data"] / "oc-source" / "oc-0.1.7-current"),
        },
    )
    # A different MindIE harness still borrows one generation's interpreter.
    _write_adapter(
        storage_env["mindie_cfg"] / "agy.json",
        {"schema": "mindie-adapter-config/1", "python": str(borrowed / "runtime/.venv" / python_rel)},
    )

    result = storage_gc.prune_component_generations(cfg, keep_recent_verified=2)

    assert result["status"] == "completed", result
    assert result["reclaimed_dirs"] == 3, result
    for kept in (current, borrowed, previous, newest, staging):
        assert kept.is_dir(), f"protected generation was reclaimed: {kept}"
    assert not failed.exists(), "an abandoned half-built generation must be reclaimed"
    for dropped in stale:
        assert not dropped.exists(), f"an excess generation survived retention: {dropped}"
    assert python_rel and (current / "runtime/.venv" / python_rel).is_file()
    assert not any(item.name.startswith(".trash-") for item in runtimes.iterdir())


def test_oc_source_and_engine_snapshot_retention_follows_references(storage_env: dict[str, Path]) -> None:
    cfg = config_path()
    sources = data_home() / "oc-source"
    created: list[Path] = []
    for index, name in enumerate(("oc-0.1.3-aaaa", "oc-0.1.4-bbbb", "oc-0.1.5-cccc", "oc-0.1.6-dddd", "oc-0.1.7-eeee")):
        payload = sources / name
        (payload / "plugins/mindie-agent").mkdir(parents=True, exist_ok=True)
        (payload / "pyproject.toml").write_text("version = \"0.1.7\"\n", encoding="utf-8")
        stamp = time.time() - (50_000 - index * 1_000)
        os.utime(payload, (stamp, stamp))
        created.append(payload)
    active_source, rolled_back, oldest = created[-1], created[-2], created[0]
    # Another harness still pins the oldest payload through its `source_root`.
    _write_adapter(
        storage_env["mindie_cfg"] / "wb.json",
        {"schema": "mindie-adapter-config/1", "source_root": str(oldest), "python": str(Path(sys.executable))},
    )
    _write_adapter(
        cfg,
        {
            "schema": "mindie-adapter-config/1",
            "harness": "opencode",
            "python": str(Path(sys.executable)),
            "state_dir": str(state_dir(cfg)),
            "source_root": str(active_source),
            "runtime_scripts": str(active_source / "plugins/mindie-agent/scripts"),
        },
    )

    generations = cfg.parent / "generations"
    active = _write_engine_snapshot(generations, "oc.engine.active0.json", age_seconds=90_000)
    borrowed_engine = _write_engine_snapshot(generations, "oc.engine.other01.json", age_seconds=85_000)
    history = [_write_engine_snapshot(generations, f"oc.engine.hist{index:02d}.json", age_seconds=80_000 - index * 100) for index in range(5)]
    _write_adapter(storage_env["mindie_cfg"] / "agy.json", {"schema": "mindie-engine-config/1", "engine_config": str(borrowed_engine)})
    adapter = read_json(cfg)
    adapter["engine_config"] = str(active)
    _write_adapter(cfg, adapter)

    snapshot_report = storage_gc.prune_engine_snapshots(cfg, keep_recent=3)
    assert snapshot_report["status"] == "completed", snapshot_report
    assert snapshot_report["removed"] == 2, snapshot_report
    for kept in (active, borrowed_engine, *history[2:]):
        assert kept.is_file(), f"referenced snapshot was reclaimed: {kept}"
    for dropped in history[:2]:
        assert not dropped.is_file(), f"unreferenced snapshot survived retention: {dropped}"

    legacy = storage_gc.sweep_legacy_baggage(force=True)
    assert legacy["status"] in {"completed", "partial"}, legacy
    assert active_source.is_dir() and rolled_back.is_dir()
    assert oldest.is_dir(), "a cross-harness `source_root` pin must survive the sweep"
    assert any(not item.is_dir() for item in created[:2]), "historical `oc-source` payloads were not reclaimed"


def test_transcript_projection_eviction_is_bounded_and_keeps_locked_sessions(
    storage_env: dict[str, Path],
) -> None:
    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    now = time.time()
    sessions = [f"ses_spool_{index:03d}" for index in range(140)]
    for index, session in enumerate(sessions):
        (spool / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
        (spool / f".{session}.lock").write_bytes(b"0")
        stamp = now - (5_000 - index)  # oldest first
        os.utime(spool / f"{session}.jsonl", (stamp, stamp))
    active_session = sessions[0]
    active_lock = _hold_spool_lock(spool / f".{active_session}.lock")

    try:
        # 1. Total-session cap: the 12 oldest projections beyond 128 are retired, the
        #    locked (still projecting) session is deferred instead.
        capped = storage_gc.prune_spool_transcripts(spool, max_sessions=128, max_age_seconds=None)
        assert capped["status"] == "completed", capped
        assert len(capped["removed_sessions"]) == 12, capped
        assert active_session not in capped["removed_sessions"]
        assert (spool / f"{active_session}.jsonl").is_file(), "a locked projection was reclaimed"
        assert capped["deferred_dirs"] >= 1, capped
        assert len(list(spool.glob("ses_spool_*.jsonl"))) == 128

        # 2. Age-based retention: projections older than the 14-day window are retired.
        expired = [f"ses_expired_{index}" for index in range(3)]
        for index, session in enumerate(expired):
            (spool / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
            stamp = now - (20 * 24 * 3600 + index)
            os.utime(spool / f"{session}.jsonl", (stamp, stamp))
        aged = storage_gc.prune_spool_transcripts(spool, max_sessions=10_000)
        assert aged["status"] == "completed", aged
        assert set(aged["removed_sessions"]) == set(expired)
        for session in expired:
            assert not (spool / f"{session}.jsonl").exists()
            assert not (spool / f".{session}.lock").exists()
    finally:
        _release_spool_lock(active_lock, spool / f".{active_session}.lock")

    # 3. Once the writer releases its lock the oldest projection becomes evictable.
    remaining = len(list(spool.glob("ses_spool_*.jsonl")))
    unlocked = storage_gc.prune_spool_transcripts(spool, max_sessions=remaining - 1, max_age_seconds=None)
    assert unlocked["status"] == "completed", unlocked
    assert active_session in unlocked["removed_sessions"]
    assert not (spool / f"{active_session}.jsonl").exists()
    assert not (spool / f".{active_session}.lock").exists()


def test_sync_session_spool_runs_bounded_maintenance_and_drops_idle_locks(
    storage_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offload = transcript_spool_dir() / ".ses_cold_001.lock"
    opencode_spool._THREAD_LOCKS[offload.as_posix()] = threading.Lock()

    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        session = f"ses_cold_{index:03d}"
        (spool / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
        stamp = time.time() - (40 * 24 * 3600)
        os.utime(spool / f"{session}.jsonl", (stamp, stamp))

    db_file = storage_env["opencode_db"]
    import sqlite3

    with sqlite3.connect(db_file) as db:
        db.executescript(
            """
            CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, fork_session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            CREATE TABLE session_part (message_id TEXT, session_id TEXT, ordinal INTEGER, type TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
            """
        )
        db.execute(
            "INSERT INTO session_v2 VALUES (?, ?, NULL, NULL, 1730000000000, 1730000000000, '{}')",
            ("ses_live_777", str(storage_env["workspace"])),
        )
        db.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("m_live_1", "ses_live_777", 1, "user", 1730000001000, 1730000001000, json.dumps({"time": {"created": 1730000001000}})),
        )
        db.execute(
            "INSERT INTO session_part VALUES (?, ?, 0, 'text', ?, ?, ?)",
            ("m_live_1", "ses_live_777", 1730000001000, 1730000001000, json.dumps({"type": "text", "text": "public user text"})),
        )

    spool_file = opencode_transcript.sync_session_spool(
        "ses_live_777", db_path=db_file, spool_dir=spool, max_seconds=5.0,
    )

    assert spool_file is not None and spool_file.is_file()
    assert not (spool / "ses_cold_000.jsonl").exists()
    assert not (spool / ".ses_cold_001.lock").exists()
    assert str(spool / ".ses_cold_001.lock") not in opencode_spool._THREAD_LOCKS
    marker = json.loads((spool / ".spool-maintenance.json").read_text(encoding="utf-8"))
    assert marker["removed"] == 3 and marker["status"] == "completed"


def test_real_subprocess_lease_protects_generation_and_survives_tree_termination(
    storage_env: dict[str, Path],
) -> None:
    cfg = config_path()
    runtimes = state_dir(cfg) / "component-runtimes"
    leased = _touch_generation(runtimes, "gen-leased", age_seconds=90_000)
    for index in range(3):
        _touch_generation(runtimes, f"gen-cold-{index}", age_seconds=60_000 - index)
    _write_adapter(
        cfg,
        {"schema": "mindie-adapter-config/1", "harness": "opencode", "state_dir": str(state_dir(cfg)),
         "python": str(Path(sys.executable))},
    )

    ready_file = storage_env["root"] / "lease.ready"
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[3]);"
        "from mindie_knowledge.storage import acquire_runtime_lease;"
        "lease = acquire_runtime_lease(sys.argv[1]);"
        "open(sys.argv[2], 'w', encoding='utf-8').write('ready\\n');"
        "sys.stdin.read(); lease.release()"
    )
    kwargs: dict = dict(
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, cwd=str(storage_env["scratch"]), env=dict(os.environ),
    )
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(leased), str(ready_file), str(SCRIPTS_DIR)], **kwargs
    )
    try:
        deadline = time.monotonic() + 15.0
        while not ready_file.is_file() and time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"lease holder exited early: {proc.stderr.read() if proc.stderr else ''}")
            time.sleep(0.02)
        assert ready_file.is_file(), "lease holder never signalled readiness"

        guarded = storage_gc.prune_component_generations(cfg, keep_recent_verified=0)
        assert leased.is_dir(), "a leased generation was retired under a live process"
        assert guarded["reclaimed_dirs"] == 3, guarded
        assert guarded["kept_dirs"] >= 1, guarded
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        try:
            proc.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10.0)

    # The lease holder refreshed the generation's mtime, so age it past the staging grace
    # before the reclaim that must now succeed.
    stamp = time.time() - 86_400
    for target in (leased, leased / "components.json"):
        os.utime(target, (stamp, stamp))
    released = storage_gc.prune_component_generations(cfg, keep_recent_verified=0)
    assert released["status"] == "completed", released
    assert released["reclaimed_dirs"] >= 1, released
    assert not leased.exists(), "the released generation was not reclaimed"


def _stage_candidate(dst: Path, *, version: str) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for item in ("package.json", "index.mjs", "pyproject.toml", "product-contract.json", "harness-compatibility.json", "plugins"):
        source = REPO_ROOT / item
        target = dst / item
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for rel in ("package.json", "plugins/mindie-agent/plugin.json"):
        manifest = json.loads((dst / rel).read_text(encoding="utf-8"))
        manifest["version"] = version
        (dst / rel).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    contract = json.loads((dst / "product-contract.json").read_text(encoding="utf-8"))
    contract["native_version"] = version
    (dst / "product-contract.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8", newline="\n")
    return dst


def test_native_candidate_builds_exactly_one_component_runtime(
    storage_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(storage_env["python"])
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=py, prepare_store=False)
    active_python = read_json(cfg)["python"]
    candidate = _stage_candidate(storage_env["root"] / "candidate-0.1.9", version="0.1.9")

    builds: list[tuple[str, str]] = []

    def _fake_prepare(*, source=None, python=None, **_kwargs):
        builds.append(("prepare", str(python)))
        return {
            "python": str(python),
            "installed": {"mindie-knowledge": {"version": "0.13.22"}},
            "components": {"runtime": {}, "source": ""},
        }

    baseline_builds: list[str] = []

    def _fake_ensure_runtime_python(*, source_root=None, python_exe=None, **_kwargs):
        baseline_builds.append(str(python_exe))
        return str(python_exe or py)

    monkeypatch.setattr(component_update, "prepare", _fake_prepare)
    monkeypatch.setattr(setup, "ensure_runtime_python", _fake_ensure_runtime_python)
    monkeypatch.setattr(component_update, "sync_store_schema", lambda *_a, **_kw: (True, None))
    monkeypatch.setattr(storage_gc, "post_switch_reclaim", lambda **_kw: {"status": "completed", "warnings": [], "partial": False})

    result = component_update.check(config=cfg, source=candidate)

    assert result["status"] == "updated", result
    assert builds == [("prepare", active_python)], builds
    assert baseline_builds == [], "a healthy active interpreter must not bootstrap a baseline runtime"
    assert "cleanup_warning" not in result

    # Only a `prepare()` that reports nothing to build may fall back to the baseline bootstrap.
    builds.clear()
    baseline_builds.clear()

    def _none_then_artifact(*, source=None, python=None, **_kwargs):
        builds.append(("prepare", str(python)))
        if len(builds) == 1:
            return None
        return {"python": str(python), "installed": {"mindie-knowledge": {"version": "0.13.22"}}, "components": {"runtime": {}, "source": ""}}

    healed = str(storage_env["root"] / "baseline-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    monkeypatch.setattr(component_update, "prepare", _none_then_artifact)
    monkeypatch.setattr(setup, "ensure_runtime_python", lambda **_kw: healed)
    candidate_v2 = _stage_candidate(storage_env["root"] / "candidate-0.1.10", version="0.1.10")

    promoted = component_update.check(config=cfg, source=candidate_v2)

    assert promoted["status"] == "updated", promoted
    assert builds == [("prepare", active_python), ("prepare", healed)], builds


def test_post_switch_cleanup_degrades_to_warning_without_rolling_back_state(
    storage_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(storage_env["python"])
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=py, prepare_store=False)
    previous_version = read_json(cfg)["product_validation"]["native_version"]
    candidate = _stage_candidate(storage_env["root"] / "candidate-0.1.11", version="0.1.11")

    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    monkeypatch.setattr(component_update, "sync_store_schema", lambda *_a, **_kw: (True, None))
    monkeypatch.setattr(component_update, "compatible_runtime", lambda interpreter, **_kw: component_update._norm_python_path(interpreter))
    monkeypatch.setattr(
        storage_gc,
        "post_switch_reclaim",
        lambda **_kw: {"status": "partial", "warnings": ["TimeoutError: busy generation"], "partial": True},
    )
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_kw: {"mindie-knowledge": {"version": "0.13.22"}})

    result = component_update.check(config=cfg, source=candidate)

    assert result["status"] == "updated", result
    assert result["needs_host_reload"] is True
    assert result["cleanup_warning"].startswith("storage cleanup partial")
    assert "receipt_unconfirmed" not in result
    adapter = read_json(cfg)
    assert adapter["product_validation"]["native_version"] == "0.1.11"
    assert adapter["product_validation"]["native_version"] != previous_version


def test_cold_bootstrap_uv_cache_keeps_an_explicit_caller_choice(storage_env: dict[str, Path]) -> None:
    explicit = {"UV_CACHE_DIR": str(storage_env["uv_cache"]), "UV_NO_PROGRESS": "1"}
    assert runtime_bootstrap.same_volume_uv_cache(storage_env["mindie_data"] / "oc-venv", explicit) == explicit
    assert explicit["UV_CACHE_DIR"] == str(storage_env["uv_cache"])

    preserved = runtime_bootstrap.same_volume_uv_cache(storage_env["mindie_data"] / "oc-venv", {})
    assert isinstance(preserved, dict)
    chosen = preserved.get("UV_CACHE_DIR")
    assert chosen is None or os.path.isabs(chosen), chosen


def test_reclamation_entry_points_never_raise_on_absent_state(storage_env: dict[str, Path]) -> None:
    absent = storage_env["root"] / "absent-config.json"
    assert storage_gc.prune_component_generations(absent)["status"] in {"absent", "completed"}
    assert storage_gc.prune_engine_snapshots(absent)["status"] == "absent"
    assert storage_gc.prune_spool_transcripts(storage_env["root"] / "absent-spool")["status"] == "absent"
    report = storage_gc.post_switch_reclaim(config=absent, budget_seconds=1.0)
    assert report["status"] in {"completed", "partial"}
    assert storage_gc.warning_text({"status": "completed", "warnings": []}) is None
    assert storage_gc.warning_text({"status": "partial", "warnings": ["TimeoutError: busy"]}) == (
        "storage cleanup partial: TimeoutError: busy"
    )


def _make_admission(state: Path, leases: dict[str, int]) -> None:
    """A minimal `mindie-admission/1` store with the given `enabled` flags."""
    import sqlite3

    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "admission.sqlite3") as db:
        db.execute(
            "CREATE TABLE leases(session TEXT PRIMARY KEY, token TEXT NOT NULL, enabled INTEGER NOT NULL,"
            " failures INTEGER NOT NULL, project_root TEXT, root_session TEXT, activated_at REAL,"
            " entry_enabled INTEGER NOT NULL DEFAULT 0)"
        )
        db.executemany("INSERT INTO leases (session, token, enabled, failures) VALUES (?, 't', ?, 0)", leases.items())


def _make_opencode_db(path: Path, session: str, rows: int) -> None:
    """A minimal OpenCode v2 transcript database with `rows` public user messages."""
    import sqlite3

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
        for seq in range(rows):
            stamp = created + seq
            db.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, 'user', ?, ?, ?)",
                (f"m_{seq}", session, seq, stamp, stamp, json.dumps({"time": {"created": stamp}, "text": f"turn {seq}"})),
            )


def test_cross_config_root_borrower_and_incomplete_pins_guard_legacy_sweep(
    storage_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #1: a default-home borrower survives, and unreadable pins skip the sweep."""
    import mindie_knowledge.storage as shared_storage

    cfg = storage_env["root"] / "custom-config" / "oc.json"
    runtimes = state_dir(config_path()) / "component-runtimes"
    active = _touch_generation(runtimes, "gen-active", age_seconds=90_000)
    borrowed = _touch_generation(runtimes, "gen-borrowed", age_seconds=85_000)
    for index in range(3):
        _touch_generation(runtimes, f"gen-stale-{index}", age_seconds=70_000 - index)
    active_py = str(active / "runtime/.venv" / _PYTHON_REL)
    _write_adapter(cfg, {"schema": "mindie-adapter-config/1", "harness": "opencode", "state_dir": str(runtimes.parent), "python": active_py})
    default_home = storage_env["home"] / ".config" / "mindie-agent"
    default_home.mkdir(parents=True, exist_ok=True)
    linked = storage_env["root"] / "borrowed-codex.json"
    linked.write_text(json.dumps({"python": str(borrowed / "runtime/.venv" / _PYTHON_REL)}), encoding="utf-8")
    (default_home / "codex.json").symlink_to(linked)

    calls: list[dict] = []
    original = shared_storage.sweep_legacy_installations

    def _spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(shared_storage, "sweep_legacy_installations", _spy)
    report = storage_gc.post_switch_reclaim(config=cfg, interpreter=active_py, budget_seconds=8.0)
    assert borrowed.is_dir() and active.is_dir(), report
    assert calls and len(calls[0]["config_home"]) >= 3, "a config home was dropped from the sweep scope"

    # An unreadable borrower config must skip the legacy sweep instead of sweeping unguarded.
    calls.clear()
    (default_home / "cc.json").write_text("{corrupt", encoding="utf-8")
    guarded = storage_gc.post_switch_reclaim(config=cfg, interpreter=active_py, budget_seconds=8.0)
    assert guarded["partial"] is True and calls == [], guarded
    assert borrowed.is_dir(), "an incomplete pin set still swept the borrowed generation"


def test_previous_python_survives_two_newer_unactivated_candidates(storage_env: dict[str, Path]) -> None:
    """P1 #2: the persisted rollback generation outranks mtime-only retention."""
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=str(storage_env["python"]), prepare_store=False)
    runtimes = state_dir(cfg) / "component-runtimes"
    prior = _touch_generation(runtimes, "gen-prior", age_seconds=90_000)
    previous = _touch_generation(runtimes, "gen-previous", age_seconds=80_000)
    candidate_a, candidate_b = (
        _touch_generation(runtimes, name, age_seconds=age) for name, age in (("gen-candidate-a", 5_000), ("gen-candidate-b", 4_000))
    )
    adapter = read_json(cfg)
    adapter["python"] = str(prior / "runtime/.venv" / _PYTHON_REL)
    adapter["previous_python"] = str(previous / "runtime/.venv" / _PYTHON_REL)
    _write_adapter(cfg, adapter)

    report = storage_gc.prune_component_generations(cfg, keep_recent_verified=1)
    assert report["status"] == "completed", report
    assert prior.is_dir() and previous.is_dir(), "the explicit rollback pointer was reclaimed"
    assert candidate_b.is_dir(), "the newest unactivated candidate must survive retention"
    assert not candidate_a.exists(), "an excess unactivated candidate must be reclaimed"


def test_engine_snapshot_scan_propagates_timeout_and_aborts_on_oserror(
    storage_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #3: an incomplete reference set never deletes a snapshot."""
    cfg = config_path()
    _write_adapter(cfg, {"schema": "mindie-adapter-config/1", "harness": "opencode", "python": str(Path(sys.executable))})
    generations = cfg.parent / "generations"
    snapshots = [_write_engine_snapshot(generations, f"oc.engine.hist{index}.json", age_seconds=90_000 - index) for index in range(4)]
    (cfg.parent / "borrower.json").write_text("{}", encoding="utf-8")
    original = storage_gc.read_json

    def _unreadable(path, *args, **kwargs):
        if Path(path).name == "borrower.json":
            raise PermissionError("borrower config unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(storage_gc, "read_json", _unreadable)
    aborted = storage_gc.prune_engine_snapshots(cfg, keep_recent=1)
    assert aborted["status"] == "partial" and aborted["removed"] == 0, aborted
    monkeypatch.setattr(storage_gc, "read_json", lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("config budget")))
    with pytest.raises(TimeoutError):
        storage_gc.prune_engine_snapshots(cfg, keep_recent=1)
    assert all(item.is_file() for item in snapshots)


def test_engine_snapshot_guards_reject_linked_git_and_user_source_roots(storage_env: dict[str, Path]) -> None:
    """P1 #4: linked ancestors, external Git trees and user roots are never unlinked."""
    cfg = config_path()
    adapter = {"schema": "mindie-adapter-config/1", "harness": "opencode", "python": str(Path(sys.executable))}
    _write_adapter(cfg, adapter)
    generations = cfg.parent / "generations"
    external = storage_env["root"] / "linked-generations"
    external.mkdir(parents=True)
    linked_snapshot = external / "oc.engine.link00.json"
    linked_snapshot.write_text("{}", encoding="utf-8")
    generations.symlink_to(external, target_is_directory=True)
    assert storage_gc.prune_engine_snapshots(cfg, keep_recent=0)["removed"] == 0
    generations.unlink()
    assert linked_snapshot.is_file(), "a snapshot behind a linked ancestor was unlinked"

    (cfg.parent / ".git").mkdir()
    git_snapshot = _write_engine_snapshot(generations, "oc.engine.git000.json", age_seconds=90_000)
    assert storage_gc.prune_engine_snapshots(cfg, keep_recent=0)["removed"] == 0
    (cfg.parent / ".git").rmdir()
    assert git_snapshot.is_file(), "a snapshot inside a Git checkout was unlinked"

    _write_adapter(cfg, dict(adapter, source_root=str(generations)))
    forbidden_snapshot = _write_engine_snapshot(generations, "oc.engine.user00.json", age_seconds=90_000)
    assert storage_gc.prune_engine_snapshots(cfg, keep_recent=0)["removed"] == 0
    assert forbidden_snapshot.is_file(), "a snapshot inside a user `source_root` was unlinked"


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock inode semantics")
def test_spool_lock_verifies_live_inode_and_unlinks_before_unlock(
    storage_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #5: a stale lock inode never blocks, and the live lock is unlinked first."""
    import fcntl
    import storage_spool

    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    session = "ses_lock_race_01"
    lock_file = spool / f".{session}.lock"
    stale = lock_file.open("a+b")
    fcntl.flock(stale.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.unlink(lock_file)
    lock_file.write_bytes(b"0")
    assert storage_gc.lock_inode_matches(stale, lock_file) is False
    with storage_gc._held_spool_lock(lock_file) as held:
        assert held is True, "the live lock inode was not acquired"
        probe = lock_file.open("a+b")
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    stale.close()

    (spool / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
    stamp = time.time() - 20 * 24 * 3600
    os.utime(spool / f"{session}.jsonl", (stamp, stamp))
    observed: list[bool] = []
    original_unlock = storage_spool._unlock_stream
    monkeypatch.setattr(
        storage_spool, "_unlock_stream",
        lambda handle: (observed.append(lock_file.exists()), original_unlock(handle))[1],
    )
    report = storage_gc.prune_spool_transcripts(spool, max_sessions=1024, budget_seconds=5.0)
    assert report["removed_sessions"] == [session], report
    assert observed == [False], "the lock file was unlinked after unlocking (inode race)"
    assert not lock_file.exists()


def test_spool_eviction_never_breaks_admitted_or_resumable_sessions(storage_env: dict[str, Path]) -> None:
    """P1 #6: only complete expired terminal bundles are retired, never a live cursor."""
    spool = transcript_spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    stamp = time.time() - 20 * 24 * 3600
    for session in ("ses_live_01", "ses_dead_01", "ses_plain_01"):
        (spool / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
        (spool / f"{session}.seq").write_text('{"seq":5,"not_before":null}\n', encoding="utf-8")
        os.utime(spool / f"{session}.jsonl", (stamp, stamp))
    _make_admission(spool.parent, {"ses_live_01": 1, "ses_dead_01": 0})

    report = storage_gc.prune_spool_transcripts(spool, max_sessions=1, budget_seconds=5.0)
    assert "ses_live_01" not in report["removed_sessions"]
    assert (spool / "ses_live_01.jsonl").is_file() and (spool / "ses_live_01.seq").is_file()
    for session in ("ses_dead_01", "ses_plain_01"):
        assert session in report["removed_sessions"], report
        assert not (spool / f"{session}.jsonl").exists()
        assert not (spool / f"{session}.seq").exists(), "a retired projection kept its stale cursor"

    db_file = storage_env["opencode_db"]
    _make_opencode_db(db_file, "ses_plain_01", 3)
    resumed = opencode_transcript.sync_session_spool("ses_plain_01", db_path=db_file, spool_dir=spool, max_seconds=5.0)
    rows = [json.loads(line) for line in resumed.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in rows] == [0, 1, 2], "a resumed session skipped its retired prefix"
    (spool.parent / "admission.sqlite3").write_text("not-a-database", encoding="utf-8")
    unknown = storage_gc.prune_spool_transcripts(spool, max_sessions=0, budget_seconds=5.0)
    assert unknown["status"] == "partial" and unknown["removed_sessions"] == []


def test_post_switch_cleanup_exception_keeps_updated_status(
    storage_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 #7: a committed switch is never downgraded by post-commit cleanup."""
    cfg = config_path()
    setup.ensure_default_profile(config=cfg, python_exe=str(storage_env["python"]), prepare_store=False)
    monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
    monkeypatch.setattr(component_update, "sync_store_schema", lambda *_a, **_kw: (True, None))
    monkeypatch.setattr(component_update, "compatible_runtime", lambda interpreter, **_kw: component_update._norm_python_path(interpreter))
    monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_kw: {"mindie-knowledge": {"version": "0.13.22"}})

    for exc, version in ((PermissionError("EACCES component-runtimes"), "0.1.12"), (TimeoutError("cleanup deadline"), "0.1.13")):
        before = read_json(cfg)
        candidate = _stage_candidate(storage_env["root"] / f"candidate-{version}", version=version)
        monkeypatch.setattr(storage_gc, "post_switch_reclaim", lambda **_kw: (_ for _ in ()).throw(exc))
        result = component_update.check(config=cfg, source=candidate)
        assert result["status"] == "updated" and result["needs_host_reload"] is True, result
        assert type(exc).__name__ in result["cleanup_warning"], result
        assert "receipt_unconfirmed" not in result
        switched = read_json(cfg)
        assert switched["product_validation"]["native_version"] == version
        assert switched["previous_python"] == before["python"]
        assert switched["previous_generation"] == before["engine_config"]


def test_core_sources_stay_under_the_line_cap() -> None:
    for source in CORE_MODULES:
        raw = source.read_bytes()
        assert b"\r\n" not in raw, f"CRLF detected in {source}"
        lines = raw.decode("utf-8").splitlines()
        assert len(lines) < 800, f"{source.name} reached the 800-line cap ({len(lines)})"
        head = "\n".join(lines[:35])
        for section in ("[Role]", "[Invariants]", "[Timeouts & Locks]", "[Side-Effect Policy]"):
            assert section in head, f"{source.name} is missing the {section} contract header"
    for source in TIGHT_MODULES:
        assert len(source.read_bytes().decode("utf-8").splitlines()) < 785, f"{source.name} is not safely below 800 lines"
