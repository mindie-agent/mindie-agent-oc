"""Bounded storage lifecycle reclamation for the OpenCode adapter (`storage_gc.py`).

[Role]
Reclaims disposable MindIE-owned storage after a committed profile switch and from
background maintenance: excess `component-runtimes` generations, historical
`oc-source` payloads, stale `generations/oc.engine.*.json` snapshots and cold
`transcripts/<session>.jsonl` bundles.

[Invariants]
- Reclamation is never authoritative: the active `oc.json` pointer and its
  `python`, the persisted `previous_python`/`previous_generation` rollback pins,
  live interpreters, cross-config-root borrowed pins, leases and user `source_root`
  trees always win over any retention count; no entry point rewrites `oc.json`.
- Deletion runs only with complete protection: an unreadable config, an unleased
  pin or an unknown admission state skips the sweep instead of narrowing the
  protected set, and `TimeoutError` is always re-raised.
- Engine snapshots keep the active target, every referenced sibling snapshot, then
  the newest `ENGINE_SNAPSHOT_RETENTION`; a linked ancestor, an external Git
  ancestor or a `source_root`/`package_dir` parent is never unlinked.
- A spool bundle (`<session>.jsonl` + `<session>.seq` + `.<session>.lock`) is
  evicted only as a unit, only under its inode-verified lock, and never while the
  session is admitted or has pending activation state.

[Timeouts & Locks]
- One absolute `deadline` bounds every call; expiry reports `partial` instead of
  ranking a truncated table. Lock probes are non-blocking; contention is a keep.
  Budgets: component `2.5s`, legacy `3.0s`, engine `0.5s`, spool `1.0s`.

[Side-Effect Policy]
- Deletes only disposable generations, stale engine snapshots and cold spool
  bundles under the MindIE-owned roots it is given; preserves a mutable rollback
  engine payload as an immutable `generations/` snapshot before it is rewritten;
  never rewrites `oc.json`, touches `opencode.db`, or starts a subprocess.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import storage_support as guards
from storage_spool import (
    _held_spool_lock,
    SPOOL_BUDGET_SECONDS,
    SPOOL_BUNDLE_MIN_AGE_SECONDS,
    SPOOL_MAX_AGE_SECONDS,
    SPOOL_SESSION_LIMIT,
    lock_inode_matches,
    prune_spool_transcripts,
    spool_maintenance_due,
    write_spool_maintenance_marker,
)
from storage_support import account as _account
from storage_support import budget as _budget
from storage_support import expired as _expired
from storage_support import merge as _merge
from storage_support import normalize_windows_path as _normalize_windows_path
from storage_support import note as _note
from paths import atomic_write_json, config_path, data_home, read_json, state_dir

COMPONENT_BUDGET_SECONDS = 6.0 if os.name == "nt" else 2.5
LEGACY_BUDGET_SECONDS = 3.0
ENGINE_BUDGET_SECONDS = 0.5
MAINTENANCE_BUDGET_SECONDS = COMPONENT_BUDGET_SECONDS + LEGACY_BUDGET_SECONDS + ENGINE_BUDGET_SECONDS
COMPONENT_RETENTION_GENERATIONS = 2
# Aligned with `mindie_knowledge.storage.legacy.VERIFIED_RETENTION` so the shared
# legacy sweep and this module never disagree about how many snapshots survive.
ENGINE_SNAPSHOT_RETENTION = 2
MAX_CONFIG_DOCUMENTS = guards.MAX_CONFIG_DOCUMENTS
_WARNING_LIMIT = 3


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except TimeoutError:
        raise
    except OSError:
        return None


def _identity(value) -> str:
    from mindie_knowledge.filesystem import path_identity

    text = _normalize_windows_path(value)
    try:
        return path_identity(os.fspath(text))
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        return guards.path_key(text)


def _has_git_ancestor_any(value) -> bool:
    """True when either the raw or the symlink-resolved spelling sits in Git."""
    if guards.has_git_ancestor(value):
        return True
    try:
        resolved = Path(os.fspath(value)).resolve()
    except TimeoutError:
        raise
    except OSError:
        return True
    return guards.has_git_ancestor(resolved)


def _within_forbidden(value, forbidden) -> bool:
    """True when the raw or resolved spelling lands inside a user source tree."""
    if not forbidden:
        return False
    try:
        from mindie_knowledge.storage.lifecycle import is_within_roots
    except ImportError:
        return True
    try:
        if is_within_roots(Path(os.fspath(value)), forbidden):
            return True
        return is_within_roots(Path(os.fspath(value)).resolve(), forbidden)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        return True


def _keep_state(config, *, extras=(), homes=None) -> tuple[list[str], bool]:
    """Protected interpreter/generation pins across every config home, plus a flag."""
    cfg = config_path(config)
    home_list, homes_complete = guards.config_homes(cfg.parent, *(homes or ()))
    documents, docs_complete = guards.config_documents(home_list)
    keep = guards.config_pins(documents)
    for value in (*(extras or ()), sys.executable, sys.prefix):
        if value and os.fspath(value) not in keep:
            keep.append(os.fspath(value))
    return keep, bool(homes_complete and docs_complete)


def _config_keep_paths(config, extras=()) -> list[str]:
    """Interpreter/source/engine pins that must never be reclaimed."""
    keep, _ = _keep_state(config, extras=extras)
    return keep


def _release_lease(lease) -> None:
    try:
        lease.release()
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        pass


def _lease_owner(pin: str):
    """Disposable interpreter generation owning `pin`, or `None` when not a runtime.

    Source/release payloads are content-addressed immutable trees: leasing one
    would add an `.inuse.lock` file and change its digest, so only interpreter
    paths (`.venv`/`venv`/`bootstrap-venv`) are leased. Everything else stays
    protected by `keep_paths` and the shared legacy pin scan.
    """
    from mindie_knowledge.storage import owning_dir

    parts = Path(os.fspath(pin)).parts
    labels = {part.casefold() for part in parts}
    if not (Path(os.fspath(pin)).name.casefold().startswith("python")
            or labels & {".venv", "venv", "bootstrap-venv"}):
        return None
    return owning_dir(os.fspath(pin))


def _hold_pin_leases(stack: ExitStack, pins) -> bool:
    """Lease every disposable generation a pin still points into; False on failure."""
    try:
        from mindie_knowledge.storage import acquire_runtime_lease
    except ImportError:
        return False
    held: set[str] = set()
    for pin in pins:
        try:
            owner = _lease_owner(pin)
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError):
            return False
        if owner is None:
            continue
        try:
            if not Path(owner).is_dir():
                continue
        except TimeoutError:
            raise
        except OSError:
            return False
        key = _identity(owner)
        if key in held:
            continue
        try:
            lease = acquire_runtime_lease(owner)
        except TimeoutError:
            raise
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError, ValueError):
            return False
        if lease is None:
            return False
        stack.callback(_release_lease, lease)
        held.add(key)
    return True


def prune_component_generations(
    config: str | Path | None = None,
    *,
    keep_paths=(),
    budget_seconds: float = COMPONENT_BUDGET_SECONDS,
    keep_recent_verified: int = COMPONENT_RETENTION_GENERATIONS,
    deadline: float | None = None,
    live_paths: set[str] | None = None,
    config_homes=None,
    forbidden_roots=(),
) -> dict:
    """Reclaim excess or failed `component-runtimes` generations (never raises)."""
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    try:
        cfg = config_path(config)
        root = state_dir(config) / "component-runtimes"
        homes, homes_complete = guards.config_homes(cfg.parent, *(config_homes or ()))
        keep, pins_complete = _keep_state(config, extras=keep_paths, homes=homes)
        forbidden, forbidden_complete = guards.forbidden_roots(homes, deadline=limit)
        try:
            cfg_doc = read_json(cfg)
        except FileNotFoundError:
            cfg_doc = None
        except TimeoutError:
            raise
        except (OSError, ValueError):
            cfg_doc = None
            forbidden_complete = False
        if isinstance(cfg_doc, dict):
            for key in ("source_root", "package_dir"):
                item = cfg_doc.get(key)
                if isinstance(item, str) and guards.is_absolute(item):
                    forbidden.add(Path(guards.path_text(item)))
            for item in guards.config_pins([(cfg, cfg_doc)]):
                if item not in keep:
                    keep.append(item)
        for item in forbidden_roots or ():
            if item:
                forbidden.add(Path(guards.path_text(item)))
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    if not root.is_dir():
        return _account("absent")
    if not (homes_complete and pins_complete and forbidden_complete):
        return _account("partial", partial=True, warnings=["protected pins incomplete; component reclamation skipped"])
    if not guards.link_free(root) or _has_git_ancestor_any(root):
        return _account("protected", warnings=["component root is linked or inside a Git checkout"])
    try:
        from mindie_knowledge.storage import prune_component_runtimes as _prune
    except ImportError as exc:
        return _account("unavailable", warnings=[f"ImportError: {exc}"])
    try:
        result = _prune(
            root,
            keep_paths=keep,
            keep_recent_verified=max(0, int(keep_recent_verified)),
            budget_seconds=_budget(limit, COMPONENT_BUDGET_SECONDS),
            live_paths=live_paths,
            forbidden_roots=forbidden,
            deadline=limit,
        )
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    account = _account(str(result.get("status") or "completed"))
    _merge(account, result)
    account["status"] = str(result.get("status") or "completed")
    return account


def sweep_legacy_baggage(
    *,
    config_home=None,
    data_home_dir: str | Path | None = None,
    budget_seconds: float = LEGACY_BUDGET_SECONDS,
    force: bool = False,
    deadline: float | None = None,
) -> dict:
    """Reclaim historical MindIE runtime baggage across every config home (never raises)."""
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    explicit = [config_home] if isinstance(config_home, (str, Path)) else list(config_home or [])
    try:
        home_data = Path(data_home_dir) if data_home_dir is not None else data_home()
        homes, homes_complete = guards.config_homes(*explicit)
        _documents, docs_complete = guards.config_documents(homes, deadline=limit)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    if not homes_complete or not docs_complete:
        return _account("partial", partial=True, warnings=["protected pins incomplete; legacy sweep skipped"])
    if not any(home.is_dir() for home in homes) and not home_data.is_dir():
        return _account("absent")
    try:
        from mindie_knowledge.storage import sweep_legacy_installations
    except ImportError as exc:
        return _account("unavailable", warnings=[f"ImportError: {exc}"])
    defaults, _ = guards.default_config_homes()
    default_keys = [guards.path_key(item) for item in defaults]
    scope = None if [guards.path_key(item) for item in homes] == default_keys else homes
    try:
        result = sweep_legacy_installations(
            config_home=scope,
            data_home=home_data,
            budget_seconds=_budget(limit, LEGACY_BUDGET_SECONDS),
            force=bool(force),
            deadline=limit,
        )
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    account = _account(str(result.get("status") or "completed"))
    _merge(account, result)
    account["status"] = str(result.get("status") or "completed")
    return account


def _engine_documents(cfg: Path, homes, deadline: float | None) -> tuple[list, bool]:
    """Config documents that may reference an engine snapshot, plus a flag."""
    documents: list = []
    candidates: list[Path] = [cfg]
    try:
        siblings = sorted(cfg.parent.glob("*.json"))
    except TimeoutError:
        raise
    except OSError:
        return documents, False
    truncated = len(siblings) > MAX_CONFIG_DOCUMENTS
    for doc in siblings[:MAX_CONFIG_DOCUMENTS]:
        if doc != cfg and doc.name.startswith("."):
            continue
        candidates.append(doc)
    for doc in candidates:
        if _expired(deadline):
            raise TimeoutError("engine snapshot reference scan budget exhausted")
        try:
            documents.append((doc, read_json(doc)))
        except TimeoutError:
            raise
        except FileNotFoundError:
            if doc == cfg:
                continue
            return documents, False
        except (OSError, ValueError):
            return documents, False
    complete = not truncated
    for home in homes:
        if guards.path_key(home) == guards.path_key(cfg.parent):
            continue
        try:
            extra, extra_complete = guards.config_documents([home], deadline=deadline)
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError):
            return documents, False
        documents.extend(extra)
        complete = complete and extra_complete
    return documents, complete


def _snapshot_removable(item: Path, cfg: Path, forbidden) -> bool:
    """True only when the snapshot is link-free, Git-free and outside user roots."""
    if not guards.link_free(item):
        return False
    if _has_git_ancestor_any(item):
        return False
    if _within_forbidden(item, forbidden):
        return False
    return True


def preserve_engine_snapshot(cfg_file: Path, prior_gen) -> str | None:
    """Copy a mutable prior engine config into `generations/` before overwriting it.

    A default install keeps its rollback payload in the mutable `oc.engine.json`;
    the legacy mirror rewrites that file with the NEW interpreter, so the old
    payload must be preserved under a content-addressed snapshot first. An
    immutable `generations/<stem>.engine.<id>.json` is returned unchanged.
    """
    if not isinstance(prior_gen, str) or not os.path.isabs(prior_gen):
        return None
    prior = Path(prior_gen)
    try:
        if not prior.is_file():
            return None
        generations = (cfg_file.parent / "generations").resolve()
        resolved = prior.resolve()
        if (
            resolved.parent == generations
            and resolved.name.startswith(f"{cfg_file.stem}.engine.")
            and resolved.suffix == ".json"
        ):
            return prior_gen
        raw = prior.read_bytes()
    except TimeoutError:
        raise
    except OSError as exc:
        raise OSError(f"cannot preserve rollback engine config {prior}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"rollback engine config is not valid JSON ({prior}): {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"rollback engine config is not a JSON object: {prior}")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    snapshot = cfg_file.parent / "generations" / f"{cfg_file.stem}.engine.{digest}.json"
    if not snapshot.is_file():
        snapshot.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(snapshot, payload)
    return str(snapshot)


def prune_engine_snapshots(
    config: str | Path | None = None,
    *,
    keep_recent: int = ENGINE_SNAPSHOT_RETENTION,
    deadline: float | None = None,
    budget_seconds: float = ENGINE_BUDGET_SECONDS,
    config_homes=None,
) -> dict:
    """Keep the active engine target plus `keep_recent` historical snapshots."""
    limit = time.monotonic() + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, float(deadline))
    cfg = config_path(config)
    generations = cfg.parent / "generations"
    if not generations.is_dir():
        return _account("absent", removed=0)
    try:
        homes, homes_complete = guards.config_homes(cfg.parent, *(config_homes or ()))
        documents, refs_complete = _engine_documents(cfg, homes, limit)
        forbidden, forbidden_complete = guards.forbidden_roots(homes, deadline=limit)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    if not (homes_complete and refs_complete and forbidden_complete):
        account = _account("partial", partial=True, removed=0)
        _note(account, "engine snapshot references incomplete; deletion skipped")
        return account
    referenced = guards.referenced_json_paths(documents)
    try:
        candidates = [
            item for item in generations.iterdir()
            if item.name.startswith(f"{cfg.stem}.engine.") and item.name.endswith(".json")
            and item.is_file() and not item.is_symlink()
        ]
    except TimeoutError:
        raise
    except OSError as exc:
        return _account("error", warnings=[f"{type(exc).__name__}: {exc}"])
    candidates.sort(key=lambda item: (_mtime(item) or 0.0, item.name), reverse=True)

    def _referenced(item: Path) -> bool:
        return guards.path_key(item) in referenced or _identity(item) in referenced

    historical = [item for item in candidates if not _referenced(item)]
    retained = {id(item) for item in historical[:max(0, int(keep_recent))]}
    retained.update(id(item) for item in candidates if _referenced(item))
    account = _account("completed")
    account["removed"] = 0
    for item in candidates:
        if _expired(limit):
            account["partial"] = True
            _note(account, "engine snapshot budget exhausted")
            break
        if id(item) in retained:
            account["kept_dirs"] += 1
            continue
        if not _snapshot_removable(item, cfg, forbidden):
            account["kept_dirs"] += 1
            _note(account, "protected engine snapshot: " + item.name)
            continue
        try:
            os.unlink(item)
        except TimeoutError:
            raise
        except FileNotFoundError:
            continue
        except OSError as exc:
            _note(account, exc)
            continue
        account["removed"] += 1
        account["reclaimed_files"] += 1
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
    """Best-effort post-switch reclamation; never rolls back a committed switch."""
    started = time.monotonic()
    limit = started + max(0.0, float(budget_seconds))
    account = _account("completed", removed=0)
    try:
        cfg = config_path(config)
        homes, homes_complete = guards.config_homes(cfg.parent)
        pins, pins_complete = _keep_state(config, extras=(interpreter, source_root), homes=homes)
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        _note(account, exc)
        account.update(partial=True, status="partial")
        account["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return account
    snapshots = _account("skipped")
    with ExitStack() as stack:
        try:
            leased = _hold_pin_leases(stack, pins)
        except TimeoutError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            _note(account, exc)
            leased = False
        component = prune_component_generations(
            config, keep_paths=(interpreter, source_root), live_paths=live_paths,
            budget_seconds=COMPONENT_BUDGET_SECONDS, deadline=limit,
            config_homes=homes, forbidden_roots=(source_root,) if source_root else (),
        )
        account["component_runtimes"] = component
        _merge(account, component)
        guarded = bool(leased and pins_complete and homes_complete)
        if guarded:
            legacy = sweep_legacy_baggage(
                config_home=homes, budget_seconds=LEGACY_BUDGET_SECONDS,
                force=force_legacy, deadline=limit,
            )
        else:
            legacy = _account("partial", partial=True)
            _note(legacy, "legacy sweep skipped: protected pins incomplete or unleased")
        account["legacy"] = legacy
        _merge(account, legacy)
        if guarded:
            try:
                snapshots = prune_engine_snapshots(
                    config, keep_recent=ENGINE_SNAPSHOT_RETENTION,
                    config_homes=homes, deadline=limit,
                )
            except TimeoutError as exc:
                snapshots = _account("partial", partial=True, warnings=[f"TimeoutError: {exc}"])
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
    """Hold the shared runtime lease of the generation owning `interpreter`."""
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
    _release_lease(lease)


__all__ = [
    "COMPONENT_BUDGET_SECONDS",
    "COMPONENT_RETENTION_GENERATIONS",
    "ENGINE_SNAPSHOT_RETENTION",
    "LEGACY_BUDGET_SECONDS",
    "MAINTENANCE_BUDGET_SECONDS",
    "SPOOL_BUDGET_SECONDS",
    "SPOOL_BUNDLE_MIN_AGE_SECONDS",
    "SPOOL_MAX_AGE_SECONDS",
    "SPOOL_SESSION_LIMIT",
    "acquire_service_lease",
    "lock_inode_matches",
    "post_switch_reclaim",
    "preserve_engine_snapshot",
    "prune_component_generations",
    "prune_engine_snapshots",
    "prune_spool_transcripts",
    "release_service_lease",
    "spool_maintenance_due",
    "sweep_legacy_baggage",
    "warning_text",
    "write_spool_maintenance_marker",
]
