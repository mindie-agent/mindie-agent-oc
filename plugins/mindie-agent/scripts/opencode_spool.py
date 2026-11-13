"""Read-only OpenCode v2 `opencode.db` SQLite spool projector (`opencode_spool.py`).

[Role]
Incrementally projects public user and assistant messages from OpenCode v2's
`opencode.db` (`session_v2`, `session_message`, `session_part`) into append-only
per-session JSONL spool files under `<state_dir>/transcripts/<session_id>.jsonl`.

[Invariants]
- Opens `opencode.db` strictly read-only (`?mode=ro`) and rejects subagent
  (`parent_id`) or forked (`fork_session_id`) sessions.
- Queries only public text parts (`type IN ('text', 'input_text', 'output_text')`),
  never loading hidden `reasoning` or `tool` blobs into memory.
- Applies `not_before` authorization filtering and deterministic secret/private-path
  redaction before writing local spool JSONL.
- Truncates any torn non-newline tail and reconciles the highest committed `seq`
  before appending, preventing crash-induced duplicate entries.

[Timeouts & Locks]
- Serializes writers per session via in-process thread lock + OS file lock
  (`<spool_dir>/.<session_id>.lock`) within a shared `max_seconds=2.0s` deadline.
- Enforces SQLite wall-clock deadline via `set_progress_handler` and paginates
  `session_message` in 500-row batches (`<= 501` SQLite bind parameters).

[Side-Effect Policy]
- Strictly read-only on `opencode.db`; appends only to MindIE-owned `<spool_dir>`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import quote

SPOOL_PAGE_SIZE = 500
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
_FALLBACK_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_FALLBACK_SECRET_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bAKIA[0-9A-Z]{16}\b)"
)
_FALLBACK_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})")
_OPAQUE_BLOB_RE = re.compile(r"(?<![A-Za-z0-9+/=._-])[A-Za-z0-9+/=._-]{4096,}")

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


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


def clean_user_text(raw: str) -> str:
    match = USER_REQUEST_RE.search(raw)
    text = match.group(1) if match is not None else STRIP_TAGS_RE.sub("", raw)
    text = STRIP_TAGS_RE.sub("", text).strip()
    if GESTURE_PREFIX_RE.match(text):
        text = GESTURE_PREFIX_RE.sub("", text, count=1).strip()
        if text in {"", "activate", "deactivate", "status", "init", "recover"}:
            return ""
    return text


def extract_public_parts(blocks: object) -> str:
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


def extract_record(record: dict) -> tuple[str, str, float | None] | None:
    if record.get("isMeta") is True or record.get("synthetic") is True:
        return None

    if record.get("schema") == SPOOL_SCHEMA:
        mtype = record.get("type")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
        stamp = to_seconds(time_info.get("created") or record.get("time_created"))
        if mtype == "user":
            if data.get("synthetic") is True or data.get("ignored") is True:
                return None
            raw_text = (
                data.get("text")
                if isinstance(data.get("text"), str)
                else extract_public_parts(data.get("content") or record.get("parts"))
            )
            cleaned = clean_user_text(raw_text)
            return ("user", cleaned, stamp) if cleaned else None
        if mtype == "assistant":
            raw_text = (
                data.get("text")
                if isinstance(data.get("text"), str)
                else extract_public_parts(data.get("content") or record.get("parts"))
            )
            cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
            return ("assistant", cleaned, stamp) if cleaned else None
        return None

    stamp = to_seconds(record.get("time_created") or record.get("timestamp") or record.get("created_at"))
    role = record.get("role") or record.get("type")
    if role == "message" and isinstance(record.get("message"), dict):
        msg = record["message"]
        role = msg.get("role")
        raw_text = extract_public_parts(msg.get("content"))
    else:
        raw_text = (
            record["text"]
            if isinstance(record.get("text"), str)
            else extract_public_parts(record.get("content") or record.get("parts"))
        )
    if role == "user":
        cleaned = clean_user_text(raw_text)
        return ("user", cleaned, stamp) if cleaned else None
    if role == "assistant":
        cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
        return ("assistant", cleaned, stamp) if cleaned else None
    return None


def _readonly_sqlite_uri(db_path: Path) -> str:
    posix = db_path.resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def _thread_lock_for(lock_file: Path) -> threading.Lock:
    key = str(lock_file)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _session_spool_lock(lock_file: Path, *, deadline: float):
    tlock = _thread_lock_for(lock_file)
    wait = max(0.0, deadline - time.monotonic())
    if not tlock.acquire(timeout=wait):
        raise TimeoutError("timed out waiting for session spool thread lock")
    handle = None
    os_locked = False
    try:
        handle = lock_file.open("a+b")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if lock_file.stat().st_size == 0:
                        handle.write(b"0")
                        handle.flush()
                        handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                os_locked = True
                break
            except TimeoutError:
                raise
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for session spool OS lock")
                time.sleep(0.02)
        yield
    finally:
        if handle is not None:
            if os_locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except TimeoutError:
                    raise
                except OSError:
                    pass
            handle.close()
        tlock.release()


def _recover_spool_tail_and_seq(spool_file: Path) -> int:
    if not spool_file.is_file():
        return -1
    with spool_file.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size == 0:
            return -1
        stream.seek(size - 1)
        if stream.read(1) != b"\n":
            pos = size
            last_nl = -1
            while pos > 0:
                step = min(65536, pos)
                pos -= step
                stream.seek(pos)
                chunk = stream.read(step)
                idx = chunk.rfind(b"\n")
                if idx != -1:
                    last_nl = pos + idx
                    break
            new_size = (last_nl + 1) if last_nl >= 0 else 0
            stream.truncate(new_size)
            stream.flush()
            size = new_size
        if size == 0:
            return -1
        pos = size - 1
        line_start = 0
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            stream.seek(pos)
            chunk = stream.read(step)
            idx = chunk.rfind(b"\n")
            if idx != -1:
                line_start = pos + idx + 1
                break
        stream.seek(line_start)
        last_line = stream.read(size - line_start)
    try:
        record = json.loads(last_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"corrupt transcript spool record in {spool_file}: {exc}") from exc
    if not isinstance(record, dict) or type(record.get("seq")) is not int:
        raise ValueError(f"corrupt transcript spool record in {spool_file}: missing integer seq")
    return int(record["seq"])


def _read_cursor_seq(cursor_file: Path, *, spool_exists: bool, boundary: float | None) -> int:
    if not cursor_file.is_file():
        return -1
    try:
        raw = cursor_file.read_text(encoding="utf-8").strip()
    except TimeoutError:
        raise
    except OSError as exc:
        raise OSError(f"unreadable transcript cursor {cursor_file}: {exc}") from exc
    if not raw:
        return -1
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or type(parsed.get("seq")) is not int or int(parsed["seq"]) < -1:
                raise ValueError(f"corrupt transcript cursor: {cursor_file}")
            saved_boundary = parsed.get("not_before")
            if not spool_exists and isinstance(saved_boundary, (int, float)):
                if boundary is None or boundary + 1e-3 < float(saved_boundary):
                    return -1
            return int(parsed["seq"])
        seq_val = int(raw)
    except ValueError as exc:
        raise ValueError(f"corrupt transcript cursor: {cursor_file}") from exc
    if seq_val < -1:
        raise ValueError(f"corrupt transcript cursor: {cursor_file}")
    return seq_val if spool_exists else -1


def _redact_spool_text(text: str, *, private_paths: tuple[str, ...]) -> str:
    if not text:
        return text
    # Mask >=4 KiB unbroken base64/hex/token runs in O(N) before `scan_text` so
    # URL/hostname backtracking regexes never stall on pasted inline binary blobs.
    out = _OPAQUE_BLOB_RE.sub(
        lambda m: f"<redacted:opaque-blob:{hashlib.sha256(m.group(0).encode('utf-8')).hexdigest()[:12]}>",
        text,
    )
    try:
        from mindie_knowledge.redact import scan_text

        findings = [
            f
            for f in scan_text(out, include_overlaps=True)
            if isinstance(f.start, int) and isinstance(f.end, int) and 0 <= f.start < f.end <= len(out)
        ]
        if findings:
            findings.sort(key=lambda item: (item.start or 0, -(item.end or 0)))
            merged: list[tuple[int, int, str]] = []
            for f in findings:
                if merged and f.start < merged[-1][1]:
                    old_s, old_e, old_r = merged[-1]
                    merged[-1] = (old_s, max(old_e, f.end), old_r)
                else:
                    merged.append((f.start, f.end, f.rule))
            pieces: list[str] = []
            cursor = 0
            for start, end, rule in merged:
                pieces.append(out[cursor:start])
                token = hashlib.sha256(out[start:end].encode("utf-8")).hexdigest()[:12]
                pieces.append(f"<redacted:{rule}:{token}>")
                cursor = end
            pieces.append(out[cursor:])
            out = "".join(pieces)
    except ImportError:
        pass

    out = _FALLBACK_PEM_RE.sub("<redacted:private-key>", out)
    out = _FALLBACK_SECRET_RE.sub("<redacted:secret>", out)
    out = _FALLBACK_BEARER_RE.sub(r"\1<redacted:bearer>", out)

    normalized_paths: list[str] = []
    for raw_p in private_paths:
        if not isinstance(raw_p, str) or len(raw_p.strip()) < 4:
            continue
        cleaned = raw_p.strip().rstrip("/\\")
        if cleaned and cleaned not in {"/", "C:", "c:"}:
            normalized_paths.append(cleaned)
            posix_variant = cleaned.replace("\\", "/")
            if posix_variant not in normalized_paths:
                normalized_paths.append(posix_variant)
    normalized_paths.sort(key=len, reverse=True)
    for priv in normalized_paths:
        if priv in out:
            out = out.replace(priv, "<private_path>")
    return out


def _is_incomplete_assistant_row(mtype: str, parsed_data: dict, extracted: tuple | None) -> bool:
    """Detect an in-flight OpenCode v2 streaming assistant message so `max_seq` stops before it."""
    if mtype != "assistant":
        return False
    if not isinstance(parsed_data, dict):
        return extracted is None
    time_obj = parsed_data.get("time")
    if (
        (isinstance(time_obj, dict) and time_obj.get("completed") is not None)
        or parsed_data.get("finish") is not None
        or parsed_data.get("rawFinish") is not None
        or parsed_data.get("error") is not None
        or parsed_data.get("status") in {"completed", "failed", "interrupted", "cancelled", "stopped"}
    ):
        return False
    if (
        (isinstance(time_obj, dict) and "streamed" in time_obj)
        or any(k in parsed_data for k in ("agent", "model", "snapshot"))
    ):
        return True
    if extracted is None:
        has_tool_only = False
        for key in ("content", "parts"):
            items = parsed_data.get(key)
            if isinstance(items, list) and any(
                isinstance(x, dict) and x.get("type") in {"tool", "tool-call", "tool_use", "tool-result"}
                for x in items
            ):
                has_tool_only = True
                break
        if not has_tool_only:
            return True
    return False


def sync_session_spool(
    session_id: str,
    *,
    db_path: str | Path,
    spool_dir: str | Path,
    not_before: float | str | None = None,
    private_paths: tuple[str, ...] | list[str] = (),
    max_seconds: float = 2.0,
) -> Path | None:
    """Incrementally project OpenCode v2 `session_message` rows into an append-only per-session JSONL spool."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()) or ".." in session_id:
        raise ValueError("native OpenCode sessionID is missing or invalid")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    sid = session_id.strip()
    db_file = Path(db_path)
    if not db_file.is_file():
        return None

    boundary = to_seconds(not_before) if not_before is not None else None
    deadline = time.monotonic() + float(max_seconds)
    out_dir = Path(spool_dir).resolve()
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    spool_file = out_dir / f"{sid}.jsonl"
    cursor_file = out_dir / f"{sid}.seq"
    lock_file = out_dir / f".{sid}.lock"

    with _session_spool_lock(lock_file, deadline=deadline):
        tail_seq = _recover_spool_tail_and_seq(spool_file)
        cursor_seq = _read_cursor_seq(cursor_file, spool_exists=spool_file.is_file(), boundary=boundary)
        last_seq = max(tail_seq, cursor_seq)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("sync_session_spool exceeded max_seconds before opening SQLite")

        db = sqlite3.connect(_readonly_sqlite_uri(db_file), uri=True, timeout=min(2.0, remaining))
        try:
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            busy_ms = max(50, int(min(2.0, remaining) * 1000))
            db.execute(f"PRAGMA busy_timeout={busy_ms};")
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

            redact_paths = tuple(
                p
                for p in (
                    *(private_paths or ()),
                    directory if isinstance(directory, str) else "",
                    str(Path.home()),
                )
                if isinstance(p, str) and p
            )

            lines: list[bytes] = []
            max_seq = last_seq
            page_cursor = last_seq

            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("sync_session_spool exceeded max_seconds budget")
                mrows = db.execute(
                    "SELECT id, type, seq, time_created, data FROM session_message "
                    "WHERE session_id=? AND seq>? AND type IN ('user', 'assistant') "
                    "ORDER BY seq ASC LIMIT ?",
                    (sid, page_cursor, SPOOL_PAGE_SIZE),
                ).fetchall()
                if not mrows:
                    break

                parts_by_msg: dict[str, list[dict]] = {}
                msg_ids = [r[0] for r in mrows if isinstance(r[0], str)]
                if msg_ids:
                    placeholders = ",".join("?" for _ in msg_ids)
                    for p_mid, p_type, p_data in db.execute(
                        "SELECT message_id, type, data FROM session_part "
                        f"WHERE session_id=? AND message_id IN ({placeholders}) "
                        "AND type IN ('text', 'input_text', 'output_text') "
                        "ORDER BY ordinal ASC",
                        (sid, *msg_ids),
                    ):
                        if isinstance(p_data, str) and p_data.strip():
                            try:
                                parsed_part = json.loads(p_data)
                            except ValueError as exc:
                                raise ValueError(f"corrupt session_part JSON for message {p_mid}: {exc}") from exc
                            if not isinstance(parsed_part, dict):
                                raise ValueError(f"corrupt session_part JSON object for message {p_mid}")
                        else:
                            parsed_part = {}
                        if parsed_part:
                            parsed_part.setdefault("type", p_type)
                            parts_by_msg.setdefault(p_mid, []).append(parsed_part)

                stop_at_inflight = False
                for mid, mtype, seq, msg_created, raw_data in mrows:
                    seq_int = int(seq)
                    if isinstance(raw_data, str) and raw_data.strip():
                        try:
                            parsed_data = json.loads(raw_data)
                        except ValueError as exc:
                            raise ValueError(f"corrupt session_message JSON for message {mid}: {exc}") from exc
                        if not isinstance(parsed_data, dict):
                            raise ValueError(f"corrupt session_message JSON object for message {mid}")
                    else:
                        parsed_data = {}
                    data_dict = parsed_data
                    candidate = dict(
                        schema=SPOOL_SCHEMA,
                        session_id=sid,
                        session_created=session_created,
                        message_id=mid,
                        type=mtype,
                        seq=seq_int,
                        time_created=msg_created,
                        data=data_dict,
                    )
                    if mid in parts_by_msg:
                        candidate["parts"] = parts_by_msg[mid]
                    extracted = extract_record(candidate)
                    if _is_incomplete_assistant_row(str(mtype or ""), data_dict, extracted):
                        stop_at_inflight = True
                        break
                    if seq_int > max_seq:
                        max_seq = seq_int
                    page_cursor = seq_int
                    if extracted is None:
                        continue
                    role, text_body, stamp = extracted
                    if boundary is not None and (stamp is None or stamp + 1e-3 < boundary):
                        continue
                    redacted_body = _redact_spool_text(text_body, private_paths=redact_paths)
                    if not redacted_body.strip():
                        continue
                    sanitized_entry = dict(
                        schema=SPOOL_SCHEMA,
                        session_id=sid,
                        session_created=session_created,
                        message_id=mid,
                        type=role,
                        seq=seq_int,
                        time_created=msg_created,
                        data=dict(text=redacted_body, time=dict(created=msg_created)),
                    )
                    lines.append(
                        (
                            json.dumps(sanitized_entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                            + "\n"
                        ).encode("utf-8")
                    )

                if stop_at_inflight or len(mrows) < SPOOL_PAGE_SIZE:
                    break
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
                raise TimeoutError("sync_session_spool SQLite query exceeded max_seconds budget") from exc
            raise
        finally:
            db.close()

        if lines:
            with spool_file.open("ab") as stream:
                for raw_line in lines:
                    stream.write(raw_line)
                stream.flush()
        if max_seq > last_seq or (spool_file.is_file() and not cursor_file.is_file()):
            cursor_tmp = out_dir / f".{sid}.seq.tmp.{os.getpid()}"
            cursor_payload = json.dumps(
                {"seq": max_seq, "not_before": boundary},
                separators=(",", ":"),
                sort_keys=True,
            )
            cursor_tmp.write_text(cursor_payload + "\n", encoding="utf-8")
            os.replace(cursor_tmp, cursor_file)
        return spool_file if spool_file.is_file() else None
