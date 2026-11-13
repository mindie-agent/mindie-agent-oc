"""OpenCode (`opencode`) release registry bridge (`oc_native_compat.py`).

[Role]
Ensures that `mindie_knowledge.releases` and `mindie_knowledge.releases_io`
recognize `harness="opencode"` -> `"mindie-agent/mindie-agent-oc"` when running
against shared `mindie-knowledge` baseline revisions released before `oc`.

[Invariants]
- Idempotent: if `mindie_knowledge.releases.REPOSITORIES` already maps
  `"opencode"`, registration is a no-op.

[Timeouts & Locks]
- Pure in-memory dictionary registration; zero I/O or locks.

[Side-Effect Policy]
- Updates in-process `mindie_knowledge.releases.REPOSITORIES` and
  `releases_io._COMPATIBILITY_PATHS` only when `"opencode"` is absent.
"""

from __future__ import annotations


def ensure_oc_native_support() -> None:
    """Register `opencode` in `mindie_knowledge.releases` when running against pre-oc components."""
    try:
        from mindie_knowledge import releases, releases_io
    except ImportError:
        return
    if "opencode" not in releases.REPOSITORIES:
        releases.REPOSITORIES["opencode"] = "mindie-agent/mindie-agent-oc"
    compat_paths = getattr(releases_io, "_COMPATIBILITY_PATHS", None)
    if isinstance(compat_paths, dict) and "opencode" not in compat_paths:
        compat_paths["opencode"] = "harness-compatibility.json"
