"""OpenCode (`oc`) lifecycle hook bridge (`gesture`, `session-state`, `pre-tool-use`, `stop`).

[Role]
Bridges `opencode_host.mjs` hook callbacks (`command.transform`, `session.hook('prompt')`,
`session.hook('context')`, `tool.hook('execute.before')`, and `event.subscribe`
`session.idle`) into MindIE's session lease store (`admission.sqlite3`), one-shot
MCP bind table (`mcp-binds.sqlite3`), and turn-end transcript capture.

[Invariants]
- Passive Default: ordinary prompts without `/mindie-agent` or `$mindie-agent`
  return `{"matched": false}` immediately without creating `admission.sqlite3`,
  starting background services, or capturing transcripts.
- Non-Inheritance: subagent (`parentID`) and forked (`forkSessionID`) sessions
  are rejected before activating a lease or capturing transcripts.
- Lease Honesty: if `Admission.activate` succeeds on `/mindie-agent` but default
  profile setup degrades, the active lease is preserved (`status="degraded"`).

[Timeouts & Locks]
- Executes within `mindie_launch.py`'s `HOOK_TOTAL = 10.0s` budget; SQLite WAL
  writes use `5.0s` busy timeout.

[Side-Effect Policy]
- Mutates `admission.sqlite3` only on explicit `/mindie-agent` gestures, writes
  one-shot binds to `mcp-binds.sqlite3` on MindIE MCP `execute.before`, and
  delegates `stop` capture to `capture_stop.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from admission import activate, deactivate, knowledge_lease, status_facts
from component_update import trigger_background_update_if_due
from identity import (
    is_eligible_main_session,
    parse_hook,
    parse_mindie_gesture,
    parse_oc_mcp_tool,
    publish_bind,
    session_from_hook,
    workspace_from_hook,
)
from paths import (
    apply_enterprise_transport_env,
    atomic_write_json,
    config_path,
    data_home,
    plugin_root,
    read_json,
)


def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _read_stdin_event() -> dict:
    return parse_hook(sys.stdin.buffer.read())


def handle_session_state(event: dict | None = None, *, config=None) -> dict:
    """Read-only check used by `opencode_host.mjs` `session.hook('context')`."""
    event = _read_stdin_event() if event is None else event
    eligible, reason = is_eligible_main_session(event)
    if not eligible:
        return dict(activated=False, eligible=False, reason=reason)
    try:
        session = session_from_hook(event)
    except ValueError:
        return dict(activated=False, eligible=False, reason="invalid_session")
    lease = knowledge_lease(session, config=config)
    if lease is None:
        return dict(activated=False, eligible=True, session_id=session)
    scripts_dir = Path(__file__).resolve().parent
    skill_file = plugin_root() / "skills" / "mindie-agent" / "SKILL.md"
    return dict(
        activated=True,
        eligible=True,
        session_id=session,
        project_root=lease.get("project_root"),
        activated_at=lease.get("activated_at"),
        skill_path=str(skill_file),
        knowledge_cli=str(scripts_dir / "knowledge_cli.py"),
        rsi_loop=str(scripts_dir / "rsi_loop.py"),
        experiment_ledger=str(scripts_dir / "experiment_ledger.py"),
    )


def _spawn_background_bootstrap(cfg_file: Path, workspace_root: str | Path | None) -> bool:
    """Spawn a detached long-budget `setup.py init` outside the 10s hook window."""
    status_file = data_home() / "oc-bootstrap-status.json"
    now = time.time()
    if status_file.is_file():
        try:
            existing = read_json(status_file)
            started_at = float(existing.get("started_at") or 0.0)
            if existing.get("status") == "in_progress" and (now - started_at) < 560.0:
                return True
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass
    try:
        atomic_write_json(status_file, {"started_at": now, "status": "in_progress", "config": str(cfg_file)})
    except TimeoutError:
        raise
    except OSError:
        pass

    setup_py = Path(__file__).resolve().parent / "setup.py"
    argv = [sys.executable, "-I", str(setup_py), "init", "--config", str(cfg_file)]
    if workspace_root:
        argv.extend(["--workspace-root", str(workspace_root)])
    env = apply_enterprise_transport_env(dict(os.environ))
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
    )
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)
    return True


def handle_gesture(event: dict | None = None, *, config=None) -> dict:
    """Handle explicit `/mindie-agent` or `$mindie-agent` commands and prompt prefixes."""
    event = _read_stdin_event() if event is None else event
    trigger_background_update_if_due(config=config)
    is_gesture, remainder = parse_mindie_gesture(event)
    if not is_gesture:
        return dict(matched=False)

    eligible, reason = is_eligible_main_session(event)
    if not eligible:
        return dict(
            matched=True,
            activated=False,
            eligible=False,
            reason=reason,
            action="rejected",
            message=(
                f"MindIE knowledge lease cannot be activated in {reason.replace('_', ' ')}. "
                "Deterministic MCP tools (mindie-remote-dev, mindie-tools) remain available."
            ),
        )

    session = session_from_hook(event)
    workspace_root = workspace_from_hook(event)
    tokens = remainder.split(maxsplit=1) if remainder else []
    first = tokens[0].lower() if tokens else "activate"

    if first == "deactivate" and len(tokens) == 1:
        result = deactivate(session, config=config)
        inactive = result.get("status") == "inactive"
        msg = (
            "MindIE knowledge is deactivated for this OpenCode session. "
            "Deterministic MCP tools (mindie-remote-dev, mindie-tools) remain available."
            if inactive
            else "MindIE session deactivation is incomplete; inspect status: "
            + json.dumps(result, ensure_ascii=False)
        )
        return dict(
            matched=True,
            activated=not inactive,
            action="deactivate",
            session_id=session,
            result=result,
            task_prompt="",
            message=msg,
        )

    if first == "status" and len(tokens) == 1:
        facts = status_facts(session, config=config)
        return dict(
            matched=True,
            activated=bool(facts.get("activated")),
            action="status",
            session_id=session,
            facts=facts,
            task_prompt="",
            message="MindIE factual status: " + json.dumps(facts, ensure_ascii=False),
        )

    task_prompt = "" if first == "activate" and len(tokens) == 1 else remainder
    # Activate the manual session lease FIRST (< 5ms SQLite WAL write) so cold-start
    # runtime provisioning can never prevent or outlive the hook deadline.
    lease_info = activate(session, workspace_root, config=config)

    cfg_file = config_path(config)
    readiness_warning = ""
    bootstrapping = False
    if not cfg_file.is_file():
        try:
            from setup import _current_python_has_runtime, _discover_existing_mindie_python, ensure_default_profile

            ready_py = (
                sys.executable
                if _current_python_has_runtime()
                else _discover_existing_mindie_python()
            )
            if ready_py is not None:
                ensure_default_profile(config=cfg_file, workspace_root=workspace_root, python_exe=str(ready_py))
            else:
                bootstrapping = _spawn_background_bootstrap(cfg_file, workspace_root)
                lease_info["status"] = "bootstrapping"
                readiness_warning = (
                    " Note: first-time MindIE runtime bootstrap (`oc-venv`) has started in the background; "
                    "MCP catalogs will refresh automatically once ready."
                )
        except TimeoutError:
            raise
        except Exception as exc:
            lease_info["status"] = "degraded"
            readiness_warning = (
                f" Note: default profile initialization degraded ({type(exc).__name__}); "
                "the session lease remains active."
            )

    scripts_dir = Path(__file__).resolve().parent
    skill_path = plugin_root() / "skills" / "mindie-agent" / "SKILL.md"
    knowledge_cli = scripts_dir / "knowledge_cli.py"
    msg = (
        f"MindIE Agent knowledge flywheel enabled for OpenCode session `{session}` "
        f"(project_root=`{lease_info['project_root']}`, status=`{lease_info['status']}`). "
        f"Skill guidance `{skill_path}` and `OPENCODE_SESSION_ID={session}` are active for `{knowledge_cli}`."
        f"{readiness_warning}"
    )
    return dict(
        matched=True,
        activated=True,
        eligible=True,
        bootstrapping=bootstrapping,
        action="activate",
        session_id=session,
        project_root=lease_info["project_root"],
        status=lease_info["status"],
        skill_path=str(skill_path),
        knowledge_cli=str(knowledge_cli),
        rsi_loop=str(scripts_dir / "rsi_loop.py"),
        experiment_ledger=str(scripts_dir / "experiment_ledger.py"),
        task_prompt=task_prompt,
        message=msg,
    )


def handle_pre_tool_use(event: dict | None = None, *, config=None) -> dict:
    """Record a one-shot bind for MindIE MCP tool calls before execution."""
    event = _read_stdin_event() if event is None else event
    tool_name = event.get("tool")
    tool_input = event.get("input")
    if not isinstance(tool_name, str) and isinstance(event.get("toolCall"), dict):
        tc = event["toolCall"]
        tool_name = tc.get("name")
        tool_input = tc.get("args")
    parsed = parse_oc_mcp_tool(tool_name or "", tool_input)
    if parsed is None:
        return dict(bound=False)
    surface, local_tool, arguments = parsed
    session = session_from_hook(event)
    workspace_root = None
    try:
        workspace_root = workspace_from_hook(event)
    except ValueError:
        pass
    bind_id = publish_bind(
        session,
        surface,
        local_tool,
        arguments,
        call_id=event.get("id") if isinstance(event.get("id"), str) else None,
        workspace_root=workspace_root,
        config=config,
    )
    return dict(bound=True, bind_id=bind_id, surface=surface, local_tool=local_tool)


def handle_stop(event: dict | None = None, *, config=None) -> dict:
    """Synchronous entry wrapper around `capture_stop.capture_event`."""
    from capture_stop import capture_event

    event = _read_stdin_event() if event is None else event
    return capture_event(event, config=config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _emit({})
    op = argv[0]
    if op == "session-state":
        return _emit(handle_session_state())
    if op in {"gesture", "pre-invocation"}:
        return _emit(handle_gesture())
    if op == "pre-tool-use":
        return _emit(handle_pre_tool_use())
    if op == "stop":
        return _emit(handle_stop())
    return _emit({})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
