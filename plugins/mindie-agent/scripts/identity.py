"""OpenCode (`oc`) native session/gesture validator and one-shot MCP bind store.

[Role]
Validates OpenCode v2 session identifiers (`ses_...` and UUIDs), parses
`/mindie-agent` and `$mindie-agent` manual activation gestures, maps OpenCode
wire MCP tool names (`mindie-remote-dev_*`, `mindie-tools_*`) to MindIE surfaces,
and manages one-shot SQLite WAL binds connecting `tool.hook("execute.before")`
to `mcp_server.py`.

[Invariants]
- Native task identity comes strictly from OpenCode's host-verified `sessionID`
  or `OPENCODE_SESSION_ID` in the environment—never from model-controlled tool
  arguments (`session_id`, `sessionId`, `sessionID`, `conversation_id`,
  `conversationId` in `arguments` are rejected).
- `publish_bind` binds `(surface, local_tool, args_digest)` to `(session_id,
  call_id, workspace_root)` with an 1800s TTL (`>= 2 * 660s` queued MCP budget);
  `claim_bind` consumes each bind at most once under `BEGIN IMMEDIATE`.

[Timeouts & Locks]
- Opens `mcp-binds.sqlite3` with `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=5000` (`timeout=5.0`) once per connection; no `.owner`
  `lstat` or schema reflection on hot paths.

[Side-Effect Policy]
- Mutates only `state_dir() / "mcp-binds.sqlite3"`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from paths import bind_db_path

SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
GESTURE_RE = re.compile(r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?=\s|$)")
MCP_WIRE_RE = re.compile(
    r"^(?:mcp[_:])?(?:mindie[-_]agent[-_:])?(mindie[-_]remote[-_]dev|mindie[-_]tools)[_:]([A-Za-z0-9_.-]+)\Z"
)
FORBIDDEN_IDENTITY_ARGS = frozenset(
    {"session_id", "sessionId", "sessionID", "conversation_id", "conversationId", "_session_id"}
)
BIND_TTL_SECONDS = 1800.0
MAX_HOOK_BYTES = 16 * 1024 * 1024


def parse_hook(raw: bytes) -> dict:
    if len(raw) > MAX_HOOK_BYTES:
        raise ValueError("native hook payload exceeds 16 MiB byte budget")
    text = raw.decode("utf-8-sig")
    if not text.strip():
        return {}
    event = json.loads(text)
    if not isinstance(event, dict):
        raise ValueError("native hook payload must be a JSON object")
    return event


def require_session(value) -> str:
    if not isinstance(value, str):
        raise ValueError("native OpenCode sessionID is missing or invalid")
    cleaned = value.strip()
    if not SESSION_RE.fullmatch(cleaned) or ".." in cleaned:
        raise ValueError("native OpenCode sessionID is missing or invalid")
    return cleaned


def reject_forged_identity_args(arguments: dict | None) -> None:
    if not isinstance(arguments, dict):
        return
    for key in FORBIDDEN_IDENTITY_ARGS:
        if key in arguments:
            raise ValueError(f"model-supplied session identity key '{key}' is forbidden in tool arguments")


def session_from_hook(event: dict) -> str:
    return require_session(event.get("sessionID") or event.get("session_id") or event.get("conversationId"))


def workspace_from_hook(event: dict) -> str:
    for key in ("directory", "workspaceRoot", "cwd", "project_root"):
        val = event.get(key)
        if isinstance(val, str) and os.path.isabs(val):
            return str(Path(val).resolve())
    paths = event.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and os.path.isabs(paths[0]):
        return str(Path(paths[0]).resolve())
    raise ValueError("native OpenCode hook payload has no absolute workspace directory")


def is_eligible_main_session(event: dict) -> tuple[bool, str]:
    """Verify the session is a top-level OpenCode session (neither subagent nor forked)."""
    parent_id = event.get("parentID") or event.get("parent_id")
    if isinstance(parent_id, str) and parent_id.strip():
        return False, "subagent_session"
    fork_id = event.get("forkSessionID") or event.get("fork_session_id")
    if isinstance(fork_id, str) and fork_id.strip():
        return False, "forked_session"
    return True, "main"


def parse_mindie_gesture(event_or_prompt: dict | str) -> tuple[bool, str]:
    """Return `(is_gesture, remainder)` from a prompt string or hook event."""
    if isinstance(event_or_prompt, str):
        prompt = event_or_prompt
    elif isinstance(event_or_prompt, dict):
        raw_prompt = event_or_prompt.get("prompt")
        if isinstance(raw_prompt, dict):
            prompt = raw_prompt.get("text")
        elif isinstance(raw_prompt, str):
            prompt = raw_prompt
        else:
            prompt = event_or_prompt.get("text") or event_or_prompt.get("lastUserInput")
    else:
        return False, ""
    if not isinstance(prompt, str):
        return False, ""
    prompt = prompt.lstrip("\ufeff")
    match = GESTURE_RE.match(prompt)
    if match is None:
        return False, ""
    return True, prompt[match.end():].strip()


def parse_oc_mcp_tool(tool_name: str, tool_input: object) -> tuple[str, str, dict] | None:
    """Resolve an OpenCode `tool.hook('execute.before')` tool ID to `(surface, local_tool, args)`."""
    if not isinstance(tool_name, str) or not tool_name:
        return None
    match = MCP_WIRE_RE.match(tool_name.strip())
    if match is None:
        return None
    raw_server, local_tool = match.group(1), match.group(2)
    surface = "remote" if "remote" in raw_server else "tools"
    args = dict(tool_input) if isinstance(tool_input, dict) else {}
    return surface, local_tool, args


def canonical_args(arguments: dict | None) -> str:
    cleaned = dict(arguments) if isinstance(arguments, dict) else {}
    cleaned.pop("_mindie_bind_id", None)
    return json.dumps(
        cleaned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def args_digest(arguments: dict | None) -> str:
    return hashlib.sha256(canonical_args(arguments).encode("utf-8")).hexdigest()


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if not path.exists() and not create:
        raise FileNotFoundError(str(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    db.execute("PRAGMA busy_timeout=5000;")
    if create:
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL;")
        db.execute(
            "CREATE TABLE IF NOT EXISTS binds("
            "bind_id TEXT PRIMARY KEY, "
            "session TEXT NOT NULL, "
            "surface TEXT NOT NULL, "
            "local_tool TEXT NOT NULL, "
            "args_digest TEXT NOT NULL, "
            "call_id TEXT NOT NULL, "
            "workspace_root TEXT, "
            "status TEXT NOT NULL, "
            "created REAL NOT NULL, "
            "updated REAL NOT NULL)"
        )
    return db


def publish_bind(
    session_id: str,
    surface: str,
    local_tool: str,
    arguments: dict,
    *,
    call_id: str | None = None,
    workspace_root: str | None = None,
    config=None,
) -> str:
    """Record a one-shot `execute.before` bind for an OpenCode MCP tool call."""
    session_id = require_session(session_id)
    if surface not in {"remote", "tools"}:
        raise ValueError("unknown MindIE MCP surface")
    if not isinstance(local_tool, str) or not local_tool:
        raise ValueError("local_tool must be a nonempty string")
    reject_forged_identity_args(arguments)
    adigest = args_digest(arguments)
    cid = str(call_id).strip() if isinstance(call_id, str) and call_id.strip() else str(uuid.uuid4())
    bind_id = hashlib.sha256(
        f"{session_id}|{surface}|{local_tool}|{adigest}|{cid}".encode("utf-8")
    ).hexdigest()
    now = time.time()
    db = _connect(bind_db_path(config), create=True)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR REPLACE INTO binds VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bind_id,
                    session_id,
                    surface,
                    local_tool,
                    adigest,
                    cid,
                    workspace_root,
                    "bound",
                    now,
                    now,
                ),
            )
            return bind_id
    finally:
        db.close()


def claim_bind(
    surface: str,
    local_tool: str,
    arguments: dict,
    *,
    bind_id: str | None = None,
    config=None,
) -> dict:
    """Atomically claim a matching unexpired bind by `bind_id` or unambiguous FIFO `(surface, local_tool, args)`."""
    if surface not in {"remote", "tools"}:
        raise ValueError("unknown MindIE MCP surface")
    reject_forged_identity_args(arguments)
    adigest = args_digest(arguments)
    path = bind_db_path(config)
    if not path.is_file():
        raise ValueError("no native OpenCode tool bind database exists; call rejected")
    now = time.time()
    cutoff = now - BIND_TTL_SECONDS
    explicit_bind = bind_id.strip() if isinstance(bind_id, str) and bind_id.strip() else None
    db = _connect(path, create=False)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if explicit_bind is not None:
                row = db.execute(
                    "SELECT bind_id, session, workspace_root, call_id "
                    "FROM binds "
                    "WHERE bind_id=? AND surface=? AND local_tool=? AND args_digest=? AND status='bound' AND created>=?",
                    (explicit_bind, surface, local_tool, adigest, cutoff),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"no active execute.before bind found for {surface}.{local_tool} (bind_id mismatch or expired); "
                        "unauthorized or replayed MCP tool call"
                    )
            else:
                rows = db.execute(
                    "SELECT bind_id, session, workspace_root, call_id "
                    "FROM binds "
                    "WHERE surface=? AND local_tool=? AND args_digest=? AND status='bound' AND created>=? "
                    "ORDER BY created ASC, rowid ASC",
                    (surface, local_tool, adigest, cutoff),
                ).fetchall()
                if not rows:
                    raise ValueError(
                        f"no active execute.before bind found for {surface}.{local_tool}; "
                        "unauthorized or replayed MCP tool call"
                    )
                identities = {(r[1], r[2] or "") for r in rows}
                if len(identities) > 1:
                    raise ValueError(
                        f"ambiguous concurrent execute.before binds for {surface}.{local_tool} across sessions; "
                        "correlation token required"
                    )
                row = rows[0]
            claimed_id, session, workspace_root, call_id = row
            db.execute(
                "UPDATE binds SET status='claimed', updated=? WHERE bind_id=?",
                (now, claimed_id),
            )
            return dict(
                bind_id=claimed_id,
                session=session,
                workspace_root=workspace_root,
                call_id=call_id,
            )
    finally:
        db.close()


def finish_bind(bind_id: str, succeeded: bool, *, config=None) -> None:
    path = bind_db_path(config)
    if not path.is_file():
        return
    now = time.time()
    status = "completed" if succeeded else "failed"
    db = _connect(path, create=False)
    try:
        with db:
            db.execute(
                "UPDATE binds SET status=?, updated=? WHERE bind_id=?",
                (status, now, bind_id),
            )
    finally:
        db.close()
