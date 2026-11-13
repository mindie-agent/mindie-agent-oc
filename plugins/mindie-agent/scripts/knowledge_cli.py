#!/usr/bin/env python3
"""Private knowledge entry loaded only by the explicitly invoked `/mindie-agent` Skill.

[Role]
Exposes the private knowledge tool catalog (`describe`) and executes authorized
knowledge operations (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`,
`knowledge_distill`, `knowledge_sync`) for a manually activated OpenCode session.

[Invariants]
- Neither this catalog nor its tool descriptions ever appear in default MCP
  discovery (`mcp_server.py`).
- Native session identity comes strictly from `OPENCODE_SESSION_ID` in the
  environment (or `--session` verified against `Admission.knowledge_lease`),
  never from model-supplied tool arguments.
- Both `describe` and every `knowledge_*` operation require an active manual
  session lease (`entry_enabled == 1`), raising `KnowledgeInactive`
  (`code="knowledge_inactive"`) when unactivated.
- Receipt Honesty: if a knowledge RPC succeeds and local lease outcome accounting
  fails, `isError` stays `False` and an `accounting` warning is attached.

[Timeouts & Locks]
- Standard knowledge RPCs use `30.0s`; `knowledge_distill` uses
  `summary_budget.distill_rpc_timeout()`. Input arguments are bounded to `1 MiB`.

[Side-Effect Policy]
- `describe` is purely read-only (`Admission.knowledge_lease` + static schema).
  Tool calls connect to the configured `mindie_knowledge` service only after
  verifying the manual lease.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if importlib.util.find_spec("mindie_knowledge") is None and not os.environ.get("_MINDIE_KC_REEXEC"):
    from mindie_launch import _preferred_python

    _target_py = _preferred_python()
    if _target_py and Path(_target_py).is_file():
        _env = dict(os.environ, _MINDIE_KC_REEXEC="1", PYTHONIOENCODING="utf-8")
        _kwargs: dict = {"env": _env}
        if os.name == "nt":
            _kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        raise SystemExit(subprocess.run([_target_py, "-I", str(Path(__file__).resolve()), *sys.argv[1:]], **_kwargs).returncode)

from admission import gate, knowledge_lease
from identity import reject_forged_identity_args, require_session
from oc_native_compat import ensure_oc_native_support
from paths import config_path, engine_config_path, read_json

ensure_oc_native_support()

MAX_INPUT_BYTES = 1024 * 1024
KNOWLEDGE_RPC_TIMEOUT = 30.0


class KnowledgeInactive(ValueError):
    """The current OpenCode session has not manually activated `/mindie-agent`."""

    code = "knowledge_inactive"


KNOWLEDGE_TOOL_SCHEMAS = [
    dict(
        name="knowledge_query",
        description=(
            "Search graded L1-L4 rules and task material in the selected domain. "
            "Pass observed task conditions to filter graded rules. Supports mode='standard' "
            "and mode='explore' (strips L3 workflows and returns exploration_context)."
        ),
        inputSchema=dict(
            type="object",
            properties=dict(
                query={"type": "string"},
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                conditions={"type": "object"},
                continuation={"type": "string"},
                domain={"type": "string"},
                domain_path={"type": "string"},
                token_budget={"type": "integer", "minimum": 1, "maximum": 64000},
                include_unknown={"type": "boolean"},
                include_archived={"type": "boolean"},
                navigation_only={"type": "boolean", "default": False},
                run_id={"type": "string"},
                mode={"type": "string", "enum": ["reuse", "diagnose", "explore", "standard"]},
                native_subcontext={"type": "boolean"},
                ledger_entries={"type": "array", "items": {"type": "object"}},
                bottleneck_summary={"type": "object"},
                goal={"type": "string"},
            ),
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_explain",
        description=(
            "Read one domain reference (`ref`) or a batch of exact unit references (`refs`) "
            "with shared prerequisite closure (`requires`) and deduplication (`known_refs`)."
        ),
        inputSchema=dict(
            type="object",
            properties=dict(
                ref={"type": "string"},
                refs={"type": "array", "items": {"type": "string"}},
                conditions={"type": "object"},
                known_refs={"type": "array", "items": {"type": "string"}},
                token_budget={"type": "integer", "minimum": 1, "maximum": 64000},
                run_id={"type": "string"},
                mode={"type": "string", "enum": ["reuse", "diagnose", "explore", "standard"]},
                ledger_entries={"type": "array", "items": {"type": "object"}},
                bottleneck_summary={"type": "object"},
                native_subcontext={"type": "boolean"},
                goal={"type": "string"},
            ),
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_feedback",
        description=(
            "Record optional up/down rating or 4-stage usage event (discovered, loaded, "
            "applied, evaluated) for an exact observed `feedback_ref`."
        ),
        inputSchema=dict(
            type="object",
            properties=dict(
                ref={"type": "string"},
                rating={"type": "string", "enum": ["up", "down"]},
                event_type={
                    "type": "string",
                    "enum": ["discovered", "loaded", "applied", "evaluated"],
                },
                run_id={"type": "string"},
                step_id={"type": "string"},
                outcome={
                    "type": "string",
                    "enum": ["supported", "refuted", "inconclusive"],
                },
            ),
            required=["ref"],
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_distill",
        description=(
            "Explicitly distill verified milestone or completed task evidence into local "
            "C5 graded L1-L4 units."
        ),
        inputSchema=dict(
            type="object",
            properties=dict(
                inputs={"type": "array", "items": {"type": "object"}},
                task_state={
                    "type": "string",
                    "enum": ["in_flight", "verified_milestone", "completed"],
                },
                domain={"type": "string"},
                related_refs={"type": "array", "items": {"type": "string"}},
                proposal={"type": "object"},
            ),
            required=["inputs", "task_state"],
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_sync",
        description="Synchronize public graded knowledge feeds for the active domain.",
        inputSchema=dict(
            type="object",
            properties=dict(domain={"type": "string"}),
            additionalProperties=False,
        ),
    ),
]

KNOWLEDGE_NAMES = {item["name"] for item in KNOWLEDGE_TOOL_SCHEMAS}


def native_session() -> str:
    raw = os.environ.get("OPENCODE_SESSION_ID", "")
    return require_session(raw)


def _select_engine_path(config: str | Path | None, arguments: dict) -> Path:
    default_engine = engine_config_path(config)
    domain = arguments.get("domain")
    ref = arguments.get("ref")
    refs = arguments.get("refs")
    if ref is None and isinstance(refs, list) and refs:
        canonical = [r for r in refs if isinstance(r, str) and r.startswith("mindie://")]
        if canonical:
            ref = canonical[0]
    if domain is None and not (isinstance(ref, str) and ref.startswith("mindie://")):
        return default_engine
    cfg_file = config_path(config)
    domain_configs = read_json(cfg_file).get("domain_configs") if cfg_file.is_file() else None
    from mindie_knowledge.loop.domain_routes import select_engine_config

    return Path(
        select_engine_config(
            str(default_engine),
            domain=domain,
            ref=ref if isinstance(ref, str) and ref.startswith("mindie://") else None,
            domain_configs=domain_configs,
        )
    )


def invoke(
    operation: str,
    arguments: dict | None = None,
    *,
    session: str | None = None,
    config: str | Path | None = None,
) -> dict:
    reject_forged_identity_args(arguments)
    session_id = require_session(session) if session is not None else native_session()
    lease = knowledge_lease(session_id, config=config)
    if lease is None:
        raise KnowledgeInactive(
            "Knowledge requires manual /mindie-agent activation in this OpenCode session"
        )
    if operation == "describe":
        return dict(tools=KNOWLEDGE_TOOL_SCHEMAS, isError=False)
    if operation not in KNOWLEDGE_NAMES:
        raise ValueError(f"unknown knowledge operation: {operation}")

    args = dict(arguments or {})
    if operation in {"knowledge_query", "knowledge_explain"} and args.get("mode") == "standard":
        args["mode"] = "reuse"
    target_engine = _select_engine_path(config, args)
    token = lease.get("token")

    from mindie_knowledge.loop.cli import ensure_service
    from mindie_knowledge.loop.transport import RequestRejected, rpc

    timeout = KNOWLEDGE_RPC_TIMEOUT
    if operation == "knowledge_distill":
        from mindie_knowledge.loop import summary_budget

        timeout = summary_budget.distill_rpc_timeout()

    connection = ensure_service(str(target_engine))
    succeeded = False
    neutral = False
    result: dict = {}
    try:
        try:
            rpc_payload = dict(args, _session_id=session_id)
            if operation == "knowledge_distill":
                rpc_payload["_activation"] = token
            else:
                rpc_payload["_session_verified"] = True
            value = rpc(
                connection,
                operation.removeprefix("knowledge_"),
                rpc_payload,
                timeout=timeout,
            )
            is_error = False
            if operation == "knowledge_distill":
                from mindie_knowledge.graded.distill import result_view

                value, is_error = result_view(value)
            succeeded = not is_error
            result = dict(
                content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value,
                isError=is_error,
            )
        except RequestRejected as exc:
            if operation not in {"knowledge_query", "knowledge_explain"}:
                raise
            code = getattr(exc, "error_code", None)
            if code == "material_corrupt":
                raise
            neutral = True
            message = f"Knowledge read rejected: {str(exc)[:240]}. No corpus change; no automatic retry."
            result = dict(
                content=[dict(type="text", text=message)],
                structuredContent=dict(
                    code=code or "read_rejected",
                    execution="not_started",
                    message=message,
                    automatic_retry=False,
                ),
                isError=True,
            )
        return result
    finally:
        pending = sys.exc_info()[1]
        if pending is not None and isinstance(pending, (TimeoutError, ConnectionError)):
            neutral = True
        if token is not None and not neutral:
            try:
                gate(config).finish(session_id, token, succeeded)
            except Exception as exc:
                if pending is None:
                    result["accounting"] = dict(
                        status="failed",
                        error_type=type(exc).__name__,
                        execution="completed",
                        automatic_retry=False,
                    )
                    result.setdefault("content", []).append(
                        dict(
                            type="text",
                            text="Knowledge operation completed; outcome accounting failed. Do not repeat the operation.",
                        )
                    )


def _format_failure(exc: Exception) -> dict:
    code = getattr(exc, "code", None) or "knowledge_error"
    message = str(exc) or type(exc).__name__
    return dict(
        content=[dict(type="text", text=message)],
        structuredContent=dict(code=code, error=message, automatic_retry=False),
        isError=True,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(argv) != 1:
            raise ValueError("exactly one knowledge operation or 'describe' is required")
        op = argv[0]
        arguments: dict = {}
        if op != "describe":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ValueError("knowledge arguments exceed 1 MiB byte budget")
            if raw.strip():
                parsed = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("knowledge arguments must be a JSON object")
                arguments = parsed
        result = invoke(op, arguments)
    except Exception as exc:
        result = _format_failure(exc)
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return int(result.get("isError") is True)


if __name__ == "__main__":
    raise SystemExit(main())
