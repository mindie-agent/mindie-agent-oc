#!/usr/bin/env python3
"""Independent shared component runtime updater for the OpenCode (`oc`) adapter.

[Role]
Discovers newer formal GitHub Releases of shared MindIE runtime components
(`mindie-knowledge`, `remote-dev`, `mindie-diagnostics`, `mindie-profiler`,
`mindie-sim`, `mindie-debugger`, `npu-top`, `mindie-coordinator`) and builds an
isolated component generation under `state_dir()/component-runtimes` without
requiring a new `mindie-agent-oc` plugin release. Also promotes newly installed
native plugin generations (`opencode plugin update`) once runtime compatibility
is verified.

[Invariants]
- Monotonic Component Floor: `discover()` selects `max(installed, baseline,
  latest_formal_release)` and never downgrades an already-installed newer
  component.
- Candidate-First Atomic Switch: candidate source staging, contract validation,
  runtime verification, `prepare()`, and generation-isolated `oc.engine.<gen>.json`
  creation ALL complete before a single atomic `os.replace()` updates `oc.json`.
  Any failure leaves the previously active `oc.json` and `engine_config` untouched.
- Single-Instance Update Lock: concurrent checks serialize via `oc-component-update.lock`.
- Never activates a session lease.

[Timeouts & Locks]
- Enclosing shared monotonic deadline defaults to `540.0s`: lock wait, `15.0s`
  metadata read, `120.0s` release catalog discovery, `420.0s` `uv` runtime build
  with OS generation lock, and `5.0s` cleanup reserve.

[Side-Effect Policy]
- Writes candidate runtime generations under `state_dir()/component-runtimes`,
  generation engine configs under `config_home()/generations/`, and atomically
  flips `oc.json` last when promoting a verified candidate.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
            interval = UPDATE_FAILURE_BACKOFF if st == "failed" else UPDATE_CHECK_INTERVAL
            if now - checked < interval:
                return False
        atomic_write_json(status_file, {"checked_at": now, "status": "in_progress"})
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
        env = apply_enterprise_transport_env(dict(os.environ))
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=env,
        )
        if os.name == "nt":
            # Never combine DETACHED_PROCESS with CREATE_NO_WINDOW on Windows (P1.5).
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except TimeoutError:
        raise
    except Exception:
        return False


def _norm_python_path(py_exe: str | Path) -> str:
    p = Path(py_exe).expanduser()
    return str(p.parent.resolve() / p.name)


def compatible_runtime(python: str, *, source=None, config=None, timeout: float = 540.0) -> str:
    """Prepare a compatible generation without changing the active profile."""
    deadline = time.monotonic() + float(timeout)
    imports = (
        "import mindie_knowledge, remote_dev, mindie_diagnostics, mindie_profiler, "
        "mindie_sim, mindie_coordinator, mindie_debugger, npu_top; "
        "from remote_dev.mcp.outcome import operation_outcome"
    )

    def probe(interpreter):
        budget = min(15.0, deadline - time.monotonic())
        if budget <= 0:
            raise TimeoutError("runtime compatibility probe exceeded its budget")
        return bounded_run([str(interpreter), "-I", "-c", imports], "", timeout=budget).returncode == 0

    if probe(python):
        return _norm_python_path(python)
    remaining = deadline - time.monotonic()
    if remaining <= 20.0:
        raise TimeoutError("runtime preparation has no remaining build budget")
    command = [
        str(python),
        "-I",
        str(Path(__file__).resolve()),
        "--prepare-only",
        "--python",
        str(python),
        "--source",
        str(source or repo_root(config)),
        "--config",
        str(config_path(config)),
        "--timeout",
        str(remaining - 20.0),
    ]
    result = bounded_run(
        command,
        "",
        timeout=remaining - 15.0,
        max_output=256 * 1024,
        env=apply_enterprise_transport_env(dict(os.environ)),
    )
    if result.returncode != 0:
        raise RuntimeError(f"runtime candidate preparation failed: {(result.stderr or result.stdout)[-1000:]}")
    artifact = json.loads(result.stdout)
    candidate = artifact.get("python") if isinstance(artifact, dict) else None
    if not candidate or not probe(candidate):
        raise RuntimeError("runtime candidate does not provide the plugin APIs")
    return _norm_python_path(candidate)


def prepare(
    source: str | Path | None = None,
    python: str | Path | None = None,
    *,
    config: str | Path | None = None,
    force: bool = False,
    timeout: float = 540.0,
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
    *,
    cfg_file: Path,
    base_adapter: dict,
    base_engine: dict,
    target_source: Path,
    target_python: str,
    validation: dict,
    artifact: dict | None,
) -> Path:
    """Write generation-isolated `oc.engine.<gen>.json` FIRST, then atomically flip `oc.json`."""
    from mindie_knowledge.component_bindings import rebind_commands

    sdir = Path(base_adapter["state_dir"]) if isinstance(base_adapter.get("state_dir"), str) else state_dir(cfg_file)
    adm_file = (
        Path(base_adapter["admission_path"])
        if isinstance(base_adapter.get("admission_path"), str)
        else admission_path(cfg_file)
    )
    comm_file = community_config_path(cfg_file)
    data_root = data_home() / "domains"
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    scripts_dir = (target_source / "plugins/mindie-agent/scripts").resolve()
    domain = str(base_adapter.get("domain") or "vllm-ascend")
    gen_id = hashlib.sha256(
        json.dumps(
            {
                "python": target_python,
                "source_root": str(target_source),
                "native_version": validation.get("native_version"),
                "components": artifact.get("components") if isinstance(artifact, dict) else base_adapter.get("component_runtime"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    gen_eng_file = cfg_file.parent / "generations" / f"{cfg_file.stem}.engine.{gen_id}.json"
    eng_payload = dict(
        base_engine,
        schema="mindie-engine-config/1",
        harness="opencode",
        domain=domain,
        root=str(base_engine.get("root") or data_root),
        state_dir=str(base_engine.get("state_dir") or (sdir / "engine")),
        admission_path=str(adm_file),
        community_config=str(comm_file),
        settings_path=str(comm_file),
        transcript_adapter=str(scripts_dir / "opencode_transcript.py"),
        summary_command=[target_python, "-I", str(scripts_dir / "organizer.py")],
        redactor_executable=base_engine.get("redactor_executable") or _find_gitleaks_executable(),
        feeds=base_engine.get("feeds")
        or [{"repository": "mindie-agent/knowledge-vllm-ascend", "ref": "main", "domain": domain, "interval_seconds": 300}],
        product_validation=validation,
    )
    # Step 1: Write isolated candidate engine config BEFORE touching active `oc.json`.
    atomic_write_json(gen_eng_file, eng_payload)

    updated_adapter = dict(
        base_adapter,
        schema="mindie-adapter-config/1",
        harness="opencode",
        python=target_python,
        runtime_scripts=str(scripts_dir),
        source_root=str(target_source),
        engine_config=str(gen_eng_file),
        state_dir=str(sdir),
        admission_path=str(adm_file),
        domain=domain,
        product_validation=validation,
    )
    if artifact is not None:
        updated_adapter["component_runtime"] = artifact["components"]
        if updated_adapter.get("component_commands"):
            updated_adapter["component_commands"] = rebind_commands(
                updated_adapter,
                target_python,
                artifact["components"]["runtime"],
            )

    # Step 2: Single atomic pointer switch! Readers of `oc.json` transition atomically
    # to `(target_python, gen_eng_file, target_source)` with zero split-brain window.
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


def check(
    *,
    config: str | Path | None = None,
    source: str | Path | None = None,
    force: bool = False,
    sync_feeds: bool = False,
    non_blocking: bool = False,
    timeout: float = 540.0,
) -> dict:
    apply_enterprise_transport_env()
    from mindie_knowledge.component_runtime import read_inventory
    from setup import (
        _write_initial_community_config,
        ensure_runtime_python,
        stage_source_baseline,
        validate_contracts,
    )

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
            return dict(
                schema="mindie-component-update/1",
                harness="opencode",
                status="locked",
                needs_host_reload=False,
            )

        candidate_checkout = (
            Path(source).resolve()
            if source is not None
            else (installed_source_root() or HERE.parent.parent.parent.resolve())
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
                    config=cfg_file,
                    timeout=remaining(10.0),
                )
            else:
                configured_sr = existing_adapter.get("source_root")
                if (
                    isinstance(configured_sr, str)
                    and os.path.isabs(configured_sr)
                    and (Path(configured_sr) / "pyproject.toml").is_file()
                ):
                    target_source = Path(configured_sr).resolve()
                else:
                    target_source = repo_root(cfg_file)
                validation = validate_contracts(target_source)
                base_python = str(existing_adapter.get("python") or sys.executable)

            artifact = prepare(
                source=target_source,
                python=base_python,
                config=cfg_file,
                force=force,
                timeout=remaining(5.0),
            )

            target_python = _norm_python_path(artifact["python"] if artifact is not None else base_python)
            if artifact is not None or native_candidate:
                active_eng_file = _write_generation_and_switch_profile(
                    cfg_file=cfg_file,
                    base_adapter=existing_adapter,
                    base_engine=existing_engine,
                    target_source=target_source,
                    target_python=target_python,
                    validation=validation,
                    artifact=artifact,
                )
                comm_file = community_config_path(cfg_file)
                if not comm_file.is_file():
                    _write_initial_community_config(
                        target_python,
                        comm_file,
                        dict(
                            enabled=False,
                            repository="mindie-agent/knowledge-vllm-ascend",
                            project_roots=[],
                            branch="main",
                            idle_seconds=300,
                            consent_config=str(consent_path(cfg_file).resolve()),
                        ),
                    )
                status = "updated"
            else:
                active_eng_file = existing_eng_file
                status = "up_to_date"
        except Exception as exc:
            import diagnostic_support

            diag = diagnostic_support.failure("component.update", "prepare", "update", exception=exc, config=cfg_file)
            try:
                atomic_write_json(
                    status_file,
                    {
                        "checked_at": time.time(),
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "diagnostic": diag,
                    },
                )
            except TimeoutError:
                raise
            except OSError:
                pass
            raise

        if artifact is not None:
            installed = artifact["installed"]
        else:
            try:
                installed = read_inventory(target_python, timeout=min(15.0, remaining(0.0)))
            except TimeoutError:
                raise
            except Exception:
                installed = {}

        needs_reload = status == "updated"
        result: dict = dict(
            schema="mindie-component-update/1",
            harness="opencode",
            status=status,
            needs_host_reload=needs_reload,
            python=target_python,
            installed=installed,
        )
        if sync_feeds and community_config_path(cfg_file).is_file() and active_eng_file.is_file():
            try:
                from mindie_knowledge.loop.cli import sync_feeds as run_sync_feeds

                result["feed_sync"] = run_sync_feeds(str(active_eng_file))
            except TimeoutError:
                raise
            except Exception as exc:
                result["feed_sync_error"] = f"{type(exc).__name__}: {exc}"
        try:
            atomic_write_json(
                status_file,
                {
                    "checked_at": time.time(),
                    "status": status,
                    "needs_host_reload": needs_reload,
                    "python": target_python,
                },
            )
        except TimeoutError:
            raise
        except OSError:
            pass
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update MindIE shared runtime components for OpenCode")
    parser.add_argument("--config", default=None, help="Path to oc.json")
    parser.add_argument("--source", default=None, help="Path to mindie-agent-oc checkout")
    parser.add_argument("--prepare-only", action="store_true", help="Build a candidate without activating it")
    parser.add_argument("--python", default=None, help="Interpreter whose installed components must be preserved")
    parser.add_argument("--check", action="store_true", help="Check and apply component updates")
    parser.add_argument("--non-blocking", action="store_true", help="Return immediately if another update holds lock")
    parser.add_argument("--force", action="store_true", help="Bypass release catalog cache")
    parser.add_argument("--sync-feeds", action="store_true", help="Also synchronize configured knowledge feeds")
    parser.add_argument("--timeout", type=float, default=540.0, help="Total update timeout in seconds")
    args = parser.parse_args(argv)
    if args.prepare_only:
        payload = prepare(
            source=args.source,
            python=args.python or sys.executable,
            config=args.config,
            force=args.force,
            timeout=args.timeout,
        )
        sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    payload = check(
        config=args.config,
        source=args.source,
        force=args.force,
        sync_feeds=args.sync_feeds,
        non_blocking=args.non_blocking,
        timeout=args.timeout,
    )
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
