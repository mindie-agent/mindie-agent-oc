#!/usr/bin/env python3
"""Thin resident MCP delegator from OpenCode (`oc`) to `mindie-harness`.
[Role] Exposes `remote`/`tools` via `mindie_harness.mcp.gateway` with `oc` defaults and a knowledge RPC fallback.
[Invariants] Zero session-binding metadata; Receipt Honesty on `Admission.finish`; `TimeoutError` propagates.
[Timeouts & Locks] Framing, deadlines, worker pools, and generation leases delegate to `serve_mcp`.
[Side-Effect Policy] Local SQLite receipts only; never mutates OpenCode configuration.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from mindie_harness.api import ProfileRef, serve_mcp
from mindie_harness.mcp.gateway import ManagedMCPAdapter, rebind_native_plugin_if_needed
from mindie_harness.mcp.receipts import apply_receipt_honesty
ROOT = Path(__file__).resolve().parent.parent
def _defaults(p) -> None:
    os.environ.setdefault("REMOTE_DEV_STATE_DIR", str(p.state_dir / "remote")); os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", "oc")
def _rpc(ctx, args, p, fallback):
    sent = False
    try:
        from mindie_knowledge.loop import activation, cli, transport
        conn = cli.connect(str(p.config_path)); sent = True
        raw = transport.rpc(conn, ctx.tool_name, dict(args or {})); raw = raw if isinstance(raw, dict) else {"result": raw}
        out = {"content": [{"type": "text", "text": json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)}], "isError": False, "structuredContent": raw}
        try:
            lease = activation.Admission(p.admission_db).active_lease(ctx.session_id)
            if lease is not None: activation.Admission(p.admission_db).finish(ctx.session_id, lease["token"], True)
        except (TimeoutError, Exception) as exc: out = apply_receipt_honesty(out, exc)
        return out
    except TimeoutError: raise
    except Exception as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True, "structuredContent": {"reason": "knowledge_outcome_unknown" if sent else "knowledge_rpc_failed"}} if sent else fallback
class OCManagedAdapter(ManagedMCPAdapter):
    host = "oc"
    def __init__(self, host="oc", **kwargs): super().__init__(host, **kwargs); _defaults(self.resolve_profile())
    def mcp_tools(self, surface, p): _defaults(p); return super().mcp_tools(surface, p)
    def dispatch_tool(self, ctx, args, p):
        out = super().dispatch_tool(ctx, args, p); reason = (out.get("structuredContent") or {}).get("reason") if isinstance(out, dict) else None
        return _rpc(ctx, args, p, out) if out.get("isError") is True and reason == "knowledge_unavailable" else out
def serve(surface, *, config=None, stdin=None, stdout=None):
    if surface not in {"remote", "tools"}: raise ValueError(f"unsupported MCP surface: {surface}")
    p = ProfileRef.resolve("oc", plugin_root=ROOT, config_path=config)
    try: rebind_native_plugin_if_needed(p, runtime_scripts=ROOT / "scripts")
    except TimeoutError: raise
    except OSError: pass
    return serve_mcp(surface, profile=p, adapter=OCManagedAdapter("oc", plugin_root=ROOT, config_path=config), stdin=stdin, stdout=stdout)
def main(argv=None):
    items = [x for x in (sys.argv[1:] if argv is None else argv) if x != "--once"]
    return serve(items[0] if items else "tools")
if __name__ == "__main__": raise SystemExit(main())
