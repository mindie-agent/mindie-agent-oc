#!/usr/bin/env python3
"""MindIE Agent profile provisioner and runtime bootstrapper for OpenCode (`setup.py`).

[Role]
Initializes and updates the MindIE-owned OpenCode adapter profile (`oc.json`,
`oc.engine.json`, `mindie-community.json`, `mindie-consent.json`), stages
immutable source payloads under `oc-source`, and discovers or bootstraps the
persistent component virtualenv (`oc-venv`).

[Invariants]
- Passive Default: `ensure_default_profile` and `configure_community` write
  MindIE-owned JSON configs atomically via `os.replace()`, never mutate
  `~/.config/opencode` or `~/.local/share/opencode`, and never activate a
  session lease or start the background knowledge loop.
- `--help`, `-h`, and parsed `--dry-run` (including unique prefixes like `--dry`)
  never bootstrap a runtime or write config.

[Timeouts & Locks]
- Local config validation/writes are immediate; first-time `uv sync` runtime
  bootstrap is bounded by `BOOTSTRAP_TIMEOUT = 540.0s`.

[Side-Effect Policy]
- Writes `oc.json`, `oc.engine.json`, `mindie-community.json`, and
  `mindie-consent.json` under `config_home()`, and stages `oc-source` / `oc-venv`
  under `data_home()`.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run, run_supervised_reexec
from contract_validation import validate_contracts as _validate_contracts
from oc_native_compat import ensure_oc_native_support
from paths import (
    admission_path, apply_enterprise_transport_env, atomic_write_json, community_config_path,
    config_path, consent_path, data_home, diagnostics_config_path, engine_config_path,
    installed_source_root, is_usable_venv_python, norm_python_path, plugin_root, read_json,
    repo_root, state_dir, which_host_binary,
)
from runtime_bootstrap import same_volume_uv_cache

ensure_oc_native_support()

BOOTSTRAP_TIMEOUT = 540.0
_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TEXT_SUFFIXES = {".py", ".mjs", ".cjs", ".js", ".json", ".md", ".toml", ".lock", ".svg", ".txt", ".yml", ".yaml"}
_BASELINE_ITEMS = ("package.json", "index.mjs", "pyproject.toml", "uv.lock", "product-contract.json", "harness-compatibility.json", "plugins")


def _copy_lf_clean(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    if src.suffix in _TEXT_SUFFIXES and b"\r\n" in raw:
        raw = raw.replace(b"\r\n", b"\n")
    dst.write_bytes(raw)


def _discover_checkout_source(workspace_root: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    installed_root = installed_source_root()
    if installed_root is not None:
        candidates.append(installed_root)
    candidates.extend([HERE.parent.parent.parent, plugin_root().parent.parent])
    if workspace_root is not None:
        ws = Path(workspace_root).expanduser()
        candidates.extend([ws, ws / "mindie-agent-oc"])
    cwd = Path.cwd()
    candidates.extend([cwd, cwd / "mindie-agent-oc", repo_root(), data_home() / "oc-source"])
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except TimeoutError:
            raise
        except OSError:
            continue
        if (resolved / "pyproject.toml").is_file() and (resolved / "product-contract.json").is_file():
            return resolved
    return None


def _payload_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def stage_source_baseline(
    source_dir: str | Path | None = None,
    dest_dir: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> Path:
    """Publish an immutable payload generation before its profile can activate."""
    src = Path(source_dir).resolve() if source_dir is not None else _discover_checkout_source(workspace_root=workspace_root)
    base = (data_home() / "oc-source").resolve()
    if src is None:
        return base if (base / "pyproject.toml").is_file() else repo_root()
    data_home().mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-stage-", dir=data_home()) as folder:
        staged = Path(folder)
        for item_name in _BASELINE_ITEMS:
            item = src / item_name
            if not item.exists():
                continue
            files = [item] if item.is_file() else sorted(item.rglob("*"))
            for path in files:
                if (
                    path.is_file()
                    and path.suffix != ".pyc"
                    and not any(part in {"__pycache__", ".pytest_cache", ".git", ".venv", "node_modules"} for part in path.relative_to(src).parts)
                ):
                    _copy_lf_clean(path, staged / path.relative_to(src))
        digest = _payload_digest(staged)
        if dest_dir is not None:
            dst = Path(dest_dir).resolve()
            dst.mkdir(parents=True, exist_ok=True)
            for path in staged.rglob("*"):
                if path.is_file():
                    _copy_lf_clean(path, dst / path.relative_to(staged))
            return dst
        version = read_json(staged / "plugins/mindie-agent/plugin.json")["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("plugin payload requires a formal version")
        dst = base / f"{version}-{digest}"
        if dst.is_dir():
            if _payload_digest(dst) != digest:
                raise ValueError("retained plugin payload content changed")
            return dst
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.replace(staged, dst)
        except TimeoutError:
            raise
        except OSError:
            if not dst.is_dir() or _payload_digest(dst) != digest:
                raise
        return dst


def _rem_budget(deadline: float, reserve: float = 0.0) -> float:
    avail = deadline - time.monotonic() - reserve
    if avail <= 0:
        raise TimeoutError("OpenCode setup exceeded its timeout budget")
    return avail


def _probe_runtime_python(py_exe: str | Path, *, timeout: float = 15.0) -> bool:
    cand = Path(py_exe)
    total = min(17.0, float(timeout))
    grace = min(2.0, max(0.15, total * 0.2))
    exec_t = total - grace
    if not cand.is_file() or exec_t <= 0.05:
        return False
    imports = "import mindie_knowledge, remote_dev, mindie_diagnostics, mindie_profiler, mindie_sim, mindie_coordinator, mindie_debugger, npu_top"
    return bounded_run([str(cand), "-I", "-c", imports], "", timeout=exec_t, cleanup_grace=grace).returncode == 0


def _norm_python_path(py_exe: str | Path) -> Path:
    return Path(norm_python_path(py_exe))


def _discover_existing_mindie_python() -> str | None:
    deadline = time.monotonic() + 2.5
    candidates: list[Path] = []
    env_py = os.environ.get("MINDIE_OC_PYTHON")
    if isinstance(env_py, str) and env_py.strip():
        candidates.append(Path(env_py.strip()))
    rel_py = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidates.extend([
        data_home() / "oc-venv" / rel_py,
        HERE.parent.parent.parent / ".venv" / rel_py,
        data_home() / "agy-venv" / rel_py,
        data_home() / "wb-venv" / rel_py,
    ])
    search_dirs = [config_path().parent]
    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    if host_cfg_dir not in search_dirs:
        search_dirs.append(host_cfg_dir)
    for cfg_dir in search_dirs:
        for cfg_name in ("oc.json", "agy.json", "wb.json", "codex.json", "cc.json", "kimi.json"):
            cfg_cand = cfg_dir / cfg_name
            if cfg_cand.is_file():
                try:
                    py = read_json(cfg_cand).get("python")
                    if is_usable_venv_python(py):
                        candidates.append(Path(py))
                except ValueError:
                    pass
    for cand in candidates:
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        if _probe_runtime_python(cand, timeout=min(2.0, rem)):
            return str(_norm_python_path(cand))
    return None


_PROCESS_FAILURE_PHRASES = (
    ("certificate", (
        "certificate verify failed", "certificate_verify_failed", "ssl certificate problem",
        "self-signed certificate", "self signed certificate", "unable to get local issuer",
        "certificate has expired", "tls handshake", "sslcertverificationerror",
    )),
    ("resolver", (
        "no solution found when resolving dependencies", "requirements contain conflicting urls",
        "version solving failed", "no matching distribution", "unsatisfiable",
        "cannot install", "depends on", "dependency resolution failed",
    )),
    ("permission", (
        "permission denied", "access is denied", "operation not permitted", "eacces", "eperm",
        "read-only file system", "os error 13",
    )),
    ("prerequisite", (
        "no space left on device", "disk quota exceeded", "enospc", "edquot", "os error 28",
        "no module named", "not a valid python", "unsupported python", "command not found",
    )),
    ("busy", (
        "resource temporarily unavailable", "text file busy", "database is locked",
        "os error 11", "os error 16", "another process is using", "lock is held",
        "could not acquire lock",
    )),
    ("deferred", (
        "already in progress", "operation in progress", "update in progress",
    )),
    ("temporary_network", (
        "could not resolve host", "temporary failure in name resolution",
        "name or service not known", "connection reset", "connection refused",
        "network is unreachable", "connection timed out", "operation timed out",
        "failed to fetch", "tls", "ssl", "http error", "502 bad gateway", "503 service",
    )),
)
_FAILURE_REASON_CODES = {
    "resolver": "update_resolver_conflict",
    "busy": "update_resource_busy",
    "deferred": "update_deferred",
    "prerequisite": "update_environment_unavailable",
    "temporary_network": "update_transport_unavailable",
    "certificate": "update_certificate_error",
    "permission": "update_permission_denied",
}


def _child_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _process_failure_category(*texts) -> str | None:
    """Map bounded child stdout/stderr to a non-reportable failure category."""
    haystack = "\n".join(_child_text(text) for text in texts)[:65536].lower()
    if not haystack:
        return None
    for category, needles in _PROCESS_FAILURE_PHRASES:
        for needle in needles:
            if needle in haystack:
                return category
    return None


def _raise_process_failure(action: str, proc, *, category: str | None = None) -> None:
    """Raise a classified `RuntimeError` without leaking raw child output."""
    if category is None:
        category = _process_failure_category(
            getattr(proc, "stderr", None), getattr(proc, "stdout", None),
        )
    detail = (_child_text(getattr(proc, "stderr", None)) or _child_text(getattr(proc, "stdout", None))).strip()[:1000]
    summary = f"{category} failure" if category else "unclassified failure"
    error = RuntimeError(f"{action} (exit {getattr(proc, 'returncode', None)}): {summary}")
    if category is not None:
        error.category = category
        error.reason_code = _FAILURE_REASON_CODES.get(category)
    error.process_detail = detail
    raise error


def _current_python_has_runtime() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None
        for mod in ("mindie_knowledge", "remote_dev", "mindie_diagnostics")
    )


def ensure_runtime_python(
    source_root: str | Path | None = None,
    python_exe: str | None = None,
    *,
    config: str | Path | None = None,
    timeout: float = BOOTSTRAP_TIMEOUT,
) -> str:
    """Return an interpreter with MindIE runtime components installed, bootstrapping `oc-venv` if needed."""
    from component_update import _update_lock, compatible_runtime

    if timeout <= 0:
        raise TimeoutError("OpenCode runtime bootstrap timeout must be positive")
    deadline = time.monotonic() + float(timeout)

    def verified(interpreter):
        return compatible_runtime(
            str(interpreter),
            source=source_root or _discover_checkout_source() or repo_root(config),
            config=config,
            timeout=_rem_budget(deadline, 0.0),
        )

    if python_exe:
        return verified(_norm_python_path(python_exe))
    if sys.prefix != sys.base_prefix and _current_python_has_runtime():
        return verified(_norm_python_path(sys.executable))
    existing = _discover_existing_mindie_python() if (deadline - time.monotonic()) >= 3.0 else None
    if existing is not None:
        return verified(existing)
    if _current_python_has_runtime():
        return verified(_norm_python_path(sys.executable))

    venv_dir = data_home() / "oc-venv"
    venv_py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    src_root = stage_source_baseline(source_root)
    uv = which_host_binary("uv", which_fn=shutil.which)
    if uv is None:
        error = RuntimeError("uv is required to bootstrap the MindIE OpenCode runtime")
        error.category = "prerequisite"
        error.reason_code = _FAILURE_REASON_CODES["prerequisite"]
        raise error

    data_home().mkdir(mode=0o700, parents=True, exist_ok=True)
    verify_reserve = min(15.0, max(1.0, float(timeout) * 0.15))
    with _update_lock(data_home() / "oc-bootstrap.lock", blocking_timeout=_rem_budget(deadline, verify_reserve)) as locked:
        if not locked:
            raise TimeoutError("timed out waiting for concurrent oc-venv bootstrap lock")
        if _probe_runtime_python(venv_py, timeout=min(15.0, _rem_budget(deadline, verify_reserve * 0.5))):
            return verified(_norm_python_path(venv_py))
        with tempfile.TemporaryDirectory(prefix=".oc-venv-build-", dir=data_home()) as tmp_build:
            build_dir = Path(tmp_build)
            _copy_lf_clean(src_root / "pyproject.toml", build_dir / "pyproject.toml")
            has_lock = (src_root / "uv.lock").is_file()
            if has_lock:
                _copy_lf_clean(src_root / "uv.lock", build_dir / "uv.lock")
            env = apply_enterprise_transport_env(dict(os.environ))
            env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)
            # A cross-volume default cache turns every clone/hardlink into a full copy; pin the
            # cold `uv sync` cache to the runtime venv's own device unless the caller chose one.
            same_volume_uv_cache(venv_dir, env)
            uv_argv = [uv, "sync", *(["--frozen"] if has_lock else [])]
            uv_argv.extend(["--no-editable", "--no-install-project", "--project", str(build_dir), "--all-extras", "--no-dev", "--python", "3.13"])
            proc = bounded_run(uv_argv, "", timeout=_rem_budget(deadline, verify_reserve), max_output=256 * 1024, env=env)
    if proc.returncode != 0 or not venv_py.is_file():
        _raise_process_failure("uv sync failed while bootstrapping oc-venv", proc)
    return verified(_norm_python_path(venv_py))


def validate_contracts(source_root: str | Path | None = None) -> dict:
    """Validate a candidate payload tree (checkout discovery stays here)."""
    root = source_root if source_root is not None else (_discover_checkout_source() or repo_root())
    return _validate_contracts(root)


def _write_initial_community_config(interpreter: str, comm_file: Path, payload: dict, *, timeout: float = 30.0) -> None:
    if importlib.util.find_spec("mindie_knowledge") is not None:
        from mindie_knowledge.loop import settings as settings_mod

        with settings_mod.CommunityWriteContext(comm_file) as ctx:
            ctx.configure(comm_file, payload)
        return
    code = (
        "import json, sys; from pathlib import Path; from mindie_knowledge.loop import settings; "
        "p = Path(sys.argv[1]); data = json.loads(sys.argv[2]); "
        "ctx = settings.CommunityWriteContext(p); ctx.__enter__(); ctx.configure(p, data); ctx.__exit__(None, None, None)"
    )
    total = max(1.0, float(timeout))
    grace = min(2.0, max(0.2, total * 0.2))
    proc = bounded_run([interpreter, "-I", "-c", code, str(comm_file), json.dumps(payload)], "", timeout=max(0.5, total - grace), cleanup_grace=grace)
    if proc.returncode != 0:
        _raise_process_failure(f"failed to initialize community config via {interpreter}", proc)


def ensure_default_profile(
    *,
    config: str | Path | None = None,
    source_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    domain: str = "vllm-ascend",
    python_exe: str | None = None,
    timeout: float = BOOTSTRAP_TIMEOUT,
    prepare_store: bool = True,
) -> dict:
    if timeout <= 0:
        raise TimeoutError("OpenCode profile setup timeout must be positive")
    deadline = time.monotonic() + float(timeout)
    tail_reserve = min(2.0, max(0.2, float(timeout) * 0.2))
    apply_enterprise_transport_env()
    interpreter = ensure_runtime_python(
        source_root=source_root, python_exe=python_exe, config=config, timeout=_rem_budget(deadline, tail_reserve)
    )

    cfg_file = config_path(config)
    eng_file, sdir, adm_file, comm_file = engine_config_path(cfg_file), state_dir(cfg_file), admission_path(cfg_file), community_config_path(cfg_file)
    data_root = data_home() / "domains"
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    resolved_source = stage_source_baseline(source_dir=source_root, workspace_root=workspace_root)
    scripts_dir = (resolved_source / "plugins/mindie-agent/scripts").resolve()
    validation = validate_contracts(resolved_source)
    gitleaks = which_host_binary("gitleaks", which_fn=shutil.which)
    if not gitleaks:
        cache_dir = Path.home() / ".cache" / "mindie-agent" / "gitleaks"
        if cache_dir.is_dir():
            for candidate in sorted(cache_dir.rglob("gitleaks*"), reverse=True):
                if candidate.is_file() and candidate.name in {"gitleaks", "gitleaks.exe"}:
                    gitleaks = str(candidate.resolve())
                    break

    cfile = consent_path(cfg_file)
    engine_payload = dict(
        read_json(eng_file) if eng_file.is_file() else {},
        schema="mindie-engine-config/1", harness="opencode", domain=domain,
        root=str(data_root), state_dir=str(sdir / "engine"), admission_path=str(adm_file),
        community_config=str(comm_file), settings_path=str(comm_file),
        transcript_adapter=str(scripts_dir / "opencode_transcript.py"),
        summary_command=[interpreter, "-I", str(scripts_dir / "organizer.py")],
        redactor_executable=gitleaks, product_validation=validation,
        feeds=[{"repository": "mindie-agent/knowledge-vllm-ascend", "ref": "main", "domain": domain, "interval_seconds": 300}],
    )
    atomic_write_json(eng_file, engine_payload)

    adapter_payload = dict(
        read_json(cfg_file) if cfg_file.is_file() else {},
        schema="mindie-adapter-config/1", harness="opencode", python=interpreter,
        runtime_scripts=str(scripts_dir), source_root=str(resolved_source),
        engine_config=str(eng_file), state_dir=str(sdir), admission_path=str(adm_file),
        domain=domain, product_validation=validation, schema_ready=False,
    )
    from component_update import _update_lock, sync_store_schema

    with _update_lock(sdir / "oc-profile-schema.lock", blocking_timeout=min(5.0, _rem_budget(deadline, 0.05))) as got:
        if not got:
            raise TimeoutError("timed out acquiring profile schema lock during profile setup")
        atomic_write_json(cfg_file, adapter_payload)
    if prepare_store:
        ready, warn = sync_store_schema(cfg_file, eng_file, interpreter=interpreter, deadline=deadline, update_status_file=False)
        adapter_payload["schema_ready"] = ready
        adapter_payload.pop("schema_warning", None) if warn is None else adapter_payload.__setitem__("schema_warning", warn)

    if not comm_file.is_file():
        _write_initial_community_config(
            interpreter, comm_file,
            dict(
                enabled=False, repository="mindie-agent/knowledge-vllm-ascend",
                project_roots=[], branch="main", idle_seconds=300, consent_config=str(cfile.resolve()),
            ),
            timeout=min(30.0, _rem_budget(deadline, 0.1)),
        )
    if prepare_store or (deadline - time.monotonic()) >= 5.2:
        from admission import flush_pending_cold_activations

        flush_pending_cold_activations(interpreter, config=cfg_file, timeout=min(10.0, _rem_budget(deadline, 0.0)))
    return adapter_payload


def _read_optional_json(path: Path) -> tuple[str, dict]:
    try:
        st = os.lstat(path)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return "missing", {}
    except OSError:
        return "unreadable", {}
    if not stat.S_ISREG(st.st_mode):
        return "unreadable", {}
    try:
        return "ok", read_json(path)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return "missing", {}
    except ValueError:
        return "corrupt", {}
    except OSError:
        return "unreadable", {}


def sharing_status(config: str | Path | None = None) -> dict:
    cfg_file = config_path(config)
    consent_state, consent_data = _read_optional_json(consent_path(cfg_file))
    comm_state, comm_data = _read_optional_json(community_config_path(cfg_file))
    diag_state, diag_data = _read_optional_json(diagnostics_config_path(cfg_file))

    choice = consent_data.get("choice") if consent_state == "ok" else None
    if consent_state == "ok" and choice not in (None, "contribute", "read-only", "later", "disabled"):
        consent_state, choice = "corrupt", None
    if comm_state == "ok" and (
        ("enabled" in comm_data and type(comm_data.get("enabled")) is not bool)
        or ("project_roots" in comm_data and not isinstance(comm_data.get("project_roots"), list))
    ):
        comm_state = "corrupt"
    if diag_state == "ok" and "reporting" in diag_data and not isinstance(diag_data.get("reporting"), dict):
        diag_state = "corrupt"
    choice_revision = consent_data.get("choice_revision") if consent_state == "ok" else None
    comm_bad = comm_state in {"corrupt", "unreadable"}
    diag_bad = diag_state in {"corrupt", "unreadable"}
    repair_required = (consent_state in {"corrupt", "unreadable"}) or comm_bad or diag_bad
    sharing_enabled = None if comm_bad else (bool(comm_data.get("enabled", False)) and choice == "contribute")
    reporting_sub = diag_data.get("reporting") if isinstance(diag_data.get("reporting"), dict) else {}
    reporting_enabled = None if diag_bad else bool(
        reporting_sub.get("enabled", False) or diag_data.get("decision") == "enabled" or diag_data.get("enabled") is True
    )
    return {
        "schema": "mindie-sharing-status/1",
        "harness": "opencode",
        "consent_state": consent_state,
        "community_state": comm_state,
        "diagnostics_state": diag_state,
        "consent_choice": choice,
        "choice_revision": choice_revision,
        "sharing_enabled": sharing_enabled,
        "community_enabled": sharing_enabled,
        "reporting_enabled": reporting_enabled,
        "repository": None if comm_bad else comm_data.get("repository", "mindie-agent/knowledge-vllm-ascend"),
        "fork": None if comm_bad else comm_data.get("fork"),
        "account": None if comm_bad else comm_data.get("account"),
        "project_roots": None if comm_bad else list(comm_data.get("project_roots") or []),
        "needs_onboarding_prompt": (not repair_required) and (choice not in {"contribute", "disabled"}),
        "repair_required": repair_required,
    }


def _validate_sharing_inputs(
    *, repository: str, visibility: str, account: str | None, fork: str | None, project_root: str | Path | None
) -> Path | None:
    if not isinstance(repository, str) or not _REPO_SLUG_RE.fullmatch(repository):
        raise ValueError("invalid repository format; expected owner/repo")
    if visibility != "public":
        raise ValueError("community sharing requires visibility='public'")
    if account is not None and (
        not isinstance(account, str) or not account.strip() or "/" in account or any(c.isspace() for c in account)
    ):
        raise ValueError("invalid GitHub account name")
    if fork is not None and (not isinstance(fork, str) or not _REPO_SLUG_RE.fullmatch(fork)):
        raise ValueError("invalid fork format; expected owner/repo")
    return Path(project_root).expanduser().resolve() if project_root is not None else None


def _resolve_github_fork(
    repository: str, account: str | None, *, deadline: float | None = None
) -> tuple[str | None, str | None]:
    dl = deadline if deadline is not None else (time.monotonic() + 45.0)
    gh = which_host_binary("gh", which_fn=shutil.which)
    if not gh:
        return account, None
    env = apply_enterprise_transport_env(dict(os.environ))
    user_proc = bounded_run([gh, "api", "--hostname", "github.com", "user", "--jq", ".login"], "", timeout=min(10.0, _rem_budget(dl, 0.2)), env=env)
    auth_login = (user_proc.stdout or "").strip() if user_proc.returncode == 0 else ""
    if "/" in auth_login or any(c.isspace() for c in auth_login):
        auth_login = ""
    resolved_account = account or (auth_login or None)
    if not resolved_account:
        return None, None
    candidate_fork = f"{resolved_account}/{repository.split('/', 1)[1]}"
    probe = bounded_run([gh, "api", "--hostname", "github.com", f"repos/{candidate_fork}", "--jq", ".full_name"], "", timeout=min(10.0, _rem_budget(dl, 0.2)), env=env)
    probed_name = (probe.stdout or "").strip()
    if probe.returncode == 0 and probed_name.lower() == candidate_fork.lower():
        return resolved_account, probed_name
    fork_cmd = [gh, "repo", "fork", repository, "--clone=false"]
    if (auth_login and resolved_account.lower() != auth_login.lower()) or (not auth_login and account):
        fork_cmd.extend(["--org", resolved_account])
    fork_proc = bounded_run(fork_cmd, "", timeout=min(15.0, _rem_budget(dl, 0.2)), env=env)
    if fork_proc.returncode != 0:
        return resolved_account, None
    verify = bounded_run([gh, "api", "--hostname", "github.com", f"repos/{candidate_fork}", "--jq", ".full_name"], "", timeout=min(10.0, _rem_budget(dl, 0.1)), env=env)
    verified_name = (verify.stdout or "").strip()
    return resolved_account, (verified_name if verify.returncode == 0 and verified_name.lower() == candidate_fork.lower() else None)


def configure_community(
    *,
    config: str | Path | None = None,
    enabled: bool,
    fork: str | None = None,
    account: str | None = None,
    project_root: str | Path | None = None,
    remote_project_root: str | None = None,
    visibility: str = "public",
    repository: str = "mindie-agent/knowledge-vllm-ascend",
    timeout: float = BOOTSTRAP_TIMEOUT,
) -> dict:
    if timeout <= 0:
        raise TimeoutError("OpenCode community configuration timeout must be positive")
    deadline = time.monotonic() + float(timeout)
    resolved_root_path = _validate_sharing_inputs(
        repository=repository, visibility=visibility, account=account, fork=fork, project_root=project_root
    )
    cfg_file = config_path(config)
    if not cfg_file.is_file():
        ensure_default_profile(config=cfg_file, workspace_root=resolved_root_path, timeout=_rem_budget(deadline, 1.0))
    if importlib.util.find_spec("mindie_knowledge") is None:
        interpreter = read_json(cfg_file).get("python") or ensure_runtime_python(config=cfg_file, timeout=_rem_budget(deadline, 2.0))
        rem_sub = _rem_budget(deadline, 0.0)
        grace = min(2.0, max(0.2, rem_sub * 0.2))
        inner_t = max(0.5, rem_sub - grace)
        argv = [str(interpreter), "-I", str(Path(__file__).resolve()), "sharing", "--config", str(cfg_file), "--timeout", f"{inner_t:.2f}"]
        argv.append("--enable" if enabled else "--disable")
        for flag, val in (
            ("--fork", fork), ("--account", account),
            ("--project-root", str(resolved_root_path) if resolved_root_path is not None else None),
            ("--remote-project-root", remote_project_root), ("--visibility", visibility), ("--repository", repository),
        ):
            if val:
                argv.extend([flag, val])
        proc = bounded_run(argv, "", timeout=inner_t, cleanup_grace=grace)
        if proc.returncode != 0:
            _raise_process_failure(f"failed to configure community sharing via {interpreter}", proc)
        return json.loads(proc.stdout.strip())

    from mindie_knowledge import consent_store
    from mindie_knowledge.loop import settings as settings_mod

    comm_file = community_config_path(cfg_file)
    cfile = consent_path(cfg_file).resolve()
    consent_info = consent_store.read(cfile)
    if consent_info["state"] in ("corrupt", "unreadable"):
        raise consent_store.ConsentError(
            f"consent file is {consent_info['state']}; explicit user repair is required", state=consent_info["state"]
        )

    current = settings_mod.load(comm_file)
    roots = [str(p) for p in current.project_roots]
    if resolved_root_path is not None and str(resolved_root_path) not in roots:
        roots.insert(0, str(resolved_root_path))
    resolved_fork = fork if fork is not None else current.raw.get("fork")
    resolved_account = (
        account
        if account is not None
        else current.raw.get("account")
        or (resolved_fork.split("/", 1)[0] if isinstance(resolved_fork, str) and "/" in resolved_fork else None)
    )
    if enabled and resolved_fork is None and which_host_binary("gh", which_fn=shutil.which):
        resolved_account, resolved_fork = _resolve_github_fork(repository, resolved_account, deadline=deadline)

    write_kwargs = dict(
        enabled=bool(enabled), repository=repository, project_roots=roots, consent_config=str(cfile),
        fork=resolved_fork, account=resolved_account, visibility=visibility,
        remote_project_root=remote_project_root if remote_project_root is not None else current.raw.get("remote_project_root"),
    )
    schema_warn_msg: str | None = None
    if enabled:
        updated = settings_mod.write(comm_file, **write_kwargs)
        consent_store.record_choice(cfile, "contribute")
        eng_file = engine_config_path(cfg_file)
        if eng_file.is_file():
            try:
                eng_doc = read_json(eng_file)
                if isinstance(eng_doc.get("root"), str) and isinstance(eng_doc.get("domain"), str):
                    from component_update import sync_store_schema

                    cfg_py = str(read_json(cfg_file).get("python") or sys.executable) if cfg_file.is_file() else sys.executable
                    ready, warn = sync_store_schema(
                        cfg_file, eng_file, interpreter=cfg_py, deadline=deadline, update_status_file=False
                    )
                    if not ready and warn:
                        schema_warn_msg = warn
            except (TimeoutError, Exception) as exc:
                schema_warn_msg = f"{type(exc).__name__}: {exc}"
                print(f"MindIE OpenCode store schema prepare warning: {schema_warn_msg}", file=sys.stderr)
    else:
        consent_store.record_choice(cfile, "disabled")
        updated = settings_mod.write(comm_file, **write_kwargs)

    out = updated.as_dict()
    if schema_warn_msg is not None:
        out["schema_warning"] = schema_warn_msg
    if hasattr(consent_store, "activate_reporting"):
        try:
            consent_store.activate_reporting(cfile, config=diagnostics_config_path(cfg_file), roots=roots or None, ensure=False)
        except (TimeoutError, Exception) as exc:
            # Community & consent writes already committed; preserve completed result with warning (P4 / Principle 14).
            out["reporting_warning"] = f"{type(exc).__name__}: {exc}"
            print(f"MindIE OpenCode reporting activation warning: {out['reporting_warning']}", file=sys.stderr)
    return out


def _emit_json(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    parser.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS if suppress_default else False,
        help="Print the plan only; do not bootstrap a runtime or write config",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure MindIE Agent for OpenCode")
    _add_dry_run(parser)
    sub = parser.add_subparsers(dest="cmd", required=False)

    init_p = sub.add_parser("init")
    _add_dry_run(init_p, suppress_default=True)
    for flag, default in (("--config", None), ("--workspace-root", None), ("--domain", "vllm-ascend")):
        init_p.add_argument(flag, default=default)
    init_p.add_argument("--timeout", type=float, default=BOOTSTRAP_TIMEOUT)

    share_p = sub.add_parser("sharing")
    _add_dry_run(share_p, suppress_default=True)
    share_p.add_argument("--config", default=None)
    mode = share_p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--status", action="store_true", help="Print read-only sharing status JSON")
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    for flag, default in (
        ("--fork", None), ("--account", None), ("--project-root", None), ("--remote-project-root", None),
        ("--visibility", "public"), ("--repository", "mindie-agent/knowledge-vllm-ascend"),
    ):
        share_p.add_argument(flag, default=default)
    share_p.add_argument("--timeout", type=float, default=BOOTSTRAP_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv) or ["init"]
    parser = _build_parser()
    args = parser.parse_args(incoming)
    if args.cmd == "sharing" and not (args.status or args.enable or args.disable):
        parser.error("sharing requires one of --status, --enable, or --disable")
    if bool(getattr(args, "dry_run", False)):
        return _emit_json({"schema": "mindie-oc-setup/1", "status": "dry-run", "bootstrap": False, "cmd": args.cmd, "uv": which_host_binary("uv", which_fn=shutil.which), "gh": which_host_binary("gh", which_fn=shutil.which)})
    if args.cmd not in {"init", "sharing"}:
        parser.error("a command is required: init | sharing")
    if args.cmd == "sharing" and args.status:
        return _emit_json(sharing_status(args.config))

    budget = float(getattr(args, "timeout", BOOTSTRAP_TIMEOUT))
    if budget <= 0:
        raise TimeoutError("OpenCode setup timeout must be positive")
    deadline = time.monotonic() + budget
    apply_enterprise_transport_env()
    if importlib.util.find_spec("mindie_knowledge") is None and os.environ.get("_MINDIE_OC_SETUP_BOOTSTRAPPED") != "1":
        source = _discover_checkout_source() or repo_root(args.config)
        target_py = ensure_runtime_python(source, config=args.config, timeout=_rem_budget(deadline, 2.0))
        staged_src = stage_source_baseline(source)
        env = apply_enterprise_transport_env(dict(os.environ))
        env["_MINDIE_OC_SETUP_BOOTSTRAPPED"] = "1"
        env["MINDIE_OC_SOURCE_ROOT"] = str(staged_src)
        rem_child = _rem_budget(deadline, 0.0)
        grace = min(2.0, max(0.2, rem_child * 0.2))
        inner_b = max(0.5, rem_child - grace)
        return run_supervised_reexec(
            [target_py, "-I", str(Path(__file__).resolve()), *incoming, "--timeout", f"{inner_b:.2f}"],
            timeout=inner_b, cleanup_grace=grace, env=env, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        )

    rem_exec = _rem_budget(deadline, 0.0)
    if args.cmd == "init":
        result = ensure_default_profile(
            config=args.config, workspace_root=args.workspace_root, domain=args.domain, timeout=rem_exec
        )
    else:
        result = configure_community(
            config=args.config, enabled=bool(args.enable), fork=args.fork, account=args.account,
            project_root=args.project_root, remote_project_root=args.remote_project_root,
            visibility=args.visibility, repository=args.repository, timeout=rem_exec,
        )
    return _emit_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
