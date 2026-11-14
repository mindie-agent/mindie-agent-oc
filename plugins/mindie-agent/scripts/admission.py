"""Session lease admission and factual control-plane status for OpenCode (`oc`).

[Role]
Wraps `mindie_knowledge.loop.activation.Admission` for manual-entry session
lease management (`activate`, `deactivate`, `knowledge_lease`, `status_facts`).

[Invariants]
- `knowledge_lease(session)` is strictly read-only: it never creates the SQLite
  file or activates a session, and returns a lease only when `enabled == 1` and
  `entry_enabled == 1`.
- `status_facts(session)` returns factual control-plane status with
  `harness="opencode"` and without onboarding prompts or implicit service
  activation.

[Timeouts & Locks]
- Uses `Admission`'s WAL + `5.0s` SQLite busy timeout.

[Side-Effect Policy]
- `activate` and `deactivate` mutate `admission_path`; `knowledge_lease` and
  `status_facts` are side-effect free.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time

from bounded import run as bounded_run
from identity import require_session
from oc_native_compat import ensure_oc_native_support
from paths import (
    admission_path,
    atomic_write_json,
    community_config_path,
    config_path,
    consent_path,
    data_home,
    engine_config_path,
    read_json,
    state_dir,
)

ensure_oc_native_support()


def _has_mindie_knowledge() -> bool:
    try:
        return importlib.util.find_spec("mindie_knowledge") is not None
    except (ImportError, ValueError):
        return False


def _pending_dir(config: str | Path | None = None) -> Path:
    return state_dir(config) / "pending-activations"


def _pending_file(session: str, config: str | Path | None = None) -> Path:
    return _pending_dir(config) / f"{session}.json"


@contextmanager
def _pending_lock(config: str | Path | None = None, *, timeout: float = 5.0):
    pdir = _pending_dir(config)
    pdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = pdir / ".pending.lock"
    deadline = time.monotonic() + max(0.1, timeout)
    handle = lock_file.open("a+b")
    locked = False
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
                locked = True
                break
            except TimeoutError:
                raise
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for pending admission lock")
                time.sleep(0.01)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except TimeoutError:
                raise
            except OSError:
                pass
        handle.close()


def _read_pending_lease(session: str, config: str | Path | None = None) -> dict | None:
    pfile = _pending_file(session, config)
    if not pfile.is_file():
        return None
    try:
        data = read_json(pfile)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("session") != session or int(data.get("enabled") or 0) != 1:
        return None
    return data


def flush_pending_cold_activations(
    python_exe: str | None = None,
    *,
    config: str | Path | None = None,
    timeout: float = 10.0,
) -> int:
    """Promote any pre-bootstrap pending session activations into `admission.sqlite3`."""
    if timeout <= 0:
        raise TimeoutError("flush_pending_cold_activations timeout must be positive")
    deadline = time.monotonic() + float(timeout)
    pdir = _pending_dir(config)
    if not pdir.is_dir():
        return 0
    if _has_mindie_knowledge():
        lock_budget = min(2.0, deadline - time.monotonic())
        if lock_budget <= 0:
            raise TimeoutError("timed out before acquiring pending admission lock")
        with _pending_lock(config, timeout=lock_budget):
            pending_files = sorted(pdir.glob("*.json"))
            if not pending_files:
                return 0
            adm = gate(config)
            adm._deadline = deadline
            adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            promoted = 0
            for pfile in pending_files:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("flush_pending_cold_activations exceeded its budget")
                try:
                    item = read_json(pfile)
                    sid = require_session(item.get("session"))
                    if int(item.get("enabled") or 0) != 1:
                        if adm.path.is_file():
                            adm.deactivate(sid, timeout=rem)
                        pfile.unlink(missing_ok=True)
                        continue
                    if adm.path.is_file() and adm.inspect(sid, deadline=deadline).get("status") == "inactive":
                        pfile.unlink(missing_ok=True)
                        continue
                    root = str(Path(str(item.get("project_root") or os.getcwd())).resolve())
                    raw_at = item.get("activated_at")
                    not_before = (
                        float(raw_at)
                        if isinstance(raw_at, (int, float)) and math.isfinite(float(raw_at)) and float(raw_at) > 0
                        else None
                    )
                    adm._write_committed = False
                    adm.activate(sid, project_root=root, root_session=sid, not_before=not_before)
                    if not getattr(adm, "_write_committed", False) and time.monotonic() >= deadline:
                        raise TimeoutError("flush_pending_cold_activations exceeded its budget")
                    pfile.unlink(missing_ok=True)
                    promoted += 1
                except TimeoutError:
                    raise
                except (OSError, ValueError):
                    continue
            return promoted
    if python_exe and Path(python_exe).exists() and os.path.realpath(python_exe) != os.path.realpath(os.sys.executable):
        rem = deadline - time.monotonic()
        if rem <= 0.3:
            raise TimeoutError("insufficient remaining budget to flush pending cold activations")
        grace = min(1.0, max(0.15, rem * 0.25))
        exec_budget = max(0.15, rem - grace)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import admission; print(admission.flush_pending_cold_activations(config=sys.argv[2] or None, timeout=float(sys.argv[3])))"
        )
        res = bounded_run(
            [str(python_exe), "-I", "-c", code, str(Path(__file__).resolve().parent), str(config or ""), f"{exec_budget:.2f}"],
            "",
            timeout=exec_budget,
            cleanup_grace=grace,
        )
        if res.returncode == 0:
            return int((res.stdout or "0").strip() or "0")
    return 0


def _bounded_gate(path: str | Path, *, deadline: float | None = None):
    from mindie_knowledge.loop.activation import Admission

    class _BoundedAdmission(Admission):
        _deadline: float | None = deadline
        _write_committed: bool = False

        def _write(self, fn, *, initialize_missing=False, missing_error=None, timeout=None):
            dl = self._deadline
            if dl is not None:
                rem = dl - time.monotonic()
                timeout = rem if timeout is None else min(float(timeout), rem)
            res = super()._write(
                fn,
                initialize_missing=initialize_missing,
                missing_error=missing_error,
                timeout=timeout,
            )
            self._write_committed = True
            return res

    return _BoundedAdmission(str(path))


def gate(config: str | Path | None = None):
    return _bounded_gate(admission_path(config))


def knowledge_lease(session: str, *, config: str | Path | None = None) -> dict | None:
    session = require_session(session)
    path = admission_path(config)
    if path.is_file() and _has_mindie_knowledge():
        lease = gate(config).knowledge_lease(session)
        if lease is not None:
            return lease
    return _read_pending_lease(session, config)


def activate(
    session: str,
    project_root: str | Path,
    *,
    domain: str | None = None,
    config: str | Path | None = None,
    timeout: float = 6.0,
) -> dict:
    if timeout <= 0:
        raise TimeoutError("session activation timeout must be positive")
    deadline = time.monotonic() + float(timeout)
    session = require_session(session)
    root = str(Path(project_root).resolve())
    if not os.path.isabs(root):
        raise ValueError("project_root must be an absolute path")
    rem_init = deadline - time.monotonic()
    if rem_init <= 0:
        raise TimeoutError("session activation exceeded its budget")
    if not _has_mindie_knowledge():
        lock_budget = min(2.0, rem_init)
        with _pending_lock(config, timeout=lock_budget):
            if time.monotonic() >= deadline:
                raise TimeoutError("session activation exceeded its budget")
            existing = _read_pending_lease(session, config)
            now = float(existing["activated_at"]) if (existing and existing.get("project_root") == root and isinstance(existing.get("activated_at"), (int, float))) else time.time()
            tok = str(existing["token"]) if (existing and existing.get("project_root") == root and isinstance(existing.get("token"), str)) else secrets.token_hex(16)
            pfile = _pending_file(session, config)
            pfile.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write_json(
                pfile,
                dict(
                    session=session,
                    token=tok,
                    enabled=1,
                    entry_enabled=1,
                    failures=0,
                    project_root=root,
                    root_session=session,
                    activated_at=now,
                ),
            )
            try:
                os.chmod(pfile, 0o600)
            except TimeoutError:
                raise
            except OSError:
                pass
        return dict(
            status="bootstrapping",
            mindie_session_id=session,
            mindie_activation=tok,
            activated_at=now,
            project_root=root,
        )
    lock_budget = min(2.0, rem_init - 5.05) if rem_init > 5.2 else min(2.0, rem_init)
    if lock_budget <= 0:
        raise TimeoutError("timed out before acquiring pending admission lock")
    with _pending_lock(config, timeout=lock_budget):
        if time.monotonic() >= deadline:
            raise TimeoutError("session activation exceeded its budget")
        pending_existing = _read_pending_lease(session, config)
        raw_pending_at = pending_existing.get("activated_at") if pending_existing and pending_existing.get("project_root") == root else None
        preserved_at = (
            float(raw_pending_at)
            if isinstance(raw_pending_at, (int, float)) and math.isfinite(float(raw_pending_at)) and float(raw_pending_at) > 0
            else None
        )
        cfg_file = config_path(config)
        if domain is not None and cfg_file.is_file():
            adapter = read_json(cfg_file)
            from mindie_knowledge.loop.cli import config_at
            from mindie_knowledge.loop.domain_routes import select_engine_config
            from mindie_knowledge.loop.settings import from_engine_config

            selected_cfg = select_engine_config(
                adapter["engine_config"],
                domain=domain,
                domain_configs=adapter.get("domain_configs"),
            )
            eng_cfg = config_at(selected_cfg)
            dom_settings = from_engine_config(eng_cfg)
            if not dom_settings.allows_capture():
                raise ValueError("selected domain contribution is not authorized: " + dom_settings.contribution_block_reason())
            if not dom_settings.in_scope(root):
                raise ValueError("native project scope is outside the selected domain contribution authorization")
            dom_adm_path = eng_cfg.get("admission_path")
            if not isinstance(dom_adm_path, str) or not Path(dom_adm_path).is_absolute():
                raise ValueError("selected domain requires an explicit absolute admission_path")
            dom_adm = _bounded_gate(dom_adm_path, deadline=deadline)
            dom_adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lease = dom_adm.activate(session, project_root=root, root_session=session, not_before=preserved_at)
            if not getattr(dom_adm, "_write_committed", False) and time.monotonic() >= deadline:
                raise TimeoutError("session activation exceeded its budget")
            _pending_file(session, config).unlink(missing_ok=True)
            return dict(
                status="active",
                domain=eng_cfg["domain"],
                engine_config=str(selected_cfg),
                generation=dom_settings.generation,
                mindie_session_id=lease["session"],
                mindie_activation=lease["token"],
                activated_at=lease["activated_at"],
                project_root=lease["project_root"],
            )
        adm = gate(config)
        adm._deadline = deadline
        adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = adm.activate(session, project_root=root, root_session=session, not_before=preserved_at)
        if not getattr(adm, "_write_committed", False) and time.monotonic() >= deadline:
            raise TimeoutError("session activation exceeded its budget")
        _pending_file(session, config).unlink(missing_ok=True)
    return dict(
        status="active",
        mindie_session_id=lease["session"],
        mindie_activation=lease["token"],
        activated_at=lease["activated_at"],
        project_root=lease["project_root"],
    )


def deactivate(session: str, *, config: str | Path | None = None, timeout: float = 6.0) -> dict:
    if timeout <= 0:
        raise TimeoutError("session deactivation timeout must be positive")
    deadline = time.monotonic() + float(timeout)
    session = require_session(session)
    rem_init = deadline - time.monotonic()
    lock_budget = min(2.0, rem_init - 5.05) if rem_init > 5.2 else min(2.0, rem_init)
    if lock_budget <= 0:
        raise TimeoutError("timed out before acquiring pending admission lock")
    with _pending_lock(config, timeout=lock_budget):
        pfile = _pending_file(session, config)
        if not _has_mindie_knowledge():
            if pfile.is_file():
                atomic_write_json(pfile, dict(session=session, enabled=0, deactivated_at=time.time()))
            return dict(status="inactive", session=session)
        pfile.unlink(missing_ok=True)
        cfg_file = config_path(config)
        if cfg_file.is_file():
            adapter = read_json(cfg_file)
            domain_configs = adapter.get("domain_configs")
            if isinstance(domain_configs, dict) and domain_configs:
                from mindie_knowledge.loop.domain_routes import deactivate_registered_domains

                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("session deactivation exceeded its budget")
                return deactivate_registered_domains(
                    adapter["engine_config"],
                    domain_configs=domain_configs,
                    session=session,
                    timeout=rem,
                )
        path = admission_path(config)
        if not path.is_file():
            return dict(status="inactive", session=session)
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("session deactivation exceeded its budget")
        gate(config).deactivate(session, timeout=rem)
        return dict(status="inactive", session=session)


def status_facts(session: str | None = None, *, config: str | Path | None = None) -> dict:
    """Return factual control-plane status without onboarding prompts or activation."""
    from setup import sharing_status

    cfg_file = config_path(config)
    configured = cfg_file.is_file()
    lease = None
    if session is not None:
        try:
            lease = knowledge_lease(session, config=config)
        except ValueError:
            lease = None
    sharing = sharing_status(config)
    c_state = sharing["consent_state"]
    c_choice = (
        "unconfigured"
        if c_state == "missing"
        else ("invalid" if c_state in {"corrupt", "unreadable"} else str(sharing["consent_choice"] or "configured"))
    )
    schema_ready: bool | None = None
    schema_warning: str | None = None
    for src_idx, src_file in enumerate((cfg_file, data_home() / "oc-component-update-status.json")):
        if src_file.is_file():
            try:
                src_data = read_json(src_file)
                if src_idx == 0 and type(src_data.get("schema_ready")) is bool:
                    schema_ready = src_data["schema_ready"]
                if schema_warning is None and isinstance(src_data.get("schema_warning"), str) and src_data["schema_warning"]:
                    schema_warning = src_data["schema_warning"]
            except TimeoutError:
                raise
            except (OSError, ValueError):
                pass
    facts = dict(
        harness="opencode",
        configured=configured,
        config_path=str(cfg_file),
        engine_config_path=str(engine_config_path(config)),
        activated=bool(lease is not None and lease.get("enabled") == 1),
        session_id=lease["session"] if lease else session,
        project_root=lease.get("project_root") if lease else None,
        activated_at=lease.get("activated_at") if lease else None,
        consent_choice=c_choice,
        consent_state=c_state,
        community_state=sharing["community_state"],
        diagnostics_state=sharing["diagnostics_state"],
        community_sharing_enabled=sharing["sharing_enabled"],
        reporting_enabled=sharing["reporting_enabled"],
        repair_required=sharing["repair_required"],
        schema_ready=schema_ready,
    )
    if schema_warning is not None:
        facts["schema_warning"] = schema_warning
    return facts
