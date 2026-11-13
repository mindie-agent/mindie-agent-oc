"""Bounded subprocess runner with cross-platform process-tree cleanup and streaming output cap.

[Role]
Executes bounded child processes (`run`) for hooks, deterministic component CLIs,
and summary workers, guaranteeing full process-group (POSIX) or Windows Job
Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) tree termination and bounded
in-memory output draining (`O(max_output)`, zero unbounded temp files).

[Invariants]
- Never calls `subprocess.run(..., timeout=None)`. Every child process has a
  positive finite wall-clock `timeout`.
- Kills the entire owned POSIX process group (`os.killpg(proc.pid, SIGKILL)`)
  unconditionally—even if the direct child already exited—so orphaned
  grandchildren never survive or hold pipes open.
- Drains stdout/stderr pipes in 64 KiB chunks while retaining at most
  `max_output` bytes per stream; preserves `returncode` when
  `strict_output_limit=False`.
- Explicitly re-raises `TimeoutError` before any `except OSError:` branch.

[Timeouts & Locks]
- Default `cleanup_grace = 2.0s` for tree termination and pipe reader join.

[Side-Effect Policy]
- Spawns and reaps one owned child process tree per `run()` call.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time


class OutputLimitExceeded(ValueError):
    """Child process output exceeded the caller's byte budget."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int
    cleanup_failed: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def checked_stdout(self) -> str:
        if self.returncode != 0:
            detail = self.stderr.strip() or self.stdout.strip() or f"exit code {self.returncode}"
            raise RuntimeError(f"child process failed ({self.returncode}): {detail}")
        return self.stdout


def _kill_process_tree(proc: subprocess.Popen, job_handle=None) -> bool:
    cleanup_failed = False
    if job_handle is not None and os.name == "nt":
        try:
            import win32api
            import win32job

            win32job.TerminateJobObject(job_handle, 1)
            win32api.CloseHandle(job_handle)
            job_handle = None
        except Exception:
            cleanup_failed = True
    if os.name == "nt":
        if proc.poll() is None:
            try:
                proc.kill()
            except TimeoutError:
                raise
            except OSError:
                cleanup_failed = True
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except TimeoutError:
            raise
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_failed = True
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        cleanup_failed = True
    return cleanup_failed


def _attach_windows_job(proc: subprocess.Popen):
    if os.name != "nt":
        return None
    try:
        import win32api
        import win32con
        import win32job

        hjob = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(hjob, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        win32job.SetInformationJobObject(hjob, win32job.JobObjectExtendedLimitInformation, info)
        hproc = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
            False,
            proc.pid,
        )
        try:
            win32job.AssignProcessToJobObject(hjob, hproc)
        finally:
            win32api.CloseHandle(hproc)
        return hjob
    except Exception:
        return None


def _drain_pipe_bounded(pipe, max_bytes: int, sink: list) -> None:
    buf = bytearray()
    overflow = False
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            remaining = max_bytes - len(buf)
            if remaining > 0:
                if len(chunk) <= remaining:
                    buf.extend(chunk)
                else:
                    buf.extend(chunk[:remaining])
                    overflow = True
            else:
                overflow = True
    except TimeoutError:
        raise
    except OSError:
        pass
    finally:
        try:
            pipe.close()
        except TimeoutError:
            raise
        except OSError:
            pass
    sink.append((bytes(buf), overflow))


def run(
    argv: list[str],
    stdin_text: str = "",
    *,
    timeout: float = 30.0,
    max_output: int = 1024 * 1024,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    cancel: threading.Event | None = None,
    allowed_returncodes: set[int] | None = None,
    strict_output_limit: bool = True,
) -> ProcessResult:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise ValueError("max_output must be a positive integer byte limit")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("argv must be a nonempty list of strings")

    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"

    with tempfile.TemporaryDirectory(prefix="mindie-oc-proc-") as tmp_dir:
        stdin_path = Path(tmp_dir) / "stdin.txt"
        stdin_path.write_bytes((stdin_text or "").encode("utf-8"))

        with stdin_path.open("rb") as fin:
            popen_kwargs: dict = dict(
                stdin=fin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                cwd=str(cwd) if cwd is not None else None,
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                )
            else:
                popen_kwargs["start_new_session"] = True

            started = time.monotonic()
            deadline = started + float(timeout)
            proc = subprocess.Popen(argv, **popen_kwargs)
            job_handle = _attach_windows_job(proc)
            out_sink: list = []
            err_sink: list = []
            out_thread = threading.Thread(
                target=_drain_pipe_bounded, args=(proc.stdout, max_output, out_sink), daemon=True
            )
            err_thread = threading.Thread(
                target=_drain_pipe_bounded, args=(proc.stderr, max_output, err_sink), daemon=True
            )
            out_thread.start()
            err_thread.start()
            cleanup_failed = False
            try:
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if cancel is not None and cancel.is_set():
                        cleanup_failed = _kill_process_tree(proc, job_handle)
                        job_handle = None
                        raise RuntimeError("operation cancelled by caller")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        cleanup_failed = _kill_process_tree(proc, job_handle)
                        job_handle = None
                        raise TimeoutError(f"process timed out after {timeout:.2f}s: {argv[0]}")
                    time.sleep(min(0.02, max(0.001, remaining)))
            finally:
                cleanup_failed = _kill_process_tree(proc, job_handle) or cleanup_failed
                job_handle = None
                out_thread.join(timeout=2.0)
                err_thread.join(timeout=2.0)
                if out_thread.is_alive() or err_thread.is_alive():
                    cleanup_failed = True

        elapsed_ms = int((time.monotonic() - started) * 1000)
        out_bytes, out_trunc = out_sink[0] if out_sink else (b"", False)
        err_bytes, err_trunc = err_sink[0] if err_sink else (b"", False)
        if strict_output_limit and (out_trunc or err_trunc):
            raise OutputLimitExceeded(f"process output exceeded {max_output} bytes")
        result = ProcessResult(
            returncode=int(proc.returncode or 0),
            stdout=out_bytes.decode("utf-8", errors="replace"),
            stderr=err_bytes.decode("utf-8", errors="replace"),
            elapsed_ms=elapsed_ms,
            cleanup_failed=cleanup_failed,
            stdout_truncated=out_trunc,
            stderr_truncated=err_trunc,
        )
        if allowed_returncodes is not None and result.returncode not in allowed_returncodes:
            result.checked_stdout()
        return result
