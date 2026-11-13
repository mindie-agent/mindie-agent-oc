"""Bounded incremental reader and SQLite-v2 spool projector for OpenCode (`oc`) transcripts.

[Role]
Per-Harness transcript adapter (`FileIdentity`, `identify`, `capture_source`,
`read_material`) and read-only OpenCode v2 `opencode.db` spool projector
(`sync_session_spool`) writing per-session append-only JSONL files under
`<state_dir>/transcripts/<session_id>.jsonl`.

[Invariants]
- Opens OpenCode's `opencode.db` strictly in read-only URI mode (`?mode=ro`) and
  rejects subagent (`parent_id`) or forked (`fork_session_id`) sessions.
- Extracts only public user and assistant text (`type == "text"`); excludes
  `reasoning`, `tool`, `tool_use`, `tool_result`, `synthetic`, `system`, `skill`,
  `shell`, `compaction`, `idle`, `ignored`/`synthetic` parts, and control tags.
- Enforces exact `[start, end)` SHA-256 byte accounting with a 512-byte prefix
  anchor (`FileIdentity`).

[Timeouts & Locks]
- `sync_session_spool` uses `timeout=2.0s` (`busy_timeout=2000`) read-only SQLite
  reads; `read_material` enforces `max_scan_bytes=16 MiB` and `max_seconds=2.0s`.

[Side-Effect Policy]
- `identify`, `capture_source`, and `read_material` are strictly read-only.
- `sync_session_spool` appends only to MindIE-owned `<state_dir>/transcripts/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

MAX_WINDOW = 256 * 1024
MAX_TEXT = 48 * 1024
MAX_RECORDS = 200
ANCHOR_BYTES = 512
SOURCE_HEAD_BYTES = 1024 * 1024
SOURCE_HEAD_LINES = 64
SPOOL_SCHEMA = "mindie-oc-spool/1"

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
STRIP_TAGS_RE = re.compile(
    r"<(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)\b[^>]*>.*?</(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>",
    re.DOTALL,
)
GESTURE_PREFIX_RE = re.compile(
    r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?:\s+|$)"
)


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


def to_seconds(raw) -> float | None:
    if type(raw) in (int, float) and raw > 0:
        value = float(raw)
        if value >= 1e12:
            value /= 1000.0
        return value if math.isfinite(value) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _readonly_sqlite_uri(db_path: Path) -> str:
    posix = db_path.resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def sync_session_spool(
    session_id: str,
    *,
    db_path: str | Path,
    spool_dir: str | Path,
) -> Path | None:
    """Incrementally project OpenCode v2 `session_message` rows into an append-only per-session JSONL spool."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()) or ".." in session_id:
        raise ValueError("native OpenCode sessionID is missing or invalid")
    sid = session_id.strip()
    db_file = Path(db_path)
    if not db_file.is_file():
        return None

    out_dir = Path(spool_dir).resolve()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    spool_file = out_dir / f"{sid}.jsonl"
    cursor_file = out_dir / f"{sid}.seq"

    last_seq = -1
    if spool_file.is_file() and cursor_file.is_file():
        try:
            last_seq = int(cursor_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last_seq = -1

    db = sqlite3.connect(_readonly_sqlite_uri(db_file), uri=True, timeout=2.0)
    try:
        db.execute("PRAGMA busy_timeout=2000;")
        srow = db.execute(
            "SELECT id, directory, parent_id, fork_session_id, time_created FROM session_v2 WHERE id=?",
            (sid,),
        ).fetchone()
        if srow is None:
            return spool_file if spool_file.is_file() else None
        _id, directory, parent_id, fork_session_id, session_created = srow
        if (isinstance(parent_id, str) and parent_id.strip()) or (
            isinstance(fork_session_id, str) and fork_session_id.strip()
        ):
            raise ValueError("subagent or forked OpenCode session cannot be captured")

        mrows = db.execute(
            "SELECT id, type, seq, time_created, data FROM session_message "
            "WHERE session_id=? AND seq>? ORDER BY seq ASC",
            (sid, last_seq),
        ).fetchall()
        if not mrows:
            return spool_file if spool_file.is_file() else None

        parts_by_msg: dict[str, list[dict]] = {}
        msg_ids = [r[0] for r in mrows if isinstance(r[0], str)]
        if msg_ids:
            placeholders = ",".join("?" for _ in msg_ids)
            for p_mid, p_type, p_data in db.execute(
                f"SELECT message_id, type, data FROM session_part WHERE session_id=? AND message_id IN ({placeholders}) ORDER BY ordinal ASC",
                (sid, *msg_ids),
            ):
                try:
                    parsed_part = json.loads(p_data) if isinstance(p_data, str) else {}
                except ValueError:
                    parsed_part = {}
                if isinstance(parsed_part, dict):
                    parsed_part.setdefault("type", p_type)
                    parts_by_msg.setdefault(p_mid, []).append(parsed_part)
    finally:
        db.close()

    lines: list[bytes] = []
    max_seq = last_seq
    for mid, mtype, seq, msg_created, raw_data in mrows:
        if int(seq) > max_seq:
            max_seq = int(seq)
        try:
            parsed_data = json.loads(raw_data) if isinstance(raw_data, str) else {}
        except ValueError:
            parsed_data = {}
        candidate = dict(
            schema=SPOOL_SCHEMA,
            session_id=sid,
            directory=directory,
            session_created=session_created,
            message_id=mid,
            type=mtype,
            seq=int(seq),
            time_created=msg_created,
            data=parsed_data if isinstance(parsed_data, dict) else {},
        )
        if mid in parts_by_msg:
            candidate["parts"] = parts_by_msg[mid]
        extracted = _extract_record(candidate)
        if extracted is None:
            continue
        role, text_body, _stamp = extracted
        sanitized_entry = dict(
            schema=SPOOL_SCHEMA,
            session_id=sid,
            directory=directory,
            session_created=session_created,
            message_id=mid,
            type=role,
            seq=int(seq),
            time_created=msg_created,
            data=dict(text=text_body, time=dict(created=msg_created)),
        )
        lines.append(
            (json.dumps(sanitized_entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        )

    if lines:
        with spool_file.open("ab") as stream:
            for raw_line in lines:
                stream.write(raw_line)
            stream.flush()
    if spool_file.is_file():
        cursor_tmp = out_dir / f".{sid}.seq.tmp"
        cursor_tmp.write_text(f"{max_seq}\n", encoding="utf-8")
        os.replace(cursor_tmp, cursor_file)
        return spool_file
    return None


def _session_in_path(path: str | Path) -> str | None:
    stem = Path(path).stem
    if SESSION_ID_RE.fullmatch(stem) and stem.lower() not in {"transcript", "transcript_full", "opencode"}:
        return stem
    parts = Path(path).parts
    for part in reversed(parts[:-1]):
        if SESSION_ID_RE.fullmatch(part) and part.lower() not in {"transcripts", "sessions", "logs", "oc-state"}:
            return part
    return None


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

    with selected.open("rb") as stream:
        head = stream.read(SOURCE_HEAD_BYTES)
    lines = head.split(b"\n")[:SOURCE_HEAD_LINES]
    created = None
    for raw in lines:
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


def _clean_user_text(raw: str) -> str:
    match = USER_REQUEST_RE.search(raw)
    text = match.group(1) if match is not None else STRIP_TAGS_RE.sub("", raw)
    text = STRIP_TAGS_RE.sub("", text).strip()
    if GESTURE_PREFIX_RE.match(text):
        text = GESTURE_PREFIX_RE.sub("", text, count=1).strip()
        if text in {"", "activate", "deactivate", "status", "init", "recover"}:
            return ""
    return text


def _extract_public_parts(blocks: object) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    chunks: list[str] = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        if item.get("synthetic") is True or item.get("ignored") is True or item.get("isMeta") is True:
            continue
        btype = item.get("type")
        if btype in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            text = item["text"]
            if not text.startswith("<persisted-output>"):
                chunks.append(text)
    return "\n".join(chunks)


def _extract_record(record: dict) -> tuple[str, str, float | None] | None:
    if record.get("isMeta") is True or record.get("synthetic") is True:
        return None

    # OpenCode v2 spool record (`mindie-oc-spool/1`)
    if record.get("schema") == SPOOL_SCHEMA:
        mtype = record.get("type")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
        stamp = to_seconds(time_info.get("created") or record.get("time_created"))
        if mtype == "user":
            if data.get("synthetic") is True or data.get("ignored") is True:
                return None
            raw_text = data.get("text") if isinstance(data.get("text"), str) else _extract_public_parts(data.get("content") or record.get("parts"))
            cleaned = _clean_user_text(raw_text)
            return ("user", cleaned, stamp) if cleaned else None
        if mtype == "assistant":
            raw_text = (
                data.get("text")
                if isinstance(data.get("text"), str)
                else _extract_public_parts(data.get("content") or record.get("parts"))
            )
            cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
            return ("assistant", cleaned, stamp) if cleaned else None
        return None

    # Direct JSONL message record (`type` or `role` in `{"user", "assistant"}`)
    stamp = to_seconds(record.get("time_created") or record.get("timestamp") or record.get("created_at"))
    role = record.get("role") or record.get("type")
    if role == "message" and isinstance(record.get("message"), dict):
        msg = record["message"]
        role = msg.get("role")
        raw_text = _extract_public_parts(msg.get("content"))
    else:
        raw_text = (
            record["text"]
            if isinstance(record.get("text"), str)
            else _extract_public_parts(record.get("content") or record.get("parts"))
        )
    if role == "user":
        cleaned = _clean_user_text(raw_text)
        return ("user", cleaned, stamp) if cleaned else None
    if role == "assistant":
        cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
        return ("assistant", cleaned, stamp) if cleaned else None
    return None


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

    result = dict(
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
                    result.update(status="replaced", coverage_note="start offset is not at a record boundary")
                    return result
            else:
                stream.seek(0)

            limit = min(stat.st_size, scan_until) if scan_until is not None else stat.st_size
            offset = start
            scanned = 0

            while offset < limit:
                if scanned >= max_scan_bytes or (time.monotonic() - begun) >= max_seconds or len(included) >= MAX_RECORDS:
                    result["more"] = True
                    break
                raw_line = stream.readline(min(MAX_WINDOW + 2, limit - offset))
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n") and (offset + len(raw_line)) < stat.st_size and len(raw_line) > MAX_WINDOW:
                    # Oversize line exceeding window: consume until newline within budget
                    remainder_budget = min(limit - (offset + len(raw_line)), max_scan_bytes - scanned)
                    tail = b""
                    while not raw_line.endswith(b"\n") and remainder_budget > 0:
                        chunk = stream.read(min(65536, remainder_budget))
                        if not chunk:
                            break
                        nl = chunk.find(b"\n")
                        if nl != -1:
                            tail += chunk[: nl + 1]
                            stream.seek(offset + len(raw_line) + len(tail))
                            break
                        tail += chunk
                        remainder_budget -= len(chunk)
                    full_line = raw_line + tail
                    if not full_line.endswith(b"\n"):
                        result["partial"] = True
                        break
                    consumed.update(full_line)
                    offset += len(full_line)
                    scanned += len(full_line)
                    result["oversize_records"] += 1
                    continue

                if not raw_line.endswith(b"\n"):
                    result["partial"] = True
                    break

                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    consumed.update(raw_line)
                    offset += len(raw_line)
                    scanned += len(raw_line)
                    result["skipped_records"] += 1
                    continue

                if not isinstance(record, dict):
                    consumed.update(raw_line)
                    offset += len(raw_line)
                    scanned += len(raw_line)
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
                            consumed.update(raw_line)
                            offset += len(raw_line)
                            scanned += len(raw_line)
                            result["skipped_records"] += 1
                            continue
                        if stamp + 1e-3 < boundary:
                            consumed.update(raw_line)
                            offset += len(raw_line)
                            scanned += len(raw_line)
                            result["skipped_records"] += 1
                            continue
                    formatted = f"[{role}]\n{text_body}"
                    encoded_len = len(formatted.encode("utf-8")) + (2 if included else 0)
                    if included and (text_size + encoded_len) > max_text_bytes:
                        result["more"] = True
                        break
                    if not included and encoded_len > max_text_bytes:
                        formatted = formatted.encode("utf-8")[:max_text_bytes].decode("utf-8", errors="ignore")
                        encoded_len = len(formatted.encode("utf-8"))
                    included.append(formatted)
                    text_size += encoded_len
                    result["records"] += 1
                    result["coverage"].append([offset, offset + len(raw_line)])
                else:
                    result["skipped_records"] += 1

                consumed.update(raw_line)
                offset += len(raw_line)
                scanned += len(raw_line)

            result["end"] = offset
            result["digest"] = consumed.hexdigest()
            result["text"] = "\n\n".join(included)
            if offset < stat.st_size:
                result["more"] = True
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
