#!/usr/bin/env python3
"""Default-enabled knowledge schemas and execution backend for OpenCode (`oc`).

[Role]
Defines the 5 `knowledge_*` schemas (`knowledge_query`, `knowledge_explain`,
`knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) exposed on
`mindie-tools` MCP and executes authorized knowledge operations via MCP or CLI.

[Invariants]
- Native session identity comes from `execute.before` / `OPENCODE_SESSION_ID`
  or falls back to connection/CLI scope (`"oc-cli-default"`), never from
  model-supplied tool arguments.
- `describe` is purely read-only; `knowledge_*` operations auto-provision a
  default session lease (`not_before=0.0`) on demand unless explicitly
  deactivated (`KnowledgeInactive`).
- Receipt Honesty: if a knowledge RPC succeeds and local lease outcome accounting
  fails, `isError` stays `False` and `receipt_warning` + `receipt_unconfirmed=True`
  are attached.

[Timeouts & Locks]
- Standard knowledge RPCs use `30.0s`; `knowledge_distill` uses
  `summary_budget.distill_rpc_timeout()`. Input arguments are bounded to `1 MiB`.

[Side-Effect Policy]
- `describe` is strictly read-only. `knowledge_*` calls provision a default lease
  on demand and connect to the configured `mindie_knowledge` service.
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

def _positive_env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = float(raw)
        return val if val > 0 and val == val and val != float("inf") else default
    except ValueError:
        return default


def _reexec_timeout_seconds(cli_args: list[str]) -> float:
    if cli_args and cli_args[0] == "knowledge_distill":
        # `outer >= distill_rpc_timeout() + 60s` startup/cleanup grace (`>= 480s` default).
        redact_s = _positive_env_seconds("MINDIE_REDACTION_TIMEOUT_SECONDS", 30.0)
        summary_s = _positive_env_seconds("MINDIE_SUMMARY_TIMEOUT_SECONDS", 315.0)
        distill_rpc = (2.0 * redact_s) + 10.0 + summary_s + 30.0 + 5.0
        return max(90.0, distill_rpc + 60.0)
    return 90.0


if importlib.util.find_spec("mindie_knowledge") is None and not os.environ.get("_MINDIE_KC_REEXEC"):
    from bounded import run_supervised_reexec
    from mindie_launch import _preferred_python

    _target_py = _preferred_python()
    if _target_py and Path(_target_py).is_file():
        _env = dict(os.environ, _MINDIE_KC_REEXEC="1", PYTHONIOENCODING="utf-8")
        try:
            _rc = run_supervised_reexec(
                [_target_py, "-I", str(Path(__file__).resolve()), *sys.argv[1:]],
                timeout=_reexec_timeout_seconds(sys.argv[1:]),
                env=_env,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        except TimeoutError as _exc:
            _err = {
                "content": [{"type": "text", "text": str(_exc)}],
                "structuredContent": {"code": "knowledge_timeout", "error": str(_exc), "automatic_retry": False},
                "isError": True,
            }
            sys.stdout.buffer.write((json.dumps(_err, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
            raise SystemExit(1) from _exc
        raise SystemExit(_rc)

from admission import _bounded_gate, ensure_default_lease, gate, is_deactivated
from identity import reject_forged_identity_args, require_session
from oc_native_compat import ensure_oc_native_support
from paths import admission_path, config_path, engine_config_path, read_json

ensure_oc_native_support()

MAX_INPUT_BYTES = 1024 * 1024
KNOWLEDGE_RPC_TIMEOUT = 30.0
DEFAULT_CLI_SESSION_ID = "oc-cli-default"


class KnowledgeInactive(ValueError):
    """Knowledge has been explicitly deactivated for the current OpenCode session."""

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
    if isinstance(raw, str) and raw.strip():
        return require_session(raw)
    return DEFAULT_CLI_SESSION_ID


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
    project_root: str | Path | None = None,
    config: str | Path | None = None,
) -> dict:
    reject_forged_identity_args(arguments)
    session_id = require_session(session) if session is not None else native_session()
    if is_deactivated(session_id, config=config):
        raise KnowledgeInactive("Knowledge has been deactivated for this OpenCode session")
    if operation == "describe":
        return dict(tools=KNOWLEDGE_TOOL_SCHEMAS, isError=False)
    if operation not in KNOWLEDGE_NAMES:
        raise ValueError(f"unknown knowledge operation: {operation}")

    eff_root = str(Path(project_root).resolve()) if project_root is not None else str(Path.cwd().resolve())
    cfg_file = config_path(config)
    if not cfg_file.is_file():
        from setup import _current_python_has_runtime, ensure_default_profile

        if _current_python_has_runtime():
            ensure_default_profile(
                config=cfg_file,
                workspace_root=eff_root,
                python_exe=sys.executable,
                prepare_store=False,
            )

    lease = ensure_default_lease(session_id, project_root=eff_root, not_before=0.0, config=config)
    args = dict(arguments or {})
    if operation in {"knowledge_query", "knowledge_explain"} and args.get("mode") == "standard":
        args["mode"] = "reuse"
    target_engine = _select_engine_path(config, args)
    finish_gate = gate(config)
    token = lease.get("token")
    if target_engine.is_file():
        try:
            eng_doc = read_json(target_engine)
            dom_adm = eng_doc.get("admission_path")
            if (
                isinstance(dom_adm, str)
                and Path(dom_adm).is_absolute()
                and Path(dom_adm).resolve() != admission_path(config).resolve()
            ):
                dom_gate = _bounded_gate(dom_adm)
                dom_gate.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                dom_lease = dom_gate.activate(
                    session_id, project_root=eff_root, root_session=session_id, not_before=0.0
                )
                finish_gate = dom_gate
                token = dom_lease.get("token")
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass

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
            structured_val = value if isinstance(value, dict) else {"feeds": value}
            result = dict(
                content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=structured_val,
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
                finish_gate.finish(session_id, token, succeeded)
            except Exception as exc:
                if pending is None:
                    warn_msg = f"{type(exc).__name__}: {exc}"
                    result["accounting"] = dict(
                        status="failed",
                        error_type=type(exc).__name__,
                        execution="completed",
                        automatic_retry=False,
                    )
                    structured = dict(result.get("structuredContent") or {})
                    structured.setdefault("receipt_warning", warn_msg)
                    structured["receipt_unconfirmed"] = True
                    result["structuredContent"] = structured
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
            if raw.lstrip(b"\xef\xbb\xbf").strip():
                parsed = json.loads(raw.decode("utf-8-sig"))
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
