"""Cutover lock contention, bounded schema supervision, and timeout diagnostic tests.

[Role]
Verifies profile cutover lock enforcement (`oc-profile-schema.lock`), bounded
subprocess supervision when `SIGALRM` cannot preempt `prepare_schema`, and
persisted `TimeoutError` schema diagnostics across `oc.json`,
`oc-component-update-status.json`, and `oc-schema-prepare.json`.

[Invariants]
- `_write_generation_and_switch_profile` never replaces `oc.json` when
  `oc-profile-schema.lock` is held by another process.
- When `SIGALRM` is unavailable or non-default (including Windows),
  `sync_store_schema` enforces its deadline via `bounded.run` subprocess
  supervision and persists `TimeoutError` warnings before propagating.

[Timeouts & Locks]
- Tier 1 real OS file-lock and `StartLock` boundary tests (`< 15s` total).

[Side-Effect Policy]
- Strictly isolated inside `isolated_oc_env` (`tmp_path`).
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import pytest

import admission
import component_update
from paths import config_path, data_home, engine_config_path, read_json, state_dir
import setup

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"


def _provision_stub_gitleaks(home: Path) -> Path:
    stub = home / ".cache" / "mindie-agent" / "gitleaks" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_bytes(b"stub")
    return stub


@contextmanager
def _held_os_update_lock(py: str, lock_file: Path, ready_file: Path):
    code = (
        f"import pathlib, sys, time; sys.path.insert(0, {str(SCRIPTS_DIR)!r}); import component_update; "
        "lf, rf = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]); "
        "ctx = component_update._update_lock(lf, blocking_timeout=5.0); got = ctx.__enter__(); "
        "assert got is True; rf.write_text('locked\\n', encoding='utf-8'); "
        "sys.stdin.read(); ctx.__exit__(None, None, None)"
    )
    proc = subprocess.Popen(
        [py, "-I", "-c", code, str(lock_file), str(ready_file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ),
    )
    try:
        dl = time.monotonic() + 5.0
        while not ready_file.is_file() and time.monotonic() < dl:
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else "lock holder exited"
            time.sleep(0.03)
        assert ready_file.is_file()
        yield
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


@contextmanager
def _held_layout_start_lock(py: str, lock_file: Path, ready_file: Path):
    code = (
        "import pathlib, sys; from mindie_knowledge.loop.locks import StartLock; "
        "lf, rf = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]); lf.parent.mkdir(parents=True, exist_ok=True); "
        "lk = StartLock(lf); lk.acquire(wait=5.0); rf.write_text('locked\\n', encoding='utf-8'); "
        "sys.stdin.read(); lk.release()"
    )
    proc = subprocess.Popen(
        [py, "-I", "-c", code, str(lock_file), str(ready_file)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ),
    )
    try:
        dl = time.monotonic() + 5.0
        while not ready_file.is_file() and time.monotonic() < dl:
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else "StartLock holder exited"
            time.sleep(0.03)
        assert ready_file.is_file()
        yield
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


def test_cutover_defers_under_profile_schema_lock_contention(
    isolated_oc_env: dict[str, Path],
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    _provision_stub_gitleaks(isolated_oc_env["home"])
    v1_dir = isolated_oc_env["root"] / "src-v1"
    v2_dir = isolated_oc_env["root"] / "src-v2"
    setup.stage_source_baseline(REPO_ROOT, dest_dir=v1_dir)
    setup.stage_source_baseline(REPO_ROOT, dest_dir=v2_dir)
    for rel, key in (("package.json", "version"), ("plugins/mindie-agent/plugin.json", "version"), ("product-contract.json", "native_version")):
        p = v2_dir / rel
        d = json.loads(p.read_text(encoding="utf-8"))
        d[key] = "0.1.9"
        p.write_text(json.dumps(d) + "\n", encoding="utf-8")

    setup.ensure_default_profile(config=cfg, source_root=v1_dir, python_exe=py)
    before = read_json(cfg)
    assert before["product_validation"]["native_version"] == "0.1.2"

    lock_file = state_dir(cfg) / "oc-profile-schema.lock"
    ready_file = isolated_oc_env["root"] / "schema-lock.ready"
    val_v2 = setup.validate_contracts(v2_dir)
    with _held_os_update_lock(py, lock_file, ready_file):
        with pytest.raises(TimeoutError, match="profile schema lock"):
            component_update._write_generation_and_switch_profile(
                cfg_file=cfg,
                base_adapter=before,
                base_engine=read_json(engine_config_path(cfg)),
                target_source=v2_dir,
                target_python=py,
                validation=val_v2,
                artifact=None,
                lock_timeout=0.15,
            )
    after = read_json(cfg)
    assert after["product_validation"]["native_version"] == "0.1.2"
    assert after["engine_config"] == before["engine_config"]


def test_bounded_subprocess_schema_timeout_and_persisted_diagnostics(
    isolated_oc_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py = str(isolated_oc_env["python"])
    cfg = config_path()
    _provision_stub_gitleaks(isolated_oc_env["home"])
    setup.ensure_default_profile(config=cfg, python_exe=py, prepare_store=False)
    assert read_json(cfg)["schema_ready"] is False

    # Force the non-alarm (`bounded.run` subprocess) path on POSIX too so Windows supervision is tested everywhere.
    prev_handler = signal.getsignal(signal.SIGALRM) if hasattr(signal, "SIGALRM") else None
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_a: None)
    try:
        layout_lock = data_home() / "domains" / "vllm-ascend" / "state-layout.lock"
        ready_file = isolated_oc_env["root"] / "layout-lock.ready"
        prep_status = data_home() / "oc-schema-prepare.json"
        upd_status = data_home() / "oc-component-update-status.json"
        monkeypatch.setattr(component_update, "prepare", lambda **_kw: None)
        monkeypatch.setattr(component_update, "read_inventory", lambda *_a, **_kw: {"mindie-knowledge": {"version": "0.13.16"}})

        with _held_layout_start_lock(py, layout_lock, ready_file):
            # 1. Standalone `--prepare-schema` enforces bounded subprocess timeout, writes degraded status, and persists warning.
            t0 = time.monotonic()
            with pytest.raises(TimeoutError):
                component_update.main(["--config", str(cfg), "--prepare-schema", "--timeout", "5.8"])
            assert (time.monotonic() - t0) < 9.5
            assert read_json(prep_status)["status"] == "degraded"
            assert "TimeoutError" in read_json(prep_status)["schema_warning"]
            assert "TimeoutError" in read_json(cfg)["schema_warning"]
            assert "TimeoutError" in admission.status_facts("ses_t01", config=cfg)["schema_warning"]

            # 2. `component_update.check` preserves completed `up_to_date` installation status while persisting timeout schema_warning.
            with pytest.raises(TimeoutError):
                component_update.check(config=cfg, timeout=6.2)
            assert read_json(upd_status)["status"] == "up_to_date"
            assert "TimeoutError" in read_json(upd_status)["schema_warning"]

        # 3. Once `state-layout.lock` is released, recovery clears persisted warnings across all status files.
        rec = component_update.check(config=cfg)
        assert rec["status"] == "up_to_date" and "schema_warning" not in rec
        assert read_json(cfg)["schema_ready"] is True and "schema_warning" not in read_json(cfg)
        assert "schema_warning" not in read_json(upd_status)
    finally:
        if hasattr(signal, "SIGALRM") and prev_handler is not None:
            signal.signal(signal.SIGALRM, prev_handler)
