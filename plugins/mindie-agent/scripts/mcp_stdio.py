"""Bounded JSON-RPC stdio framing for the MindIE MCP server (`mcp_stdio.py`).

[Role]
Encodes and decodes the two wire shapes an MCP client may speak: newline
delimited JSON-RPC and `Content-Length`-framed JSON-RPC. Kept separate so
`mcp_server.py` stays a thin dispatch surface and the resident MCP process can
hold its runtime lease in one obvious place.

[Invariants]
- A declared frame larger than the caller's `max_frame_bytes` is drained in
  bounded chunks and reported as `-32700`; the stream is never re-read out of
  alignment and memory never grows past the limit.
- A truncated frame (EOF mid-body) ends iteration instead of emitting a partial
  request.
- Only `dict` JSON payloads are raised to the caller; invalid JSON and invalid
  `Content-Length` values surface as `{"__parse_error__": ...}` markers.

[Timeouts & Locks]
- Every read is bounded by `max_frame_bytes` (`+1` sentinel) or a `65536`-byte
  chunk; no lock, timer, or subprocess is used here.

[Side-Effect Policy]
- Pure stream I/O on the handles the caller provides: writes only the response
  frame passed in, and never touches the filesystem.
"""

from __future__ import annotations

import json

DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 65536


def discard_bytes(stream, count: int) -> None:
    """Drain `count` bytes in bounded chunks so an oversized frame stays aligned."""
    rem = count
    while rem > 0:
        chunk = stream.read(min(CHUNK_BYTES, rem))
        if not chunk:
            break
        rem -= len(chunk)


def iter_stdio_messages(stream, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES):
    """Yield `(framed, request_dict)` for newline-delimited or `Content-Length` frames."""
    while True:
        first = stream.readline(max_frame_bytes + 1)
        if not first:
            return
        if len(first) > max_frame_bytes and not first.endswith(b"\n"):
            while True:
                tail = stream.readline(CHUNK_BYTES)
                if not tail or tail.endswith(b"\n"):
                    break
            yield False, {"__parse_error__": f"Parse error: line exceeds {max_frame_bytes} byte limit"}
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
                header = stream.readline(8192)
                if not header or header in (b"\r\n", b"\n"):
                    break
            if length > max_frame_bytes:
                discard_bytes(stream, length)
                yield True, {"__parse_error__": f"Parse error: Content-Length {length} exceeds {max_frame_bytes} byte limit"}
                continue
            buffer = bytearray()
            while len(buffer) < length:
                chunk = stream.read(min(CHUNK_BYTES, length - len(buffer)))
                if not chunk:
                    break
                buffer.extend(chunk)
            if len(buffer) < length:
                return
            try:
                yield True, json.loads(buffer.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield True, {"__parse_error__": f"Parse error: {exc}"}
        else:
            try:
                yield False, json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield False, {"__parse_error__": f"Parse error: {exc}"}


def write_rpc_frame(out_stream, response: dict, *, framed: bool) -> None:
    """Write one JSON-RPC response using the framing the request selected."""
    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if framed:
        out_stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
    else:
        out_stream.write(encoded + b"\n")
    out_stream.flush()


__all__ = ["DEFAULT_MAX_FRAME_BYTES", "discard_bytes", "iter_stdio_messages", "write_rpc_frame"]
