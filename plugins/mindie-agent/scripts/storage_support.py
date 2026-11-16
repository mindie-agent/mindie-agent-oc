"""Path/config guards and accounting primitives for storage reclamation.

[Role]
Resolves MindIE config homes, protected interpreter/generation pins, the link/Git
guard predicates that decide whether a path may be reclaimed, and the shared
accounting primitives every reclamation entry point returns.

[Invariants]
- Windows extended-length spellings (`\\\\?\\`, `\\\\?\\UNC\\`, `\\??\\`) are stripped
  before any identity or containment comparison, so a config spelling and a disk
  spelling of one generation compare equal.
- Every existing config document is read through symlinks: a borrowed interpreter
  may live behind a linked `codex.json`. An enumeration, read or parse failure is
  reported as incomplete protection instead of silently narrowing the pin set.
- A deletion target must be link-free from itself up to a trusted base and have no
  Git ancestor; an inspection error is a keep, never an optimization.
- `source_root`/`package_dir` and every other config value are protection pins
  only; they are never enumerated as deletion roots.

[Timeouts & Locks]
- Every reader is synchronous, bounded by the caller's absolute `deadline` and
  raises `TimeoutError` on expiry. No lock is acquired here.

[Side-Effect Policy]
- Guards are strictly read-only; the accounting helpers only mutate the dict the
  caller handed them. No subprocess or network call is made here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

_SEPARATORS = re.compile(r"[\\/]+")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_KNOWN_CONFIG_FILES = ("dsh/config.json", "dsh/engine.json", "codex.updates/native.json")
_PIN_KEYS = (
    "python", "previous_python", "rollback_python",
    "source_root", "package_dir", "runtime_scripts", "state_dir",
    "engine_config", "previous_generation", "previous_engine_config",
    "rollback_engine_config",
)
MAX_CONFIG_DOCUMENTS = 64


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("protected config scan budget exhausted")


def normalize_windows_path(value) -> str:
    """Strip a Windows extended-length prefix; a pure string operation."""
    text = os.fspath(value)
    if not isinstance(text, str):
        return text
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("//?/UNC/"):
        return "//" + text[len("//?/UNC/"):]
    if text.startswith("\\??\\"):
        return text[len("\\??\\"):]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\"):]
    if text.startswith("//?/"):
        return text[len("//?/"):]
    return text


def path_text(value) -> str:
    """Normalized spelling of `value` (Windows prefix removed, separators collapsed)."""
    text = normalize_windows_path(value)
    try:
        return os.path.normpath(text)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return text


def path_key(value) -> str:
    """Comparison key: absolute, case-folded on Windows, prefix-free everywhere."""
    text = path_text(value)
    try:
        return os.path.normcase(os.path.abspath(text))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return os.path.normcase(text)


def is_absolute(value) -> bool:
    """True for POSIX/Windows absolute spellings, including extended-length ones."""
    text = normalize_windows_path(value)
    try:
        return bool(os.path.isabs(text) or _DRIVE.match(text) or text.startswith("\\\\"))
    except (TypeError, ValueError):
        return False


def _parts(value) -> list[str]:
    return [part for part in _SEPARATORS.split(path_key(value)) if part]


def within_root(child, parent) -> bool:
    """True when `child` is `parent` or lies below it after prefix normalization."""
    if parent is None:
        return False
    child_parts, parent_parts = _parts(child), _parts(parent)
    if not parent_parts or len(child_parts) < len(parent_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def _is_junction(path: Path) -> bool:
    try:
        from mindie_knowledge.storage.lifecycle import is_junction
    except (ImportError, AttributeError):
        return False
    try:
        return bool(is_junction(path))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return True


def _is_system_root_link(path: Path) -> bool:
    """macOS `/var`, `/tmp` and `/etc` are root firmlinks, not user symlinks."""
    import sys

    return sys.platform == "darwin" and path.parent == Path("/") and path.name in {"var", "tmp", "etc"}


def link_free(path, *, bases=()) -> bool:
    """True when `path` and every ancestor up to the filesystem root is a real dir.

    `bases` is accepted for call-site compatibility, but the walk never stops at a
    caller base: a symlinked/junction parent above a custom `state_dir` must be
    caught. The only exemptions are the three macOS root firmlinks.
    """
    try:
        probe = Path(os.path.abspath(os.fspath(path)))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    while True:
        try:
            if not _is_system_root_link(probe) and (probe.is_symlink() or _is_junction(probe)):
                return False
        except TimeoutError:
            raise
        except OSError:
            return False
        if probe.parent == probe:
            return True
        probe = probe.parent


def has_git_ancestor(value) -> bool:
    """True when `value` or any ancestor is a Git checkout; errors are a keep."""
    try:
        from mindie_knowledge.storage.lifecycle import has_git_ancestor as _probe
    except (ImportError, AttributeError):
        return False
    try:
        return bool(_probe(Path(path_text(value))))
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        return True


def default_config_homes() -> tuple[list[Path], bool]:
    """Production config homes from the shared legacy resolver plus a flag."""
    try:
        from mindie_knowledge.storage import legacy_roots
    except ImportError:
        return [], True
    try:
        return list(legacy_roots._config_homes(None)), True
    except (AttributeError, KeyError):
        return [], True
    except TimeoutError:
        raise
    except (OSError, RuntimeError, ValueError):
        return [], False


def config_homes(*extra) -> tuple[list[Path], bool]:
    """Ordered de-duplicated config homes (explicit first) and an `incomplete` flag."""
    homes: list[Path] = []
    complete = True
    defaults, default_complete = default_config_homes()
    complete = complete and default_complete
    for value in (*extra, *defaults):
        if value is None:
            continue
        try:
            home = Path(value).expanduser()
        except TimeoutError:
            raise
        except (OSError, ValueError, TypeError):
            complete = False
            continue
        key = path_key(home)
        if key not in {path_key(item) for item in homes}:
            homes.append(home)
    return homes, complete


def _config_paths(home: Path, *, deadline: float | None) -> tuple[list[Path], bool]:
    """Existing JSON config files in one home (linked files included)."""
    paths: list[Path] = []
    complete = True
    try:
        with os.scandir(home) as iterator:
            for item in iterator:
                _check(deadline)
                if not item.name.endswith(".json") or item.name.startswith("."):
                    continue
                try:
                    if item.is_file(follow_symlinks=True):
                        paths.append(home / item.name)
                except TimeoutError:
                    raise
                except OSError:
                    complete = False
    except TimeoutError:
        raise
    except FileNotFoundError:
        pass
    except OSError:
        complete = False
    for relative in _KNOWN_CONFIG_FILES:
        _check(deadline)
        candidate = home / relative
        try:
            if candidate.is_file():
                paths.append(candidate)
        except TimeoutError:
            raise
        except OSError:
            complete = False
    return paths, complete


def config_documents(homes, *, deadline: float | None = None) -> tuple[list[tuple[Path, dict]], bool]:
    """Every readable JSON config document plus an `incomplete` protection flag."""
    documents: list[tuple[Path, dict]] = []
    complete = True
    seen: set[str] = set()
    for home in homes:
        _check(deadline)
        paths, enumerated = _config_paths(Path(home), deadline=deadline)
        complete = complete and enumerated
        for path in paths:
            _check(deadline)
            key = path_key(path)
            if key in seen:
                continue
            seen.add(key)
            if len(documents) >= MAX_CONFIG_DOCUMENTS:
                complete = False
                return documents, complete
            try:
                if os.stat(path).st_size > 4 * 1024 * 1024:
                    complete = False
                    continue
                raw = Path(path).read_text(encoding="utf-8")
            except TimeoutError:
                raise
            except (OSError, ValueError):
                complete = False
                continue
            try:
                value = json.loads(raw)
            except ValueError:
                complete = False
                continue
            if isinstance(value, dict):
                documents.append((Path(path), value))
    return documents, complete


def config_pins(documents) -> list[str]:
    """Interpreter/generation spellings protected by the scanned config documents."""
    pins: list[str] = []
    for _path, value in documents:
        for key in _PIN_KEYS:
            item = value.get(key)
            if not isinstance(item, str) or not item.strip():
                continue
            text = path_text(item.strip())
            if text and text not in pins:
                pins.append(text)
    return pins


def _strings(value, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)


def referenced_json_paths(documents) -> set[str]:
    """Every absolute `*.json` path a scanned config document mentions.

    Both the unresolved spelling a config uses and its symlink-resolved identity
    are recorded, so a reference through an aliased parent still protects the
    real snapshot the GC enumerates.
    """
    found: set[str] = set()
    for _path, value in documents:
        for text in _strings(value):
            stripped = text.strip()
            if not stripped.lower().endswith(".json") or not is_absolute(stripped):
                continue
            found.add(path_key(stripped))
            try:
                resolved = path_key(Path(path_text(stripped)).resolve())
            except TimeoutError:
                raise
            except (OSError, ValueError):
                continue
            found.add(resolved)
    return found


def forbidden_roots(homes, *, deadline: float | None = None) -> tuple[set[Path], bool]:
    """Configured `source_root`/`package_dir` trees no candidate may be deleted in."""
    documents, complete = config_documents(homes, deadline=deadline)
    roots: set[Path] = set()
    for _path, value in documents:
        for key in ("source_root", "package_dir"):
            item = value.get(key)
            if isinstance(item, str) and is_absolute(item):
                roots.add(Path(path_text(item)))
    return roots, complete


def account(status: str = "completed", **extra) -> dict:
    """Standard reclamation accounting dict."""
    value = dict(
        status=status, reclaimed_dirs=0, kept_dirs=0, deferred_dirs=0,
        reclaimed_files=0, warnings=[], partial=False,
    )
    value.update(extra)
    return value


def expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def budget(deadline: float | None, seconds: float) -> float:
    if deadline is None:
        return max(0.0, float(seconds))
    return max(0.0, min(float(seconds), deadline - time.monotonic()))


def note(record: dict, message) -> None:
    """Append one de-duplicated warning (an exception keeps its type name)."""
    text = f"{type(message).__name__}: {message}" if isinstance(message, BaseException) else str(message)
    text = text.strip()
    if text and text not in record["warnings"]:
        record["warnings"].append(text)


def merge(into: dict, other: dict) -> None:
    for key in ("reclaimed_dirs", "kept_dirs", "deferred_dirs", "reclaimed_files", "reclaimed_trash"):
        into[key] = int(into.get(key, 0)) + int((other or {}).get(key, 0) or 0)
    for warning in (other or {}).get("warnings") or ():
        note(into, warning)
    if (other or {}).get("partial"):
        into["partial"] = True


def unlink_file(path: Path, record: dict) -> bool:
    """Unlink one file, counting the success and never raising `OSError`."""
    try:
        os.unlink(path)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return False
    except OSError as exc:
        note(record, exc)
        return False
    record["reclaimed_files"] += 1
    return True


__all__ = [
    "MAX_CONFIG_DOCUMENTS",
    "account",
    "budget",
    "expired",
    "merge",
    "note",
    "unlink_file",
    "config_documents",
    "config_homes",
    "config_pins",
    "default_config_homes",
    "forbidden_roots",
    "has_git_ancestor",
    "is_absolute",
    "link_free",
    "normalize_windows_path",
    "path_key",
    "path_text",
    "referenced_json_paths",
    "within_root",
]
