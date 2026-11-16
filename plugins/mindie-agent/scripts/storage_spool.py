"""Bounded eviction of cold OpenCode transcript spool bundles (`storage_spool`).

[Role]
Retires `<spool>/<session>.jsonl` projections together with their `<session>.seq`
cursor and `.<session>.lock` sibling, plus the spool maintenance cooldown marker.

[Invariants]
- A bundle is evicted only as a unit: deleting the projection while keeping the
  cursor makes a resumed session skip its retired prefix.
- A session that is still admitted (`admission.sqlite3` lease `enabled=1`) or has a
  pending activation is never evicted. An unreadable admission store aborts the
  whole pass instead of guessing.
- Expiry (`>= 14 days`) is required for age-driven eviction; count pressure may
  only retire bundles past `SPOOL_BUNDLE_MIN_AGE_SECONDS` when the caller disables
  the age rule explicitly.
- A lock is held only after `fstat(fd) == lstat(path)` proves the descriptor names
  the live lock inode; a concurrently unlinked inode is re-opened and retried, and
  a held lock file is unlinked before it is unlocked (POSIX inode safety).

[Timeouts & Locks]
- One absolute `deadline` bounds the scan, the admission check and every lock
  attempt. Lock probes are non-blocking; a contended lock is a keep, and an
  exhausted budget reports `partial`.

[Side-Effect Policy]
- Unlinks only files under the given spool directory and writes the advisory
  `.spool-maintenance.json` marker. Never touches `opencode.db` or a harness config.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

from paths import admission_path, atomic_write_json
from storage_support import account, expired, note, unlink_file

SPOOL_BUDGET_SECONDS = 1.0
SPOOL_SESSION_LIMIT = 128
SPOOL_MAX_AGE_SECONDS = 14 * 24 * 60 * 60.0
SPOOL_BUNDLE_MIN_AGE_SECONDS = 1800.0
_LOCK_RETRIES = 3


def _spool_entries(root: Path, deadline: float | None, record: dict) -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []
    try:
        with os.scandir(root) as iterator:
            for item in iterator:
                if expired(deadline):
                    record["partial"] = True
                    note(record, "spool scan budget exhausted")
                    break
                if not item.name.endswith(".jsonl") or item.name.startswith("."):
                    continue
                try:
                    rows.append((item.stat().st_mtime, item.name[: -len(".jsonl")]))
                except TimeoutError:
                    raise
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
    except TimeoutError:
        raise
    except OSError as exc:
        note(record, exc)
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def _prepare_lock_handle(handle, lock_file: Path) -> None:
    if os.name != "nt":
        return
    import msvcrt

    handle.seek(0)
    if os.stat(lock_file).st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)


def _lock_stream(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lock_inode_matches(handle, lock_file: Path) -> bool:
    """True when the locked descriptor still names the live `lock_file` inode."""
    try:
        opened = os.fstat(handle.fileno())
        current = os.lstat(lock_file)
    except TimeoutError:
        raise
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


@contextmanager
def _held_spool_lock(lock_file: Path, deadline: float | None = None):
    """Yield `True` only while holding the live `<spool>/.<session>.lock` inode."""
    handle = None
    locked = False
    for attempt in range(_LOCK_RETRIES):
        try:
            handle = lock_file.open("a+b")
        except TimeoutError:
            raise
        except OSError:
            yield False
            return
        try:
            _prepare_lock_handle(handle, lock_file)
            _lock_stream(handle)
            locked = True
        except TimeoutError:
            raise
        except OSError:
            locked = False
        if not locked:
            handle.close()
            handle = None
            yield False
            return
        if lock_inode_matches(handle, lock_file):
            break
        # A concurrent eviction unlinked the inode we locked: retry on the live path.
        try:
            _unlock_stream(handle)
        except TimeoutError:
            raise
        except OSError:
            pass
        handle.close()
        handle, locked = None, False
        if expired(deadline) or attempt + 1 >= _LOCK_RETRIES:
            yield False
            return
    try:
        yield locked
    finally:
        if handle is not None:
            if locked:
                try:
                    _unlock_stream(handle)
                except TimeoutError:
                    raise
                except OSError:
                    pass
            handle.close()


def _unlink_locked(lock_file: Path, record: dict) -> bool:
    """Unlink a held lock file before unlocking it (POSIX lock-inode safety)."""
    try:
        os.unlink(lock_file)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return False
    except OSError as exc:
        note(record, exc)
        return False
    record["lock_files_removed"] += 1
    return True


def _sqlite_ro_uri(database: Path) -> str:
    posix = Path(database).resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    return f"file:{quote(posix, safe='/:')}?mode=ro"


def _default_admission_db(state: Path) -> Path:
    try:
        configured = admission_path()
    except TimeoutError:
        raise
    except (OSError, ValueError):
        configured = None
    if configured is not None and Path(configured).parent == state:
        return Path(configured)
    return state / "admission.sqlite3"


def _admission_sessions(root: Path, record: dict, deadline: float | None, *,
                        admission_db=None) -> set[str] | None:
    """Sessions still admitted or pending capture; `None` means unknown (skip)."""
    import sqlite3

    state = root.parent
    database = Path(admission_db) if admission_db is not None else _default_admission_db(state)
    live: set[str] = set()
    if database.is_file():
        connection = None
        try:
            connection = sqlite3.connect(_sqlite_ro_uri(database), uri=True, timeout=0.5)
            rows = connection.execute("SELECT session FROM leases WHERE enabled=1").fetchall()
        except TimeoutError:
            raise
        except (sqlite3.Error, OSError) as exc:
            note(record, exc)
            return None
        finally:
            if connection is not None:
                connection.close()
        live.update(row[0] for row in rows if row and isinstance(row[0], str))
    try:
        with os.scandir(state / "pending-activations") as iterator:
            for item in iterator:
                if expired(deadline):
                    record["partial"] = True
                    return None
                if item.name.endswith(".json"):
                    live.add(item.name[: -len(".json")])
    except TimeoutError:
        raise
    except FileNotFoundError:
        pass
    except OSError as exc:
        note(record, exc)
        return None
    return live


def prune_spool_transcripts(
    spool_dir: str | Path,
    *,
    exclude=(),
    max_sessions: int = SPOOL_SESSION_LIMIT,
    max_age_seconds: float | None = SPOOL_MAX_AGE_SECONDS,
    budget_seconds: float = SPOOL_BUDGET_SECONDS,
    now: float | None = None,
    deadline: float | None = None,
    admission_db: str | Path | None = None,
) -> dict:
    """Evict complete expired `transcripts/<session>` bundles, never a live one."""
    root = Path(spool_dir)
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    if not root.is_dir():
        return account("absent", removed_sessions=[])
    stamp = float(now) if now is not None else time.time()
    protected = {sid for sid in exclude if isinstance(sid, str) and sid}
    record = account("completed", removed_sessions=[], lock_files_removed=0)
    rows = _spool_entries(root, limit, record)
    live = _admission_sessions(root, record, limit, admission_db=admission_db)
    if live is None:
        record["partial"] = True
        record["status"] = "partial"
        return record
    needed = max(0, len(rows) - max(0, int(max_sessions)))
    removed_any = False
    for mtime, sid in rows:
        if sid in protected or sid in live:
            record["kept_dirs"] += 1
            continue
        age = stamp - mtime
        aged = max_age_seconds is not None and age > float(max_age_seconds)
        capped = not aged and max_age_seconds is None and needed > 0 \
            and age >= SPOOL_BUNDLE_MIN_AGE_SECONDS
        if not aged and not capped:
            record["kept_dirs"] += 1
            continue
        if expired(limit):
            record["partial"] = True
            note(record, "spool prune budget exhausted")
            break
        lock_file = root / f".{sid}.lock"
        removed = False
        with _held_spool_lock(lock_file, limit) as held:
            if not held:
                record["deferred_dirs"] += 1
                continue
            removed = unlink_file(root / f"{sid}.jsonl", record)
            if not removed:
                continue
            # Retire the cursor with the projection so a resumed session rebuilds
            # from sequence 0 instead of skipping the retired prefix.
            unlink_file(root / f"{sid}.seq", record)
            removed_any = _unlink_locked(lock_file, record) or removed_any
        if removed:
            if needed > 0:
                needed -= 1
            record["removed_sessions"].append(sid)
    if os.name == "nt" and removed_any:
        # Windows refuses to delete an open lock: retried after the handle closed.
        for sid in record["removed_sessions"]:
            lock_file = root / f".{sid}.lock"
            try:
                if lock_file.is_file():
                    os.unlink(lock_file)
                    record["lock_files_removed"] += 1
            except TimeoutError:
                raise
            except OSError:
                pass
    if record["partial"]:
        record["status"] = "partial"
    return record


def spool_maintenance_marker(spool_dir: str | Path) -> Path:
    return Path(spool_dir) / ".spool-maintenance.json"


def spool_maintenance_due(spool_dir: str | Path, *, interval_seconds: float, now: float | None = None) -> bool:
    """True when the cooldown marker is absent, unreadable, or older than `interval_seconds`."""
    marker = spool_maintenance_marker(spool_dir)
    try:
        raw = marker.read_text(encoding="utf-8")
    except TimeoutError:
        raise
    except OSError:
        return True
    try:
        payload = json.loads(raw)
    except ValueError:
        return True
    stamp = payload.get("at") if isinstance(payload, dict) else None
    if not isinstance(stamp, (int, float)):
        return True
    return (float(now) if now is not None else time.time()) - float(stamp) >= float(interval_seconds)


def write_spool_maintenance_marker(spool_dir: str | Path, payload: dict) -> None:
    try:
        atomic_write_json(spool_maintenance_marker(spool_dir), payload)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        pass


__all__ = [
    "SPOOL_BUDGET_SECONDS",
    "SPOOL_BUNDLE_MIN_AGE_SECONDS",
    "SPOOL_MAX_AGE_SECONDS",
    "SPOOL_SESSION_LIMIT",
    "lock_inode_matches",
    "prune_spool_transcripts",
    "spool_maintenance_due",
    "spool_maintenance_marker",
    "write_spool_maintenance_marker",
]
