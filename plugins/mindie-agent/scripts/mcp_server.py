#!/usr/bin/env python3
"""Thin resident MCP delegator from OpenCode (`oc`) to `mindie-harness`.
[Role] Exposes `remote`/`tools` via `mindie_harness.mcp.gateway.ManagedMCPAdapter` with `oc` profile defaults.
[Invariants] Zero session-binding metadata; `TimeoutError` propagates; delegates all tool and knowledge RPCs to `mindie-harness`.
[Timeouts & Locks] Framing, deadlines, worker pools, and generation leases delegate to `serve_mcp`.
[Side-Effect Policy] Local SQLite receipts and native plugin rebinding only; never mutates OpenCode configuration.
"""
from __future__ import annotations
import os, sys
from pathlib import Path
from mindie_harness.api import ProfileRef, serve_mcp
from mindie_harness.mcp.gateway import ManagedMCPAdapter, rebind_native_plugin_if_needed
ROOT = Path(__file__).resolve().parent.parent
def _defaults(profile: ProfileRef) -> None:
    os.environ.setdefault("REMOTE_DEV_STATE_DIR", str(profile.state_dir / "remote"))
    os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", "oc")
class OCManagedAdapter(ManagedMCPAdapter):
    host = "oc"
    def __init__(self, host: str = "oc", **kwargs) -> None:
        super().__init__(host, **kwargs); _defaults(self.resolve_profile())
    def mcp_tools(self, surface: str, profile: ProfileRef) -> list[dict]:
        _defaults(profile); return super().mcp_tools(surface, profile)
def serve(surface: str, *, config=None, stdin=None, stdout=None) -> int:
    if surface not in {"remote", "tools"}: raise ValueError(f"unsupported MCP surface: {surface}")
    profile = ProfileRef.resolve("oc", plugin_root=ROOT, config_path=config)
    try: rebind_native_plugin_if_needed(profile, runtime_scripts=ROOT / "scripts")
    except TimeoutError: raise
    except OSError: pass
    return serve_mcp(surface, profile=profile, adapter=OCManagedAdapter("oc", plugin_root=ROOT, config_path=config), stdin=stdin, stdout=stdout)
def main(argv: list[str] | None = None) -> int:
    items = [item for item in (sys.argv[1:] if argv is None else argv) if item != "--once"]
    return serve(items[0] if items else "tools")
if __name__ == "__main__": raise SystemExit(main())
