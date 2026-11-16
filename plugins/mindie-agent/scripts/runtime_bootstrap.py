"""Interpreter bootstrap helpers for the OpenCode adapter (`runtime_bootstrap.py`).

[Role]
Owns the two interpreter-level bootstrap primitives the adapter shares: the
bounded `compatible_runtime` probe/`--prepare-only` build used by `setup.py` and
`component_update.py`, the cheap `inventory_ready` health probe that lets a
candidate upgrade reuse an existing healthy interpreter instead of building a
redundant baseline generation, and same-volume `UV_CACHE_DIR` placement for a
cold `uv sync`.

[Invariants]
- `compatible_runtime` returns the caller's interpreter byte-for-byte when it
  already provides the plugin APIs; it only spawns a build when the probe fails.
- `inventory_ready` is a read-only receipt probe: it never installs, never
  builds a virtualenv, and a broken interpreter is reported as `False` (a
  candidate upgrade then falls back to the baseline bootstrap).
- An explicit non-empty `UV_CACHE_DIR` is never overwritten; the fallback cache
  is placed on the target's own filesystem only.

[Timeouts & Locks]
- Every entry point takes an explicit budget: `compatible_runtime` reserves a
  `15.0s` probe ceiling and cleanup grace inside the caller deadline, and
  `inventory_ready` bounds the receipt read to `15.0s`. No lock is acquired here;
  build serialization stays inside `mindie_knowledge.component_runtime`.

[Side-Effect Policy]
- `compatible_runtime` may build a candidate generation through
  `component_update.py --prepare-only`; the other helpers are read-only and never
  write configuration, `oc.json`, or a cache directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from paths import apply_enterprise_transport_env, config_path, norm_python_path, repo_root, same_interpreter

REQUIRED_RUNTIME_MODULES = (
    "mindie_knowledge", "remote_dev", "mindie_diagnostics", "mindie_profiler",
    "mindie_sim", "mindie_coordinator", "mindie_debugger", "npu_top",
)


def current_python_has_runtime() -> bool:
    """True when the running interpreter imports the first-party runtime modules."""
    return all(importlib.util.find_spec(mod) is not None for mod in REQUIRED_RUNTIME_MODULES)


def inventory_ready(interpreter: str | Path, *, timeout: float = 15.0) -> bool:
    """True when `interpreter` runs the runtime inventory (a usable receipt source)."""
    candidate = Path(interpreter)
    if not candidate.is_file() or timeout <= 0:
        return False
    try:
        from mindie_knowledge.component_runtime import read_inventory

        inventory = read_inventory(str(candidate), timeout=min(15.0, float(timeout)))
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False
    except Exception:  # noqa: BLE001 - any broken interpreter is a cold-start fallback
        return False
    return isinstance(inventory, dict)


def compatible_runtime(
    python: str | Path,
    *,
    source=None,
    config=None,
    timeout: float = 540.0,
    script: str | Path | None = None,
) -> str:
    """Return an interpreter that provides the plugin APIs, preparing one if needed."""
    if timeout <= 0:
        raise TimeoutError("runtime compatibility probe exceeded its budget")
    deadline = time.monotonic() + float(timeout)
    norm_py = norm_python_path(python)
    imports = f"import {', '.join(REQUIRED_RUNTIME_MODULES)}; from remote_dev.mcp.outcome import operation_outcome"
    if (
        same_interpreter(python, sys.executable)
        and all(importlib.util.find_spec(m) is not None for m in REQUIRED_RUNTIME_MODULES)
        and importlib.util.find_spec("remote_dev.mcp.outcome") is not None
    ):
        return norm_py

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
    build_script = Path(script) if script is not None else Path(__file__).with_name("component_update.py")
    command = [
        str(python), "-I", str(build_script), "--prepare-only", "--python", str(python),
        "--source", str(source or repo_root(config)), "--config", str(config_path(config)),
        "--timeout", str(remaining - 20.0),
    ]
    result = bounded_run(command, "", timeout=remaining - 15.0, max_output=256 * 1024,
                         env=apply_enterprise_transport_env(dict(os.environ)))
    if result.returncode != 0:
        raise RuntimeError(f"runtime candidate preparation failed: {(result.stderr or result.stdout)[-1000:]}")
    artifact = json.loads(result.stdout)
    candidate = artifact.get("python") if isinstance(artifact, dict) else None
    if not candidate or not probe(candidate):
        raise RuntimeError("runtime candidate does not provide the plugin APIs")
    return norm_python_path(candidate)


def same_volume_uv_cache(target_dir: str | Path, env: dict) -> dict:
    """Pin `UV_CACHE_DIR` to `target_dir`'s device when uv's default crosses it."""
    if not isinstance(env, dict):
        return env
    try:
        from mindie_knowledge.storage import configure_same_volume_uv_cache
    except ImportError:
        return env
    try:
        return configure_same_volume_uv_cache(Path(target_dir), env)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return env


__all__ = [
    "REQUIRED_RUNTIME_MODULES",
    "compatible_runtime",
    "current_python_has_runtime",
    "inventory_ready",
    "same_volume_uv_cache",
]
