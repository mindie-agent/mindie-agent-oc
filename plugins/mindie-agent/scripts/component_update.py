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
- Atomic Profile Rebinding: `oc.json` and `oc.engine.json` are updated via
  `atomic_write_json` (`os.replace()`) only after the candidate generation
  passes `runtime_inventory` lock and VCS receipt verification.
- Single-Instance Update Lock: concurrent background checks acquire
  `oc-component-update.lock` without racing `oc.json` writes.
- Never activates a session lease.

[Timeouts & Locks]
- Enclosing budget defaults to `540.0s`: `15.0s` metadata read, `120.0s` release
  catalog discovery, `420.0s` `uv` runtime build with OS generation lock, and
  `5.0s` cleanup reserve (`outer_timeout >= sum(inner_budgets) + cleanup_grace`).

[Side-Effect Policy]
- Writes candidate runtime generations under `state_dir()/component-runtimes`
  and atomically updates `oc.json` / `oc.engine.json` when a newer component
  release or native plugin generation is activated.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from oc_native_compat import ensure_oc_native_support
from paths import (
    apply_enterprise_transport_env,
    atomic_write_json,
    community_config_path,
    config_path,
    data_home,
    engine_config_path,
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
        subprocess.Popen(
            [str(py_exe), "-I", str(Path(__file__).resolve()), "--config", str(cfg_file), "--non-blocking"],
            **kwargs,
        )
        return True
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
    from mindie_knowledge.component_bindings import rebind_commands
    from mindie_knowledge.component_runtime import read_inventory
    from setup import ensure_default_profile

    cfg_file = config_path(config)
    lock_file = data_home() / "oc-component-update.lock"
    with _update_lock(lock_file, blocking_timeout=0.0 if non_blocking else 5.0) as acquired:
        if not acquired:
            return dict(
                schema="mindie-component-update/1",
                harness="opencode",
                status="locked",
                needs_host_reload=False,
            )

        native_promoted = False
        candidate_checkout = Path(source).resolve() if source else HERE.parent.parent.parent.resolve()
        status_file = data_home() / "oc-component-update-status.json"
        try:
            if not cfg_file.is_file():
                ensure_default_profile(config=cfg_file, source_root=candidate_checkout)
                native_promoted = True
            else:
                existing_adapter = read_json(cfg_file)
                if _candidate_plugin_is_newer(candidate_checkout, existing_adapter):
                    ensure_default_profile(config=cfg_file, source_root=candidate_checkout)
                    native_promoted = True

            adapter = read_json(cfg_file)
            eng_file = engine_config_path(cfg_file)
            current_python = adapter.get("python") or sys.executable
            artifact = prepare(
                source=source or repo_root(cfg_file),
                python=current_python,
                config=cfg_file,
                force=force,
                timeout=timeout,
            )
        except Exception as exc:
            try:
                atomic_write_json(
                    status_file,
                    {"checked_at": time.time(), "status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                )
            except OSError:
                pass
            raise

        if artifact is not None:
            new_python = _norm_python_path(artifact["python"])
            updated_adapter = dict(adapter)
            updated_adapter["python"] = new_python
            updated_adapter["component_runtime"] = artifact["components"]
            if updated_adapter.get("component_commands"):
                updated_adapter["component_commands"] = rebind_commands(
                    updated_adapter,
                    new_python,
                    artifact["components"]["runtime"],
                )
            atomic_write_json(cfg_file, updated_adapter)
            if eng_file.is_file():
                eng_payload = read_json(eng_file)
                scripts_dir = Path(updated_adapter.get("runtime_scripts") or HERE)
                eng_payload["summary_command"] = [new_python, "-I", str(scripts_dir / "organizer.py")]
                atomic_write_json(eng_file, eng_payload)
            status = "updated"
            active_python = new_python
            installed = artifact["installed"]
        else:
            status = "updated" if native_promoted else "up_to_date"
            active_python = _norm_python_path(current_python)
            try:
                installed = read_inventory(active_python, timeout=15.0)
            except Exception:
                installed = {}

        needs_reload = status == "updated"
        result: dict = dict(
            schema="mindie-component-update/1",
            harness="opencode",
            status=status,
            needs_host_reload=needs_reload,
            python=active_python,
            installed=installed,
        )
        if sync_feeds and community_config_path(cfg_file).is_file() and eng_file.is_file():
            try:
                from mindie_knowledge.loop.cli import sync_feeds as run_sync_feeds

                result["feed_sync"] = run_sync_feeds(str(eng_file))
            except Exception as exc:
                result["feed_sync_error"] = f"{type(exc).__name__}: {exc}"
        try:
            atomic_write_json(
                status_file,
                {
                    "checked_at": time.time(),
                    "status": status,
                    "needs_host_reload": needs_reload,
                    "python": active_python,
                },
            )
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
