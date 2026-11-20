#!/usr/bin/env python3
"""Read-only OpenCode v2 SQLite spool projector and incremental transcript reader.
[Role] Projects public user/assistant text into per-session JSONL spools and reads exact `[start, end)` SHA-256 ranges with `scan_until` clamping and `FileIdentity.unserialize`.
[Invariants] Strictly `?mode=ro`; rejects parent/forked sessions; projects only public text (no synthetic/ignored/isMeta/reasoning/tool); redacts secrets and private paths; recovers torn tails; empty-anchor accepted iff `size == 0`; caps cold spools at 64 under `_spool_gc.lock`.
[Timeouts & Locks] Per-session `<spool_dir>/.<session>.lock` and interprocess `<spool_dir>/_spool_gc.lock` under a 2.0s deadline plus a SQLite progress handler.
[Side-Effect Policy] Read-only on `opencode.db`; appends, refreshes `utime`, and prunes only inside MindIE `<spool_dir>`.
"""
from __future__ import annotations
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib, json, math, os, re, sqlite3, threading, time
from pathlib import Path
from typing import Any
from urllib.parse import quote
SPOOL_SCHEMA, PRUNE_LOCK_NAME = "mindie-oc-spool/1", "_spool_gc.lock"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
ANCHOR_BYTES, MAX_TEXT, MAX_RECORDS, COLD_SPOOL_LIMIT = 512, 48 * 1024, 200, 64
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_TEXT_TYPES = {"text", "input_text", "output_text"}
_USER_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_STRIP_RE = re.compile(r"<(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)\b[^>]*>.*?</(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>", re.DOTALL)
_GESTURE_RE = re.compile(r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?:\s+|$)")
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
_SECRET_RE = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bAKIA[0-9A-Z]{16}\b)")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
def to_seconds(raw) -> float | None:
    if type(raw) in (int, float) and raw > 0:
        value = float(raw) / (1000.0 if raw >= 1e12 else 1.0); return value if math.isfinite(value) else None
    if not isinstance(raw, str) or not raw.strip(): return None
    try: stamp = datetime.fromisoformat(raw.strip()[:-1] + "+00:00" if raw.strip().endswith("Z") else raw.strip())
    except ValueError: return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
@dataclass(frozen=True)
class FileIdentity:
    path: str; dev: int; ino: int; size: int; mtime_ns: int; anchor_len: int = 0; anchor_digest: str = ""
    def serialize(self) -> str:
        return json.dumps(dict(path=self.path, dev=self.dev, ino=self.ino, size=self.size, mtime_ns=self.mtime_ns, anchor_len=self.anchor_len, anchor_digest=self.anchor_digest), sort_keys=True, separators=(",", ":"))
    @classmethod
    def unserialize(cls, payload: Mapping[str, Any] | str, path: str | Path | None = None) -> FileIdentity | None:
        try:
            data = json.loads(payload) if isinstance(payload, str) else (dict(payload) if isinstance(payload, Mapping) else None)
            if not isinstance(data, dict): return None
            dev, ino, anchor_len, anchor_digest = data["dev"], data["ino"], data["anchor_len"], data["anchor_digest"]
            has_size, raw_size = "size" in data, data.get("size", 0)
            if any(type(v) is not int for v in (dev, ino, anchor_len)) or not isinstance(anchor_digest, str) or (has_size and (type(raw_size) is not int or raw_size < 0)):
                return None
            if anchor_len == 0:
                disk_empty = not has_size and path is not None and Path(path).is_file() and Path(path).stat().st_size == 0
                if not (((has_size and raw_size == 0) or disk_empty) and anchor_digest in {"", _EMPTY_DIGEST}): return None
            elif not 0 < anchor_len <= ANCHOR_BYTES or len(anchor_digest) != 64 or (has_size and raw_size < anchor_len):
                return None
            raw_path = path if path is not None else data.get("path")
            resolved = str(Path(raw_path).resolve()) if isinstance(raw_path, (str, Path)) and str(raw_path) else ""
            mtime_ns = data.get("mtime_ns", 0) if type(data.get("mtime_ns", 0)) is int else 0
            return cls(resolved, dev, ino, raw_size if has_size else 0, mtime_ns, anchor_len, anchor_digest)
        except (ValueError, KeyError, TypeError): return None
def identify(path: str | Path) -> FileIdentity | None:
    try: fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except TimeoutError: raise
    except (OSError, ValueError): return None
    try: stat, anchor = os.fstat(fd), os.read(fd, ANCHOR_BYTES)
    except TimeoutError: raise
    except OSError: return None
    finally: os.close(fd)
    return FileIdentity(str(Path(path).resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
def _same_expected(expected: FileIdentity, current: FileIdentity, stream) -> bool:
    if (bool(expected.path) and expected.path != current.path) or expected.dev != current.dev or expected.ino != current.ino: return False
    if expected.anchor_len == 0: return expected.size == 0 and current.size == 0 and expected.anchor_digest in {"", _EMPTY_DIGEST}
    stream.seek(0); sample = stream.read(expected.anchor_len)
    return len(sample) == expected.anchor_len and hashlib.sha256(sample).hexdigest() == expected.anchor_digest
def _session_in_path(path: str | Path) -> str | None:
    stem = Path(path).stem
    if SESSION_ID_RE.fullmatch(stem) and stem.lower() not in {"transcript", "transcript_full", "opencode"}: return stem
    return next((part for part in reversed(Path(path).parts[:-1]) if SESSION_ID_RE.fullmatch(part) and part.lower() not in {"transcripts", "sessions", "logs"}), None)
def _readonly_uri(db_path: Path) -> str:
    posix = db_path.resolve().as_posix()
    return f"file:{quote(posix if posix.startswith('/') else '/' + posix, safe='/:')}?mode=ro"
def _os_try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if os.fstat(handle.fileno()).st_size == 0: handle.write(b"0"); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except TimeoutError: raise
    except OSError: return False
def _os_unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except TimeoutError: raise
    except OSError: pass
@contextmanager
def _try_file_lock(lock_file: Path):
    with _LOCKS_GUARD: tlock = _LOCKS.setdefault(str(lock_file), threading.Lock())
    if not tlock.acquire(blocking=False): yield False; return
    handle, locked = None, False
    try: handle = lock_file.open("a+b"); locked = _os_try_lock(handle); yield locked
    except TimeoutError: raise
    except OSError: yield False
    finally:
        if handle is not None:
            if locked: _os_unlock(handle)
            handle.close()
        tlock.release()
@contextmanager
def _session_lock(lock_file: Path, *, deadline: float):
    with _LOCKS_GUARD: tlock = _LOCKS.setdefault(str(lock_file), threading.Lock())
    if not tlock.acquire(timeout=max(0.0, deadline - time.monotonic())): raise TimeoutError("timed out waiting for session spool thread lock")
    handle, locked = None, False
    try:
        handle = lock_file.open("a+b")
        while not _os_try_lock(handle):
            if time.monotonic() >= deadline: raise TimeoutError(f"timed out waiting for file lock {lock_file.name}")
            time.sleep(0.01)
        locked = True; yield
    finally:
        if handle is not None:
            if locked: _os_unlock(handle)
            handle.close()
        tlock.release()
def _recover_tail(spool_file: Path) -> int:
    data = spool_file.read_bytes() if spool_file.is_file() else b""
    if not data: return -1
    if not data.endswith(b"\n"): data = data[: data.rfind(b"\n") + 1]; spool_file.write_bytes(data)
    if not data: return -1
    record = json.loads(data[data.rfind(b"\n", 0, len(data) - 1) + 1 :].decode("utf-8"))
    if not isinstance(record, dict) or type(record.get("seq")) is not int: raise ValueError(f"corrupt transcript spool record in {spool_file}")
    return int(record["seq"])
def _redact(text: str, private_paths: tuple[str, ...]) -> str:
    if not text: return text
    out = text
    try:
        from mindie_knowledge.redact import scan_text
        pieces, cursor = [], 0
        for f in sorted((f for f in scan_text(out, include_overlaps=True) if type(f.start) is int and type(f.end) is int and 0 <= f.start < f.end <= len(out)), key=lambda f: (f.start, -f.end)):
            if f.start >= cursor: pieces += [out[cursor : f.start], f"<redacted:{f.rule}:{hashlib.sha256(out[f.start : f.end].encode()).hexdigest()[:12]}>"]; cursor = f.end
        out = "".join([*pieces, out[cursor:]])
    except TimeoutError: raise
    except Exception: pass
    out = _BEARER_RE.sub(r"\1<redacted:bearer>", _SECRET_RE.sub("<redacted:secret>", _PEM_RE.sub("<redacted:private-key>", out)))
    for root in sorted({str(raw).strip().rstrip("/\\") for raw in private_paths if isinstance(raw, str) and len(str(raw).strip()) >= 4}, key=len, reverse=True):
        for variant in (root, root.replace("\\", "/")):
            if variant not in {"/", "C:", "c:"}: out = out.replace(variant, "<private_path>")
    return out
def _public_text(parts: list[dict], data: dict) -> str:
    visible = lambda item: isinstance(item, dict) and item.get("synthetic") is not True and item.get("ignored") is not True and item.get("isMeta") is not True
    chunks = [part["text"] for part in parts if visible(part) and part.get("type") in _TEXT_TYPES and isinstance(part.get("text"), str) and not part["text"].startswith("<persisted-output>")]
    if not chunks and not parts:
        for value in (data.get("text"), data.get("content"), data.get("parts")):
            if isinstance(value, str): chunks.append(value)
            elif isinstance(value, list): chunks.extend(item["text"] for item in value if visible(item) and item.get("type") in _TEXT_TYPES and isinstance(item.get("text"), str))
    return "\n".join(chunks)
def _clean(mtype: str, text: str) -> str:
    text = _STRIP_RE.sub("", text)
    if mtype == "user":
        match = _USER_RE.search(text)
        text = _GESTURE_RE.sub("", (match.group(1) if match is not None else text).strip(), count=1).strip()
        if text in {"", "activate", "deactivate", "status", "init", "recover"}: return ""
    return text.strip()
def _prune(spool_dir: Path, current: Path, *, deadline: float) -> None:
    try:
        prune_deadline = min(deadline, time.monotonic() + 0.5)
        with _session_lock(spool_dir / PRUNE_LOCK_NAME, deadline=max(time.monotonic() + 0.05, prune_deadline)):
            files = [path for path in spool_dir.glob("*.jsonl") if path.is_file()]
            remaining = max(0, len(files) - COLD_SPOOL_LIMIT)
            if remaining <= 0: return
            stamped: list[tuple[float, Path]] = []
            for path in files:
                try: stamped.append((path.stat().st_mtime, path))
                except OSError: continue
            for _, path in sorted(stamped, key=lambda item: item[0]):
                if remaining <= 0: break
                if path == current: continue
                with _try_file_lock(spool_dir / f".{path.stem}.lock") as acquired:
                    if not acquired: continue
                    try: path.unlink(missing_ok=True); remaining -= 1
                    except TimeoutError: raise
                    except OSError: pass
    except TimeoutError:
        if time.monotonic() >= deadline: raise
    except OSError: pass
def sync_session_spool(session_id: str, *, db_path: str | Path, spool_dir: str | Path, not_before: float | str | None = None, private_paths: tuple[str, ...] | list[str] = (), max_seconds: float = 2.0, config: str | Path | None = None, expected_directory: str | None = None) -> Path | None:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()) or ".." in session_id: raise ValueError("native OpenCode sessionID is missing or invalid")
    if max_seconds <= 0: raise ValueError("max_seconds must be positive")
    sid, db_file = session_id.strip(), Path(db_path)
    if not db_file.is_file(): return None
    boundary = to_seconds(not_before) if not_before is not None else None
    deadline = time.monotonic() + float(max_seconds)
    out_dir = Path(spool_dir).resolve(); out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    spool_file = out_dir / f"{sid}.jsonl"
    with _session_lock(out_dir / f".{sid}.lock", deadline=deadline):
        last_seq = _recover_tail(spool_file); remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError("sync_session_spool exceeded max_seconds before opening SQLite")
        db = sqlite3.connect(_readonly_uri(db_file), uri=True, timeout=min(2.0, remaining))
        try:
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            db.execute(f"PRAGMA busy_timeout={max(50, int(min(2.0, remaining) * 1000))};")
            row = db.execute("SELECT directory, parent_id, fork_session_id, time_created FROM session_v2 WHERE id=?", (sid,)).fetchone()
            if row is None: return None
            directory, parent_id, fork_id, created = row
            if (isinstance(parent_id, str) and parent_id.strip()) or (isinstance(fork_id, str) and fork_id.strip()): raise ValueError("subagent or forked OpenCode session cannot be captured")
            if expected_directory and isinstance(directory, str) and os.path.isabs(directory) and os.path.normcase(os.path.realpath(directory)) != os.path.normcase(os.path.realpath(expected_directory)):
                raise ValueError("session directory mismatch")
            redact_paths = tuple(p for p in (*(private_paths or ()), directory if isinstance(directory, str) else "", str(Path.home())) if isinstance(p, str) and p)
            lines: list[bytes] = []; cursor = last_seq
            while True:
                messages = db.execute("SELECT id, type, seq, time_created, data FROM session_message WHERE session_id=? AND seq>? AND type IN ('user','assistant') ORDER BY seq ASC LIMIT 500", (sid, cursor)).fetchall()
                if not messages: break
                ids = [m[0] for m in messages if isinstance(m[0], str)]
                parts: dict[str, list[dict]] = {}; has_parts: set[str] = set()
                if ids:
                    marks = ",".join("?" for _ in ids)
                    for message_id, ptype, pdata in db.execute("SELECT message_id, type, data FROM session_part WHERE session_id=? AND message_id IN (" + marks + ") ORDER BY ordinal ASC", (sid, *ids)):
                        has_parts.add(message_id)
                        if ptype not in _TEXT_TYPES: continue
                        try: parsed = json.loads(pdata) if isinstance(pdata, str) and pdata.strip() else {}
                        except ValueError as exc: raise ValueError(f"corrupt session_part JSON for message {message_id}: {exc}") from exc
                        if isinstance(parsed, dict): parsed.setdefault("type", ptype); parts.setdefault(message_id, []).append(parsed)
                for message_id, mtype, seq, msg_created, raw in messages:
                    try: data = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
                    except ValueError as exc: raise ValueError(f"corrupt session_message JSON for message {message_id}: {exc}") from exc
                    if not isinstance(data, dict): data = {}
                    seq_value = int(seq)
                    stamp = to_seconds((data.get("time") or {}).get("created") if isinstance(data.get("time"), dict) else None) or to_seconds(msg_created)
                    msg_parts = parts.get(message_id, [{"type": "non_text"}] if message_id in has_parts else [])
                    transcript = "" if data.get("synthetic") is True or data.get("ignored") is True or data.get("isMeta") is True else _clean(str(mtype), _public_text(msg_parts, data))
                    if transcript and not (boundary is not None and (stamp is None or stamp + 1e-3 < boundary)):
                        record = dict(schema=SPOOL_SCHEMA, session_id=sid, session_created=created, message_id=message_id, type=mtype, seq=seq_value, time_created=msg_created, data=dict(text=_redact(transcript, redact_paths), time=dict(created=msg_created)))
                        lines.append((json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
                    cursor = max(cursor, seq_value)
                if len(messages) < 500: break
                if time.monotonic() >= deadline: raise TimeoutError("sync_session_spool exceeded max_seconds budget")
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or time.monotonic() >= deadline: raise TimeoutError("sync_session_spool SQLite query exceeded max_seconds budget") from exc
            raise
        finally: db.close()
        if lines:
            with spool_file.open("ab") as stream: stream.write(b"".join(lines)); stream.flush()
        exists = spool_file.is_file()
        if exists:
            try: os.utime(spool_file, None)
            except TimeoutError: raise
            except OSError: pass
        _prune(out_dir, spool_file, deadline=deadline)
        return spool_file if exists and spool_file.is_file() else None
def capture_source(path: str | Path, session_id: str, *, binding: dict | None = None) -> dict:
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()) or ".." in session_id: raise ValueError("native OpenCode session_id is missing or invalid")
    sid = session_id.strip()
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute(): raise ValueError("capture transcript path must be absolute")
    try: selected = Path(path).resolve(strict=True)
    except TimeoutError: raise
    except (OSError, RuntimeError) as exc: raise ValueError("capture transcript is unavailable") from exc
    if not selected.is_file() or _session_in_path(selected) != sid: raise ValueError("capture transcript does not belong to this session_id")
    stamps = []
    for raw in selected.read_text(encoding="utf-8", errors="replace").splitlines()[:64]:
        try: record = json.loads(raw)
        except ValueError: continue
        if isinstance(record, dict): stamps.append(to_seconds(record.get("session_created") or record.get("time_created") or record.get("timestamp")))
    created = next((stamp for stamp in stamps if stamp is not None), None)
    if created is None: raise ValueError("native OpenCode task creation timestamp is unavailable")
    if not isinstance(binding, dict): raise ValueError("native task binding is required to establish project scope")
    scope, activated = binding.get("project_root"), binding.get("activated_at")
    if str(binding.get("session", "")) != sid or type(binding.get("enabled")) not in (bool, int) or int(binding["enabled"]) != 1 or not isinstance(scope, str) or not os.path.isabs(scope) or type(activated) not in (int, float) or not math.isfinite(activated) or activated < 0:
        raise ValueError("native task binding is invalid")
    return dict(session_id=sid, project_root=str(Path(scope).resolve()), created_at=created, transcript_path=str(selected))
def read_material(path: str | Path, start: int = 0, *, start_offset: int | None = None, scan_until: int | None = None, session_id: str | None = None, not_before: float | str | None = None, expected: FileIdentity | None = None, max_scan_bytes: int = 16777216, max_seconds: float = 2.0, max_text_bytes: int = MAX_TEXT) -> dict:
    if start_offset is not None: start = start_offset
    if type(start) is not int or start < 0 or max_scan_bytes <= 0 or max_seconds <= 0 or max_text_bytes <= 0: raise ValueError("invalid scan budget or start offset")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start): raise ValueError("scan_until must be an exact byte boundary at or after start")
    boundary = to_seconds(not_before) if not_before is not None else None
    sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    result = dict(status="ok", start=start, end=start, digest=_EMPTY_DIGEST, text="", records=0, skipped_records=0, partial=False, more=False, session_match=None)
    consumed, included, text_size, began = hashlib.sha256(), [], 0, time.monotonic()
    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno()); anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(str(Path(path).resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
            result["identity"], result["snapshot_size"] = current.serialize(), stat.st_size
            if expected is not None and not _same_expected(expected, current, stream):
                result.update(status="replaced"); return result
            if stat.st_size < start: result.update(status="replaced", coverage_note="transcript truncated"); return result
            owner = _session_in_path(path)
            if owner and sid and owner != sid: result.update(status="wrong-task", coverage_note="transcript belongs to another session"); return result
            if not owner and sid: result.update(status="unknown-format"); return result
            if start > 0:
                stream.seek(start - 1)
                if stream.read(1) != b"\n": result.update(status="invalid-boundary", coverage_note="cursor is not a whole-record boundary"); return result
            effective_end = min(stat.st_size, scan_until) if scan_until is not None else stat.st_size
            stream.seek(start); limit = min(effective_end, start + max_scan_bytes)
            data = stream.read(max(0, limit - start)); offset = start
            truncated, stopped_early = bool(data) and not data.endswith(b"\n"), False
            for raw in data.split(b"\n")[:-1]:
                line = raw + b"\n"
                if offset + len(line) > limit or time.monotonic() - began >= max_seconds or len(included) >= MAX_RECORDS:
                    stopped_early = True; break
                try: record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError): record = None
                if not isinstance(record, dict):
                    result["skipped_records"] += 1; consumed.update(line); offset += len(line); continue
                if sid and (not isinstance(record.get("session_id"), str) or record["session_id"] != sid):
                    result.update(status="wrong-task", coverage_note="spool record belongs to another session"); return result
                item = record.get("data") if isinstance(record.get("data"), dict) else {}
                role = record.get("type") if record.get("type") in {"user", "assistant"} else None
                text = item.get("text") if isinstance(item.get("text"), str) else _public_text([], item)
                stamp = to_seconds((item.get("time") or {}).get("created") if isinstance(item.get("time"), dict) else None) or to_seconds(record.get("time_created") or record.get("timestamp"))
                if role and text.strip() and not (boundary is not None and (stamp is None or stamp + 1e-3 < boundary)):
                    formatted = f"[{role}]\n" + text.replace("\r\n", "\n").replace("\r", "\n")
                    encoded = formatted.encode("utf-8")
                    if not included and len(encoded) > max_text_bytes:
                        formatted = encoded[:max_text_bytes].decode("utf-8", errors="ignore"); encoded = formatted.encode("utf-8")
                    size = len(encoded) + (2 if included else 0)
                    if included and text_size + size > max_text_bytes:
                        stopped_early = True; break
                    included.append(formatted); text_size += size; result["records"] += 1
                else: result["skipped_records"] += 1
                consumed.update(line); offset += len(line)
            result.update(end=offset, digest=consumed.hexdigest(), text="\n\n".join(included), more=stopped_early or (limit < effective_end))
            if truncated and limit >= effective_end: result["partial"] = True
            if result["status"] == "ok" and offset == start: result["status"] = "unchanged"
            return result
    except TimeoutError: raise
    except FileNotFoundError: result.update(status="unavailable", coverage_note="transcript file missing"); return result
    except OSError as exc: result.update(status="unavailable", coverage_note=f"transcript read failed: {type(exc).__name__}"); return result
read_increment = read_material
