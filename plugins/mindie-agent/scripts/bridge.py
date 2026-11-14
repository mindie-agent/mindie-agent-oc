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
    try:
        lease = knowledge_lease(session, config=config)
    except TimeoutError:
        raise
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        print(f"MindIE OpenCode session-state unavailable ({session}): {err_text}", file=sys.stderr)
        return dict(
            activated=False,
            eligible=True,
            status="unavailable",
            reason="admission_unavailable",
            error=err_text,
            session_id=session,
        )
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


def _spawn_detached_task(argv: list[str], status_file: Path, *, cfg_file: Path, max_age: float) -> bool:
    from component_update import _pid_running, _stop_untracked_updater

    now = time.time()
    if status_file.is_file():
        try:
            existing = read_json(status_file)
            started_at = float(existing.get("started_at") or 0.0)
            rec_pid = existing.get("pid")
            alive = True if rec_pid is None else _pid_running(rec_pid)
            if existing.get("status") == "in_progress" and alive and (now - started_at) < max_age:
                return True
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass

    no_breakaway_marker = os.environ.get("_MINDIE_OC_BG_NO_BREAKAWAY")
    env = apply_enterprise_transport_env(dict(os.environ))
    for strip_key in (
        "_MINDIE_OC_PGID_FILE",
        "_MINDIE_OC_PGID_FILES",
        "_MINDIE_OC_BG_NO_BREAKAWAY",
        "_MINDIE_OC_REEXEC",
        "_MINDIE_OC_SUPERVISOR_DEADLINE",
    ):
        env.pop(strip_key, None)
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
    )
    if os.name == "nt":
        base_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        kwargs["creationflags"] = base_flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except TimeoutError:
            raise
        except OSError as exc:
            if getattr(exc, "winerror", None) != 5:
                raise
            if no_breakaway_marker:
                Path(no_breakaway_marker).write_text("1\n", encoding="utf-8", newline="\n")
            kwargs["creationflags"] = base_flags
            proc = subprocess.Popen(argv, **kwargs)
    else:
        kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    try:
        atomic_write_json(
            status_file,
            {"started_at": now, "status": "in_progress", "pid": proc.pid, "config": str(cfg_file)},
        )
    except TimeoutError:
        _stop_untracked_updater(proc)
        raise
    except OSError as exc:
        print(f"MindIE OpenCode background status write warning: {exc}", file=sys.stderr)
    return True


def _spawn_background_bootstrap(cfg_file: Path, workspace_root: str | Path | None) -> bool:
    """Spawn a detached long-budget `setup.py init` outside the 10s hook window."""
    setup_py = Path(__file__).resolve().parent / "setup.py"
    argv = [sys.executable, "-I", str(setup_py), "init", "--config", str(cfg_file)]
    if workspace_root:
        argv.extend(["--workspace-root", str(workspace_root)])
    return _spawn_detached_task(argv, data_home() / "oc-bootstrap-status.json", cfg_file=cfg_file, max_age=560.0)


def _spawn_background_schema_prepare(cfg_file: Path, python_exe: str) -> bool:
    """Spawn detached bounded `component_update.py --prepare-schema` off the critical hook path."""
    comp_py = Path(__file__).resolve().parent / "component_update.py"
    argv = [str(python_exe), "-I", str(comp_py), "--config", str(cfg_file), "--prepare-schema", "--timeout", "90"]
    return _spawn_detached_task(argv, data_home() / "oc-schema-prepare.json", cfg_file=cfg_file, max_age=95.0)


def handle_gesture(event: dict | None = None, *, config=None) -> dict:
    """Handle explicit `/mindie-agent` or `$mindie-agent` commands and prompt prefixes."""
    hook_deadline = time.monotonic() + 7.5
    event = _read_stdin_event() if event is None else event
    try:
        trigger_background_update_if_due(config=config)
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"MindIE OpenCode background update trigger warning: {exc}", file=sys.stderr)
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
        rem = hook_deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("hook budget exhausted before deactivation")
        result = deactivate(session, config=config, timeout=rem)
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
    rem_act = hook_deadline - time.monotonic()
    if rem_act <= 0:
        raise TimeoutError("hook budget exhausted before activation")
    lease_info = activate(session, workspace_root, config=config, timeout=rem_act)

    cfg_file = config_path(config)
    readiness_warning = ""
    bootstrapping = False
    if not cfg_file.is_file():
        try:
            from setup import _current_python_has_runtime, ensure_default_profile

            rem_prof = hook_deadline - time.monotonic()
            if _current_python_has_runtime() and rem_prof >= 2.5:
                try:
                    ensure_default_profile(
                        config=cfg_file,
                        workspace_root=workspace_root,
                        python_exe=sys.executable,
                        timeout=rem_prof - 0.3,
                        prepare_store=False,
                    )
                except TimeoutError:
                    if hook_deadline - time.monotonic() < 0.1:
                        raise
                    lease_info["status"] = "degraded"
                    readiness_warning = (
                        " Note: default profile initialization exceeded its in-hook budget; "
                        "the session lease remains active."
                    )
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
    schema_warning = lease_info.get("schema_warning")
    if not schema_warning:
        for warn_src in (cfg_file, data_home() / "oc-component-update-status.json"):
            if warn_src.is_file():
                try:
                    persisted_warn = read_json(warn_src).get("schema_warning")
                    if isinstance(persisted_warn, str) and persisted_warn:
                        schema_warning = persisted_warn
                        break
                except TimeoutError:
                    raise
                except (OSError, ValueError):
                    pass
    if not bootstrapping and cfg_file.is_file():
        try:
            from component_update import store_schema_needs_prepare
            from setup import _current_python_has_runtime

            if store_schema_needs_prepare(cfg_file):
                cfg_py = read_json(cfg_file).get("python")
                target_py = str(cfg_py) if isinstance(cfg_py, str) and Path(cfg_py).is_file() else (sys.executable if _current_python_has_runtime() else None)
                if target_py is not None:
                    _spawn_background_schema_prepare(cfg_file, target_py)
        except TimeoutError:
            raise
        except Exception as exc:
            print(f"MindIE OpenCode deferred schema prepare schedule warning: {exc}", file=sys.stderr)
    if isinstance(schema_warning, str) and schema_warning:
        readiness_warning += f" Note: schema preparation warning ({schema_warning})."

    scripts_dir = Path(__file__).resolve().parent
    skill_path = plugin_root() / "skills" / "mindie-agent" / "SKILL.md"
    knowledge_cli = scripts_dir / "knowledge_cli.py"
    msg = (
        f"MindIE Agent knowledge flywheel enabled for OpenCode session `{session}` "
        f"(project_root=`{lease_info['project_root']}`, status=`{lease_info['status']}`). "
        f"Skill guidance `{skill_path}` and `OPENCODE_SESSION_ID={session}` are active for `{knowledge_cli}`."
        f"{readiness_warning}"
    )
    out_gesture = dict(
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
    if isinstance(schema_warning, str) and schema_warning:
        out_gesture["schema_warning"] = schema_warning
    return out_gesture


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
