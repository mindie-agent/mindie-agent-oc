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

import os
from pathlib import Path

from identity import require_session
from oc_native_compat import ensure_oc_native_support
from paths import (
    admission_path,
    community_config_path,
    config_path,
    consent_path,
    engine_config_path,
    read_json,
)

ensure_oc_native_support()


def gate(config: str | Path | None = None):
    from mindie_knowledge.loop.activation import Admission

    return Admission(str(admission_path(config)))


def knowledge_lease(session: str, *, config: str | Path | None = None) -> dict | None:
    session = require_session(session)
    path = admission_path(config)
    if not path.is_file():
        return None
    return gate(config).knowledge_lease(session)


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
    lease = adm.activate(session, project_root=root, root_session=session)
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
