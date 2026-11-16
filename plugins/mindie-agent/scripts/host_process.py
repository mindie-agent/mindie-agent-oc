"""Cross-platform host process probes for the OpenCode adapter (`host_process.py`).

[Role]
Small, side-effect-light OS helpers shared by the updater: a Windows
`OpenProcess` liveness probe (never `os.kill(pid, 0)`), a bounded stop for an
untracked background updater child, and the `gitleaks` executable discovery used
to populate an engine config's `redactor_executable`.

[Invariants]
- Windows liveness uses `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` plus
  `GetExitCodeProcess == STILL_ACTIVE`; `os.kill(pid, 0)` is POSIX-only.
- Every helper treats an unreadable/denied probe as "unknown is not alive", so a
  stale PID record is reclaimed instead of pinning a dead updater forever.
- `find_gitleaks_executable` prefers the host `PATH` and falls back to the
  `~/.cache/mindie-agent/gitleaks` cache directory.

[Timeouts & Locks]
- `stop_untracked_updater` waits at most `1.0s` before escalating to `kill()`;
  discovery only stats the filesystem. No lock is acquired.

[Side-Effect Policy]
- Only `stop_untracked_updater` mutates host state (it terminates the process it
  is handed). Everything else is a read-only probe with no configuration write.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from paths import which_host_binary


def pid_running_windows(pid: int) -> bool:
    """Windows liveness without `os.kill(pid, 0)` (which signals, not probes)."""
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code)) and int(code.value) == 259)
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - an unavailable probe must not crash callers
        return False


def stop_untracked_updater(proc: subprocess.Popen) -> None:
    """Terminate a spawned updater that could not be recorded in the status file."""
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:  # noqa: BLE001 - best-effort cleanup of an unrecorded child
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def find_gitleaks_executable() -> str | None:
    """Locate a `gitleaks` binary on `PATH` or in the MindIE cache directory."""
    found = which_host_binary("gitleaks", which_fn=shutil.which)
    if found:
        return found
    cache_dir = Path.home() / ".cache" / "mindie-agent" / "gitleaks"
    if cache_dir.is_dir():
        for candidate in sorted(cache_dir.rglob("gitleaks*"), reverse=True):
            if candidate.is_file() and candidate.name in {"gitleaks", "gitleaks.exe"}:
                return str(candidate.resolve())
    return None


__all__ = ["find_gitleaks_executable", "pid_running_windows", "stop_untracked_updater"]
