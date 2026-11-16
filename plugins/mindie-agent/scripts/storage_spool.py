"""Bounded eviction of cold OpenCode transcript spool bundles (`storage_spool`).

[Role]
Retires `<spool>/<session>.jsonl` projections together with their `<session>.seq`
cursor and `.<session>.lock` sibling, plus the spool maintenance cooldown marker.

[Invariants]
- A bundle is evicted only as a unit: deleting the projection while keeping the
  cursor makes a resumed session skip its retired prefix.
- Every configured `mindie_knowledge` SQLite store is consulted under the held
  `<session>.lock`: an unreadable store, a partial cursor or a pending capture
  keeps the projection; fully consumed cursor rows are deleted before the unlink.
- A session that is still admitted (`enabled=1`) or has a pending activation is
  never evicted. An unreadable admission store or adapter config aborts the whole
  pass instead of guessing, and a caller-configured external `admission_path` is
  honored regardless of its directory.
- Expiry (`>= 14 days`) is required for age-driven eviction; count pressure may
  only retire bundles past `SPOOL_BUNDLE_MIN_AGE_SECONDS` when the caller disables
  the age rule explicitly.
- A lock is held only after `fstat(fd) == lstat(path)` proves the descriptor names
  the live lock inode; a concurrently unlinked inode is re-opened and retried, and
  a held lock file is unlinked before it is unlocked (POSIX inode safety).

[Timeouts & Locks]
- One absolute `deadline` bounds the scan, admission/knowledge SQLite reads, and
  every lock attempt. SQLite uses `busy_timeout=5000` plus a deadline progress
  handler; lock probes are non-blocking and an exhausted budget reports `partial`.

[Side-Effect Policy]
- Unlinks only files under the given spool directory, deletes fully consumed
  `cursors` rows from configured knowledge stores, and writes the advisory
  `.spool-maintenance.json` marker. Never touches `opencode.db` or a harness config.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from paths import atomic_write_json, config_path, read_json
from storage_support import account, expired, is_absolute, note, path_key, path_text, unlink_file

SPOOL_BUDGET_SECONDS = 1.0
SPOOL_SESSION_LIMIT = 128
SPOOL_MAX_AGE_SECONDS = 14 * 24 * 60 * 60.0
SPOOL_BUNDLE_MIN_AGE_SECONDS = 1800.0
_LOCK_RETRIES = 3
_SQLITE_BUSY_TIMEOUT_MS = 5000
_KNOWLEDGE_DB_NAME = "state-v4.sqlite3"
_KNOWLEDGE_ENGINE_KEYS = (
    "engine_config", "previous_generation", "previous_engine_config", "rollback_engine_config",
)
_PENDING_CAPTURE_STATUSES = frozenset({"queued", "pending", "deferred"})
_DOMAIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_KNOWLEDGE_DATABASES = 64


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


def _open_sqlite(database: Path, deadline: float | None, *, readonly: bool):
    """Open one SQLite store with a 5s busy timeout and the caller's deadline."""
    import sqlite3

    if readonly:
        connection = sqlite3.connect(_sqlite_ro_uri(database), uri=True, timeout=5.0)
    else:
        connection = sqlite3.connect(os.fspath(database), timeout=5.0)
    try:
        connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS};")
        connection.set_progress_handler(lambda: 1 if expired(deadline) else 0, 1000)
    except BaseException:
        connection.close()
        raise
    return connection


def _admission_database(state: Path, config, admission_db) -> tuple[Path | None, bool]:
    """Resolve the admission store; `(path, False)` means the config is unreadable."""
    if admission_db is not None:
        try:
            return Path(admission_db).expanduser(), True
        except (TypeError, ValueError):
            return None, False
    try:
        cfg = config_path(config)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None, False
    if not cfg.is_file():
        return state / "admission.sqlite3", True
    try:
        payload = read_json(cfg)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None, False
    if "admission_path" not in payload:
        return state / "admission.sqlite3", True
    configured = payload.get("admission_path")
    if not isinstance(configured, str) or not is_absolute(configured):
        return None, False
    return Path(path_text(configured)), True


def _admission_sessions(root: Path, record: dict, deadline: float | None, *,
                        admission_db=None, config=None) -> set[str] | None:
    """Sessions still admitted or pending capture; `None` means unknown (skip)."""
    import sqlite3

    state = root.parent
    database, known = _admission_database(state, config, admission_db)
    if not known or database is None:
        record["partial"] = True
        note(record, "admission configuration is unreadable; spool eviction skipped")
        return None
    live: set[str] = set()
    try:
        exists = database.exists()
        regular = database.is_file()
    except TimeoutError:
        raise
    except OSError as exc:
        note(record, exc)
        return None
    if exists and not regular:
        record["partial"] = True
        note(record, "configured admission store is not a regular file")
        return None
    if regular:
        connection = None
        try:
            connection = _open_sqlite(database, deadline, readonly=True)
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


def _engine_config_candidates(cfg: Path) -> tuple[list[Path], set[str], bool]:
    """Engine configs that may describe a configured knowledge store."""
    try:
        adapter = read_json(cfg)
    except FileNotFoundError:
        adapter = {}
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return [], set(), False
    candidates: list[Path] = [cfg] if adapter else []
    required: set[str] = {path_key(cfg)} if adapter else set()
    complete = True
    for key in _KNOWLEDGE_ENGINE_KEYS:
        if key not in adapter:
            continue
        value = adapter.get(key)
        if value is None:
            continue
        if isinstance(value, str) and is_absolute(value):
            candidate = Path(path_text(value))
            candidates.append(candidate)
            required.add(path_key(candidate))
            if key == "engine_config" and not candidate.is_file():
                complete = False
        else:
            complete = False
    mappings = adapter.get("domain_configs")
    if mappings is not None and not isinstance(mappings, dict):
        complete = False
    elif isinstance(mappings, dict):
        for value in mappings.values():
            if isinstance(value, str) and is_absolute(value):
                candidate = Path(path_text(value))
                candidates.append(candidate)
                required.add(path_key(candidate))
            else:
                complete = False
    for pattern_root, pattern in (
        (cfg.parent, f"{cfg.stem}.engine.json"),
        (cfg.parent, f"{cfg.stem}.engine.*.json"),
        (cfg.parent, "oc.engine.json"),
        (cfg.parent, "oc.engine.*.json"),
        (cfg.parent / "generations", f"{cfg.stem}.engine.*.json"),
        (cfg.parent / "generations", "oc.engine.*.json"),
    ):
        try:
            candidates.extend(sorted(pattern_root.glob(pattern)))
        except TimeoutError:
            raise
        except OSError:
            complete = False
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = path_key(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    if len(unique) > _MAX_KNOWLEDGE_DATABASES:
        return unique[:_MAX_KNOWLEDGE_DATABASES], required, False
    return unique, required, complete


def _state_databases(root: str, domain: str) -> tuple[list[Path], bool]:
    """Knowledge SQLite databases for one engine `root`/`domain` pair."""
    if not is_absolute(root) or not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain):
        return [], False
    base = Path(path_text(root)) / domain
    try:
        from mindie_knowledge.state_layout import DATABASE, state_root

        return [state_root(root, domain) / DATABASE], True
    except TimeoutError:
        raise
    except ImportError:
        pass
    except ValueError:
        pass
    except OSError:
        return [], False
    try:
        found = sorted(base.glob(f"state-v*/{_KNOWLEDGE_DB_NAME}"))
    except TimeoutError:
        raise
    except OSError:
        return [], False
    databases = [item for item in found if item.is_file()]
    return (databases or [base / "state-v4" / _KNOWLEDGE_DB_NAME]), True


def knowledge_databases(config=None, *, deadline: float | None = None) -> tuple[list[Path], bool]:
    """All configured knowledge SQLite stores plus a completeness flag."""
    try:
        cfg = config_path(config)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return [], False
    engines, required, complete = _engine_config_candidates(cfg)
    cfg_key = path_key(cfg)
    databases: list[Path] = []
    seen: set[str] = set()
    for engine in engines:
        if expired(deadline):
            return databases, False
        engine_key = path_key(engine)
        try:
            payload = read_json(engine)
        except FileNotFoundError:
            if engine_key in required:
                complete = False
            continue
        except TimeoutError:
            raise
        except (OSError, ValueError):
            complete = False
            continue
        root, domain = payload.get("root"), payload.get("domain")
        if not isinstance(root, str) or not isinstance(domain, str):
            if engine_key in required and engine_key != cfg_key:
                complete = False
            continue
        candidates, resolved = _state_databases(root, domain)
        complete = complete and resolved
        for database in candidates:
            key = path_key(database)
            if key not in seen:
                seen.add(key)
                databases.append(database)
    return databases, complete


def _cursor_spellings(spool_file: Path) -> set[str]:
    """Normalized identities a knowledge cursor may have stored for one file."""
    values = {os.path.abspath(os.fspath(spool_file)), path_text(spool_file)}
    try:
        values.add(str(Path(spool_file).resolve()))
    except TimeoutError:
        raise
    except OSError:
        pass
    try:
        values.add(os.path.realpath(spool_file))
    except TimeoutError:
        raise
    except OSError:
        pass
    try:
        from mindie_knowledge.filesystem import path_aliases

        values.update(path_aliases(spool_file))
    except TimeoutError:
        raise
    except (ImportError, OSError, ValueError):
        pass
    return {path_key(value) for value in values if isinstance(value, str)}


def _matching_cursor_rows(connection, targets: set[str]) -> list[tuple]:
    import sqlite3

    try:
        rows = connection.execute("SELECT file_identity, finish FROM cursors").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    return [
        (str(row[0]), row[1]) for row in rows
        if isinstance(row[0], str) and path_key(row[0]) in targets
    ]


def _matching_pending_captures(connection, targets: set[str]) -> bool:
    import sqlite3

    placeholders = ",".join("?" for _ in _PENDING_CAPTURE_STATUSES)
    try:
        rows = connection.execute(
            f"SELECT transcript FROM captures WHERE status IN ({placeholders})",
            tuple(_PENDING_CAPTURE_STATUSES),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    return any(isinstance(row[0], str) and path_key(row[0]) in targets for row in rows)


def _retire_cursor_in_database(database: Path, spool_file: Path, size: int,
                               deadline: float | None, record: dict) -> str:
    """`consumed` (rows deleted), `pending` (keep) or `unknown` (keep + partial)."""
    import sqlite3

    targets = _cursor_spellings(spool_file)
    connection = None
    try:
        connection = _open_sqlite(database, deadline, readonly=False)
        connection.execute("BEGIN IMMEDIATE")
        rows = _matching_cursor_rows(connection, targets)
        for _key, finish in rows:
            try:
                consumed = int(finish)
            except (TypeError, ValueError):
                connection.rollback()
                record["partial"] = True
                note(record, "knowledge transcript cursor has an invalid offset")
                return "unknown"
            if consumed < size:
                connection.rollback()
                return "pending"
        if _matching_pending_captures(connection, targets):
            connection.rollback()
            return "pending"
        for key, _finish in rows:
            connection.execute("DELETE FROM cursors WHERE file_identity=?", (key,))
        connection.commit()
        return "consumed"
    except TimeoutError:
        raise
    except sqlite3.Error as exc:
        note(record, exc)
        return "unknown"
    finally:
        if connection is not None:
            connection.close()


def _retire_knowledge_cursors(databases, spool_file: Path, size: int,
                              deadline: float | None, record: dict) -> str:
    for database in databases:
        if expired(deadline):
            record["partial"] = True
            note(record, "spool prune budget exhausted during cursor retirement")
            return "unknown"
        try:
            exists, regular = database.exists(), database.is_file()
        except TimeoutError:
            raise
        except OSError as exc:
            note(record, exc)
            return "unknown"
        if exists and not regular:
            record["partial"] = True
            note(record, "configured knowledge store is not a regular file")
            return "unknown"
        if not regular:
            continue
        verdict = _retire_cursor_in_database(database, spool_file, size, deadline, record)
        if verdict != "consumed":
            return verdict
    return "consumed"


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
    config: str | Path | None = None,
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
    live = _admission_sessions(root, record, limit, admission_db=admission_db, config=config)
    if live is None:
        record["partial"] = True
        record["status"] = "partial"
        return record
    databases, knowledge_complete = knowledge_databases(config, deadline=limit)
    if not knowledge_complete:
        record["partial"] = True
        record["status"] = "partial"
        note(record, "knowledge state configuration incomplete; spool eviction skipped")
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
        spool_file = root / f"{sid}.jsonl"
        removed = False
        with _held_spool_lock(lock_file, limit) as held:
            if not held:
                record["deferred_dirs"] += 1
                continue
            try:
                size = os.stat(spool_file).st_size
            except TimeoutError:
                raise
            except FileNotFoundError:
                continue
            except OSError as exc:
                note(record, exc)
                continue
            verdict = _retire_knowledge_cursors(databases, spool_file, size, limit, record)
            if verdict != "consumed":
                if verdict == "unknown":
                    record["partial"] = True
                record["kept_dirs"] += 1
                continue
            removed = unlink_file(spool_file, record)
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
    "knowledge_databases",
    "lock_inode_matches",
    "prune_spool_transcripts",
    "spool_maintenance_due",
    "spool_maintenance_marker",
    "write_spool_maintenance_marker",
]
