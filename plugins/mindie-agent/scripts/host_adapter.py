#!/usr/bin/env python3
"""OpenCode (`oc`) host adapter, seed verifier, and concurrent MCP wire loop.

[Role]
Implements `OCHostAdapter` conforming to `mindie_harness.api.HostAdapter` (`host = "oc"`),
`verify_seed_bundle()` for streaming `64 KiB` SHA-256 verification of bundled
`assets/mindie-harness-seed.pyz`, and `serve_concurrent_stdio()` for concurrent dual-framing
(`Content-Length` + JSONL) MCP stdio serving across `remote` and `tools`.

[Invariants]
- Pure Python stdlib at module import time (`< 15ms`, zero device context, zero network).
- `extract_session_identity` reads verified host metadata or `OPENCODE_SESSION_ID` only—never
  model-supplied tool `arguments` (`P2.7`).
- Deterministic `remote` and `tools` surfaces never inject `_mindie_bind_id` or expose
  `knowledge_*` tools (`P2.1` & `P2.7`).

[Timeouts & Locks]
- Seed verification streams `64 KiB` chunks in `< 5ms`; resident stdio serving uses separate
  read/control (`16` workers) and mutation (`8` workers) pools with serialized writes.

[Side-Effect Policy]
- Importing and `verify_seed_bundle()` are strictly read-only; CLI execution delegates to
  `mindie_harness.api.run_host(..., adapter=OCHostAdapter())`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, BinaryIO

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import config_path, data_home, plugin_root

SEED_LOCK_SCHEMA = "mindie-harness-seed/1"
MAX_WIRE_FRAME_BYTES = 16 * 1024 * 1024
_FAST_REMOTE_SUFFIXES = (
    "job_status", "job_tail", "job_output", "job_stop", "job_list", "job_kill",
    "taskoutput", "taskstop", "tasklist", "read", "ls", "glob", "grep", "probe", "stat", "status",
)
_FAST_TOOLS = frozenset({
    "mindie_status", "diagnostics_status", "npu_top", "npu_status", "server_status",
    "list_mounts", "find_npu_capacity", "npu_telemetry_window", "list_npu_servers",
})


def seed_bundle_paths(root: str | Path | None = None) -> tuple[Path, Path]:
    base = Path(root).resolve() if root is not None else plugin_root()
    assets = base / "assets"
    return assets / "mindie-harness-seed.pyz", assets / "seed-lock.json"


def _stdlib_verify_seed_lock(pyz_path: Path, lock_path: Path) -> dict[str, Any]:
    if not lock_path.is_file():
        raise ValueError(f"Missing seed lock file: {lock_path}")
    if not pyz_path.is_file():
        raise ValueError(f"Missing seed archive: {pyz_path}")
    lock_doc = json.loads(lock_path.read_bytes().decode("utf-8-sig"))
    if not isinstance(lock_doc, dict) or lock_doc.get("schema") != SEED_LOCK_SCHEMA:
        raise ValueError(f"Invalid seed-lock schema in {lock_path}")
    expected_sha = str(lock_doc.get("sha256") or "").strip().lower()
    expected_size = lock_doc.get("size_bytes")
    if len(expected_sha) != 64 or type(expected_size) is not int or expected_size <= 0:
        raise ValueError("Malformed sha256 or size_bytes in seed-lock.json")
    digest = hashlib.sha256()
    actual_size = 0
    with pyz_path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
            actual_size += len(chunk)
    actual_sha = digest.hexdigest()
    if actual_size != expected_size or actual_sha != expected_sha:
        raise ValueError(
            f"Seed integrity mismatch for {pyz_path}: expected {expected_sha} ({expected_size}B), "
            f"got {actual_sha} ({actual_size}B)"
        )
    return lock_doc


def verify_seed_bundle(root: str | Path | None = None) -> dict[str, Any]:
    pyz_path, lock_path = seed_bundle_paths(root)
    try:
        from mindie_harness.bootstrap.seed_main import verify_seed_lock
    except ImportError:
        return _stdlib_verify_seed_lock(pyz_path, lock_path)
    return verify_seed_lock(pyz_path, lock_path)


def is_fast_lane_tool(surface: str, name: str, arguments: Mapping[str, Any]) -> bool:
    if surface == "tools":
        return name in _FAST_TOOLS
    lowered = name.lower()
    if lowered == "remote_auth":
        return arguments.get("action") == "status"
    return lowered.endswith(_FAST_REMOTE_SUFFIXES)


def _file_revision(*files: Path) -> tuple[tuple[int | None, int | None], ...]:
    out: list[tuple[int | None, int | None]] = []
    for f in files:
        try:
            st = f.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((None, None))
    return tuple(out)


@contextlib.contextmanager
def hold_combined_service_lease():
    """Hold both `storage_gc.acquire_service_lease` and `mindie_harness` generation lease."""
    from storage_gc import acquire_service_lease, release_service_lease

    lease = acquire_service_lease()
    harness_lease = None
    if lease is not None and hasattr(lease, "directory"):
        try:
            from mindie_harness.locks import acquire_generation_lease

            harness_lease = acquire_generation_lease(lease.directory)
        except TimeoutError:
            release_service_lease(lease)
            raise
        except Exception:
            harness_lease = None
    try:
        yield lease
    finally:
        try:
            if harness_lease is not None:
                harness_lease.release()
        except TimeoutError:
            raise
        except OSError:
            pass
        finally:
            release_service_lease(lease)


def serve_concurrent_stdio(
    *,
    surface: str,
    rpc_handler: Callable[[Any, threading.Event | None], dict[str, Any] | None],
    watched_files: tuple[Path, ...] = (),
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    max_frame_bytes: int = MAX_WIRE_FRAME_BYTES,
) -> int:
    from mcp_stdio import iter_stdio_messages, write_rpc_frame

    in_stream = sys.stdin.buffer if stdin is None else stdin
    out_stream = sys.stdout.buffer if stdout is None else stdout
    write_lock = threading.Lock()
    inflight_lock = threading.Lock()
    inflight: dict[object, threading.Event] = {}
    last_rev = [_file_revision(*watched_files)]

    def _send(payload: dict[str, Any], framed: bool) -> None:
        with write_lock:
            write_rpc_frame(out_stream, payload, framed=framed)

    def _check_revision(framed: bool) -> None:
        if not watched_files:
            return
        cur = _file_revision(*watched_files)
        if cur != last_rev[0]:
            last_rev[0] = cur
            _send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, framed)

    def _dispatch_call(req: dict[str, Any], req_id: object, cancel_ev: threading.Event, framed: bool) -> None:
        try:
            resp = rpc_handler(req, cancel_ev)
            _check_revision(framed)
            if resp is not None:
                _send(resp, framed)
        except BrokenPipeError:
            return
        finally:
            with inflight_lock:
                inflight.pop(req_id, None)

    read_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="mindie-oc-read")
    mut_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mindie-oc-mut")
    try:
        for framed, req in iter_stdio_messages(in_stream, max_frame_bytes=max_frame_bytes):
            if isinstance(req, dict) and "__parse_error__" in req:
                _send(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(req["__parse_error__"])}},
                    framed,
                )
                continue
            if not isinstance(req, dict):
                resp = rpc_handler(req, None)
                if resp is not None:
                    _send(resp, framed)
                continue
            method = req.get("method")
            req_id = req.get("id")
            if method == "notifications/cancelled":
                target_id = (req.get("params") or {}).get("requestId") if isinstance(req.get("params"), dict) else None
                with inflight_lock:
                    ev = inflight.get(target_id)
                if ev is not None:
                    ev.set()
                continue
            if method == "tools/call" and req_id is not None:
                params = req.get("params") if isinstance(req.get("params"), dict) else {}
                tname = params.get("name") if isinstance(params.get("name"), str) else ""
                targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                cancel_ev = threading.Event()
                with inflight_lock:
                    if req_id in inflight:
                        _send(
                            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Duplicate in-flight request id"}},
                            framed,
                        )
                        continue
                    inflight[req_id] = cancel_ev
                pool = read_pool if is_fast_lane_tool(surface, tname, targs) else mut_pool
                pool.submit(_dispatch_call, req, req_id, cancel_ev, framed)
                continue
            _check_revision(framed)
            resp = rpc_handler(req, None)
            if resp is not None:
                _send(resp, framed)
    except BrokenPipeError:
        with inflight_lock:
            for ev in inflight.values():
                ev.set()
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except BaseException:
        with inflight_lock:
            for ev in inflight.values():
                ev.set()
        raise
    finally:
        read_pool.shutdown(wait=True)
        mut_pool.shutdown(wait=True)
    return 0


class OCHostAdapter:
    """Thin OpenCode (`oc`) adapter over `mindie_harness.api.HostAdapter`."""

    host = "oc"

    def __init__(self, *, config: str | Path | None = None) -> None:
        self._config = config

    def resolve_profile(self, explicit_config: str | Path | None = None):
        from mindie_harness.api import ProfileRef

        cfg = config_path(explicit_config or self._config)
        return ProfileRef.resolve(
            self.host,
            plugin_root=plugin_root(),
            config_path=cfg,
            data_dir=data_home(),
        )

    def extract_session_identity(
        self,
        request: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> str | None:
        from identity import require_session

        params = request.get("params")
        meta = params.get("_meta") if isinstance(params, Mapping) else None
        if isinstance(meta, Mapping):
            for key in ("sessionID", "session_id", "sessionId"):
                raw = meta.get(key)
                if isinstance(raw, str) and raw.strip():
                    try:
                        return require_session(raw)
                    except ValueError:
                        return None
        raw_env = env.get("OPENCODE_SESSION_ID")
        if isinstance(raw_env, str) and raw_env.strip():
            try:
                return require_session(raw_env)
            except ValueError:
                return None
        return None

    def mcp_tools(self, surface: str, profile) -> list[dict[str, Any]]:
        import mcp_server

        if surface == "remote":
            return mcp_server.remote_catalog()
        if surface in {"tools", "components"}:
            return mcp_server.tools_catalog()
        if surface == "all":
            return mcp_server.remote_catalog() + mcp_server.tools_catalog()
        raise ValueError(f"Unsupported OpenCode MCP surface: {surface!r}")

    def dispatch_tool(self, ctx, arguments: dict[str, Any], profile) -> dict[str, Any]:
        import mcp_server

        cfg = profile.config_path if profile is not None else config_path(self._config)
        if ctx.surface == "remote" or ctx.tool_name.startswith("remote_"):
            return mcp_server.call_remote_surface(
                ctx.tool_name, arguments, config=cfg, cancel=ctx.cancel
            )
        return mcp_server.call_tools_surface(
            ctx.tool_name, arguments, config=cfg, cancel=ctx.cancel
        )


def main(argv: list[str] | None = None) -> int:
    from mindie_harness.api import run_host

    return int(run_host(argv, adapter=OCHostAdapter()))


if __name__ == "__main__":
    raise SystemExit(main())
