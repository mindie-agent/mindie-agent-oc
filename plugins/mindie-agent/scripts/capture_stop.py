#!/usr/bin/env python3
"""Detached OpenCode (`oc`) turn-end (`session.execution.*` / `session.idle`) capture helper.

[Role]
Verifies community sharing consent and `project_roots` scope before opening SQLite,
auto-provisions a default session lease (`not_before=0.0`) with the verified session
workspace, prepares `state-v4.sqlite3` if uninitialized, projects newly appended public
`session_message` rows from OpenCode's read-only `opencode.db` into
`<state_dir>/transcripts/<session_id>.jsonl`, and hands off the turn receipt to
`mindie_knowledge.loop.cli.capture_hook`.

[Invariants]
- Unconfigured/disabled community sharing, missing or out-of-scope workspaces, subagent
  (`parentID`), or forked (`forkSessionID`) sessions short-circuit immediately without
  creating `admission.sqlite3`, touching transcripts, or starting services.
- Never guesses `project_root` from a stale lease or `os.getcwd()`.
- Never emits to stdout when run as detached background helper; rotates
  `capture.log` atomically via `os.replace()` at `1 MiB`.

[Timeouts & Locks]
- Bounded by 2.0s read-only SQLite spool sync and `capture_hook`'s local SQLite
  WAL handoff.

[Side-Effect Policy]
- When community sharing is enabled and in scope, ensures a session lease in
  `admission.sqlite3`, prepares `state-v4.sqlite3` if absent, appends to
  `<state_dir>/transcripts/<session_id>.jsonl`, writes at most one `captures`
  row via `capture_hook`, and logs to `capture.log`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import ensure_default_lease, is_deactivated
from identity import is_eligible_main_session, session_from_hook
from oc_native_compat import ensure_oc_native_support
from opencode_transcript import capture_source, sync_session_spool
from paths import (
    community_config_path,
    config_path,
    engine_config_path,
    opencode_db_path,
    read_json,
    state_dir,
    transcript_spool_dir,
)

ensure_oc_native_support()

MAX_INPUT = 512 * 1024
LOG_LIMIT = 1024 * 1024


def log_path(config=None) -> Path:
    return state_dir(config) / "capture.log"


def append_log(value: dict, *, config=None) -> None:
    path = log_path(config)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_suffix(".1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    except TimeoutError:
        raise
    except OSError as exc:
        print(
            f"MindIE OpenCode capture receipt write failed: {type(exc).__name__}: {exc}"[:500],
            file=sys.stderr,
        )


def _has_regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def _peek_session_meta_readonly(db_file: Path, session: str) -> tuple[str | None, bool]:
    """Read `(directory, is_subagent_or_forked)` from `opencode.db` without mutating it."""
    uri = f"file:{quote(db_file.resolve().as_posix())}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        row = db.execute(
            "SELECT directory, parent_id, fork_session_id FROM session_v2 WHERE id = ?",
            (session,),
        ).fetchone()
        if row is None:
            return None, False
        directory, parent_id, fork_id = row
        if (isinstance(parent_id, str) and parent_id.strip()) or (isinstance(fork_id, str) and fork_id.strip()):
            return None, True
        if isinstance(directory, str) and os.path.isabs(directory.strip()):
            return str(Path(directory.strip()).resolve()), False
        return None, False
    finally:
        db.close()


def _resolve_verified_project_root(event: dict, session: str, db_file: Path) -> tuple[str | None, bool]:
    """Resolve verified workspace strictly from the current event or `opencode.db` (never stale leases or cwd)."""
    for key in ("directory", "workspaceRoot", "cwd", "project_root"):
        val = event.get(key)
        if isinstance(val, str) and os.path.isabs(val.strip()):
            return str(Path(val.strip()).resolve()), False
    if _has_regular_file(db_file):
        try:
            db_dir, is_child = _peek_session_meta_readonly(db_file, session)
            if is_child:
                return None, True
            if db_dir is not None:
                return db_dir, False
        except TimeoutError:
            raise
        except sqlite3.Error:
            pass
    return None, False


def _ensure_capture_store_ready(eng_file: Path) -> None:
    try:
        eng_doc = read_json(eng_file)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return
    if not (isinstance(eng_doc.get("root"), str) and isinstance(eng_doc.get("domain"), str)):
        return
    from mindie_knowledge.loop.handoff import _store_path, prepare_schema

    if not _store_path(eng_doc).is_file():
        prepare_schema(str(eng_file))


def capture_event(event: dict, *, config=None) -> dict:
    """Validate consent + verified scope first, ensure session lease & store, and hand off to `capture_hook`."""
    eligible, reason = is_eligible_main_session(event)
    if not eligible:
        return dict(stage="inert", reason=reason)
    try:
        session = session_from_hook(event)
    except ValueError:
        return dict(stage="inert", reason="invalid_session")

    comm_file = community_config_path(config)
    if not comm_file.is_file():
        return dict(stage="inert", reason="community_unconfigured")

    from mindie_knowledge.loop import settings as settings_mod

    settings = settings_mod.load(comm_file)
    if not settings.allows_capture():
        block_reason = settings.capture_block_kind() or "disabled"
        if block_reason == "fault":
            return dict(stage="unavailable", reason="authority-unavailable")
        return dict(stage="inert", reason=block_reason)

    db_candidate = event.get("opencodeDb") or event.get("opencode_db")
    db_file = Path(db_candidate) if (isinstance(db_candidate, str) and os.path.isabs(db_candidate)) else opencode_db_path(config)
    explicit_transcript = event.get("transcriptPath") or event.get("transcript_path")
    has_explicit_transcript = (
        isinstance(explicit_transcript, str)
        and os.path.isabs(explicit_transcript)
        and _has_regular_file(Path(explicit_transcript))
    )

    project_root, is_child = _resolve_verified_project_root(event, session, db_file)
    if is_child:
        return dict(stage="inert", reason="subagent_or_forked")
    if project_root is None:
        if not has_explicit_transcript and not _has_regular_file(db_file):
            return dict(stage="unavailable", reason="transcript_unavailable")
        return dict(stage="inert", reason="missing_project_root")

    if not settings.in_scope(project_root):
        return dict(stage="inert", reason="out_of_scope")

    eng_file = engine_config_path(config)
    if not config_path(config).is_file() or not eng_file.is_file():
        return dict(stage="unavailable", reason="profile_unconfigured")
    if is_deactivated(session, config=config):
        return dict(stage="inert", reason="deactivated")

    enabled_at = float(settings.enabled_at) if isinstance(getattr(settings, "enabled_at", None), (int, float)) else 0.0
    lease = ensure_default_lease(session, project_root, not_before=0.0, config=config)
    activated_at = lease.get("activated_at")
    auth_boundary = max(
        enabled_at,
        float(activated_at) if isinstance(activated_at, (int, float)) else 0.0,
    )

    if has_explicit_transcript:
        transcript_file = Path(str(explicit_transcript))
    else:
        if not _has_regular_file(db_file):
            return dict(stage="unavailable", reason="transcript_unavailable")
        try:
            synced = sync_session_spool(
                session,
                db_path=db_file,
                spool_dir=transcript_spool_dir(config),
                not_before=auth_boundary if auth_boundary > 0 else None,
                private_paths=(str(lease.get("project_root") or project_root), str(Path.home())),
                config=config,
            )
        except TimeoutError:
            raise
        except ValueError as exc:
            if "subagent or forked" in str(exc):
                return dict(stage="inert", reason="subagent_or_forked")
            raise
        if synced is None:
            return dict(stage="inert", reason="transcript_unavailable")
        transcript_file = synced

    _ensure_capture_store_ready(eng_file)
    source = capture_source(transcript_file, session, binding=lease)
    event_id = event.get("eventID") or event.get("turn_id") or f"{session}:idle:{int(time.time() * 1000)}"
    forwarded = dict(
        hook_event_name="Stop",
        identity_kind="turn",
        session_id=session,
        turn_id=str(event_id)[:256],
        mindie_activation=lease.get("token"),
        harness="opencode",
        transcript_path=source["transcript_path"],
    )
    from mindie_knowledge.loop.cli import capture_hook

    receipt = capture_hook(str(eng_file), forwarded)
    return receipt if isinstance(receipt, dict) else dict(stage="accepted")


def main(argv: list[str] | None = None) -> int:
    started = time.time()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) >= 2 and args[0] == "--event-json":
            raw = args[1].encode("utf-8", errors="replace")
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("capture envelope exceeds 512 KiB limit")
        event = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        if not isinstance(event, dict):
            raise ValueError("capture envelope must be a JSON object")
        receipt = capture_event(event)
        if receipt.get("stage") != "inert":
            slim = {k: receipt.get(k) for k in ("stage", "reason", "cause", "capture_id", "duplicate")}
            append_log({"time": time.time(), "elapsed": round(time.time() - started, 3), "receipt": slim})
        return 0
    except TimeoutError as exc:
        append_log(
            {
                "time": time.time(),
                "elapsed": round(time.time() - started, 3),
                "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
            }
        )
        return 1
    except Exception as exc:
        append_log(
            {
                "time": time.time(),
                "elapsed": round(time.time() - started, 3),
                "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
