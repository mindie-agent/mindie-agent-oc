"""Thin stdlib diagnostics adapter for `mindie-agent-oc`.

[Role]
Bridges OpenCode adapter failures, component CLI crash tracebacks, and
contribution-gated reporting status to `mindie_diagnostics.integration`.

[Invariants]
- Importing this module performs zero I/O or subprocess calls.
- Never includes prompts, transcripts, commands, paths, environment variables,
  or credentials in diagnostic records.
- Respects saved consent authority in `mindie-consent.json` (`effective_reporting`).

[Timeouts & Locks]
- Fault recording uses bounded local reads and append-only JSONL writes only.

[Side-Effect Policy]
- `failure()` and `record_component_crash()` write local diagnostic events under
  the active MindIE diagnostics root; `reporting_status()` is strictly read-only.
"""

from __future__ import annotations

import heapq
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

from paths import config_path, consent_path, diagnostics_config_path

COMPONENT = "mindie-agent-oc"
HOST = "opencode"
REPOSITORY = "mindie-agent/mindie-agent"
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,79}\Z")
_COMPONENT_PACKAGES = {
    "profiler": ("mindie-profiler", "mindie_profiler"),
    "sim": ("mindie-sim", "mindie_sim"),
    "coordinator": ("mindie-coordinator", "mindie_coordinator"),
    "debugger": ("mindie-debugger", "mindie_debugger"),
    "npu_top": ("npu-top", "npu_top"),
}
_REPORTABLE_EXC_TYPES = {
    "RuntimeError": RuntimeError,
    "KeyError": KeyError,
    "AttributeError": AttributeError,
    "IndexError": IndexError,
    "AssertionError": AssertionError,
    "ZeroDivisionError": ZeroDivisionError,
    "ImportError": ImportError,
    "ModuleNotFoundError": ModuleNotFoundError,
    "NotImplementedError": NotImplementedError,
    "RecursionError": RecursionError,
    "UnboundLocalError": UnboundLocalError,
    "NameError": NameError,
    "LookupError": LookupError,
    "ArithmeticError": ArithmeticError,
}
_NON_REPORTABLE_MCP_EXC_NAMES = frozenset({
    "ClientError",
    "RemoteDevError",
    "RemoteExecutionError",
    "EndpointError",
    "PathPolicyError",
    "InvalidToolArguments",
    "CommandTimedOut",
    "OutputLimitExceeded",
    "CoordinationError",
    "MachineDirectoryUnavailable",
    "HostQueueUnavailable",
    "CancelledError",
})
_STDERR_INCIDENT_RE = re.compile(r"(?:^|\n)mindie-diagnostic-incident:\s*([0-9a-f]{32})\s*(?:$|\r?\n)")
_KNOWN_COMPONENT_STAGES = {
    "profiler": frozenset({"profile", "analyze", "compare", "export", "inspect", "summary", "timeline", "doctor", "version"}),
    "sim": frozenset({"solve", "calibrate", "sweep", "roofline", "predict", "inspect", "catalog", "doctor", "version"}),
    "coordinator": frozenset({"hosts", "run", "exec", "status", "logs", "cancel", "stop", "probe", "doctor", "version"}),
    "debugger": frozenset({"precision", "hang", "oom", "accuracy", "triage", "capsule", "inspect", "doctor", "version"}),
    "npu_top": frozenset({"snapshot", "watch", "top", "serve", "daemon", "status", "doctor", "version"}),
}
_DIST_VER_RE = re.compile(r"(?m)^Version:\s*([0-9A-Za-z][0-9A-Za-z.+-]{0,79})\s*$")
_PYPROJ_VER_RE = re.compile(r'(?m)^version\s*=\s*"([0-9A-Za-z][0-9A-Za-z.+-]{0,79})"\s*$')


def _hex(value, length: int) -> str | None:
    if isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _close_fd(fd: int) -> None:
    if fd >= 0:
        try:
            os.close(fd)
        except TimeoutError:
            raise
        except OSError:
            pass


def _read_bytes_bounded(path: Path, limit: int = 16384, *, head_only: bool = False, tail_only: bool = False) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = -1
    try:
        fd = os.open(str(path), flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (not head_only and not tail_only and st.st_size > limit):
            return None
        if tail_only and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
        rem, chunks = (limit if (head_only or tail_only) else limit + 1), []
        while rem > 0 and (buf := os.read(fd, min(rem, 65536))):
            chunks.append(buf)
            rem -= len(buf)
        raw = b"".join(chunks)
        return raw if (head_only or tail_only or len(raw) <= limit) else None
    except TimeoutError:
        raise
    except OSError:
        return None
    finally:
        _close_fd(fd)


def _read_small(path: Path, limit: int = 8192) -> bytes | None:
    return _read_bytes_bounded(path, limit)


def _git_head_revision(repo_dir: Path) -> str | None:
    dot_git = repo_dir / ".git"
    git_dir = dot_git
    common_dir = dot_git
    if dot_git.is_file():
        raw_ptr = _read_small(dot_git, 512)
        if not raw_ptr:
            return None
        ptr = raw_ptr.decode("utf-8", errors="replace").strip()
        if not ptr.startswith("gitdir:"):
            return None
        target = Path(ptr[7:].strip())
        git_dir = target if target.is_absolute() else (repo_dir / target).resolve()
        raw_common = _read_small(git_dir / "commondir", 256)
        if raw_common:
            rel_c = raw_common.decode("utf-8", errors="replace").strip()
            common_dir = (git_dir / rel_c).resolve() if rel_c else git_dir
        else:
            common_dir = git_dir
    raw_head = _read_small(git_dir / "HEAD", 256)
    if not raw_head:
        return None
    head = raw_head.decode("utf-8", errors="replace").strip()
    if direct := _hex(head, 40):
        return direct
    if head.startswith("ref: "):
        ref_rel = head[5:].strip()
        if ref_rel.startswith("refs/") and ".." not in ref_rel:
            for base in (git_dir, common_dir):
                raw_ref = _read_small(base / ref_rel, 256)
                if raw_ref and (rev := _hex(raw_ref.decode("utf-8", errors="replace").strip(), 40)):
                    return rev
            raw_packed = _read_small(common_dir / "packed-refs", 65536)
            if raw_packed:
                suffix = " " + ref_rel
                for line in raw_packed.decode("utf-8", errors="replace").splitlines():
                    if line.endswith(suffix) and (rev := _hex(line[:40], 40)):
                        return rev
    return None


def build_metadata() -> dict:
    here = Path(__file__).resolve().parent
    result: dict = {}
    raw_stamp = _read_small(here / "diagnostic-build.json", 2048)
    if raw_stamp:
        try:
            stamp = json.loads(raw_stamp.decode("utf-8"))
        except ValueError:
            stamp = {}
        if isinstance(stamp, dict):
            if rev := _hex(stamp.get("revision"), 40):
                result["revision"] = rev
            ver = stamp.get("version")
            if isinstance(ver, str) and _VERSION_RE.fullmatch(ver):
                result["version"] = ver
    if "version" not in result:
        for cand, key in (
            (here.parent / "plugin.json", "version"),
            (here.parents[2] / "product-contract.json", "native_version"),
        ):
            raw = _read_small(cand, 16384)
            if not raw:
                continue
            try:
                doc = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            ver = doc.get(key) if isinstance(doc, dict) else None
            if isinstance(ver, str) and _VERSION_RE.fullmatch(ver):
                result["version"] = ver
                break
    if "revision" not in result:
        if rev := _git_head_revision(here.parents[2]):
            result["revision"] = rev
    return result


def default_diagnostics_root() -> str:
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_ROOT")
    if explicit:
        return str(Path(explicit).expanduser())
    if os.name == "nt" and not os.environ.get("XDG_STATE_HOME") and os.environ.get("LOCALAPPDATA"):
        return str(Path(os.environ["LOCALAPPDATA"]) / "mindie" / "diagnostics")
    if os.environ.get("XDG_STATE_HOME"):
        return str(Path(os.environ["XDG_STATE_HOME"]) / "mindie" / "diagnostics")
    return str(Path.home() / ".local" / "state" / "mindie" / "diagnostics")


def _invoke_record(record_fn, component, operation, *, stage, category, exception,
                   revision, version, elapsed_ms, exit_code, reportable,
                   source_root, evidence, reason_code, substage, infer_build_meta=None):
    base = dict(
        stage=stage, category=category, exception=exception, host=HOST,
        revision=revision, version=version, elapsed_ms=elapsed_ms,
        exit_code=exit_code, reportable=reportable,
    )
    extended = dict(base, source_root=source_root, evidence=evidence)
    v2 = dict(extended)
    if reason_code is not None:
        v2["reason_code"] = reason_code
    if substage is not None:
        v2["substage"] = substage
    if infer_build_meta is not None:
        v2["infer_build_meta"] = infer_build_meta
    try:
        return record_fn(component, operation, **v2)
    except TypeError:
        if infer_build_meta is False and getattr(record_fn, "__module__", "") == "mindie_diagnostics.integration":
            from mindie_diagnostics.fallback import record_failure as fb_record
            return fb_record(component, operation, **base)
        try:
            return record_fn(component, operation, **extended)
        except TypeError:
            return record_fn(component, operation, **base)


class _GatewayFailureMarker:
    __slots__ = ("ref",)

    def __init__(self, ref):
        self.ref = ref


def _chain_inner_diagnostic(exc):
    if exc is None:
        return None
    seen: set[int] = set()
    pending = [exc]
    while pending and len(seen) < 32:
        cur = pending.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        marker = getattr(cur, "_mindie_gateway_diag", None)
        if type(marker) is _GatewayFailureMarker and isinstance(marker.ref, dict):
            return dict(marker.ref)
        diag = getattr(cur, "mindie_diagnostic", None)
        if isinstance(diag, dict) and type(diag.get("logging_failed")) is bool:
            if inc := _hex(diag.get("incident_id"), 32):
                return {"incident_id": inc, "logging_failed": diag["logging_failed"]}
            if diag == {"logging_failed": True}:
                return {"logging_failed": True}
        if getattr(cur, "__cause__", None) is not None:
            pending.append(cur.__cause__)
        if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
            pending.append(cur.__context__)
    return None


def failure(operation: str, stage: str, category: str, *, exception=None,
            config=None, revision=None, elapsed_ms=None, exit_code=None,
            reportable: bool = True, reason_code=None, substage=None) -> dict:
    if (inner := _chain_inner_diagnostic(exception)) is not None:
        return inner
    try:
        from mindie_diagnostics.integration import record_failure

        os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(diagnostics_config_path(config))
        build = build_metadata()
        explicit = _hex(revision, 40)
        eff_reason = reason_code if reason_code is not None else getattr(exception, "reason_code", None)
        eff_substage = substage if substage is not None else getattr(exception, "substage", None)
        eff_exit = exit_code if exit_code is not None else getattr(exception, "returncode", None)
        raw = _invoke_record(
            record_failure, COMPONENT, operation, stage=stage, category=category,
            exception=exception, revision=explicit or build.get("revision"),
            version=build.get("version"), elapsed_ms=elapsed_ms,
            exit_code=eff_exit if type(eff_exit) is int else None,
            reportable=reportable, source_root=Path(__file__).resolve().parent,
            evidence=getattr(exception, "mindie_failure_evidence", None),
            reason_code=eff_reason, substage=eff_substage,
        )
        if not isinstance(raw, dict):
            out = {"logging_failed": True}
        else:
            out = {"recorded": bool(raw.get("recorded")), "logging_failed": bool(raw.get("logging_failed"))}
            if inc := _hex(raw.get("incident_id"), 32):
                out["incident_id"] = inc
    except TimeoutError:
        raise
    except Exception:
        out = {"logging_failed": True}
    if exception is not None:
        try:
            exception._mindie_gateway_diag = _GatewayFailureMarker(out)
        except Exception:
            pass
    return out


def classify_update_exception(exc: BaseException) -> tuple[str, bool, str | None]:
    """Map transient transport/TLS/HTTP/DNS update failures without masking local bugs."""
    import http.client
    import socket
    import ssl
    import urllib.error

    local_http_bugs = (
        http.client.InvalidURL,
        http.client.ImproperConnectionState,
        http.client.NotConnected,
        http.client.UnimplementedFileMode,
    )
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout", False, None
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < 16:
        cur = pending.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        cls_name = type(cur).__name__
        if cls_name == "ReleaseResponseTooLarge" or isinstance(cur, local_http_bugs):
            return "update", True, None
        if isinstance(cur, (TimeoutError, subprocess.TimeoutExpired)):
            return "timeout", False, None
        if isinstance(cur, urllib.error.HTTPError) or cls_name == "ReleaseFetchError":
            status = getattr(cur, "code", None) if isinstance(cur, urllib.error.HTTPError) else getattr(cur, "status", None)
            code = getattr(cur, "code", None) if cls_name == "ReleaseFetchError" else None
            code_s = code if isinstance(code, str) else None
            if code_s == "release_certificate_error":
                return "certificate", False, "release_certificate_error"
            if status == 429 or (status == 403 and code_s == "release_rate_limited"):
                return "rate_limited", False, code_s or "release_rate_limited"
            return "temporary_network", False, code_s or "release_http_error"
        if isinstance(cur, ssl.SSLCertVerificationError):
            return "certificate", False, "release_certificate_error"
        if isinstance(cur, urllib.error.URLError):
            if isinstance(getattr(cur, "reason", None), ssl.SSLCertVerificationError):
                return "certificate", False, "release_certificate_error"
            return "temporary_network", False, "release_transport_unavailable"
        if isinstance(cur, (ssl.SSLError, ConnectionError, socket.gaierror, socket.herror, http.client.HTTPException)):
            return "temporary_network", False, "release_transport_unavailable"
        if getattr(cur, "__cause__", None) is not None:
            pending.append(cur.__cause__)
        if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
            pending.append(cur.__context__)
    return "update", True, None


def is_internal_mcp_exception(exc: BaseException) -> bool:
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if not (mro_names & (_REPORTABLE_EXC_TYPES.keys() | {"TypeError"})):
        return False
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < 32:
        cur = pending.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(
            cur,
            (ValueError, OSError, TimeoutError, subprocess.SubprocessError, KeyboardInterrupt, SystemExit, GeneratorExit),
        ):
            return False
        if {cls.__name__ for cls in type(cur).__mro__} & _NON_REPORTABLE_MCP_EXC_NAMES:
            return False
        cat = getattr(cur, "category", None)
        if isinstance(cat, str) and cat:
            return False
        if getattr(cur, "__cause__", None) is not None:
            pending.append(cur.__cause__)
        if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
            pending.append(cur.__context__)
    if pending:
        return False
    scripts_prefix = os.path.normcase(str(Path(__file__).resolve().parent)) + os.sep
    tb = exc.__traceback__
    steps = 0
    while tb is not None and steps < 64:
        filename = getattr(tb.tb_frame.f_code, "co_filename", "")
        if isinstance(filename, str) and filename:
            if os.path.normcase(os.path.abspath(filename)).startswith(scripts_prefix):
                return True
        tb = tb.tb_next
        steps += 1
    return False


def _scan_subdirs_bounded(parent: Path, *, prefix: str = "", suffix: str = "") -> list[Path]:
    matches: list[Path] = []
    try:
        with os.scandir(parent) as it:
            for idx, entry in enumerate(it):
                if idx >= 512 or len(matches) >= 4:
                    break
                if (not prefix or entry.name.startswith(prefix)) and (not suffix or entry.name.endswith(suffix)):
                    if entry.is_dir(follow_symlinks=False):
                        matches.append(Path(entry.path))
    except TimeoutError:
        raise
    except OSError:
        return []
    return sorted(matches)


def _file_url_to_path(url: str) -> Path | None:
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse
    raw = unquote(urlparse(url).path or "")
    return Path(raw[1:] if re.match(r"^/[A-Za-z]:/", raw) else raw) if raw else None


def _inspect_dist_info_full(site_dir: Path, dist_norm: str) -> tuple[str | None, str | None, Path | None]:
    for dist_dir in _scan_subdirs_bounded(site_dir, prefix=f"{dist_norm}-", suffix=".dist-info"):
        ver = rev = editable_co = None
        if raw_meta := _read_bytes_bounded(dist_dir / "METADATA", 32768):
            m = _DIST_VER_RE.search(raw_meta.decode("utf-8", errors="replace"))
            ver = m.group(1) if m else None
        if raw_du := _read_bytes_bounded(dist_dir / "direct_url.json", 8192):
            try:
                du = json.loads(raw_du.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                du = {}
            if isinstance(du, dict):
                vcs, dir_info = du.get("vcs_info"), du.get("dir_info")
                rev = _hex(vcs.get("commit_id"), 40) if isinstance(vcs, dict) else None
                if isinstance(dir_info, dict) and dir_info.get("editable") is True and isinstance(du.get("url"), str):
                    editable_co = _file_url_to_path(du["url"])
                    rev = rev or (_git_head_revision(editable_co) if editable_co is not None else None)
        if ver or rev or editable_co is not None:
            return ver, rev, editable_co
    return None, None, None


def _inspect_dist_info(site_dir: Path, dist_norm: str) -> tuple[str | None, str | None]:
    return _inspect_dist_info_full(site_dir, dist_norm)[:2]


def _inspect_checkout(repo_dir: Path, comp_name: str) -> tuple[str | None, str | None]:
    if not (raw_pp := _read_bytes_bounded(repo_dir / "pyproject.toml", 32768)):
        return None, None
    text = raw_pp.decode("utf-8", errors="replace")
    if f'name = "{comp_name}"' not in text and f"name = '{comp_name}'" not in text:
        return None, None
    m = _PYPROJ_VER_RE.search(text)
    return (m.group(1) if m else None), _git_head_revision(repo_dir)


def _venv_roots_from_executable(executable) -> list[Path]:
    if not isinstance(executable, (str, os.PathLike)) or not str(executable):
        return []
    try:
        exe_abs = Path(os.path.abspath(str(Path(executable).expanduser())))
    except TimeoutError:
        raise
    except OSError:
        return []
    roots = [exe_abs.parent.parent] if exe_abs.parent.name.lower() in {"bin", "scripts"} else []
    if (head := _read_bytes_bounded(exe_abs, 256, head_only=True)) and head.startswith(b"#!"):
        line = head.splitlines()[0][2:].decode("utf-8", errors="replace").strip()
        line = line[13:].strip() if line.startswith("/usr/bin/env ") else line
        tok = line.strip("\"'").split()[0] if line else ""
        if tok and os.path.isabs(tok):
            try:
                sb = Path(os.path.abspath(tok))
                if sb.parent.name.lower() in {"bin", "scripts"} and sb.parent.parent not in roots:
                    roots.append(sb.parent.parent)
            except TimeoutError:
                raise
            except OSError:
                pass
    try:
        res = exe_abs.resolve()
        if res.parent.name.lower() in {"bin", "scripts"} and res.parent.parent not in roots:
            roots.append(res.parent.parent)
    except TimeoutError:
        raise
    except OSError:
        pass
    return roots


def _venv_site_dirs(venv_root: Path) -> list[Path]:
    return [venv_root / "Lib" / "site-packages", *[d / "site-packages" for d in _scan_subdirs_bounded(venv_root / "lib", prefix="python")]]


def _resolve_child_component_build(comp_name: str, pkg_roots: list[str], executable=None) -> tuple[str | None, str | None]:
    dist_norm = comp_name.lower().replace("-", "_")
    for raw_root in pkg_roots[:4]:
        rp = Path(raw_root)
        d_ver, d_rev = _inspect_dist_info(rp, dist_norm)
        if d_ver or d_rev:
            return d_ver, d_rev
        if rp.name != "site-packages" and (rp / ".git").exists():
            c_ver, c_rev = _inspect_checkout(rp, comp_name)
            if c_ver or c_rev:
                return c_ver, c_rev
    for venv_root in _venv_roots_from_executable(executable):
        for sp in _venv_site_dirs(venv_root):
            d_ver, d_rev = _inspect_dist_info(sp, dist_norm)
            if d_ver or d_rev:
                return d_ver, d_rev
    return None, None


_FATAL_SIGNAL_REASONS = {
    code: reason
    for sig_name, reason in (
        ("SIGILL", "component_sigill"), ("SIGABRT", "component_sigabrt"),
        ("SIGBUS", "component_sigbus"), ("SIGFPE", "component_sigfpe"),
        ("SIGSEGV", "component_sigsegv"),
    )
    if (sig_num := getattr(signal, sig_name, None)) is not None
    for code in (-int(sig_num), 128 + int(sig_num))
}


def _verified_failure_ids(comp_name: str, *, invocation_token: str | None = None, config=None) -> list[str]:
    tok = _hex(invocation_token, 32)
    if not tok:
        return []
    if callable(cfg_fn := globals().get("diagnostics_config_path")):
        try:
            if config is not None:
                os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(cfg_fn(config))
            else:
                os.environ.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_fn(None)))
        except TimeoutError:
            raise
        except Exception:
            pass
    if callable(binder := globals().get("bind_environment")):
        try:
            binder()
        except TimeoutError:
            raise
        except Exception:
            pass
    try:
        from mindie_diagnostics.fallback import default_root
        ev_dir = Path(default_root()) / "events" / comp_name
    except TimeoutError:
        raise
    except Exception:
        if not (raw_root := os.environ.get("MINDIE_DIAGNOSTICS_ROOT")):
            return []
        ev_dir = Path(raw_root).expanduser() / "events" / comp_name
    top_cands: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(ev_dir) as it:
            for idx, entry in enumerate(it):
                if idx >= 4096:
                    break
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime_ns = entry.stat(follow_symlinks=False).st_mtime_ns
                except TimeoutError:
                    raise
                except OSError:
                    continue
                item = (mtime_ns, entry.name, Path(entry.path))
                if len(top_cands) < 8:
                    heapq.heappush(top_cands, item)
                elif item > top_cands[0]:
                    heapq.heapreplace(top_cands, item)
    except TimeoutError:
        raise
    except OSError:
        return []
    found: list[str] = []
    for _, _, fp in sorted(top_cands, reverse=True):
        raw = _read_bytes_bounded(fp, 65536, tail_only=True)
        if not raw:
            continue
        for raw_ln in reversed(raw.splitlines()):
            ln = raw_ln.strip()
            if not (ln.startswith(b"{") and ln.endswith(b"}")):
                continue
            try:
                obj = json.loads(ln.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if (
                isinstance(obj, dict)
                and obj.get("component") == comp_name
                and obj.get("event") == "operation.end"
                and obj.get("status") == "error"
                and obj.get("reportable") is True
                and obj.get("invocation_token") == tok
                and (oid := _hex(obj.get("operation_id"), 32))
                and oid not in found
            ):
                found.append(oid)
    return found


def _verified_local_incident(
    comp_name: str, inc_id: str, *, invocation_token: str | None = None, config=None
) -> bool:
    inc = _hex(inc_id, 32)
    if not inc:
        return False
    return inc in _verified_failure_ids(comp_name, invocation_token=invocation_token, config=config)


def record_component_crash(
    tool_name: str,
    argv,
    exit_code,
    stderr: str,
    *,
    parsed_result=None,
    config=None,
    executable=None,
    invocation_token=None,
):
    """Return a provenance-verified child crash receipt or fatal native signal crash envelope."""
    target = _COMPONENT_PACKAGES.get(tool_name)
    if target is None or exit_code in (0, 2, None):
        return None
    comp_name, _pkg_prefix = target
    verified = _verified_failure_ids(comp_name, invocation_token=invocation_token, config=config)
    if verified:
        diag_d = parsed_result.get("diagnostic") if isinstance(parsed_result, dict) else None
        if not isinstance(diag_d, dict) and isinstance(parsed_result, dict) and isinstance(parsed_result.get("structuredContent"), dict):
            diag_d = parsed_result["structuredContent"].get("diagnostic")
        parsed_inc = _hex(diag_d.get("incident_id"), 32) if isinstance(diag_d, dict) else None
        if parsed_inc in verified:
            return {"incident_id": parsed_inc, "logging_failed": False}
        if isinstance(stderr, str):
            for inc_id in reversed(_STDERR_INCIDENT_RE.findall(stderr)[-4:]):
                if inc_id in verified:
                    return {"incident_id": inc_id, "logging_failed": False}
        return {"incident_id": verified[0], "logging_failed": False}
    sig_reason = _FATAL_SIGNAL_REASONS.get(exit_code) if type(exit_code) is int else None
    if sig_reason is None:
        return None
    allowed_stages = _KNOWN_COMPONENT_STAGES.get(tool_name, frozenset())
    first_arg = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else None
    stage = first_arg if first_arg in allowed_stages else "cli"
    c_ver, c_rev = _resolve_child_component_build(comp_name, [], executable=executable)
    try:
        from mindie_diagnostics.integration import record_failure
        os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(diagnostics_config_path(config))
        raw = _invoke_record(
            record_failure, comp_name, "cli.execute", stage=stage, category="subprocess", exception=None,
            revision=c_rev, version=c_ver, elapsed_ms=None, exit_code=exit_code, reportable=True,
            source_root=None, evidence=None, reason_code=sig_reason, substage=None,
            infer_build_meta=False,
        )
        inc = _hex(raw.get("incident_id"), 32) if isinstance(raw, dict) else None
        return {"incident_id": inc, "logging_failed": False} if inc else {"logging_failed": True}
    except TimeoutError:
        raise
    except Exception:
        return {"logging_failed": True}


def reporting_status(*, config=None) -> dict:
    try:
        from mindie_diagnostics.integration import reporting_status as upstream
        from mindie_knowledge.consent_store import read as read_consent

        cfg = config_path(config)
        view = upstream(config=diagnostics_config_path(cfg))
        saved = read_consent(consent_path(cfg))
        if saved.get("state") in {"corrupt", "unreadable"}:
            return dict(view, enabled=False, status="unavailable", consent=saved)
        if not saved.get("choice_revision"):
            return dict(view, enabled=False, status="needs_choice", consent=saved)
        if saved.get("choice") != "contribute":
            return dict(view, enabled=False, status="disabled", consent=saved)
        return dict(view, consent=saved)
    except TimeoutError:
        raise
    except Exception as exc:
        return {"enabled": None, "status": "unavailable", "error": {"stage": "reporting_status", "type": type(exc).__name__}}
