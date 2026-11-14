#!/usr/bin/env python3
"""Stable entrypoint invoked by `opencode_host.mjs` for hooks, MCP, setup, and updates.

[Role]
Dispatches OpenCode lifecycle operations (`hook gesture`, `hook session-state`,
`hook pre-tool-use`, `hook stop`), stdio MCP servers (`mcp remote`, `mcp tools`),
profile provisioning (`setup ...`), and component updates (`update ...`) under
explicit timeout budgets and paired interpreter/payload selection.

[Invariants]
- Enforces `HOOK_OUTER_SECONDS (15s) >= HOOK_SUPERVISOR_BUDGET (10.0s) + HOOK_CLEANUP_GRACE (2.0s) + 3.0s`
  and `HOOK_SUPERVISOR_BUDGET (10.0s) >= HOOK_EXEC_BUDGET (8.0s) + 2.0s` child startup/shutdown margin.
- Pairs `python` interpreter and `source_root` payload generation from `oc.json`
  atomically; re-executes into the paired interpreter via `run_supervised_reexec`.
- Never masks hook failures as `{}` + exit `0`; `pre-tool-use` fails closed on
  timeout or exception (`decision="deny"`, non-zero exit).

[Timeouts & Locks]
- `HOOK_EXEC_BUDGET = 8.0s`, `HOOK_SUPERVISOR_BUDGET = 10.0s`, `HOOK_TOTAL = 10.0s`,
  `HOOK_OUTER_SECONDS = 15`, `HOOK_LOCK_BUDGET = 2.0s`, `BOOTSTRAP_TOTAL = 550.0s`.

[Side-Effect Policy]
- Delegates hook, MCP, setup, and update execution to `bridge.py`, `mcp_server.py`,
  `setup.py`, and `component_update.py`.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run_supervised_reexec
from paths import apply_enterprise_transport_env, config_path, data_home, read_json

HOOK_EXEC_BUDGET = 8.0
HOOK_CLEANUP_GRACE = 2.0
HOOK_TOTAL = HOOK_EXEC_BUDGET + HOOK_CLEANUP_GRACE
HOOK_SUPERVISOR_BUDGET = HOOK_EXEC_BUDGET + 2.0
HOOK_OUTER_SECONDS = 15
HOOK_LOCK_BUDGET = 2.0
BOOTSTRAP_EXEC_BUDGET = 540.0
BOOTSTRAP_CLEANUP_GRACE = 10.0
BOOTSTRAP_TOTAL = BOOTSTRAP_EXEC_BUDGET + BOOTSTRAP_CLEANUP_GRACE
BOOTSTRAP_SUPERVISOR_BUDGET = BOOTSTRAP_EXEC_BUDGET + 15.0
BOOTSTRAP_OUTER_SECONDS = 580.0


def _norm_python_path(py_exe: str | Path) -> Path:
    p = Path(py_exe).expanduser()
    return p.parent.resolve() / p.name


def _is_venv_python(py: str | None) -> bool:
    return bool(
        isinstance(py, str)
        and ("venv" in py or ".venv" in py)
        and "/uv/python/" not in py
        and "/Cellar/python" not in py
        and Path(py).is_file()
    )


def _preferred_python() -> str | None:
    explicit = os.environ.get("MINDIE_OC_PYTHON")
    if _is_venv_python(explicit):
        return str(_norm_python_path(explicit))  # type: ignore[arg-type]
    cfg = config_path()
    if cfg.is_file():
        try:
            py = read_json(cfg).get("python")
            if _is_venv_python(py):
                return str(_norm_python_path(py))
        except ValueError as exc:
            print(f"MindIE OpenCode config ignored (invalid JSON in {cfg}): {exc}", file=sys.stderr)
    oc_venv = data_home() / "oc-venv"
    for candidate in (oc_venv / "Scripts" / "python.exe", oc_venv / "bin" / "python"):
        if candidate.is_file():
            return str(_norm_python_path(candidate))
    ws_venv = HERE.parent.parent.parent / ".venv"
    for candidate in (ws_venv / "Scripts" / "python.exe", ws_venv / "bin" / "python"):
        if candidate.is_file():
            return str(_norm_python_path(candidate))
    search_dirs = [cfg.parent]
    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    if host_cfg_dir not in search_dirs:
        search_dirs.append(host_cfg_dir)
    for cdir in search_dirs:
        for sibling_cfg in ("oc.json", "agy.json", "wb.json", "codex.json", "cc.json", "kimi.json"):
            scfg = cdir / sibling_cfg
            if scfg.is_file():
                try:
                    py = read_json(scfg).get("python")
                    if _is_venv_python(py):
                        return str(_norm_python_path(py))
                except ValueError as exc:
                    print(f"MindIE sibling config ignored ({scfg}): {exc}", file=sys.stderr)
    return None


def _reexec_stdio(stream):
    try:
        if os.name == "nt":
            import msvcrt

            if msvcrt.get_osfhandle(stream.fileno()) in (-1, -2, 0):
                return subprocess.DEVNULL
        else:
            stream.fileno()
        return stream
    except TimeoutError:
        raise
    except Exception:
        return subprocess.DEVNULL


def _parse_semver(raw: object) -> tuple[int, int, int] | None:
    if not isinstance(raw, str):
        return None
    parts = raw.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


@contextmanager
def _setup_outer_budget(seconds: float):
    raw_sup_dl = os.environ.pop("_MINDIE_OC_SUPERVISOR_DEADLINE", None)
    effective = float(seconds)
    if raw_sup_dl:
        try:
            sup_rem = float(raw_sup_dl) - time.monotonic() - 0.25
            if math.isfinite(sup_rem):
                effective = min(effective, sup_rem)
        except ValueError:
            pass
    if effective <= 0:
        raise TimeoutError("MindIE OpenCode supervisor budget exhausted before child execution")
    if os.name == "nt" or not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return

    def _on_alarm(_signum, _frame):
        raise TimeoutError(f"MindIE OpenCode operation timed out after {effective:.1f}s")

    prev_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, effective)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prev_handler)


def _emit_pre_tool_deny(reason: str) -> int:
    payload = {"bound": False, "decision": "deny", "reason": reason}
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 2


def _emit_hook_failure(hook_name: str, reason: str, exit_code: int) -> int:
    if hook_name == "pre-tool-use":
        return _emit_pre_tool_deny(reason)
    payload = {"activated": False, "matched": False, "status": "unavailable", "error": reason}
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a subcommand: hook | mcp | setup | update")
    hook_op = argv[1] if argv[0] == "hook" and len(argv) > 1 else ""

    try:
        target_py = _preferred_python()
        target_script = Path(__file__).resolve()
        local_manifest = target_script.parent.parent / "plugin.json"
        local_ver = _parse_semver(read_json(local_manifest).get("version")) if local_manifest.is_file() else None
        if argv[0] in {"hook", "mcp"} and config_path().is_file():
            active = read_json(config_path())
            active_python = active.get("python")
            active_scripts = active.get("runtime_scripts")
            validation = active.get("product_validation") or {}
            expected_version = validation.get("native_version")
            expected_ver_tuple = _parse_semver(expected_version)
            retained = active.get("source_root")
            directories = [Path(retained) / "plugins/mindie-agent/scripts"] if isinstance(retained, str) else []
            if isinstance(active_scripts, str):
                directories.append(Path(active_scripts))
            if isinstance(active_python, str) and Path(active_python).is_file():
                target_py = str(_norm_python_path(active_python))
                if local_ver is None or (expected_ver_tuple is not None and expected_ver_tuple >= local_ver):
                    for directory in directories:
                        manifest = directory.parent / "plugin.json"
                        paired_script = directory / "mindie_launch.py"
                        if (
                            manifest.is_file()
                            and paired_script.is_file()
                            and read_json(manifest).get("version") == expected_version
                        ):
                            target_script = paired_script.resolve()
                            break
                    else:
                        raise RuntimeError("active plugin payload does not match its recorded version")
    except Exception as exc:
        if argv[0] == "hook":
            msg = f"{type(exc).__name__}: {exc}"
            print(f"MindIE OpenCode hook config resolution failed: {msg}", file=sys.stderr)
            return _emit_hook_failure(hook_op, msg, 1)
        raise

    needs_reexec = (
        target_py is not None
        and (
            _norm_python_path(sys.executable) != _norm_python_path(target_py)
            or target_script != Path(__file__).resolve()
        )
    )
    # On Windows (`SIGALRM` unavailable), supervise finite subcommands via a Job-bounded child.
    if os.name == "nt" and argv[0] in {"hook", "setup", "update"}:
        needs_reexec = True

    if not os.environ.get("_MINDIE_OC_REEXEC") and needs_reexec:
        exec_py = target_py or sys.executable
        env = dict(os.environ)
        env["_MINDIE_OC_REEXEC"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if argv[0] in {"hook", "mcp"}:
            env["OC_PLUGIN_ROOT"] = str(target_script.parent.parent)
        cmd = [exec_py, "-I", str(target_script), *argv]
        stdin_s = _reexec_stdio(sys.stdin)
        stdout_s = _reexec_stdio(sys.stdout)
        stderr_s = _reexec_stdio(sys.stderr)
        if argv[0] == "mcp":
            mcp_kwargs: dict = {"env": env, "stdin": stdin_s, "stdout": stdout_s, "stderr": stderr_s}
            if os.name == "nt":
                mcp_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            return int(subprocess.run(cmd, **mcp_kwargs).returncode)
        if argv[0] == "hook":
            budget, grace = HOOK_SUPERVISOR_BUDGET, HOOK_CLEANUP_GRACE
        else:
            budget, grace = BOOTSTRAP_SUPERVISOR_BUDGET, BOOTSTRAP_CLEANUP_GRACE
        env["_MINDIE_OC_SUPERVISOR_DEADLINE"] = f"{time.monotonic() + budget:.6f}"
        try:
            return run_supervised_reexec(
                cmd, timeout=budget, cleanup_grace=grace, env=env, stdin=stdin_s, stdout=stdout_s, stderr=stderr_s
            )
        except TimeoutError as exc:
            print(f"MindIE OpenCode {argv[0]} timed out: {exc}", file=sys.stderr)
            if argv[0] == "hook":
                return _emit_hook_failure(hook_op, str(exc), 124)
            return 124

    from oc_native_compat import ensure_oc_native_support

    ensure_oc_native_support()

    subcmd = argv[0]
    rest = argv[1:]
    if subcmd == "hook":
        import bridge

        try:
            with _setup_outer_budget(HOOK_EXEC_BUDGET):
                return bridge.main(rest)
        except TimeoutError as exc:
            print(f"MindIE OpenCode hook timed out: {exc}", file=sys.stderr)
            return _emit_hook_failure(hook_op, str(exc), 124)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            print(f"MindIE OpenCode hook failed: {msg}", file=sys.stderr)
            return _emit_hook_failure(hook_op, msg, 1)
    if subcmd == "mcp":
        import mcp_server

        return mcp_server.main(rest)
    if subcmd == "setup":
        import setup

        with _setup_outer_budget(BOOTSTRAP_EXEC_BUDGET + 5.0):
            return setup.main(rest)
    if subcmd == "update":
        import component_update

        with _setup_outer_budget(BOOTSTRAP_EXEC_BUDGET + 5.0):
            return component_update.main(rest)
    raise SystemExit(f"unsupported mindie_launch subcommand: {subcmd}")


if __name__ == "__main__":
    raise SystemExit(main())
