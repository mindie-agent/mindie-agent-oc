#!/usr/bin/env python3
"""Stable entrypoint invoked by `opencode_host.mjs` for hooks, MCP, setup, and updates.

[Role]
Dispatches OpenCode lifecycle operations (`hook gesture`, `hook session-state`,
`hook pre-tool-use`, `hook stop`), stdio MCP servers (`mcp remote`, `mcp tools`),
profile provisioning (`setup ...`), and component updates (`update ...`) under
explicit timeout budgets and paired interpreter/payload selection.

[Invariants]
- Enforces `HOOK_TOTAL = 10.0s >= HOOK_EXEC_BUDGET (8.0s) + CLEANUP_GRACE (2.0s)`.
- Pairs `python` interpreter and `source_root` payload generation from `oc.json`
  atomically; re-executes into the paired interpreter when `sys.executable` differs.
- Uses `subprocess.CREATE_NO_WINDOW` on Windows when re-executing.

[Timeouts & Locks]
- `HOOK_TOTAL = 10.0s`, `HOOK_LOCK_BUDGET = 2.0s`, `BOOTSTRAP_TOTAL = 540.0s`.

[Side-Effect Policy]
- Delegates hook, MCP, setup, and update execution to `bridge.py`, `mcp_server.py`,
  `setup.py`, and `component_update.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import apply_enterprise_transport_env, config_path, data_home, read_json

HOOK_TOTAL = 10.0
HOOK_LOCK_BUDGET = 2.0
BOOTSTRAP_TOTAL = 540.0


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
        except ValueError:
            pass
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
                except ValueError:
                    pass
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
    except Exception:
        return subprocess.DEVNULL


def _parse_semver(raw: object) -> tuple[int, int, int] | None:
    if not isinstance(raw, str):
        return None
    parts = raw.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a subcommand: hook | mcp | setup | update")

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

    if (
        not os.environ.get("_MINDIE_OC_REEXEC")
        and target_py is not None
        and (
            _norm_python_path(sys.executable) != _norm_python_path(target_py)
            or target_script != Path(__file__).resolve()
        )
    ):
        env = dict(os.environ)
        env["_MINDIE_OC_REEXEC"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if argv[0] in {"hook", "mcp"}:
            env["OC_PLUGIN_ROOT"] = str(target_script.parent.parent)
        run_kwargs: dict = {
            "env": env,
            "stdin": _reexec_stdio(sys.stdin),
            "stdout": _reexec_stdio(sys.stdout),
            "stderr": _reexec_stdio(sys.stderr),
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if argv[0] == "hook":
            run_kwargs["timeout"] = HOOK_TOTAL
        elif argv[0] in {"setup", "update"}:
            run_kwargs["timeout"] = BOOTSTRAP_TOTAL
        proc = subprocess.run([target_py, "-I", str(target_script), *argv], **run_kwargs)
        return int(proc.returncode)

    from oc_native_compat import ensure_oc_native_support

    ensure_oc_native_support()

    subcmd = argv[0]
    rest = argv[1:]
    if subcmd == "hook":
        import bridge

        try:
            return bridge.main(rest)
        except Exception as exc:
            print(f"MindIE OpenCode hook warning: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.stdout.buffer.write(b"{}\n")
            sys.stdout.buffer.flush()
            return 0
    if subcmd == "mcp":
        import mcp_server

        return mcp_server.main(rest)
    if subcmd == "setup":
        import setup

        return setup.main(rest)
    if subcmd == "update":
        import component_update

        return component_update.main(rest)
    raise SystemExit(f"unsupported mindie_launch subcommand: {subcmd}")


if __name__ == "__main__":
    raise SystemExit(main())
