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


def flush_pending_cold_activations(python_exe: str | None = None, *, config: str | Path | None = None) -> int:
    """Promote any pre-bootstrap pending session activations into `admission.sqlite3`."""
    pdir = _pending_dir(config)
    if not pdir.is_dir():
        return 0
    if _has_mindie_knowledge():
        with _pending_lock(config):
            pending_files = sorted(pdir.glob("*.json"))
            if not pending_files:
                return 0
            adm = gate(config)
            adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            promoted = 0
            for pfile in pending_files:
                try:
                    item = read_json(pfile)
                    sid = require_session(item.get("session"))
                    if int(item.get("enabled") or 0) != 1:
                        if adm.path.is_file():
                            adm.deactivate(sid)
                        pfile.unlink(missing_ok=True)
                        continue
                    if adm.path.is_file() and adm.inspect(sid).get("status") == "inactive":
                        pfile.unlink(missing_ok=True)
                        continue
                    root = str(Path(str(item.get("project_root") or os.getcwd())).resolve())
                    raw_at = item.get("activated_at")
                    not_before = (
                        float(raw_at)
                        if isinstance(raw_at, (int, float)) and math.isfinite(float(raw_at)) and float(raw_at) > 0
                        else None
                    )
                    adm.activate(sid, project_root=root, root_session=sid, not_before=not_before)
                    pfile.unlink(missing_ok=True)
                    promoted += 1
                except TimeoutError:
                    raise
                except (OSError, ValueError):
                    continue
            return promoted
    if python_exe and Path(python_exe).exists() and os.path.realpath(python_exe) != os.path.realpath(os.sys.executable):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import admission; print(admission.flush_pending_cold_activations(config=sys.argv[2] or None))"
        )
        res = bounded_run(
            [str(python_exe), "-I", "-c", code, str(Path(__file__).resolve().parent), str(config or "")],
            "",
            timeout=10.0,
        )
        if res.returncode == 0:
            return int((res.stdout or "0").strip() or "0")
    return 0


def gate(config: str | Path | None = None):
    from mindie_knowledge.loop.activation import Admission

    return Admission(str(admission_path(config)))


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
) -> dict:
    session = require_session(session)
    root = str(Path(project_root).resolve())
    if not os.path.isabs(root):
        raise ValueError("project_root must be an absolute path")
    if not _has_mindie_knowledge():
        with _pending_lock(config):
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
    with _pending_lock(config):
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
            from mindie_knowledge.loop.domain_routes import activate_selected_domain

            selected = activate_selected_domain(
                adapter["engine_config"],
                domain=domain,
                domain_configs=adapter.get("domain_configs"),
                session=session,
                project_root=root,
                root_session=session,
            )
            lease = selected["lease"]
            _pending_file(session, config).unlink(missing_ok=True)
            return dict(
                status="active",
                domain=selected["domain"],
                engine_config=selected["engine_config"],
                generation=selected["generation"],
                mindie_session_id=lease["session"],
                mindie_activation=lease["token"],
                activated_at=lease["activated_at"],
                project_root=lease["project_root"],
            )
        adm = gate(config)
        adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = adm.activate(session, project_root=root, root_session=session, not_before=preserved_at)
        _pending_file(session, config).unlink(missing_ok=True)
    eng_file = engine_config_path(config)
    if eng_file.is_file():
        try:
            from mindie_knowledge.loop.handoff import prepare_schema

            prepare_schema(str(eng_file))
        except Exception:
            pass
    return dict(
        status="active",
        mindie_session_id=lease["session"],
        mindie_activation=lease["token"],
        activated_at=lease["activated_at"],
        project_root=lease["project_root"],
    )


def deactivate(session: str, *, config: str | Path | None = None) -> dict:
    session = require_session(session)
    with _pending_lock(config):
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

                return deactivate_registered_domains(
                    adapter["engine_config"],
                    domain_configs=domain_configs,
                    session=session,
                )
        path = admission_path(config)
        if not path.is_file():
            return dict(status="inactive", session=session)
        gate(config).deactivate(session)
        return dict(status="inactive", session=session)


def status_facts(session: str | None = None, *, config: str | Path | None = None) -> dict:
    """Return factual control-plane status without onboarding prompts or activation."""
    cfg_file = config_path(config)
    configured = cfg_file.is_file()
    lease = None
    if session is not None:
        try:
            lease = knowledge_lease(session, config=config)
        except ValueError:
            lease = None
    consent_state = "unconfigured"
    cpath = consent_path(config)
    if cpath.is_file():
        try:
            cdata = read_json(cpath)
            consent_state = str(cdata.get("choice") or cdata.get("status") or "configured")
        except ValueError:
            consent_state = "invalid"
    community_enabled = False
    comm_path = community_config_path(config)
    if comm_path.is_file():
        try:
            comm_data = read_json(comm_path)
            community_enabled = bool(comm_data.get("enabled"))
        except ValueError:
            community_enabled = False
    return dict(
        harness="opencode",
        configured=configured,
        config_path=str(cfg_file),
        engine_config_path=str(engine_config_path(config)),
        activated=bool(lease is not None and lease.get("enabled") == 1),
        session_id=lease["session"] if lease else session,
        project_root=lease.get("project_root") if lease else None,
        activated_at=lease.get("activated_at") if lease else None,
        consent_choice=consent_state,
        community_sharing_enabled=community_enabled,
    )
