"""Bounded incremental public-transcript reader for OpenCode (`opencode_transcript.py`).

[Role]
Per-Harness transcript adapter (`FileIdentity`, `identify`, `capture_source`,
`read_material`) consumed by `mindie_knowledge.loop` (`Engine`), re-exporting
`sync_session_spool` from `opencode_spool.py`.

[Invariants]
- Enforces exact `[start, end)` SHA-256 byte accounting with a 512-byte prefix
  anchor (`FileIdentity`).
- Never populates `coverage` on complete reads; corrupt, truncated, oversize-unreadable,
  or timestamp-less public records populate `discarded_records` without advancing cursors.
- Never truncates or drops single large public messages (`<= max_scan_bytes`).

[Timeouts & Locks]
- `read_material` enforces `max_scan_bytes=16 MiB` and `max_seconds=2.0s` with
  zero locks or subprocesses.

[Side-Effect Policy]
- `identify`, `capture_source`, and `read_material` are strictly read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from opencode_spool import (  # noqa: E402
    SESSION_ID_RE,
    SPOOL_SCHEMA,
    extract_record as _extract_record,
    sync_session_spool,
    to_seconds,
)

__all__ = [
    "ANCHOR_BYTES",
    "FileIdentity",
    "MAX_RECORDS",
    "MAX_TEXT",
    "SPOOL_SCHEMA",
    "capture_source",
    "identify",
    "read_increment",
    "read_material",
    "sync_session_spool",
    "to_seconds",
]

MAX_TEXT = 48 * 1024
MAX_RECORDS = 200
ANCHOR_BYTES = 512
SOURCE_HEAD_BYTES = 1024 * 1024
SOURCE_HEAD_LINES = 64
MAX_SPOOL_LINE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FileIdentity:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    anchor_len: int = 0
    anchor_digest: str = ""

    @property
    def key(self) -> str:
        return f"{self.path}|{self.dev}:{self.ino}"

    def serialize(self) -> str:
        return json.dumps(
            dict(
                dev=self.dev,
                ino=self.ino,
                anchor_len=self.anchor_len,
                anchor_digest=self.anchor_digest,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def unserialize(text: str, path: str) -> FileIdentity | None:
        try:
            data = json.loads(text)
            anchor_len = data["anchor_len"]
            anchor_digest = data["anchor_digest"]
            if (
                type(anchor_len) is not int
                or not 0 < anchor_len <= ANCHOR_BYTES
                or not isinstance(anchor_digest, str)
                or len(anchor_digest) != 64
            ):
                return None
            return FileIdentity(
                path,
                int(data["dev"]),
                int(data["ino"]),
                0,
                0,
                anchor_len,
                anchor_digest,
            )
        except (ValueError, KeyError, TypeError):
            return None


def identify(path: str | Path) -> FileIdentity | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    try:
        stat = os.fstat(fd)
        anchor = os.read(fd, ANCHOR_BYTES)
    except TimeoutError:
        raise
    except OSError:
        return None
    finally:
        os.close(fd)
    return FileIdentity(
        str(Path(path).resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        len(anchor),
        hashlib.sha256(anchor).hexdigest(),
    )


def _session_in_path(path: str | Path) -> str | None:
    stem = Path(path).stem
    if SESSION_ID_RE.fullmatch(stem) and stem.lower() not in {"transcript", "transcript_full", "opencode"}:
        return stem
    parts = Path(path).parts
    for part in reversed(parts[:-1]):
        if SESSION_ID_RE.fullmatch(part) and part.lower() not in {"transcripts", "sessions", "logs", "oc-state"}:
            return part
    return None


def _iter_head_lines(path: Path, *, max_lines: int, max_line_bytes: int):
    """Stream up to `max_lines` newline-delimited records, allowing each line up to `max_line_bytes` (`16 MiB`)."""
    with path.open("rb") as stream:
        emitted = 0
        buf = bytearray()
        overflow = False
        while emitted < max_lines:
            chunk = stream.read(65536)
            if not chunk:
                if buf and not overflow:
                    yield bytes(buf)
                break
            start_idx = 0
            while emitted < max_lines and start_idx < len(chunk):
                nl = chunk.find(b"\n", start_idx)
                if nl == -1:
                    rem = chunk[start_idx:]
                    if not overflow:
                        if len(buf) + len(rem) <= max_line_bytes:
                            buf.extend(rem)
                        else:
                            buf.clear()
                            overflow = True
                    break
                piece = chunk[start_idx:nl]
                start_idx = nl + 1
                emitted += 1
                if not overflow and len(buf) + len(piece) <= max_line_bytes:
                    line = (bytes(buf) + piece) if buf else piece
                    buf.clear()
                    yield line.rstrip(b"\r")
                else:
                    buf.clear()
                    overflow = False


def capture_source(path: str | Path, session_id: str, *, binding: dict | None = None) -> dict:
    """Validate an OpenCode spool/transcript path and return its capture source descriptor."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()) or ".." in session_id:
        raise ValueError("native OpenCode session_id is missing or invalid")
    norm_session = session_id.strip()
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValueError("capture transcript path must be absolute")
    try:
        selected = Path(path).resolve(strict=True)
    except TimeoutError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError("capture transcript is unavailable") from exc
    if not selected.is_file():
        raise ValueError("capture source must be a regular transcript file")
    owner = _session_in_path(selected)
    if owner != norm_session:
        raise ValueError("capture transcript does not belong to this session_id")

    created = None
    for raw in _iter_head_lines(selected, max_lines=SOURCE_HEAD_LINES, max_line_bytes=MAX_SPOOL_LINE_BYTES):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("session_id"), str) and record["session_id"] != norm_session:
            raise ValueError("capture transcript session_id header mismatch")
        stamp = to_seconds(
            record.get("session_created")
            or record.get("time_created")
            or record.get("timestamp")
            or record.get("created_at")
        )
        if stamp is not None and math.isfinite(stamp):
            created = stamp
            break
    if created is None:
        raise ValueError("native OpenCode task creation timestamp is unavailable")

    if binding is not None:
        scope = binding.get("project_root")
        boundary = binding.get("activated_at")
        if (
            str(binding.get("session", "")) != norm_session
            or type(binding.get("enabled")) not in (bool, int)
            or int(binding["enabled"]) != 1
            or not isinstance(scope, str)
            or not os.path.isabs(scope)
            or type(boundary) not in (int, float)
            or not math.isfinite(boundary)
            or boundary < 0
        ):
            raise ValueError("native task binding is invalid")
        return dict(
            session_id=norm_session,
            project_root=str(Path(scope).resolve()),
            created_at=created,
            transcript_path=str(selected),
        )
    raise ValueError("native task binding is required to establish project scope")


def read_material(
    path: str | Path,
    start: int = 0,
    *,
    session_id: str | None = None,
    not_before: float | str | None = None,
    expected: FileIdentity | None = None,
    max_scan_bytes: int = 16777216,
    max_seconds: float = 2.0,
    max_text_bytes: int = MAX_TEXT,
    scan_until: int | None = None,
) -> dict:
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative byte offset")
    if max_scan_bytes <= 0 or max_seconds <= 0 or max_text_bytes <= 0:
        raise ValueError("invalid scan or text budget")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start):
        raise ValueError("scan_until must be an integer byte boundary at or after start")
    boundary = to_seconds(not_before) if not_before is not None else None
    norm_session = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None

    result: dict = dict(
        status="ok",
        start=start,
        end=start,
        digest=hashlib.sha256(b"").hexdigest(),
        text="",
        records=0,
        skipped_records=0,
        oversize_records=0,
        partial=False,
        more=False,
        timestamps_reliable=True,
        session_match=None,
        coverage_note=None,
        coverage=[],
    )
    consumed = hashlib.sha256()
    included: list[str] = []
    text_size = 0
    begun = time.monotonic()

    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(
                str(Path(path).resolve()),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                len(anchor),
                hashlib.sha256(anchor).hexdigest(),
            )
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (
                    expected.path != current.path
                    or expected.dev != stat.st_dev
                    or expected.ino != stat.st_ino
                    or not expected.anchor_len
                    or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest() != expected.anchor_digest
                ):
                    result.update(status="replaced", coverage_note="transcript changed before read")
                    return result
            if stat.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result

            owner = _session_in_path(path)
            if owner:
                result["session_match"] = not norm_session or owner == norm_session
                if norm_session and owner != norm_session:
                    result.update(status="wrong-task", coverage_note="transcript belongs to another session")
                    return result
            elif norm_session:
                result.update(status="unknown-format", coverage_note="session path identity unavailable")
                return result

            if start > 0:
                stream.seek(start - 1)
                if stream.read(1) != b"\n":
                    result.update(status="invalid-boundary", coverage_note="cursor is not a whole-record boundary")
                    return result
            stream.seek(start)

            end_limit = min(stat.st_size, start + max_scan_bytes)
            if scan_until is not None:
                end_limit = min(end_limit, scan_until)
            offset = start

            while offset < end_limit:
                if (time.monotonic() - begun) >= max_seconds or len(included) >= MAX_RECORDS:
                    result["more"] = True
                    break
                room = end_limit - offset
                raw_line = stream.readline(room)
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    if offset + len(raw_line) == stat.st_size:
                        result["partial"] = True
                        break
                    if scan_until is not None and offset + len(raw_line) == scan_until:
                        result["partial"] = True
                        break
                    if offset > start:
                        result["more"] = True
                        break
                    result.setdefault("discarded_records", []).append(
                        dict(start=offset, end=offset + len(raw_line), reason="record exceeds max_scan_bytes")
                    )
                    result["oversize_records"] += 1
                    result.update(status="incomplete", coverage_note="record exceeds max_scan_bytes")
                    return result

                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    record = None
                if not isinstance(record, dict):
                    result.setdefault("discarded_records", []).append(
                        dict(start=offset, end=offset + len(raw_line), reason="invalid record")
                    )
                    consumed.update(raw_line)
                    offset += len(raw_line)
                    result["skipped_records"] += 1
                    continue

                rec_sid = record.get("session_id")
                if norm_session and isinstance(rec_sid, str) and rec_sid != norm_session:
                    result.update(status="wrong-task", coverage_note="spool record belongs to another session")
                    return result

                extracted = _extract_record(record)
                if extracted is not None:
                    role, text_body, stamp = extracted
                    if boundary is not None:
                        if stamp is None:
                            result["timestamps_reliable"] = False
                            result.setdefault("discarded_records", []).append(
                                dict(start=offset, end=offset + len(raw_line), reason="missing public timestamp")
                            )
                            consumed.update(raw_line)
                            offset += len(raw_line)
                            result["skipped_records"] += 1
                            continue
                        if stamp + 1e-3 < boundary:
                            consumed.update(raw_line)
                            offset += len(raw_line)
                            result["skipped_records"] += 1
                            continue
                    norm_text = text_body.replace("\r\n", "\n").replace("\r", "\n")
                    formatted = f"[{role}]\n{norm_text}"
                    encoded_len = len(formatted.encode("utf-8")) + (2 if included else 0)
                    if included and (text_size + encoded_len) > max_text_bytes:
                        result["more"] = True
                        break
                    included.append(formatted)
                    text_size += encoded_len
                    result["records"] += 1
                else:
                    result["skipped_records"] += 1

                consumed.update(raw_line)
                offset += len(raw_line)

            result["end"] = offset
            result["digest"] = consumed.hexdigest()
            result["text"] = "\n\n".join(included)
            if offset < stat.st_size:
                result["more"] = True
            if result["status"] == "ok" and result["end"] == start and not result.get("discarded_records"):
                result["status"] = "unchanged"
            return result
    except TimeoutError:
        raise
    except FileNotFoundError:
        result.update(status="unavailable", coverage_note="transcript file missing")
        return result
    except OSError as exc:
        result.update(status="unavailable", coverage_note=f"transcript read failed: {type(exc).__name__}")
        return result


read_increment = read_material
