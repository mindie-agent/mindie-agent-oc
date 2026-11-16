"""Bounded storage lifecycle reclamation for the OpenCode adapter (`storage_gc.py`).

[Role]
Reclaims disposable MindIE-owned storage after a committed profile switch and from
background maintenance: excess `component-runtimes` generations, historical
`oc-source` payloads, stale `generations/oc.engine.*.json` snapshots, and cold
`transcripts/<session>.jsonl` projections with their `.<session>.lock` siblings.

[Invariants]
- Reclamation is never authoritative. The active `oc.json` pointer, the live
  interpreter, `oc-source` payloads referenced by any harness config, borrowed
  cross-plugin interpreters, live leases/processes and user `source_root` trees
  always win over any retention count.
- No entry point rolls back or rewrites a committed switch; every function returns
  an accounting dict and reports failures as warnings instead of raising into the
  primary operation.
- Engine snapshot retention keeps the active `engine_config` target plus every
  snapshot referenced by a sibling harness config, then the newest
  `ENGINE_SNAPSHOT_RETENTION` unreferenced snapshots.
- A spool projection is reclaimed only while its `.<session>.lock` can be taken
  non-blocking, so an active projection or in-flight organizer keeps its spool.
- Path identity comparisons use `mindie_knowledge.filesystem.path_identity`; no
  `os.kill(pid, 0)` probe is performed here on Windows.

[Timeouts & Locks]
- One absolute `deadline` bounds every call; scans re-check it entry by entry and
  report `partial` instead of ranking a truncated table. Lock probes are
  non-blocking and a contended lock is a keep. Budgets: component runtime `2.5s`,
  legacy sweep `3.0s`, engine snapshots `0.5s`, spool maintenance `1.0s`.

[Side-Effect Policy]
- Deletes only disposable generations, stale engine snapshots, and cold spool
  projections under the MindIE-owned roots it is given; never rewrites `oc.json`,
  never touches `opencode.db`, and never starts a subprocess or network call.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import atomic_write_json, config_path, data_home, read_json, state_dir

COMPONENT_BUDGET_SECONDS = 2.5
LEGACY_BUDGET_SECONDS = 3.0
ENGINE_BUDGET_SECONDS = 0.5
SPOOL_BUDGET_SECONDS = 1.0
MAINTENANCE_BUDGET_SECONDS = COMPONENT_BUDGET_SECONDS + LEGACY_BUDGET_SECONDS + ENGINE_BUDGET_SECONDS
COMPONENT_RETENTION_GENERATIONS = 2
ENGINE_SNAPSHOT_RETENTION = 3
SPOOL_SESSION_LIMIT = 128
SPOOL_MAX_AGE_SECONDS = 14 * 24 * 60 * 60.0
MAX_CONFIG_DOCUMENTS = 64
_WARNING_LIMIT = 3


def _account(status: str = "completed", **extra) -> dict:
    value = dict(
        status=status, reclaimed_dirs=0, kept_dirs=0, deferred_dirs=0,
        reclaimed_files=0, warnings=[], partial=False,
    )
    value.update(extra)
    return value


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _budget(deadline: float | None, seconds: float) -> float:
    if deadline is None:
        return max(0.0, float(seconds))
    return max(0.0, min(float(seconds), deadline - time.monotonic()))


def _note(account: dict, message) -> None:
    text = f"{type(message).__name__}: {message}" if isinstance(message, BaseException) else str(message)
    text = text.strip()
    if text and text not in account["warnings"]:
        account["warnings"].append(text)


def _merge(into: dict, other: dict) -> None:
    for key in ("reclaimed_dirs", "kept_dirs", "deferred_dirs", "reclaimed_files", "reclaimed_trash"):
        into[key] = int(into.get(key, 0)) + int((other or {}).get(key, 0) or 0)
    for warning in (other or {}).get("warnings") or ():
        _note(into, warning)
    if (other or {}).get("partial"):
        into["partial"] = True


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _identity(value) -> str:
    from mindie_knowledge.filesystem import path_identity

    try:
        return path_identity(os.fspath(value))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(os.fspath(value)))


def _unlink_file(path: Path, account: dict) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _note(account, exc)
        return False
    account["reclaimed_files"] += 1
    return True


def _iter_strings(value, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def _config_keep_paths(config: str | Path | None, extras=()) -> list[str]:
    """Interpreter/source/engine pins that must never be reclaimed."""
    keep: list[str] = []
    cfg = config_path(config)
    try:
        adapter = read_json(cfg)
    except (TimeoutError, OSError, ValueError):
        adapter = {}
    for key in ("python", "source_root", "runtime_scripts", "engine_config", "state_dir"):
        value = adapter.get(key)
        if isinstance(value, str) and value.strip():
            keep.append(value)
    for value in (*(extras or ()), sys.executable, sys.prefix):
        if value and os.fspath(value) not in keep:
            keep.append(os.fspath(value))
    return keep


def prune_component_generations(
    config: str | Path | None = None,
    *,
    keep_paths=(),
    budget_seconds: float = COMPONENT_BUDGET_SECONDS,
    keep_recent_verified: int = COMPONENT_RETENTION_GENERATIONS,
    deadline: float | None = None,
    live_paths: set[str] | None = None,
) -> dict:
    """Reclaim excess or failed `component-runtimes` generations (never raises)."""
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    root = state_dir(config) / "component-runtimes"
    if not root.is_dir():
        return _account("absent")
    try:
        from mindie_knowledge.storage import prune_component_runtimes as _prune
    except ImportError as exc:
        return _account("unavailable", warnings=[f"ImportError: {exc}"])
    keep = _config_keep_paths(config, extras=keep_paths)
    try:
        result = _prune(
            root,
            keep_paths=keep,
            keep_recent_verified=max(0, int(keep_recent_verified)),
            budget_seconds=_budget(limit, COMPONENT_BUDGET_SECONDS),
            live_paths=live_paths,
            deadline=limit,
        )
    except TimeoutError as exc:
        return _account("partial", partial=True, warnings=[f"TimeoutError: {exc}"])
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    account = _account(str(result.get("status") or "completed"))
    _merge(account, result)
    account["status"] = str(result.get("status") or "completed")
    return account


def sweep_legacy_baggage(
    *,
    config_home: str | Path | None = None,
    data_home_dir: str | Path | None = None,
    budget_seconds: float = LEGACY_BUDGET_SECONDS,
    force: bool = False,
    deadline: float | None = None,
) -> dict:
    """Reclaim historical MindIE runtime baggage for this MindIE home (never raises)."""
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    home_config = Path(config_home) if config_home is not None else config_path().parent
    home_data = Path(data_home_dir) if data_home_dir is not None else data_home()
    if not home_config.is_dir() and not home_data.is_dir():
        return _account("absent")
    try:
        from mindie_knowledge.storage import sweep_legacy_installations
    except ImportError as exc:
        return _account("unavailable", warnings=[f"ImportError: {exc}"])
    try:
        result = sweep_legacy_installations(
            config_home=home_config,
            data_home=home_data,
            budget_seconds=_budget(limit, LEGACY_BUDGET_SECONDS),
            force=bool(force),
            deadline=limit,
        )
    except TimeoutError as exc:
        return _account("partial", partial=True, warnings=[f"TimeoutError: {exc}"])
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    account = _account(str(result.get("status") or "completed"))
    _merge(account, result)
    account["status"] = str(result.get("status") or "completed")
    return account


def _referenced_engine_snapshots(cfg: Path) -> set[str]:
    """Every `generations/*.json` path any sibling harness config still points at."""
    referenced: set[str] = set()
    candidates: list[Path] = [cfg]
    try:
        siblings = sorted(cfg.parent.glob("*.json"))
    except OSError:
        siblings = []
    for doc in siblings[:MAX_CONFIG_DOCUMENTS]:
        if doc != cfg and doc.name.startswith("."):
            continue
        candidates.append(doc)
    for doc in candidates:
        try:
            document = read_json(doc)
        except (TimeoutError, OSError, ValueError):
            continue
        for text in _iter_strings(document):
            stripped = text.strip()
            if not stripped.lower().endswith(".json") or not os.path.isabs(stripped):
                continue
            try:
                referenced.add(_identity(stripped))
            except (OSError, RuntimeError, ValueError):
                continue
    return referenced


def prune_engine_snapshots(
    config: str | Path | None = None,
    *,
    keep_recent: int = ENGINE_SNAPSHOT_RETENTION,
    deadline: float | None = None,
    budget_seconds: float = ENGINE_BUDGET_SECONDS,
) -> dict:
    """Keep the active engine target plus `keep_recent` historical snapshots."""
    cfg = config_path(config)
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    generations = cfg.parent / "generations"
    if not generations.is_dir():
        return _account("absent")
    try:
        referenced = _referenced_engine_snapshots(cfg)
    except TimeoutError as exc:
        return _account("partial", partial=True, warnings=[f"TimeoutError: {exc}"])
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    try:
        candidates = [
            item for item in generations.iterdir()
            if item.name.startswith(f"{cfg.stem}.engine.") and item.name.endswith(".json")
            and item.is_file() and not item.is_symlink()
        ]
    except TimeoutError as exc:
        return _account("partial", partial=True, warnings=[f"TimeoutError: {exc}"])
    except OSError as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    candidates.sort(key=lambda item: (_mtime(item) or 0.0, item.name), reverse=True)
    historical = [item for item in candidates if _identity(item) not in referenced]
    retained = {_identity(item) for item in historical[:max(0, int(keep_recent))]}
    retained.update(_identity(item) for item in candidates if _identity(item) in referenced)
    account = _account("completed")
    account["removed"] = 0
    for item in candidates:
        if _expired(limit):
            account["partial"] = True
            _note(account, "engine snapshot budget exhausted")
            break
        if _identity(item) in retained:
            account["kept_dirs"] += 1
            continue
        try:
            os.unlink(item)
        except FileNotFoundError:
            continue
        except TimeoutError as exc:
            account["partial"] = True
            _note(account, exc)
            break
        except OSError as exc:
            _note(account, exc)
            continue
        account["removed"] += 1
        account["reclaimed_files"] += 1
    if account["partial"]:
        account["status"] = "partial"
    return account


def _spool_entries(root: Path, deadline: float | None, account: dict) -> list[tuple[float, str]]:
    rows: list[tuple[float, str]] = []
    try:
        with os.scandir(root) as iterator:
            for item in iterator:
                if _expired(deadline):
                    account["partial"] = True
                    _note(account, "spool scan budget exhausted")
                    break
                if not item.name.endswith(".jsonl") or item.name.startswith("."):
                    continue
                try:
                    rows.append((item.stat().st_mtime, item.name[: -len(".jsonl")]))
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
    except OSError as exc:
        _note(account, exc)
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


@contextmanager
def _held_spool_lock(lock_file: Path):
    """Yield `True` only when this process holds `<spool>/.<session>.lock`."""
    handle = None
    locked = False
    try:
        try:
            handle = lock_file.open("a+b")
        except TimeoutError:
            raise
        except OSError:
            yield False
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if os.stat(lock_file).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                    handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except TimeoutError:
            raise
        except OSError:
            locked = False
        yield locked
    finally:
        if handle is not None:
            if locked:
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


def prune_spool_transcripts(
    spool_dir: str | Path,
    *,
    exclude=(),
    max_sessions: int = SPOOL_SESSION_LIMIT,
    max_age_seconds: float | None = SPOOL_MAX_AGE_SECONDS,
    budget_seconds: float = SPOOL_BUDGET_SECONDS,
    now: float | None = None,
    deadline: float | None = None,
) -> dict:
    """Reclaim cold `transcripts/*.jsonl` projections and their idle lock files."""
    root = Path(spool_dir)
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    if not root.is_dir():
        return _account("absent", removed_sessions=[])
    stamp = float(now) if now is not None else time.time()
    protected = {sid for sid in exclude if isinstance(sid, str) and sid}
    account = _account("completed", removed_sessions=[], lock_files_removed=0)
    rows = _spool_entries(root, limit, account)
    # Evict the oldest removable projections until the session cap is met; an expired
    # projection is always removable, so a locked one never pins the total above the cap.
    needed = max(0, len(rows) - max(0, int(max_sessions)))
    for mtime, sid in rows:
        if sid in protected:
            account["kept_dirs"] += 1
            continue
        expired = max_age_seconds is not None and (stamp - mtime) > float(max_age_seconds)
        if needed <= 0 and not expired:
            account["kept_dirs"] += 1
            continue
        if _expired(limit):
            account["partial"] = True
            _note(account, "spool prune budget exhausted")
            break
        lock_file = root / f".{sid}.lock"
        removed = False
        with _held_spool_lock(lock_file) as held:
            if not held:
                account["deferred_dirs"] += 1
                continue
            removed = _unlink_file(root / f"{sid}.jsonl", account)
        if not removed:
            continue
        if needed > 0:
            needed -= 1
        account["removed_sessions"].append(sid)
        try:
            os.unlink(lock_file)
        except FileNotFoundError:
            pass
        except TimeoutError as exc:
            account["partial"] = True
            _note(account, exc)
        except OSError:
            pass
        else:
            account["lock_files_removed"] += 1
    if account["partial"]:
        account["status"] = "partial"
    return account


def post_switch_reclaim(
    *,
    config: str | Path | None = None,
    interpreter: str | None = None,
    source_root: str | None = None,
    budget_seconds: float = MAINTENANCE_BUDGET_SECONDS,
    force_legacy: bool = False,
    live_paths: set[str] | None = None,
) -> dict:
    """Best-effort post-switch reclamation; never raises into a committed switch."""
    started = time.monotonic()
    limit = started + max(0.0, float(budget_seconds))
    account = _account("completed", removed=0)
    component = prune_component_generations(
        config, keep_paths=(interpreter, source_root), live_paths=live_paths,
        budget_seconds=COMPONENT_BUDGET_SECONDS, deadline=limit,
    )
    account["component_runtimes"] = component
    _merge(account, component)
    legacy = sweep_legacy_baggage(
        budget_seconds=LEGACY_BUDGET_SECONDS, force=force_legacy, deadline=limit,
    )
    account["legacy"] = legacy
    _merge(account, legacy)
    snapshots = prune_engine_snapshots(config, deadline=limit)
    account["engine_snapshots"] = snapshots
    _merge(account, snapshots)
    account["removed"] = int(snapshots.get("removed") or 0)
    if account["partial"] or any(
        str(item.get("status") or "") not in {"completed", "absent", "skipped_cooldown"}
        for item in (component, legacy, snapshots)
    ):
        account["status"] = "partial"
    account["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return account


def warning_text(account: dict | None) -> str | None:
    """Compact `cleanup_warning` text for a reclaim accounting dict, if any."""
    if not isinstance(account, dict):
        return None
    status = str(account.get("status") or "")
    warnings = [str(item).strip() for item in account.get("warnings") or [] if str(item).strip()]
    if status in {"", "completed", "absent", "skipped_cooldown"} and not warnings:
        return None
    detail = "; ".join(warnings[:_WARNING_LIMIT]) or status or "incomplete"
    return f"storage cleanup {status or 'incomplete'}: {detail}"[:400]


def acquire_service_lease(interpreter: str | None = None):
    """Hold the shared runtime lease of the generation owning `interpreter`.

    Returns a `mindie_knowledge.storage.RuntimeLease` (call `.release()`), or
    `None` when the interpreter is not inside a disposable generation, the
    generation is gone, or the lease cannot be taken. A missing lease never
    disables the caller.
    """
    candidate = interpreter or sys.executable
    try:
        from mindie_knowledge.storage import acquire_runtime_lease, owning_dir
    except ImportError:
        return None
    try:
        owner = owning_dir(candidate)
        if owner is None or not Path(owner).is_dir():
            return None
        return acquire_runtime_lease(owner)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        return None


def release_service_lease(lease) -> None:
    """Release a lease returned by `acquire_service_lease` without raising."""
    if lease is None:
        return
    try:
        lease.release()
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        pass


def spool_maintenance_marker(spool_dir: str | Path) -> Path:
    return Path(spool_dir) / ".spool-maintenance.json"


def spool_maintenance_due(spool_dir: str | Path, *, interval_seconds: float, now: float | None = None) -> bool:
    """True when the cooldown marker is absent, unreadable, or older than `interval_seconds`."""
    marker = spool_maintenance_marker(spool_dir)
    try:
        raw = marker.read_text(encoding="utf-8")
    except (TimeoutError, OSError):
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
    "COMPONENT_BUDGET_SECONDS",
    "COMPONENT_RETENTION_GENERATIONS",
    "ENGINE_SNAPSHOT_RETENTION",
    "LEGACY_BUDGET_SECONDS",
    "MAINTENANCE_BUDGET_SECONDS",
    "SPOOL_BUDGET_SECONDS",
    "SPOOL_MAX_AGE_SECONDS",
    "SPOOL_SESSION_LIMIT",
    "acquire_service_lease",
    "post_switch_reclaim",
    "prune_component_generations",
    "prune_engine_snapshots",
    "prune_spool_transcripts",
    "release_service_lease",
    "spool_maintenance_due",
    "sweep_legacy_baggage",
    "warning_text",
    "write_spool_maintenance_marker",
]
