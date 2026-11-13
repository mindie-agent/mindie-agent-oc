"""Public stdio JSON-RPC 2.0 MCP server for OpenCode (`remote` and `tools` surfaces).

[Role]
Serves public MCP discovery (`tools/list`) and execution (`tools/call`) for
`mindie-remote-dev` (20 `remote_*` tools) and `mindie-tools` (`mindie_status`,
`profiler`, `sim`, `coordinator`, `debugger`, `npu_top*`, `diagnostics_*`).

[Invariants]
- `tools/list` NEVER exposes `knowledge_*` names, descriptions, or schemas.
- Deterministic tools work in ordinary sessions without requiring a knowledge lease.
- Session identity comes only from one-shot `claim_bind` or `OPENCODE_SESSION_ID`;
  model-supplied session keys in `params.arguments` are rejected.
- Receipt Honesty: once a primary remote/component call succeeds, local receipt or
  `finish_bind` `OSError`/`sqlite3.Error` keeps `isError=False` and attaches
  `receipt_warning` + `receipt_unconfirmed=True`.

[Timeouts & Locks]
- SQLite WAL + `busy_timeout=5000` (`timeout=5.0`) on `remote-receipts.sqlite3`.
- Enforces `remote_timeout = max(minimum, inner_wait + 60.0s)` (`660s >= 600s + 60s`)
  and decouples `COMPONENT_CAPTURE_LIMIT_BYTES = 16 MiB` from MCP truncation.

[Side-Effect Policy]
- `initialize` triggers non-blocking background update checks if due; `tools/list`
  is a pure in-memory catalog operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import status_facts
from bounded import run as bounded_run
from component_update import trigger_background_update_if_due
from identity import claim_bind, finish_bind, reject_forged_identity_args, require_session
from oc_native_compat import ensure_oc_native_support
from paths import bind_db_path, config_path, diagnostics_config_path, plugin_root, read_json, remote_receipts_path, state_dir

ensure_oc_native_support()

MAX_YIELD_TIME_MS = 30000
REMOTE_GRACE_SECONDS = 60.0
REMOTE_MIN_BUDGET_SECONDS = 180.0
COMPONENT_CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
REMOTE_NATIVE_MAX_WAIT_MS = {
    "remote_write_stdin": 300000,
    "remote_job_stdin": 300000,
    "remote_bash": 600000,
    "remote_run_command": 600000,
    "remote_exec_command": 30000,
}

COMPONENT_DESCRIPTIONS = {
    "profiler": (
        "Inspect and analyze Ascend NPU profiling captures using mindie-profiler. "
        "Pass CLI arguments as an argv array, e.g. ['inventory', '--root', '/capture'] "
        "or ['analyze', '--root', '/capture', '--full-range']. Commands: inventory, analyze, "
        "correlate, compare, sql, camodel, and kernels. Use ['--help'] to inspect options."
    ),
    "sim": (
        "Compute Ascend hardware bounds (A2/A3/A5), operator/graph/serving latency, HBM capacity "
        "(including async_scheduling delayed release), Roofline efficiency, SLA concurrency "
        "frontiers, and P/D disaggregation using mindie-sim. Pass CLI arguments as an argv "
        "array, e.g. ['compose', '--model', 'deepseek-v2-lite', '--hardware', 'Ascend910B2C', "
        "'--tp', '8', '--output', '/tmp/case.json'] or ['predict', '/tmp/case.json']."
    ),
    "coordinator": (
        "Manage Ascend host inventory, probe CPU/RAM/NPU capacity, bind containers, "
        "and acquire/inspect/release NPU and service-port leases through mindie-coordinator. "
        "Pass CLI argv, e.g. ['hosts', '--action', 'list'] or "
        "['probe', '--json', '{\"action\":\"hosts\"}']. Preserve lease IDs and uncertain "
        "external outcomes; never resubmit an uncertain mutation."
    ),
    "npu_top": (
        "Observe live Ascend NPU fleet telemetry, per-Die HBM/utilization, mounts, and active "
        "processes across A2/A3/A5 servers using npu-top (observation-only)."
    ),
    "debugger": (
        "Ascend NPU fault triage (`triage`, `decode-code`), ACLGraph/NPUGraph capture/replay & "
        "buffer-address debugger (`aclgraph`: `guard`, `inspect`), and operator precision/memory-stomp "
        "toolkit (`precision`: `check-call`, `replay-call`, `inspect-capsule`, `compare-traces`, `watch`) "
        "via mindie-debugger. Pass CLI arguments as an argv array, e.g. ['decode-code', '507015'] "
        "or ['triage', '--text', '<err>'] or ['remote-guide']."
    ),
}

NPU_TOP_OBSERVATION_NAMES = frozenset(
    {"npu_status", "server_status", "list_mounts", "find_npu_capacity", "npu_telemetry_window", "list_npu_servers"}
)


def _npu_top_observation_tools() -> list[dict]:
    try:
        from npu_top.mcp import TOOLS as npu_tools

        return list(npu_tools)
    except ImportError:
        return [
            dict(
                name=name,
                description=f"Observe Ascend NPU telemetry via npu-top ({name}).",
                inputSchema=dict(type="object", properties={}, additionalProperties=True),
                annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
            )
            for name in sorted(NPU_TOP_OBSERVATION_NAMES)
        ]


def _execute_npu_top_observation(name: str, args: dict) -> dict:
    from npu_top.client import ClientError, VawsTopClient
    from npu_top.mcp import handle_request

    try:
        client = VawsTopClient()
    except ClientError as exc:
        payload = {"error": str(exc), "status": "monitor_unavailable"}
        return dict(content=[dict(type="text", text=str(exc))], structuredContent=payload, isError=True)
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    resp = handle_request(req, client)
    if not isinstance(resp, dict):
        return dict(
            content=[dict(type="text", text="npu-top returned invalid non-object response")],
            structuredContent={"error": "invalid_npu_top_response", "status": "monitor_unavailable"},
            isError=True,
        )
    if "error" in resp:
        err_obj = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        msg = err_obj.get("message", "npu-top MCP call failed")
        return dict(content=[dict(type="text", text=msg)], structuredContent={"error": msg}, isError=True)
    res = resp.get("result")
    if not isinstance(res, dict) or not res:
        return dict(
            content=[dict(type="text", text="npu-top returned empty MCP result")],
            structuredContent={"error": "empty_npu_top_result", "status": "monitor_unavailable"},
            isError=True,
        )
    return res


def remote_timeout(
    name: str,
    args: dict | None = None,
    *,
    native_wait_ms: int | float | None = None,
    grace: float = REMOTE_GRACE_SECONDS,
    minimum: float = REMOTE_MIN_BUDGET_SECONDS,
) -> float:
    """Compute `outer_timeout >= max(minimum, inner_wait + grace)`."""
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or grace <= 0:
        raise ValueError("remote grace must be a positive finite number")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("remote minimum budget must be a positive finite number")
    if native_wait_ms is None:
        native_wait_ms = REMOTE_NATIVE_MAX_WAIT_MS.get(name, 0)
    explicit_ms = 0.0
    if isinstance(args, dict):
        for key in ("yield_time_ms", "timeout_ms", "WaitMsBeforeAsync"):
            value = args.get(key)
            if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                explicit_ms = max(explicit_ms, float(value))
    inner_seconds = max(float(native_wait_ms) / 1000.0, explicit_ms / 1000.0)
    return max(float(minimum), inner_seconds + float(grace))


class RemoteReceipts:
    """Lightweight SQLite WAL store tracking in-flight and completed remote calls."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or remote_receipts_path())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA busy_timeout=5000;")
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL;")
        db.execute(
            "CREATE TABLE IF NOT EXISTS receipts("
            "call_id TEXT PRIMARY KEY, "
            "session TEXT NOT NULL, "
            "tool TEXT NOT NULL, "
            "args_sha256 TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "started REAL NOT NULL, "
            "updated REAL NOT NULL)"
        )
        return db

    def begin(self, call_id: str, session: str, tool: str, args: dict) -> None:
        digest = hashlib.sha256(
            json.dumps(args or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,?,?)",
                    (call_id, session, tool, digest, "running", now, now),
                )
        finally:
            db.close()

    def finish(self, call_id: str, status: str) -> None:
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute(
                    "UPDATE receipts SET status=?, updated=? WHERE call_id=?",
                    (status, now, call_id),
                )
        finally:
            db.close()


def _with_bind_property(tool: dict) -> dict:
    """Declare optional `_mindie_bind_id` so OpenCode's post-hook schema decoder preserves exact call correlation."""
    item = dict(tool)
    raw_schema = item.get("inputSchema")
    if isinstance(raw_schema, dict):
        props = dict(raw_schema.get("properties") or {})
        props.setdefault("_mindie_bind_id", dict(type="string"))
        item["inputSchema"] = dict(raw_schema, properties=props)
    return item


def tools_catalog() -> list[dict]:
    """Return the deterministic `mindie-tools` MCP catalog (zero knowledge tools)."""
    limits = {
        "cwd": dict(type="string", description="Optional absolute working directory."),
        "timeout_ms": dict(type="integer", minimum=1, maximum=600000, default=120000, description="Execution deadline in milliseconds; bounded within OpenCode's 660s MCP ceiling."),
        "max_output_tokens": dict(type="integer", minimum=1, maximum=262144, default=16384, description="Output byte budget divided by four."),
    }
    items: list[dict] = [
        dict(
            name="mindie_status",
            description="Read factual MindIE control-plane status for the current OpenCode session. Does not activate a knowledge lease, start background services, or emit onboarding prompts.",
            inputSchema=dict(type="object", properties={}, additionalProperties=False),
            annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        )
    ]
    for name, description in COMPONENT_DESCRIPTIONS.items():
        items.append(
            dict(
                name=name, description=description,
                inputSchema=dict(
                    type="object", additionalProperties=False, required=["argv"],
                    properties=dict(argv=dict(type="array", items=dict(type="string"), description="CLI arguments array; passed without shell expansion."), **limits),
                ),
                annotations=dict(readOnlyHint=name == "npu_top", destructiveHint=name not in {"npu_top", "debugger"}, openWorldHint=True),
            )
        )
    items.extend(_npu_top_observation_tools())
    items.append(
        dict(
            name="diagnostics_status",
            description="Inspect local MindIE diagnostic/reporting status without starting a worker or contacting GitHub.",
            inputSchema=dict(type="object", additionalProperties=False, properties=dict(config=dict(type="string"), **limits)),
            annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        )
    )
    items.append(
        dict(
            name="diagnostics_bundle",
            description="Export a bounded, sanitized local diagnostic support bundle without uploading it.",
            inputSchema=dict(
                type="object", additionalProperties=False, required=[],
                properties=dict(root=dict(type="string"), operation_id=dict(type="string"), incident_id=dict(type="string"), output=dict(type="string"), **limits),
            ),
            annotations=dict(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        )
    )
    return [_with_bind_property(item) for item in items]


def remote_catalog() -> list[dict]:
    """Return the `mindie-remote-dev` canonical MCP catalog."""
    from remote_dev.mcp.tools import list_tools

    return [_with_bind_property(item) for item in list_tools("canonical")]


def _resolve_component_command(component: str, *, config=None) -> list[str]:
    cfg_file = config_path(config)
    adapter = read_json(cfg_file) if cfg_file.is_file() else {}
    configured = adapter.get("component_commands", {}).get(component)
    if isinstance(configured, list) and configured and all(isinstance(x, str) and x for x in configured):
        return configured
    python_exe = adapter.get("python") or sys.executable
    if component == "diagnostics":
        return [python_exe, "-I", "-X", "utf8", "-m", "mindie_diagnostics.cli"]
    if component == "npu_top":
        return [python_exe, "-I", "-X", "utf8", "-m", "npu_top.cli"]
    module_map = {
        "profiler": "mindie_profiler",
        "sim": "mindie_sim",
        "coordinator": "mindie_coordinator.cli",
        "debugger": "mindie_debugger.cli",
    }
    return [python_exe, "-I", "-X", "utf8", "-m", module_map[component]]


def _resolve_session(
    surface: str,
    tool_name: str,
    arguments: dict,
    *,
    bind_id: str | None = None,
    config=None,
) -> tuple[str, str | None, str | None]:
    """Resolve session identity from an `execute.before` bind or `OPENCODE_SESSION_ID`."""
    reject_forged_identity_args(arguments)
    if bind_db_path(config).is_file():
        bound = claim_bind(surface, tool_name, arguments, bind_id=bind_id, config=config)
        return bound["session"], bound["bind_id"], bound.get("workspace_root")
    return require_session(os.environ.get("OPENCODE_SESSION_ID")), None, None


def _admission_failure(exc: Exception) -> dict:
    return dict(
        isError=True,
        structuredContent=dict(
            code="admission_unavailable",
            execution="not_started",
            automatic_retry=False,
            error_type=type(exc).__name__,
            message=str(exc),
        ),
        content=[
            dict(
                type="text",
                text=(
                    f"This MindIE call did not start because local session admission rejected it "
                    f"({type(exc).__name__}: {exc}). Continue independent task steps or use equivalent "
                    "authorized host-native tools; preserve task identity and resource ownership. "
                    "Do not repeat this call unchanged."
                ),
            )
        ],
    )


def _attach_accounting_warning(result: dict, warning_key: str, exc: Exception) -> dict:
    structured = dict(result.get("structuredContent") or {})
    structured[warning_key] = f"{type(exc).__name__}: {exc}"
    structured["receipt_unconfirmed"] = True
    result["structuredContent"] = structured
    content = list(result.get("content") or [])
    content.append(
        dict(
            type="text",
            text=(
                f"Operation completed, but local {warning_key} failed ({type(exc).__name__}). "
                "Do not repeat the completed operation."
            ),
        )
    )
    result["content"] = content
    return result


def call_tools_surface(
    name: str,
    arguments: dict | None = None,
    *,
    config=None,
    receipts: RemoteReceipts | None = None,
) -> dict:
    raw_args = dict(arguments or {})
    explicit_bind = raw_args.pop("_mindie_bind_id", None)
    try:
        session, bind_id, bound_cwd = _resolve_session(
            "tools",
            name,
            raw_args,
            bind_id=explicit_bind if isinstance(explicit_bind, str) else None,
            config=config,
        )
    except Exception as exc:
        return _admission_failure(exc)
    args = dict(raw_args)
    if name == "debugger" and "argv" not in args and isinstance(args.get("action"), str):
        action = args.pop("action")
        code = args.pop("code", None)
        args["argv"] = [action, str(code)] if code is not None else [action]

    succeeded = False
    try:
        if name == "mindie_status":
            facts = status_facts(session, config=config)
            succeeded = True
            result = dict(
                content=[dict(type="text", text=json.dumps(facts, ensure_ascii=False))],
                structuredContent=facts,
                isError=False,
            )
        elif name in NPU_TOP_OBSERVATION_NAMES:
            result = _execute_npu_top_observation(name, args)
            succeeded = result.get("isError") is not True
        elif name in COMPONENT_DESCRIPTIONS or name in {"diagnostics_status", "diagnostics_bundle"}:
            if name in COMPONENT_DESCRIPTIONS:
                component = name
                argv = args.get("argv")
                if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                    raise ValueError("argv must be a list of strings")
            elif name == "diagnostics_status":
                component = "diagnostics"
                diag_cfg = args.get("config") if isinstance(args.get("config"), str) else str(diagnostics_config_path(config))
                argv = ["reporting", "status", "--config", diag_cfg]
            else:
                import diagnostic_support

                component = "diagnostics"
                root_arg = args.get("root")
                if root_arg is not None and (not isinstance(root_arg, str) or not root_arg):
                    raise ValueError("diagnostics_bundle 'root' must be a non-empty string")
                argv = ["bundle", "--root", root_arg or diagnostic_support.default_diagnostics_root()]
                op_id = args.get("operation_id") if isinstance(args.get("operation_id"), str) else args.get("incident_id")
                if isinstance(op_id, str) and op_id:
                    argv += ["--operation-id", op_id]
                if isinstance(args.get("output"), str) and args["output"]:
                    argv += ["--output", args["output"]]

            budget = args.get("max_output_tokens", 16384)
            if type(budget) is not int or not 1 <= budget <= 262144:
                raise ValueError("max_output_tokens must be an integer between 1 and 262144")
            timeout_ms = args.get("timeout_ms", 120000)
            if type(timeout_ms) is not int or not 1 <= timeout_ms <= 600000:
                raise ValueError("timeout_ms must be an integer between 1 and 600000")

            explicit_cwd = args.get("cwd")
            run_cwd = None
            if isinstance(explicit_cwd, str) and os.path.isabs(explicit_cwd) and Path(explicit_cwd).is_dir():
                run_cwd = explicit_cwd
            elif isinstance(bound_cwd, str) and os.path.isabs(bound_cwd) and Path(bound_cwd).is_dir():
                run_cwd = bound_cwd

            child_env = dict(os.environ)
            child_env["OPENCODE_SESSION_ID"] = session
            child_env["MINDIE_AGENT_CONFIG"] = str(config_path(config))
            child_env.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(diagnostics_config_path(config)))
            child_env.pop("PYTHONPATH", None)

            cmd = _resolve_component_command(component, config=config) + list(argv)
            proc = bounded_run(
                cmd,
                "",
                timeout=timeout_ms / 1000.0,
                max_output=COMPONENT_CAPTURE_LIMIT_BYTES,
                env=child_env,
                cwd=run_cwd,
                strict_output_limit=False,
            )
            reply_limit = budget * 4
            stdout_bytes = proc.stdout.encode("utf-8", errors="replace")
            stderr_bytes = proc.stderr.encode("utf-8", errors="replace")
            truncated = (
                proc.stdout_truncated
                or proc.stderr_truncated
                or len(stdout_bytes) + len(stderr_bytes) > reply_limit
            )
            err_budget = min(len(stderr_bytes), reply_limit // 2)
            out_budget = min(len(stdout_bytes), reply_limit - err_budget)
            err_budget = min(len(stderr_bytes), reply_limit - out_budget)
            payload = dict(
                component=component,
                exit_code=proc.returncode,
                stdout=stdout_bytes[:out_budget].decode("utf-8", errors="replace"),
                stderr=stderr_bytes[:err_budget].decode("utf-8", errors="replace"),
                output_truncated=truncated,
            )
            if not truncated and proc.stdout:
                try:
                    payload["result"] = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    pass
            if name in COMPONENT_DESCRIPTIONS and proc.returncode not in (0, 2):
                import diagnostic_support

                diag = diagnostic_support.record_component_crash(
                    component, argv, proc.returncode, proc.stderr,
                    parsed_result=payload.get("result"), config=config, executable=cmd[0],
                )
                if diag is not None:
                    payload["diagnostic"] = diag
            succeeded = proc.returncode == 0
            result = dict(
                content=[dict(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structuredContent=payload,
                isError=proc.returncode != 0,
            )
        else:
            raise ValueError(f"unknown mindie-tools tool: {name}")
    except Exception as exc:
        import diagnostic_support

        err_struct = dict(error=str(exc), error_type=type(exc).__name__)
        if diagnostic_support.is_internal_mcp_exception(exc):
            err_struct["diagnostic"] = diagnostic_support.failure(
                "mcp.tools", str(name or "dispatch"), "internal", exception=exc, config=config
            )
        result = dict(
            content=[dict(type="text", text=f"{type(exc).__name__}: {exc}")],
            structuredContent=err_struct,
            isError=True,
        )

    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except TimeoutError as exc:
            if not succeeded:
                raise
            result = _attach_accounting_warning(result, "bind_warning", exc)
        except (OSError, sqlite3.Error) as exc:
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def call_remote_surface(
    name: str,
    arguments: dict | None = None,
    *,
    config=None,
    receipts: RemoteReceipts | None = None,
) -> dict:
    os.environ.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(diagnostics_config_path(config)))
    raw_args = dict(arguments or {})
    explicit_bind = raw_args.pop("_mindie_bind_id", None)
    try:
        session, bind_id, _bound_cwd = _resolve_session(
            "remote",
            name,
            raw_args,
            bind_id=explicit_bind if isinstance(explicit_bind, str) else None,
            config=config,
        )
    except Exception as exc:
        return _admission_failure(exc)

    exec_args = dict(raw_args)
    if "yield_time_ms" in exec_args and type(exec_args["yield_time_ms"]) is int:
        exec_args["yield_time_ms"] = min(exec_args["yield_time_ms"], MAX_YIELD_TIME_MS)

    receipt_store = receipts or RemoteReceipts(remote_receipts_path(config))
    call_id = str(uuid.uuid4())
    try:
        remote_root = state_dir(config) / "remote" / session
        remote_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt_store.begin(call_id, session, name, exec_args)
    except Exception as exc:
        return _admission_failure(exc)

    os.environ["REMOTE_DEV_SESSION_ID"] = session
    os.environ["REMOTE_DEV_STATE_DIR"] = str(remote_root)
    succeeded = False
    outcome = "failed"
    try:
        from remote_dev.mcp.outcome import operation_outcome
        from remote_dev.mcp.server import tool_text
        from remote_dev.mcp.tools import call_tool

        raw = call_tool(name, exec_args, profile="canonical")
        outcome = operation_outcome(name, raw)
        is_error = outcome != "succeeded"
        succeeded = not is_error
        blocks = [dict(type="text", text=tool_text(raw))]
        native_blocks = raw.get("content") if isinstance(raw, dict) else None
        if isinstance(native_blocks, list):
            blocks.extend(
                item for item in native_blocks if isinstance(item, dict) and isinstance(item.get("type"), str)
            )
        result = dict(
            content=blocks,
            structuredContent=raw if isinstance(raw, dict) else dict(result=raw),
            isError=is_error,
        )
    except Exception as exc:
        from remote_dev.core.errors import CALLER_ERROR_HINTS, error_details, safe_auth_error_details

        details = error_details(exc)
        outcome = "unknown" if details.get("submission_state") == "uncertain" else "failed"
        auth = safe_auth_error_details(details)
        caller_hint = CALLER_ERROR_HINTS.get(details.get("error_code") or "") if details.get("category") == "caller" else None
        text_msg = auth["hint"] if auth else (f"{exc} ({caller_hint})" if caller_hint else f"{type(exc).__name__}: {exc}")
        result = dict(
            content=[dict(type="text", text=text_msg)],
            structuredContent=(
                dict(ok=False, error_details=auth)
                if auth
                else dict(ok=False, error=str(exc), error_type=type(exc).__name__, error_details=details)
            ),
            isError=True,
        )

    try:
        receipt_store.finish(call_id, outcome)
    except TimeoutError as exc:
        if not succeeded:
            raise
        result = _attach_accounting_warning(result, "receipt_warning", exc)
    except (OSError, sqlite3.Error) as exc:
        if succeeded:
            result = _attach_accounting_warning(result, "receipt_warning", exc)

    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except TimeoutError as exc:
            if not succeeded:
                raise
            result = _attach_accounting_warning(result, "bind_warning", exc)
        except (OSError, sqlite3.Error) as exc:
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def _safe_trigger_background_update(config=None) -> None:
    try:
        trigger_background_update_if_due(config=config)
    except (TimeoutError, Exception) as exc:
        print(f"MindIE OpenCode background update check warning: {type(exc).__name__}: {exc}", file=sys.stderr)


def handle_rpc(request: dict, surface: str, *, config=None, receipts: RemoteReceipts | None = None) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        req_id = request.get("id") if isinstance(request, dict) else None
        return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32600, message="Invalid Request"))
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    if method == "notifications/initialized" or req_id is None:
        return None
    if method == "ping":
        return dict(jsonrpc="2.0", id=req_id, result={})
    if method == "initialize":
        _safe_trigger_background_update(config=config)
        manifest_file = plugin_root() / "plugin.json"
        srv_ver = "0.1.0"
        if manifest_file.is_file():
            try:
                srv_ver = str(read_json(manifest_file).get("version") or "0.1.0")
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                print(f"MindIE OpenCode manifest read warning: {exc}", file=sys.stderr)
        return dict(
            jsonrpc="2.0", id=req_id,
            result=dict(
                protocolVersion=params.get("protocolVersion") or "2024-11-05",
                capabilities=dict(tools=dict(listChanged=False)),
                serverInfo=dict(name=f"mindie-oc-{surface}", version=srv_ver),
            ),
        )
    if method == "tools/list":
        tools = remote_catalog() if surface == "remote" else tools_catalog()
        return dict(jsonrpc="2.0", id=req_id, result=dict(tools=tools))
    if method == "tools/call":
        _safe_trigger_background_update(config=config)
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not isinstance(name, str) or not name:
            return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32602, message="MCP tools/call requires a tool name"))
        outcome = (
            call_remote_surface(name, arguments, config=config, receipts=receipts)
            if surface == "remote"
            else call_tools_surface(name, arguments, config=config, receipts=receipts)
        )
        return dict(jsonrpc="2.0", id=req_id, result=outcome)
    return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32601, message=f"Method not found: {method}"))


MAX_STDIO_FRAME_BYTES = 16 * 1024 * 1024


def _discard_bytes(stream, count: int) -> None:
    rem = count
    while rem > 0:
        chunk = stream.read(min(65536, rem))
        if not chunk:
            break
        rem -= len(chunk)


def _iter_stdio_messages(stream):
    """Yield `(framed, request_dict)` supporting bounded newline-delimited and Content-Length JSON-RPC."""
    while True:
        first = stream.readline(MAX_STDIO_FRAME_BYTES + 1)
        if not first:
            return
        if len(first) > MAX_STDIO_FRAME_BYTES and not first.endswith(b"\n"):
            while True:
                tail = stream.readline(65536)
                if not tail or tail.endswith(b"\n"):
                    break
            yield False, {"__parse_error__": f"Parse error: line exceeds {MAX_STDIO_FRAME_BYTES} byte limit"}
            continue
        stripped = first.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError as exc:
                yield True, {"__parse_error__": f"Parse error: invalid Content-Length ({exc})"}
                continue
            for _ in range(64):
                hdr = stream.readline(8192)
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            if length > MAX_STDIO_FRAME_BYTES:
                _discard_bytes(stream, length)
                yield True, {"__parse_error__": f"Parse error: Content-Length {length} exceeds {MAX_STDIO_FRAME_BYTES} byte limit"}
                continue
            buf = bytearray()
            while len(buf) < length:
                chunk = stream.read(min(65536, length - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
            if len(buf) < length:
                return
            try:
                yield True, json.loads(buf.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield True, {"__parse_error__": f"Parse error: {exc}"}
        else:
            try:
                yield False, json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield False, {"__parse_error__": f"Parse error: {exc}"}


def _write_rpc_frame(out_stream, resp: dict, *, framed: bool) -> None:
    encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
    if framed:
        out_stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
    else:
        out_stream.write(encoded + b"\n")
    out_stream.flush()


def serve(surface: str, *, config=None, stdin=None, stdout=None) -> int:
    if surface not in {"remote", "tools"}:
        raise ValueError(f"unsupported MCP surface: {surface}")
    in_stream = sys.stdin.buffer if stdin is None else stdin
    out_stream = sys.stdout.buffer if stdout is None else stdout
    receipts = RemoteReceipts(remote_receipts_path(config))
    try:
        for framed, req in _iter_stdio_messages(in_stream):
            if isinstance(req, dict) and "__parse_error__" in req:
                _write_rpc_frame(
                    out_stream,
                    dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message=str(req["__parse_error__"]))),
                    framed=framed,
                )
                continue
            resp = handle_rpc(req, surface, config=config, receipts=receipts)
            if resp is not None:
                _write_rpc_frame(out_stream, resp, framed=framed)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    surface = argv[0] if argv else "tools"
    return serve(surface)


if __name__ == "__main__":
    raise SystemExit(main())
