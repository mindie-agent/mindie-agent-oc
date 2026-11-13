"""Bounded subprocess runner with cross-platform process-tree cleanup and streaming output cap.

[Role]
Executes bounded child processes (`run`, `run_supervised_reexec`) for hooks,
deterministic component CLIs, launcher re-execution, and summary workers,
guaranteeing full process-group (POSIX, including nested session groups) or
Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) tree termination and
bounded in-memory output draining (`O(max_output)`, zero unbounded temp files).

[Invariants]
- Never calls `subprocess.run(..., timeout=None)` for finite operations. Every
  supervised child process has a positive finite wall-clock `timeout`.
- Kills owned POSIX process groups (`os.killpg(..., SIGKILL)`) unconditionally—even
  if the direct child already exited or spawned nested `start_new_session` groups.
- Drains stdout/stderr pipes in 64 KiB chunks while retaining at most `max_output`
  bytes per stream; preserves `returncode` when `strict_output_limit=False`.
- Explicitly re-raises `TimeoutError` before any `except OSError:` branch.

[Timeouts & Locks]
- Default `cleanup_grace = 2.0s` for tree termination and pipe reader join.

[Side-Effect Policy]
- Spawns and reaps one owned child process tree per `run()` / `run_supervised_reexec()` call.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
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


def _close_windows_job(job_handle, *, disarm_kill_on_close: bool = False) -> bool:
    if job_handle is None or os.name != "nt":
        return False
    failed = False
    try:
        import win32api
        import win32job

        if disarm_kill_on_close:
            try:
                info = win32job.QueryInformationJobObject(job_handle, win32job.JobObjectExtendedLimitInformation)
                info["BasicLimitInformation"]["LimitFlags"] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job_handle, win32job.JobObjectExtendedLimitInformation, info)
            except Exception as exc:
                failed = True
                print(f"MindIE OpenCode Windows Job disarm warning: {type(exc).__name__}: {exc}", file=sys.stderr)
        win32api.CloseHandle(job_handle)
        return failed
    except Exception as exc:
        print(f"MindIE OpenCode Windows Job close warning: {type(exc).__name__}: {exc}", file=sys.stderr)
        return True


def _ancestor_pgid_files(env: dict[str, str] | None = None) -> list[Path]:
    if os.name == "nt":
        return []
    src = env if env is not None else os.environ
    out: list[Path] = []
    seen: set[str] = set()
    raw_list = src.get("_MINDIE_OC_PGID_FILES", "")
    single = src.get("_MINDIE_OC_PGID_FILE", "")
    for item in [*(raw_list.split(os.pathsep) if raw_list else []), single]:
        token = item.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(Path(token))
    return out


def _register_owned_pgid(pgid: int, targets: list[Path]) -> None:
    if os.name == "nt" or pgid <= 0:
        return
    line = f"{pgid}\n".encode("ascii")
    for target in targets:
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except TimeoutError:
            raise
        except OSError:
            pass


def _read_registered_pgids(pgid_file: Path | None) -> list[int]:
    if pgid_file is None or os.name == "nt":
        return []
    try:
        raw = pgid_file.read_text(encoding="utf-8", errors="ignore")
    except TimeoutError:
        raise
    except OSError:
        return []
    own_pgrp = os.getpgrp()
    seen: list[int] = []
    for line in raw.splitlines():
        token = line.strip()
        if token.isdigit():
            val = int(token)
            if val > 0 and val != own_pgrp and val not in seen:
                seen.append(val)
    return seen


def _signal_posix_pgids(pgids: list[int], sig: int) -> bool:
    own_pgrp = os.getpgrp()
    failed = False
    for pgid in pgids:
        if pgid <= 0 or pgid == own_pgrp:
            continue
        try:
            os.killpg(pgid, sig)
        except TimeoutError:
            raise
        except ProcessLookupError:
            pass
        except OSError:
            failed = True
    return failed


def _kill_process_tree(
    proc: subprocess.Popen,
    job_handle=None,
    *,
    pgid_file: Path | None = None,
    sigterm_grace: float = 0.0,
    wait_timeout: float = 1.0,
) -> bool:
    kill_deadline = time.monotonic() + max(0.02, float(wait_timeout))
    cleanup_failed = False
    if job_handle is not None and os.name == "nt":
        try:
            import win32api
            import win32job

            win32job.TerminateJobObject(job_handle, 1)
            win32api.CloseHandle(job_handle)
            job_handle = None
        except TimeoutError:
            raise
        except Exception as exc:
            cleanup_failed = True
            print(f"MindIE OpenCode Windows Job terminate warning: {type(exc).__name__}: {exc}", file=sys.stderr)
            _close_windows_job(job_handle)
            job_handle = None
    if os.name == "nt":
        if proc.poll() is None:
            try:
                proc.kill()
            except TimeoutError:
                raise
            except OSError:
                cleanup_failed = True
    else:
        own_pgrp = os.getpgrp()
        pgids = [g for g in [proc.pid, *_read_registered_pgids(pgid_file)] if g > 0 and g != own_pgrp]
        if sigterm_grace > 0 and pgids:
            _signal_posix_pgids(pgids, signal.SIGTERM)
            grace_deadline = min(kill_deadline, time.monotonic() + float(sigterm_grace))
            while proc.poll() is None and time.monotonic() < grace_deadline:
                time.sleep(0.01)
            for extra in _read_registered_pgids(pgid_file):
                if extra > 0 and extra != own_pgrp and extra not in pgids:
                    pgids.append(extra)
        if pgids:
            cleanup_failed = _signal_posix_pgids(pgids, signal.SIGKILL) or cleanup_failed
    if proc.poll() is None:
        try:
            proc.wait(timeout=max(0.01, kill_deadline - time.monotonic()))
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
    except ImportError as exc:
        print(f"MindIE OpenCode pywin32 unavailable for Job supervision: {exc}", file=sys.stderr)
        return None
    try:
        hjob = win32job.CreateJobObject(None, "")
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"MindIE OpenCode CreateJobObject warning: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    try:
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
    except TimeoutError:
        _close_windows_job(hjob)
        raise
    except Exception as exc:
        print(f"MindIE OpenCode AssignProcessToJobObject warning: {type(exc).__name__}: {exc}", file=sys.stderr)
        _close_windows_job(hjob)
        return None


class _PipeSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._overflow = False

    def append_chunk(self, chunk: bytes, max_bytes: int) -> None:
        with self._lock:
            remaining = max_bytes - len(self._buf)
            if remaining > 0:
                if len(chunk) <= remaining:
                    self._buf.extend(chunk)
                else:
                    self._buf.extend(chunk[:remaining])
                    self._overflow = True
            else:
                self._overflow = True

    def snapshot(self) -> tuple[bytes, bool]:
        with self._lock:
            return bytes(self._buf), self._overflow


def _drain_pipe_bounded(pipe, max_bytes: int, sink: _PipeSink) -> None:
    read_fn = getattr(pipe, "read1", pipe.read)
    try:
        while True:
            chunk = read_fn(65536)
            if not chunk:
                break
            sink.append_chunk(chunk, max_bytes)
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


def run_supervised_reexec(
    argv: list[str],
    *,
    timeout: float,
    cleanup_grace: float = 2.0,
    env: dict[str, str] | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    """Run a stdio-inheriting re-exec child under a process group / Windows Job budget."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("argv must be a nonempty list of strings")

    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"

    tmp_dir = Path(tempfile.mkdtemp(prefix="mindie-oc-reexec-"))
    proc: subprocess.Popen | None = None
    job_handle = None
    completed = False
    tree_cleaned = False
    grace_end: float | None = None
    rc = 1
    own_pgid_file = tmp_dir / "pgids.txt"
    no_breakaway_marker = tmp_dir / "bg-no-breakaway"
    try:
        ancestors = _ancestor_pgid_files(child_env)
        active_files = [*ancestors, own_pgid_file]
        child_env.pop("_MINDIE_OC_PGID_FILE", None)
        if os.name == "nt":
            child_env["_MINDIE_OC_BG_NO_BREAKAWAY"] = str(no_breakaway_marker)
        else:
            child_env["_MINDIE_OC_PGID_FILES"] = os.pathsep.join(str(p) for p in active_files)

        popen_kwargs: dict = dict(env=child_env, stdin=stdin, stdout=stdout, stderr=stderr)
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(argv, **popen_kwargs)
        _register_owned_pgid(proc.pid, active_files)
        job_handle = _attach_windows_job(proc)
        try:
            rc = int(proc.wait(timeout=float(timeout)))
            completed = True
        except subprocess.TimeoutExpired as exc:
            grace_end = time.monotonic() + max(0.1, float(cleanup_grace))
            rem_grace = max(0.02, grace_end - time.monotonic())
            _kill_process_tree(
                proc,
                job_handle,
                pgid_file=own_pgid_file,
                sigterm_grace=min(0.15, rem_grace * 0.25),
                wait_timeout=rem_grace,
            )
            tree_cleaned = True
            job_handle = None
            raise TimeoutError(f"supervised process timed out after {timeout:.2f}s: {argv[0]}") from exc
    finally:
        if grace_end is None:
            grace_end = time.monotonic() + max(0.1, float(cleanup_grace))
        if completed:
            try:
                if os.name == "nt":
                    _close_windows_job(job_handle, disarm_kill_on_close=no_breakaway_marker.is_file())
                elif proc is not None:
                    rem_grace = max(0.02, grace_end - time.monotonic())
                    _kill_process_tree(
                        proc,
                        None,
                        pgid_file=own_pgid_file,
                        sigterm_grace=min(0.05, rem_grace * 0.2),
                        wait_timeout=rem_grace,
                    )
                shutil.rmtree(tmp_dir)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as cleanup_exc:
                print(
                    f"MindIE OpenCode supervisor post-completion cleanup warning: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                    file=sys.stderr,
                )
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            try:
                if not tree_cleaned and proc is not None:
                    rem_grace = max(0.02, grace_end - time.monotonic())
                    _kill_process_tree(
                        proc,
                        job_handle,
                        pgid_file=own_pgid_file,
                        sigterm_grace=min(0.1, rem_grace * 0.25),
                        wait_timeout=rem_grace,
                    )
                elif job_handle is not None and os.name == "nt":
                    _close_windows_job(job_handle)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    return rc


def run(
    argv: list[str],
    stdin_text: str = "",
    *,
    timeout: float = 30.0,
    cleanup_grace: float = 2.0,
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="mindie-oc-proc-"))
    proc: subprocess.Popen | None = None
    job_handle = None
    out_thread: threading.Thread | None = None
    err_thread: threading.Thread | None = None
    out_sink = _PipeSink()
    err_sink = _PipeSink()
    cleanup_failed = False
    completed = False
    tree_cleaned = False
    cleanup_deadline: float | None = None
    own_pgid_file = tmp_dir / "pgids.txt"
    started = time.monotonic()
    try:
        ancestors = _ancestor_pgid_files(child_env)
        active_files = [*ancestors, own_pgid_file]
        child_env.pop("_MINDIE_OC_PGID_FILE", None)
        if os.name != "nt":
            child_env["_MINDIE_OC_PGID_FILES"] = os.pathsep.join(str(p) for p in active_files)

        stdin_path = tmp_dir / "stdin.txt"
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
            _register_owned_pgid(proc.pid, active_files)
            job_handle = _attach_windows_job(proc)
            out_thread = threading.Thread(
                target=_drain_pipe_bounded, args=(proc.stdout, max_output, out_sink), daemon=True
            )
            err_thread = threading.Thread(
                target=_drain_pipe_bounded, args=(proc.stderr, max_output, err_sink), daemon=True
            )
            out_thread.start()
            err_thread.start()
            while True:
                rc = proc.poll()
                if rc is not None:
                    completed = True
                    break
                if cancel is not None and cancel.is_set():
                    cleanup_deadline = time.monotonic() + max(0.1, float(cleanup_grace))
                    cleanup_failed = _kill_process_tree(
                        proc,
                        job_handle,
                        pgid_file=own_pgid_file,
                        wait_timeout=max(0.02, cleanup_deadline - time.monotonic()),
                    )
                    tree_cleaned = True
                    job_handle = None
                    raise RuntimeError("operation cancelled by caller")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cleanup_deadline = time.monotonic() + max(0.1, float(cleanup_grace))
                    cleanup_failed = _kill_process_tree(
                        proc,
                        job_handle,
                        pgid_file=own_pgid_file,
                        wait_timeout=max(0.02, cleanup_deadline - time.monotonic()),
                    )
                    tree_cleaned = True
                    job_handle = None
                    raise TimeoutError(f"process timed out after {timeout:.2f}s: {argv[0]}")
                time.sleep(min(0.02, max(0.001, remaining)))
    finally:
        grace_sec = max(0.1, float(cleanup_grace))
        if cleanup_deadline is None:
            cleanup_deadline = time.monotonic() + grace_sec
        if completed:
            drain_reserve = min(0.1, max(0.02, grace_sec * 0.25))
            if proc is not None:
                try:
                    cleanup_failed = (
                        _kill_process_tree(
                            proc,
                            job_handle,
                            pgid_file=own_pgid_file,
                            wait_timeout=max(0.02, cleanup_deadline - drain_reserve - time.monotonic()),
                        )
                        or cleanup_failed
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as cleanup_exc:
                    cleanup_failed = True
                    print(
                        f"MindIE OpenCode bounded.run post-completion cleanup warning: "
                        f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                        file=sys.stderr,
                    )
                finally:
                    job_handle = None
            try:
                if out_thread is not None:
                    out_thread.join(timeout=max(0.02, cleanup_deadline - time.monotonic()))
                if err_thread is not None:
                    err_thread.join(timeout=max(0.02, cleanup_deadline - time.monotonic()))
                if (out_thread is not None and out_thread.is_alive()) or (err_thread is not None and err_thread.is_alive()):
                    cleanup_failed = True
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as drain_exc:
                cleanup_failed = True
                print(
                    f"MindIE OpenCode bounded.run post-completion drain warning: "
                    f"{type(drain_exc).__name__}: {drain_exc}",
                    file=sys.stderr,
                )
            try:
                shutil.rmtree(tmp_dir)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as rm_exc:
                cleanup_failed = True
                print(
                    f"MindIE OpenCode bounded.run post-completion tempdir warning: "
                    f"{type(rm_exc).__name__}: {rm_exc}",
                    file=sys.stderr,
                )
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            try:
                if not tree_cleaned and proc is not None:
                    _kill_process_tree(
                        proc,
                        job_handle,
                        pgid_file=own_pgid_file,
                        wait_timeout=max(0.02, cleanup_deadline - time.monotonic()),
                    )
                elif job_handle is not None and os.name == "nt":
                    _close_windows_job(job_handle)
            finally:
                try:
                    if out_thread is not None:
                        out_thread.join(timeout=max(0.01, cleanup_deadline - time.monotonic()))
                    if err_thread is not None:
                        err_thread.join(timeout=max(0.01, cleanup_deadline - time.monotonic()))
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

    assert proc is not None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    out_bytes, out_trunc = out_sink.snapshot()
    err_bytes, err_trunc = err_sink.snapshot()
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
