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

import json
import os
from pathlib import Path
import re
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
_TB_FILE_RE = re.compile(
    r'^\s*File "([^"]+)", line (\d+), in ([A-Za-z_][A-Za-z0-9_]{0,99}|<module>|<lambda>|<listcomp>|<dictcomp>|<setcomp>|<genexpr>)\s*$'
)
_TB_EXC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,119})(?::(?:\s.*|$)|\s*$)")
_STDERR_INCIDENT_RE = re.compile(r"(?:^|\n)mindie-diagnostic-incident:\s*([0-9a-f]{32})\s*(?:$|\r?\n)")
_SUBCMD_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DIST_VER_RE = re.compile(r"(?m)^Version:\s*([0-9A-Za-z][0-9A-Za-z.+-]{0,79})\s*$")
_PYPROJ_VER_RE = re.compile(r'(?m)^version\s*=\s*"([0-9A-Za-z][0-9A-Za-z.+-]{0,79})"\s*$')


def _hex(value, length: int) -> str | None:
    if isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _read_small(path: Path, limit: int = 8192) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        raw = path.read_bytes()
        return raw if len(raw) <= limit else None
    except TimeoutError:
        raise
    except OSError:
        return None


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
                   source_root, evidence, reason_code, substage):
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
    try:
        return record_fn(component, operation, **v2)
    except TypeError:
        try:
            return record_fn(component, operation, **extended)
        except TypeError:
            return record_fn(component, operation, **base)


def failure(operation: str, stage: str, category: str, *, exception=None,
            config=None, revision=None, elapsed_ms=None, exit_code=None,
            reportable: bool = True, reason_code=None, substage=None) -> dict:
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
            return {"logging_failed": True}
        out = {"recorded": bool(raw.get("recorded")), "logging_failed": bool(raw.get("logging_failed"))}
        if inc := _hex(raw.get("incident_id"), 32):
            out["incident_id"] = inc
        return out
    except TimeoutError:
        raise
    except Exception:
        return {"logging_failed": True}


def is_internal_mcp_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ValueError, OSError, TimeoutError, subprocess.SubprocessError)):
        return False
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if mro_names & _NON_REPORTABLE_MCP_EXC_NAMES:
        return False
    if not (mro_names & (_REPORTABLE_EXC_TYPES.keys() | {"TypeError"})):
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


def _file_url_to_path(url: str) -> Path | None:
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse

    raw_path = unquote(urlparse(url).path or "")
    if re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    return Path(raw_path) if raw_path else None


def _inspect_dist_info(site_dir: Path, dist_norm: str) -> tuple[str | None, str | None]:
    try:
        if not site_dir.is_dir():
            return None, None
        candidates = sorted(site_dir.glob(f"{dist_norm}-*.dist-info"))[:4]
    except TimeoutError:
        raise
    except OSError:
        return None, None
    for dist_dir in candidates:
        ver = None
        rev = None
        raw_meta = _read_small(dist_dir / "METADATA", 32768)
        if raw_meta:
            m = _DIST_VER_RE.search(raw_meta.decode("utf-8", errors="replace"))
            if m:
                ver = m.group(1)
        raw_du = _read_small(dist_dir / "direct_url.json", 8192)
        if raw_du:
            try:
                du = json.loads(raw_du.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                du = {}
            if isinstance(du, dict):
                vcs = du.get("vcs_info")
                if isinstance(vcs, dict):
                    rev = _hex(vcs.get("commit_id"), 40)
                if not rev and isinstance(du.get("url"), str):
                    co = _file_url_to_path(du["url"])
                    if co is not None:
                        rev = _git_head_revision(co)
        if ver or rev:
            return ver, rev
    return None, None


def _inspect_checkout(repo_dir: Path, comp_name: str) -> tuple[str | None, str | None]:
    raw_pp = _read_small(repo_dir / "pyproject.toml", 32768)
    if not raw_pp:
        return None, None
    text = raw_pp.decode("utf-8", errors="replace")
    if f'name = "{comp_name}"' not in text and f"name = '{comp_name}'" not in text:
        return None, None
    m = _PYPROJ_VER_RE.search(text)
    ver = m.group(1) if m else None
    rev = _git_head_revision(repo_dir)
    return ver, rev


def _venv_roots_from_executable(executable) -> list[Path]:
    if not isinstance(executable, (str, os.PathLike)) or not str(executable):
        return []
    roots: list[Path] = []
    try:
        exe_abs = Path(os.path.abspath(str(Path(executable).expanduser())))
    except TimeoutError:
        raise
    except OSError:
        return []
    if exe_abs.parent.name.lower() in {"bin", "scripts"}:
        roots.append(exe_abs.parent.parent)
    try:
        if exe_abs.is_file():
            with exe_abs.open("rb") as stream:
                head = stream.read(256)
            if head.startswith(b"#!"):
                first_line = head.splitlines()[0][2:].decode("utf-8", errors="replace").strip()
                if first_line.startswith("/usr/bin/env "):
                    first_line = first_line[len("/usr/bin/env "):].strip()
                token = first_line.strip("\"'").split()[0] if first_line else ""
                if token and os.path.isabs(token):
                    sb = Path(os.path.abspath(token))
                    if sb.parent.name.lower() in {"bin", "scripts"} and sb.parent.parent not in roots:
                        roots.append(sb.parent.parent)
    except TimeoutError:
        raise
    except OSError:
        pass
    try:
        resolved = exe_abs.resolve()
        if resolved.parent.name.lower() in {"bin", "scripts"} and resolved.parent.parent not in roots:
            roots.append(resolved.parent.parent)
    except TimeoutError:
        raise
    except OSError:
        pass
    return roots


def _resolve_child_component_build(comp_name: str, pkg_roots: list[str], executable=None) -> tuple[str | None, str | None]:
    dist_norm = comp_name.lower().replace("-", "_")
    ver: str | None = None
    rev: str | None = None
    for raw_root in pkg_roots[:4]:
        root_path = Path(raw_root)
        d_ver, d_rev = _inspect_dist_info(root_path, dist_norm)
        ver = ver or d_ver
        rev = rev or d_rev
        if not ver or not rev:
            c_ver, c_rev = _inspect_checkout(root_path, comp_name)
            ver = ver or c_ver
            rev = rev or c_rev
        if ver and rev:
            return ver, rev
    for venv_root in _venv_roots_from_executable(executable):
        site_dirs = [venv_root / "Lib" / "site-packages"]
        lib_dir = venv_root / "lib"
        try:
            if lib_dir.is_dir():
                site_dirs.extend(sorted(lib_dir.glob("python*/site-packages"))[:4])
        except TimeoutError:
            raise
        except OSError:
            pass
        for sp in site_dirs:
            d_ver, d_rev = _inspect_dist_info(sp, dist_norm)
            ver = ver or d_ver
            rev = rev or d_rev
            if ver and rev:
                return ver, rev
    return ver, rev


def _extract_owned_traceback_with_roots(stderr: str, pkg_prefix: str) -> tuple[list[dict], list[str], list[str]]:
    if not isinstance(stderr, str) or "Traceback (most recent call last):" not in stderr:
        return [], [], []
    tail = stderr[stderr.rfind("Traceback (most recent call last):"):][:65536]
    frames: list[dict] = []
    roots: list[str] = []
    saw_frame = False
    exc_name: str | None = None
    marker = f"/{pkg_prefix}/"
    for line in tail.splitlines()[1:1000]:
        normalized = line.replace("\\", "/")
        match = _TB_FILE_RE.match(normalized)
        if match:
            saw_frame = True
            raw_path, lineno_s, func = match.groups()
            idx = raw_path.rfind(marker)
            if idx != -1:
                parent_dir = raw_path[:idx]
                if parent_dir and parent_dir not in roots:
                    roots.append(parent_dir)
                rel = raw_path[idx + 1:]
                if rel.endswith(".py"):
                    rel = rel[:-3]
                parts = [p for p in rel.split("/") if p and p != "__init__"]
                mod = ".".join(parts) if parts else pkg_prefix
                lineno = int(lineno_s)
                if 1 <= lineno <= 1_000_000:
                    frames.append({"module": mod[:120], "function": func, "line": lineno})
            continue
        if line and not line[0].isspace():
            if saw_frame:
                exc_match = _TB_EXC_RE.match(line)
                if exc_match:
                    candidate = exc_match.group(1).rsplit(".", 1)[-1]
                    if candidate in _REPORTABLE_EXC_TYPES:
                        exc_name = candidate
            break
    if not frames or exc_name is None:
        return [], [], []
    return frames[-12:], [exc_name], roots


def _extract_owned_traceback(stderr: str, pkg_prefix: str) -> tuple[list[dict], list[str]]:
    frames, chain, _ = _extract_owned_traceback_with_roots(stderr, pkg_prefix)
    return frames, chain


def record_component_crash(
    tool_name: str, argv, exit_code, stderr: str, *, parsed_result=None, config=None, executable=None
):
    """Record an unhandled internal Python traceback from an owned component CLI."""
    if isinstance(parsed_result, dict) and isinstance(parsed_result.get("diagnostic"), dict):
        diag = parsed_result["diagnostic"]
        if _hex(diag.get("incident_id"), 32):
            return {"incident_id": diag["incident_id"], "logging_failed": bool(diag.get("logging_failed", False))}
    if isinstance(stderr, str):
        inc_match = _STDERR_INCIDENT_RE.search(stderr)
        if inc_match:
            return {"incident_id": inc_match.group(1), "logging_failed": False}
    target = _COMPONENT_PACKAGES.get(tool_name)
    if target is None or exit_code in (0, 2, None):
        return None
    comp_name, pkg_prefix = target
    frames, chain, pkg_roots = _extract_owned_traceback_with_roots(stderr, pkg_prefix)
    if not frames or not chain:
        return None
    stage = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) and _SUBCMD_RE.fullmatch(argv[0]) else "cli"
    synth_cls = _REPORTABLE_EXC_TYPES.get(chain[0], RuntimeError)
    synth = synth_cls("component cli unhandled traceback")
    evidence = {"stack_frames": frames, "exception_chain": chain, "reason_code": "unhandled_component_traceback"}
    try:
        from mindie_diagnostics.integration import record_failure

        os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(diagnostics_config_path(config))
        comp_ver, comp_rev = _resolve_child_component_build(comp_name, pkg_roots, executable=executable)
        raw = _invoke_record(
            record_failure, comp_name, "cli.execute", stage=stage, category="internal",
            exception=synth, revision=comp_rev, version=comp_ver, elapsed_ms=None,
            exit_code=exit_code, reportable=True, source_root=None,
            evidence=evidence, reason_code="unhandled_component_traceback", substage=None,
        )
        if isinstance(raw, dict) and _hex(raw.get("incident_id"), 32):
            return {"incident_id": raw["incident_id"], "logging_failed": bool(raw.get("logging_failed", False))}
        return None
    except TimeoutError:
        raise
    except Exception:
        return None


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
