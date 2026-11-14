#!/usr/bin/env python3
"""Independent shared component runtime updater for the OpenCode (`oc`) adapter.

[Role]
Discovers newer formal GitHub Releases of shared MindIE runtime components and
builds isolated component generations under `state_dir()/component-runtimes`,
promoting verified native plugin generations atomically via `os.replace()`.

[Invariants]
- Monotonic Component Floor: `discover()` selects `max(installed, baseline,
  latest_formal_release)` and never downgrades an installed newer component.
- Candidate-First Atomic Switch: candidate staging, contract validation, runtime
  verification, `prepare()`, and `oc.engine.<gen>.json` complete before flipping `oc.json`.
- Single-Instance Lock & State Honesty: serializes via `oc-component-update.lock`,
  reclaims dead-PID `in_progress` status, and reports `read_inventory` failures as `"failed"`.

[Timeouts & Locks]
- Shared `540.0s` deadline (`15s` metadata, `120s` catalog, `420s` `uv` build, `5s` reserve).

[Side-Effect Policy]
- Writes candidate generations under `state_dir()/component-runtimes` and flips `oc.json` last.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from oc_native_compat import ensure_oc_native_support
from paths import (
    admission_path,
    apply_enterprise_transport_env,
    atomic_write_json,
    community_config_path,
    config_path,
    consent_path,
    data_home,
    engine_config_path,
    installed_source_root,
    read_json,
    repo_root,
    state_dir,
)

ensure_oc_native_support()

UPDATE_CHECK_INTERVAL = 300.0
UPDATE_FAILURE_BACKOFF = 60.0
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(raw: str | None) -> tuple[int, int, int] | None:
    if not isinstance(raw, str):
        return None
    m = _SEMVER_RE.fullmatch(raw.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


@contextmanager
def _update_lock(lock_file: Path, *, blocking_timeout: float = 0.0):
    """Acquire an OS file lock (`flock` / `msvcrt.locking`); yields `True` if acquired."""
    lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = lock_file.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0.0, float(blocking_timeout))
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if lock_file.stat().st_size == 0:
                        handle.write(b"0")
                        handle.flush()
                        handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except TimeoutError:
                raise
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except TimeoutError:
                raise
            except OSError:
                pass
        handle.close()


def _pid_running_windows(pid: int) -> bool:
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code)) and int(code.value) == 259)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _pid_running(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_running_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except TimeoutError:
        raise
    except PermissionError:
        return True
    except OSError:
        return False


def _stop_untracked_updater(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def read_inventory(python: str, *, timeout: float = 15.0) -> dict:
    from mindie_knowledge.component_runtime import read_inventory as _core_read_inventory

    return _core_read_inventory(python, timeout=timeout)


def trigger_background_update_if_due(*, config: str | Path | None = None) -> bool:
    """Spawn a non-blocking background `component_update.py` check if the interval has elapsed."""
    if os.environ.get("MINDIE_CONFIG_HOME") is not None and os.environ.get("MINDIE_OC_ALLOW_BG_UPDATE") != "1":
        return False
    try:
        cfg_file = config_path(config)
        if not cfg_file.is_file():
            return False
        status_file = data_home() / "oc-component-update-status.json"
        now = time.time()
        if status_file.is_file():
            data = read_json(status_file)
            checked = float(data.get("checked_at") or 0.0)
            st = data.get("status")
            if st == "in_progress":
                if _pid_running(data.get("pid")) and (now - checked) < 540.0:
                    return False
                print(
                    f"MindIE OpenCode component_update reclaiming leftover in_progress status (pid={data.get('pid')})",
                    file=sys.stderr,
                )
            else:
                interval = UPDATE_FAILURE_BACKOFF if st == "failed" else UPDATE_CHECK_INTERVAL
                if now - checked < interval:
                    return False
        adapter = read_json(cfg_file)
        py_exe = adapter.get("python") or sys.executable
        installed_root = installed_source_root()
        target_script = (
            installed_root / "plugins" / "mindie-agent" / "scripts" / "component_update.py"
            if installed_root is not None
            and (installed_root / "plugins" / "mindie-agent" / "scripts" / "component_update.py").is_file()
            else Path(__file__).resolve()
        )
        argv = [str(py_exe), "-I", str(target_script), "--config", str(cfg_file), "--non-blocking"]
        if installed_root is not None:
            argv.extend(["--source", str(installed_root)])
        no_breakaway_marker = os.environ.get("_MINDIE_OC_BG_NO_BREAKAWAY")
        env = apply_enterprise_transport_env(dict(os.environ))
        for strip_key in ("_MINDIE_OC_PGID_FILE", "_MINDIE_OC_PGID_FILES", "_MINDIE_OC_BG_NO_BREAKAWAY", "_MINDIE_OC_REEXEC", "_MINDIE_OC_SUPERVISOR_DEADLINE"):
            env.pop(strip_key, None)
        kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, env=env)
        if os.name == "nt":
            # Never combine DETACHED_PROCESS with CREATE_NO_WINDOW on Windows (P1.5).
            base_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            kwargs["creationflags"] = base_flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            try:
                proc = subprocess.Popen(argv, **kwargs)
            except TimeoutError:
                raise
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    raise
                if no_breakaway_marker:
                    Path(no_breakaway_marker).write_text("1\n", encoding="utf-8", newline="\n")
                kwargs["creationflags"] = base_flags
                proc = subprocess.Popen(argv, **kwargs)
        else:
            kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs)
        try:
            atomic_write_json(status_file, {"checked_at": now, "status": "in_progress", "pid": proc.pid})
        except BaseException:
            _stop_untracked_updater(proc)
            raise
        return True
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"MindIE OpenCode background component_update trigger failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _norm_python_path(py_exe: str | Path) -> str:
    p = Path(py_exe).expanduser()
    return str(p.parent.resolve() / p.name)


def compatible_runtime(python: str, *, source=None, config=None, timeout: float = 540.0) -> str:
    """Prepare a compatible generation without changing the active profile."""
    if timeout <= 0:
        raise TimeoutError("runtime compatibility probe exceeded its budget")
    deadline = time.monotonic() + float(timeout)
    norm_py = _norm_python_path(python)
    req_mods = ("mindie_knowledge", "remote_dev", "mindie_diagnostics", "mindie_profiler", "mindie_sim", "mindie_coordinator", "mindie_debugger", "npu_top")
    if norm_py == _norm_python_path(sys.executable) and all(importlib.util.find_spec(m) is not None for m in req_mods) and importlib.util.find_spec("remote_dev.mcp.outcome") is not None:
        return norm_py
    imports = f"import {', '.join(req_mods)}; from remote_dev.mcp.outcome import operation_outcome"

    def probe(interpreter):
        rem = min(15.0, deadline - time.monotonic())
        grace = min(1.5, max(0.15, rem * 0.25))
        budget = rem - grace
        if budget <= 0.05:
            raise TimeoutError("runtime compatibility probe exceeded its budget")
        return bounded_run([str(interpreter), "-I", "-c", imports], "", timeout=budget, cleanup_grace=grace).returncode == 0

    if probe(python):
        return norm_py
    remaining = deadline - time.monotonic()
    if remaining <= 20.0:
        raise TimeoutError("runtime preparation has no remaining build budget")
    command = [
        str(python), "-I", str(Path(__file__).resolve()), "--prepare-only", "--python", str(python),
        "--source", str(source or repo_root(config)), "--config", str(config_path(config)), "--timeout", str(remaining - 20.0),
    ]
    result = bounded_run(command, "", timeout=remaining - 15.0, max_output=256 * 1024, env=apply_enterprise_transport_env(dict(os.environ)))
    if result.returncode != 0:
        raise RuntimeError(f"runtime candidate preparation failed: {(result.stderr or result.stdout)[-1000:]}")
    artifact = json.loads(result.stdout)
    candidate = artifact.get("python") if isinstance(artifact, dict) else None
    if not candidate or not probe(candidate):
        raise RuntimeError("runtime candidate does not provide the plugin APIs")
    return _norm_python_path(candidate)


def prepare(
    source: str | Path | None = None, python: str | Path | None = None, *,
    config: str | Path | None = None, force: bool = False, timeout: float = 540.0,
) -> dict | None:
    apply_enterprise_transport_env()
    from mindie_knowledge.component_bindings import rebind_commands, required_components
    from mindie_knowledge.component_releases import Catalog
    from mindie_knowledge.component_runtime import build, discover, source_baseline

    deadline = time.monotonic() + float(timeout)

    def remaining(reserve: float = 0.0) -> float:
        avail = deadline - time.monotonic() - reserve
        if avail <= 0:
            raise TimeoutError("OpenCode component update exceeded its total budget")
        return avail

    cfg_file = config_path(config)
    adapter = read_json(cfg_file) if cfg_file.is_file() else {}
    source_root = Path(source or repo_root(cfg_file)).resolve()
    current_python = _norm_python_path(python or adapter.get("python") or sys.executable)

    required = required_components(adapter, timeout=min(15.0, remaining(5.0)))
    baseline = source_baseline(source_root)
    selected = discover(
        current_python,
        baseline["runtime"],
        force=force,
        required_components=required,
        catalog=Catalog(budget=min(120.0, remaining(20.0))),
    )
    needs_binding = bool(adapter.get("component_commands")) and (
        rebind_commands(adapter, current_python, selected["runtime"]) != adapter.get("component_commands")
    )
    installed = selected["installed"]
    needs_build = installed != selected["runtime"]
    if not needs_build and not needs_binding:
        return None
    if (
        not needs_binding
        and {name: row["revision"] for name, row in selected["runtime"].items()} == baseline["runtime"]
        and not selected["changed"]
    ):
        return None

    return build(
        source_root,
        state_dir(cfg_file) / "component-runtimes",
        baseline,
        selected["runtime"],
        python=current_python,
        timeout=min(420.0, remaining(5.0)),
        env=apply_enterprise_transport_env(dict(os.environ)),
        files=["pyproject.toml", "product-contract.json", "harness-compatibility.json"],
    )


def _candidate_plugin_is_newer(candidate_source: Path, adapter: dict) -> bool:
    manifest = candidate_source / "plugins" / "mindie-agent" / "plugin.json"
    if not manifest.is_file() or not (candidate_source / "pyproject.toml").is_file():
        return False
    cand_ver = _parse_semver(read_json(manifest).get("version"))
    active_ver = _parse_semver((adapter.get("product_validation") or {}).get("native_version"))
    if cand_ver is None:
        return False
    return active_ver is None or cand_ver > active_ver


def _find_gitleaks_executable() -> str | None:
    found = shutil.which("gitleaks")
    if found:
        return found
    cache_dir = Path.home() / ".cache" / "mindie-agent" / "gitleaks"
    if cache_dir.is_dir():
        for candidate in sorted(cache_dir.rglob("gitleaks*"), reverse=True):
            if candidate.is_file() and candidate.name in {"gitleaks", "gitleaks.exe"}:
                return str(candidate.resolve())
    return None


def _write_generation_and_switch_profile(
    *, cfg_file: Path, base_adapter: dict, base_engine: dict,
    target_source: Path, target_python: str, validation: dict, artifact: dict | None,
    lock_timeout: float = 5.0,
) -> Path:
    """Write generation-isolated `oc.engine.<gen>.json` FIRST, then atomically flip `oc.json`."""
    from mindie_knowledge.component_bindings import rebind_commands

    sdir = Path(base_adapter["state_dir"]) if isinstance(base_adapter.get("state_dir"), str) else state_dir(cfg_file)
    adm_file = Path(base_adapter["admission_path"]) if isinstance(base_adapter.get("admission_path"), str) else admission_path(cfg_file)
    comm_file, data_root = community_config_path(cfg_file), data_home() / "domains"
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    scripts_dir = (target_source / "plugins/mindie-agent/scripts").resolve()
    domain = str(base_adapter.get("domain") or "vllm-ascend")
    gen_id = hashlib.sha256(
        json.dumps(
            {
                "python": target_python, "source_root": str(target_source), "native_version": validation.get("native_version"),
                "components": artifact.get("components") if isinstance(artifact, dict) else base_adapter.get("component_runtime"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    gen_eng_file = cfg_file.parent / "generations" / f"{cfg_file.stem}.engine.{gen_id}.json"
    eng_payload = dict(
        base_engine, schema="mindie-engine-config/1", harness="opencode", domain=domain,
        root=str(base_engine.get("root") or data_root), state_dir=str(base_engine.get("state_dir") or (sdir / "engine")),
        admission_path=str(adm_file), community_config=str(comm_file), settings_path=str(comm_file),
        transcript_adapter=str(scripts_dir / "opencode_transcript.py"),
        summary_command=[target_python, "-I", str(scripts_dir / "organizer.py")],
        redactor_executable=base_engine.get("redactor_executable") or _find_gitleaks_executable(),
        feeds=base_engine.get("feeds") or [{"repository": "mindie-agent/knowledge-vllm-ascend", "ref": "main", "domain": domain, "interval_seconds": 300}],
        product_validation=validation,
    )
    # Step 1: Write isolated candidate engine config BEFORE touching active `oc.json`.
    atomic_write_json(gen_eng_file, eng_payload)

    updated_adapter = dict(
        base_adapter, schema="mindie-adapter-config/1", harness="opencode", python=target_python,
        runtime_scripts=str(scripts_dir), source_root=str(target_source), engine_config=str(gen_eng_file),
        state_dir=str(sdir), admission_path=str(adm_file), domain=domain, product_validation=validation,
        schema_ready=False,
    )
    if artifact is not None:
        updated_adapter["component_runtime"] = artifact["components"]
        if updated_adapter.get("component_commands"):
            updated_adapter["component_commands"] = rebind_commands(
                updated_adapter, target_python, artifact["components"]["runtime"],
            )

    # Step 2: Single atomic pointer switch under profile schema lock (defer before publishing on contention).
    with _update_lock(sdir / "oc-profile-schema.lock", blocking_timeout=max(0.05, float(lock_timeout))) as got:
        if not got:
            raise TimeoutError("timed out acquiring profile schema lock before publishing oc.json")
        atomic_write_json(cfg_file, updated_adapter)

    # Best-effort legacy mirror for external callers reading `<stem>.engine.json` directly.
    default_eng = cfg_file.with_name(cfg_file.stem + ".engine.json")
    if default_eng != gen_eng_file:
        try:
            atomic_write_json(default_eng, eng_payload)
        except TimeoutError:
            raise
        except OSError:
            pass
    return gen_eng_file


def store_schema_needs_prepare(cfg_file: str | Path | None = None, eng_file: str | Path | None = None) -> bool:
    cpath = config_path(cfg_file)
    epath = Path(eng_file) if eng_file is not None else engine_config_path(cpath)
    if not cpath.is_file() or not epath.is_file():
        return False
    try:
        adapter, eng = read_json(cpath), read_json(epath)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return True
    if not adapter.get("schema_ready") or bool(adapter.get("schema_warning")):
        return True
    root_dir, dom = eng.get("root"), eng.get("domain")
    if not isinstance(root_dir, str) or not isinstance(dom, str):
        return False
    try:
        if importlib.util.find_spec("mindie_knowledge") is not None:
            from mindie_knowledge.state_layout import DATABASE, state_root

            return not (state_root(root_dir, dom) / DATABASE).is_file()
        fmt = read_json(Path(root_dir) / dom / "state-layout.json").get("format")
        return type(fmt) is not int or not (Path(root_dir) / dom / f"state-v{fmt}" / "state-v4.sqlite3").is_file()
    except TimeoutError:
        raise
    except Exception:
        return True


def _persist_schema_state(cpath: Path, epath: Path, warn: str | None, *, update_status_file: bool) -> None:
    try:
        with _update_lock(state_dir(cpath) / "oc-profile-schema.lock", blocking_timeout=2.0) as got:
            if got and cpath.is_file():
                cur = read_json(cpath)
                cur_eng = Path(cur["engine_config"]) if isinstance(cur.get("engine_config"), str) else engine_config_path(cpath)
                if cur_eng.resolve() == epath.resolve():
                    cur["schema_ready"] = warn is None
                    cur.pop("schema_warning", None) if warn is None else cur.__setitem__("schema_warning", warn)
                    atomic_write_json(cpath, cur)
            if got and update_status_file:
                st_file = data_home() / "oc-component-update-status.json"
                if st_file.is_file():
                    st_cur = read_json(st_file)
                    if not (st_cur.get("status") == "in_progress" and _pid_running(st_cur.get("pid"))):
                        if warn is None and "schema_warning" in st_cur:
                            st_cur.pop("schema_warning", None)
                            atomic_write_json(st_file, st_cur)
                        elif warn is not None:
                            st_cur["schema_warning"] = warn
                            atomic_write_json(st_file, st_cur)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        pass


def sync_store_schema(
    cfg_file: str | Path | None = None,
    eng_file: str | Path | None = None,
    *,
    interpreter: str | None = None,
    deadline: float | None = None,
    update_status_file: bool = True,
) -> tuple[bool, str | None]:
    cpath = config_path(cfg_file)
    epath = Path(eng_file) if eng_file is not None else engine_config_path(cpath)
    if not epath.is_file():
        return False, None
    py = interpreter or (read_json(cpath).get("python") if cpath.is_file() else sys.executable)
    can_alarm = (
        hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
        and signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
    )
    same_py = not isinstance(py, str) or _norm_python_path(py) == _norm_python_path(sys.executable)
    in_proc = importlib.util.find_spec("mindie_knowledge") is not None and same_py and (deadline is None or can_alarm)
    rem = None if deadline is None else (deadline - time.monotonic())
    min_budget = 5.2 if in_proc else 5.6
    if rem is not None and rem < min_budget:
        err = TimeoutError(f"insufficient budget ({rem:.2f}s) for store schema preparation")
        _persist_schema_state(cpath, epath, f"TimeoutError: {err}", update_status_file=update_status_file)
        raise err
    if not in_proc and (not isinstance(py, str) or not Path(py).is_file()):
        miss_w = f"FileNotFoundError: runtime interpreter not found ({py})"
        _persist_schema_state(cpath, epath, miss_w, update_status_file=update_status_file)
        return False, miss_w
    warn: str | None = None
    timed_out: TimeoutError | None = None
    if in_proc:
        armed_alarm = rem is not None and can_alarm
        if armed_alarm:
            signal.signal(signal.SIGALRM, lambda *_a: (_ for _ in ()).throw(TimeoutError("store schema preparation timed out")))
            signal.setitimer(signal.ITIMER_REAL, max(0.1, float(rem)), 0.0)
        try:
            from mindie_knowledge.loop.handoff import prepare_schema

            prepare_schema(str(epath))
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("store schema preparation exceeded its deadline")
        except TimeoutError as exc:
            timed_out, warn = exc, f"TimeoutError: {exc}"
            print(f"MindIE OpenCode prepare_schema warning: {warn}", file=sys.stderr)
        except Exception as exc:
            warn = f"{type(exc).__name__}: {exc}"
            print(f"MindIE OpenCode prepare_schema warning: {warn}", file=sys.stderr)
        finally:
            if armed_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0.0, 0.0)
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
    else:
        grace = min(2.0, max(0.5, (rem - 5.0) if rem is not None else 2.0))
        sub_cmd = "import sys; sys.path.insert(0, sys.argv[1]); from oc_native_compat import ensure_oc_native_support; ensure_oc_native_support(); from mindie_knowledge.loop.handoff import prepare_schema; prepare_schema(sys.argv[2])"
        try:
            proc = bounded_run(
                [str(py), "-I", "-c", sub_cmd, str(HERE), str(epath)],
                "", timeout=max(5.0, (rem - grace) if rem is not None else 75.0), cleanup_grace=grace,
            )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("store schema preparation exceeded its deadline")
            if proc.returncode != 0:
                warn = f"RuntimeError: {(proc.stderr or proc.stdout).strip()[-400:] or f'exit {proc.returncode}'}"
                print(f"MindIE OpenCode prepare_schema warning: {warn}", file=sys.stderr)
        except TimeoutError as exc:
            timed_out, warn = exc, f"TimeoutError: {exc}"
            print(f"MindIE OpenCode prepare_schema warning: {warn}", file=sys.stderr)
        except Exception as exc:
            warn = f"{type(exc).__name__}: {exc}"
            print(f"MindIE OpenCode prepare_schema warning: {warn}", file=sys.stderr)
    _persist_schema_state(cpath, epath, warn, update_status_file=update_status_file)
    if timed_out is not None:
        raise timed_out
    return (warn is None), warn


def check(
    *, config: str | Path | None = None, source: str | Path | None = None,
    force: bool = False, sync_feeds: bool = False, non_blocking: bool = False, timeout: float = 540.0,
) -> dict:
    apply_enterprise_transport_env()
    from setup import _write_initial_community_config, ensure_runtime_python, stage_source_baseline, validate_contracts

    if timeout <= 0:
        raise TimeoutError("OpenCode component update timeout must be positive")
    deadline = time.monotonic() + float(timeout)

    def remaining(reserve: float = 0.0) -> float:
        avail = deadline - time.monotonic() - reserve
        if avail <= 0:
            raise TimeoutError("OpenCode component update exceeded its total budget")
        return avail

    cfg_file = config_path(config)
    lock_file = data_home() / "oc-component-update.lock"
    lock_wait = 0.0 if non_blocking else min(5.0, max(0.0, deadline - time.monotonic()))
    with _update_lock(lock_file, blocking_timeout=lock_wait) as acquired:
        if not acquired:
            return dict(schema="mindie-component-update/1", harness="opencode", status="locked", needs_host_reload=False)

        candidate_checkout = (
            Path(source).resolve() if source is not None else (installed_source_root() or HERE.parent.parent.parent.resolve())
        )
        status_file = data_home() / "oc-component-update-status.json"
        profile_existed = cfg_file.is_file()
        existing_adapter = read_json(cfg_file) if profile_existed else {}
        existing_eng_file = engine_config_path(cfg_file)
        existing_engine = read_json(existing_eng_file) if existing_eng_file.is_file() else {}
        native_candidate = (not profile_existed) or _candidate_plugin_is_newer(candidate_checkout, existing_adapter)

        try:
            if native_candidate:
                # Stage immutable candidate snapshot & validate contracts WITHOUT touching active `oc.json`.
                target_source = stage_source_baseline(source_dir=candidate_checkout)
                validation = validate_contracts(target_source)
                base_python = ensure_runtime_python(
                    source_root=target_source,
                    python_exe=existing_adapter.get("python") if isinstance(existing_adapter.get("python"), str) else None,
                    config=cfg_file, timeout=remaining(10.0),
                )
            else:
                configured_sr = existing_adapter.get("source_root")
                target_source = (
                    Path(configured_sr).resolve()
                    if isinstance(configured_sr, str) and os.path.isabs(configured_sr) and (Path(configured_sr) / "pyproject.toml").is_file()
                    else repo_root(cfg_file)
                )
                validation = validate_contracts(target_source)
                base_python = str(existing_adapter.get("python") or sys.executable)

            artifact = prepare(source=target_source, python=base_python, config=cfg_file, force=force, timeout=remaining(5.0))
            target_python = _norm_python_path(artifact["python"] if artifact is not None else base_python)
            inventory_error: str | None = None
            if artifact is not None:
                installed: dict | None = artifact["installed"]
            else:
                try:
                    installed = read_inventory(target_python, timeout=min(15.0, remaining(0.0)))
                except TimeoutError:
                    raise
                except Exception as exc:
                    installed, inventory_error = None, f"{type(exc).__name__}: {exc}"

            if inventory_error is not None:
                active_eng_file, status = existing_eng_file, "failed"
            elif artifact is not None or native_candidate:
                active_eng_file = _write_generation_and_switch_profile(
                    cfg_file=cfg_file, base_adapter=existing_adapter, base_engine=existing_engine,
                    target_source=target_source, target_python=target_python, validation=validation, artifact=artifact,
                    lock_timeout=min(5.0, remaining(0.0)),
                )
                comm_file = community_config_path(cfg_file)
                if not comm_file.is_file():
                    _write_initial_community_config(
                        target_python, comm_file,
                        dict(
                            enabled=False, repository="mindie-agent/knowledge-vllm-ascend",
                            project_roots=[], branch="main", idle_seconds=300, consent_config=str(consent_path(cfg_file).resolve()),
                        ),
                    )
                status = "updated"
            else:
                active_eng_file, status = existing_eng_file, "up_to_date"
        except Exception as exc:
            import diagnostic_support

            diag = diagnostic_support.failure("component.update", "prepare", "update", exception=exc, config=cfg_file)
            try:
                atomic_write_json(
                    status_file,
                    {"checked_at": time.time(), "status": "failed", "error": f"{type(exc).__name__}: {exc}", "diagnostic": diag},
                )
            except TimeoutError:
                raise
            except OSError:
                pass
            raise

        needs_reload = status == "updated"
        result: dict = dict(
            schema="mindie-component-update/1", harness="opencode", status=status,
            needs_host_reload=needs_reload, python=target_python, installed=installed,
        )
        if inventory_error is not None:
            result["inventory_error"] = inventory_error
        schema_warn: str | None = None
        post_timeout: TimeoutError | None = None
        if status in {"updated", "up_to_date"} and active_eng_file.is_file():
            needs_schema = status == "updated" or store_schema_needs_prepare(cfg_file, active_eng_file)
            if needs_schema:
                try:
                    _, schema_warn = sync_store_schema(
                        cfg_file, active_eng_file, interpreter=target_python, deadline=deadline, update_status_file=False,
                    )
                except TimeoutError as exc:
                    post_timeout, schema_warn = exc, f"TimeoutError: {exc}"
            elif cfg_file.is_file():
                try:
                    prev_w = read_json(cfg_file).get("schema_warning")
                    schema_warn = prev_w if isinstance(prev_w, str) and prev_w else None
                except TimeoutError:
                    raise
                except (OSError, ValueError):
                    pass
        if schema_warn is not None:
            result["schema_warning"] = schema_warn
        if post_timeout is None and sync_feeds and status != "failed" and community_config_path(cfg_file).is_file() and active_eng_file.is_file():
            try:
                from mindie_knowledge.loop.cli import sync_feeds as run_sync_feeds

                result["feed_sync"] = run_sync_feeds(str(active_eng_file))
            except TimeoutError as exc:
                post_timeout = exc
            except Exception as exc:
                result["feed_sync_error"] = f"{type(exc).__name__}: {exc}"
        status_record: dict = {
            "checked_at": time.time(), "status": status, "needs_host_reload": needs_reload, "python": target_python,
        }
        if inventory_error is not None:
            status_record["error"] = inventory_error
        if schema_warn is not None:
            status_record["schema_warning"] = schema_warn
        try:
            atomic_write_json(status_file, status_record)
        except TimeoutError:
            raise
        except OSError:
            pass
        if post_timeout is not None:
            raise post_timeout
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update MindIE shared runtime components for OpenCode")
    parser.add_argument("--config", default=None, help="Path to oc.json")
    parser.add_argument("--source", default=None, help="Path to mindie-agent-oc checkout")
    parser.add_argument("--prepare-only", action="store_true", help="Build a candidate without activating it")
    parser.add_argument("--prepare-schema", action="store_true", help="Prepare and migrate the active knowledge store schema")
    parser.add_argument("--python", default=None, help="Interpreter whose installed components must be preserved")
    parser.add_argument("--check", action="store_true", help="Check and apply component updates")
    parser.add_argument("--non-blocking", action="store_true", help="Return immediately if another update holds lock")
    parser.add_argument("--force", action="store_true", help="Bypass release catalog cache")
    parser.add_argument("--sync-feeds", action="store_true", help="Also synchronize configured knowledge feeds")
    parser.add_argument("--timeout", type=float, default=540.0, help="Total update timeout in seconds")
    args = parser.parse_args(argv)
    if args.prepare_schema:
        budget = float(args.timeout)
        if budget <= 0:
            raise TimeoutError("OpenCode schema preparation timeout must be positive")
        dl = time.monotonic() + budget
        with _update_lock(data_home() / "oc-schema-prepare.lock", blocking_timeout=min(5.0, max(0.0, budget * 0.2))) as got:
            if not got:
                sys.stdout.buffer.write(b'{"status":"locked"}\n')
                sys.stdout.buffer.flush()
                return 0
            try:
                ready, warn = sync_store_schema(args.config, deadline=dl, update_status_file=True)
            except Exception as exc:
                t_rec = {"finished_at": time.time(), "status": "degraded", "schema_warning": f"{type(exc).__name__}: {exc}"}
                try:
                    atomic_write_json(data_home() / "oc-schema-prepare.json", t_rec)
                except TimeoutError:
                    raise
                except OSError:
                    pass
                raise
        rec = {"finished_at": time.time(), "status": "degraded" if warn else ("ready" if ready else "skipped")}
        if warn is not None:
            rec["schema_warning"] = warn
        try:
            atomic_write_json(data_home() / "oc-schema-prepare.json", rec)
        except TimeoutError:
            raise
        except OSError:
            pass
        sys.stdout.buffer.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0 if warn is None else 1
    if args.prepare_only:
        payload = prepare(source=args.source, python=args.python or sys.executable, config=args.config, force=args.force, timeout=args.timeout)
        sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    payload = check(
        config=args.config, source=args.source, force=args.force,
        sync_feeds=args.sync_feeds, non_blocking=args.non_blocking, timeout=args.timeout,
    )
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
