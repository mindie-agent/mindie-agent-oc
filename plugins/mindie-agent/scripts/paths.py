"""Canonical MindIE-owned path resolution for the OpenCode (`oc`) adapter.

[Role]
Resolves MindIE-owned configuration, state, admission, consent, transcript spool,
and runtime paths (`oc.json`, `oc.engine.json`, `mindie-consent.json`,
`admission.sqlite3`, `mcp-binds.sqlite3`, `remote-receipts.sqlite3`), plus
read-only discovery of OpenCode's `opencode.db` and CLI binary.

[Invariants]
- Never mutates files inside `~/.config/opencode` or `~/.local/share/opencode`;
  all MindIE state lives under MindIE-owned directories (`config_home`,
  `data_home`, `state_dir`).
- Atomic file writes use `os.replace()` on regular files in the same directory.

[Timeouts & Locks]
- Pure path resolution and bounded local JSON reads (`<= 64 KiB`); no network
  or subprocess calls.

[Side-Effect Policy]
- Read-only path resolution except when `atomic_write_json` is explicitly called
  on a MindIE-owned target path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

MAX_CONFIG_BYTES = 65536


def plugin_root() -> Path:
    explicit = os.environ.get("OC_PLUGIN_ROOT")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return Path(__file__).resolve().parent.parent


def repo_root(config: str | Path | None = None) -> Path:
    explicit = os.environ.get("MINDIE_OC_SOURCE_ROOT")
    if explicit and os.path.isabs(explicit):
        ep = Path(explicit).resolve()
        if (ep / "pyproject.toml").is_file():
            return ep
    candidate = plugin_root().parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            sr = data.get("source_root")
            if isinstance(sr, str) and os.path.isabs(sr) and (Path(sr) / "pyproject.toml").is_file():
                return Path(sr).resolve()
        except ValueError as exc:
            print(f"MindIE OpenCode config ignored in repo_root ({cfg}): {exc}", file=sys.stderr)
    retained = data_home() / "oc-source"
    if (retained / "pyproject.toml").is_file():
        return retained
    return candidate


def apply_enterprise_transport_env(target_env: dict[str, str] | None = None) -> dict[str, str]:
    """Configure enterprise TLS trust, non-interactive GCM fast-path, and Windows Git longpaths/LF."""
    env = os.environ if target_env is None else target_env
    env.setdefault("UV_SYSTEM_CERTS", "true")
    env.setdefault("UV_NATIVE_TLS", "true")
    env.setdefault("UV_NO_PROGRESS", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    git_pairs = (
        ("core.longpaths", "true"),
        ("core.autocrlf", "false"),
        ("credential.https://github.com.provider", "github"),
    )
    if "GIT_CONFIG_COUNT" not in env:
        env["GIT_CONFIG_COUNT"] = str(len(git_pairs))
        for idx, (k, v) in enumerate(git_pairs):
            env[f"GIT_CONFIG_KEY_{idx}"] = k
            env[f"GIT_CONFIG_VALUE_{idx}"] = v
    else:
        try:
            count = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))
        except ValueError:
            count = 0
        existing_keys = {env.get(f"GIT_CONFIG_KEY_{i}") for i in range(count)}
        for k, v in git_pairs:
            if k not in existing_keys:
                env[f"GIT_CONFIG_KEY_{count}"] = k
                env[f"GIT_CONFIG_VALUE_{count}"] = v
                existing_keys.add(k)
                count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
    return dict(env)


def config_home() -> Path:
    explicit = os.environ.get("MINDIE_CONFIG_HOME")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return (Path.home() / ".config" / "mindie-agent").resolve()


def data_home() -> Path:
    explicit = os.environ.get("MINDIE_DATA_HOME")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return (Path.home() / ".local" / "share" / "mindie-agent").resolve()


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env_path = os.environ.get("MINDIE_AGENT_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return config_home() / "oc.json"


def engine_config_path(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        data = read_json(cfg)
        engine = data.get("engine_config")
        if isinstance(engine, str) and os.path.isabs(engine):
            return Path(engine)
    return cfg.with_name(cfg.stem + ".engine.json")


def consent_path(config: str | Path | None = None) -> Path:
    return config_path(config).parent / "mindie-consent.json"


def community_config_path(config: str | Path | None = None) -> Path:
    return config_path(config).parent / "mindie-community.json"


def diagnostics_config_path(config: str | Path | None = None) -> Path:
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return config_path(config).parent / "diagnostics.json"


def state_dir(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            sd = data.get("state_dir")
            if isinstance(sd, str) and os.path.isabs(sd):
                return Path(sd)
        except ValueError as exc:
            print(f"MindIE OpenCode config ignored in state_dir ({cfg}): {exc}", file=sys.stderr)
    return data_home() / "oc-state"


def admission_path(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        data = read_json(cfg)
        adm = data.get("admission_path")
        if isinstance(adm, str) and os.path.isabs(adm):
            return Path(adm)
    return state_dir(config) / "admission.sqlite3"


def bind_db_path(config: str | Path | None = None) -> Path:
    return state_dir(config) / "mcp-binds.sqlite3"


def remote_receipts_path(config: str | Path | None = None) -> Path:
    return state_dir(config) / "remote-receipts.sqlite3"


def transcript_spool_dir(config: str | Path | None = None) -> Path:
    return state_dir(config) / "transcripts"


def installed_source_root() -> Path | None:
    explicit = os.environ.get("MINDIE_OC_INSTALLED_ROOT")
    if explicit and os.path.isabs(explicit):
        cand = Path(explicit).resolve()
        if (cand / "pyproject.toml").is_file() and (cand / "plugins/mindie-agent/plugin.json").is_file():
            return cand
    return None


def opencode_db_path(config: str | Path | None = None) -> Path:
    """Return OpenCode v2's SQLite database path for strictly read-only inspection."""
    explicit = os.environ.get("MINDIE_OPENCODE_DB")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    if config is not None or config_path(config).is_file():
        try:
            data = read_json(config_path(config))
            custom = data.get("opencode_db")
            if isinstance(custom, str) and os.path.isabs(custom):
                return Path(custom).resolve()
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data and os.path.isabs(xdg_data):
        base = Path(xdg_data).resolve() / "opencode"
    else:
        test_home = os.environ.get("OPENCODE_TEST_HOME")
        home_dir = Path(test_home).resolve() if (test_home and os.path.isabs(test_home)) else Path.home().resolve()
        base = home_dir / ".local" / "share" / "opencode"
    native_db = os.environ.get("OPENCODE_DB")
    if isinstance(native_db, str) and native_db.strip() and native_db.strip() != ":memory:":
        return (base / native_db.strip()).resolve()
    return base / "opencode.db"


def opencode_cli_bin() -> str | None:
    """Discover the local `opencode` CLI binary without modifying PATH."""
    explicit = os.environ.get("MINDIE_OC_BIN")
    if explicit and Path(explicit).is_file():
        return str(Path(explicit).resolve())
    found = shutil.which("opencode")
    if found:
        return found
    for cand in (
        Path.home() / ".opencode" / "bin" / ("opencode.exe" if os.name == "nt" else "opencode"),
        Path.home() / ".local" / "bin" / ("opencode.exe" if os.name == "nt" else "opencode"),
    ):
        if cand.is_file():
            return str(cand.resolve())
    return None


def read_json(path: str | Path) -> dict:
    target = Path(path)
    try:
        st = os.lstat(target)
    except TimeoutError:
        raise
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"MindIE metadata file not found: {target}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"MindIE metadata must be a regular file: {target}")
    if st.st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"MindIE metadata exceeds {MAX_CONFIG_BYTES} byte limit: {target}")
    with target.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"MindIE metadata exceeds {MAX_CONFIG_BYTES} byte limit: {target}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"MindIE metadata is malformed JSON: {target}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"MindIE metadata must be a JSON object: {target}")
    return parsed


def atomic_write_json(path: str | Path, payload: dict) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(serialized)
            stream.flush()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass
        raise
    return target
