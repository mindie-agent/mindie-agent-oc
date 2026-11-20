#!/usr/bin/env python3
"""Pure-stdlib launcher, hook dispatcher, and summary bridge for OpenCode (`oc`).
[Role] Dispatches `hook`, `summary`, `mcp`, `setup`, `sharing`, `doctor`, and `update`; re-execs into the active profile interpreter or cold-provisions via `mindie-harness-seed.pyz`.
[Invariants] Fast paths stay pure stdlib (`session-start`, unknown `hook <op>`, passive `stop`); `eventID` stays `notification` (`turn_id` only from real `turnID`/`turn_id`); rejects `WindowsApps`/0-byte Python stubs and non-terminal/recursive Stop events.
[Timeouts & Locks] `run_stop_capture(..., timeout_sec=6.0)`; summary HTTP 120s; cold seed provisioning bounded to `300s` with captured stdout so MCP `stdout` stays byte-0 clean.
[Side-Effect Policy] Writes only MindIE-owned `pending-update-check.json`, `capture.log`, and authorized transcript spool; never mutates host OpenCode state.
"""
from __future__ import annotations
from collections.abc import Mapping
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT / "scripts") not in sys.path: sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MAX_EVENT, SEED_TIMEOUT_SEC = 512 * 1024, 300.0
TERMINAL_EVENTS = frozenset({"session.idle", "session.turn.completed", "session.execution.completed", "session.execution.idle"})
def _norm_abs(raw: str | Path) -> Path: return Path(os.path.abspath(os.path.expanduser(str(raw))))
def _home_dir(env: Mapping[str, str]) -> Path:
    raw = (env.get("USERPROFILE") or env.get("HOME")) if os.name == "nt" else (env.get("HOME") or env.get("USERPROFILE"))
    return Path(raw).expanduser() if raw else Path.home()
def _config_home(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    if explicit := environ.get("MINDIE_CONFIG_HOME"): return _norm_abs(explicit)
    home = _home_dir(environ)
    base = Path(environ.get("APPDATA") or (home / "AppData" / "Roaming")) if os.name == "nt" else Path(environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return _norm_abs(base / "mindie-agent")
def _data_home(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    if explicit := environ.get("MINDIE_DATA_HOME"): return _norm_abs(explicit)
    home = _home_dir(environ)
    base = Path(environ.get("LOCALAPPDATA") or (home / "AppData" / "Local")) if os.name == "nt" else Path(environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    return _norm_abs(base / "mindie-agent")
def config_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    raw = environ.get("MINDIE_AGENT_CONFIG")
    return _norm_abs(raw) if raw else _config_home(environ) / "oc.json"
def state_dir(env: Mapping[str, str] | None = None) -> Path:
    cfg = _read_json(config_path(env)); raw = cfg.get("state_dir")
    return _norm_abs(raw) if isinstance(raw, str) and os.path.isabs(raw) else _data_home(env) / "state" / "oc"
def _read_json(path: Path) -> dict:
    try: value = json.loads(path.read_text(encoding="utf-8-sig"))
    except TimeoutError: raise
    except (OSError, ValueError): return {}
    return value if isinstance(value, dict) else {}
def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally: tmp.unlink(missing_ok=True)
def _is_usable_python(raw: object, *, is_win: bool | None = None) -> bool:
    win = (os.name == "nt") if is_win is None else is_win
    if not isinstance(raw, str) or not raw.strip() or "\\microsoft\\windowsapps\\" in raw.replace("/", "\\").lower(): return False
    try: st = os.stat(raw); return os.path.isfile(raw) and (not win or st.st_size > 0)
    except TimeoutError: raise
    except OSError: return False
def verify_seed_bundle(plugin_dir: str | Path | None = None) -> dict:
    base = Path(plugin_dir).resolve() if plugin_dir is not None else PLUGIN_ROOT
    pyz, lock_path = base / "assets" / "mindie-harness-seed.pyz", base / "assets" / "seed-lock.json"
    if not lock_path.is_file() or not pyz.is_file(): raise ValueError(f"Missing seed archive or lock under {base / 'assets'}")
    lock = _read_json(lock_path)
    expected_sha, expected_size = str(lock.get("sha256") or "").lower(), lock.get("size_bytes")
    if lock.get("schema") != "mindie-harness-seed/1" or len(expected_sha) != 64 or type(expected_size) is not int or expected_size <= 0:
        raise ValueError(f"Invalid seed-lock schema or digest in {lock_path}")
    digest, size = hashlib.sha256(), 0
    with pyz.open("rb") as stream:
        while chunk := stream.read(65536): digest.update(chunk); size += len(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha: raise ValueError(f"Seed integrity mismatch for {pyz}")
    return lock
def _opencode_db(cfg: dict, env: Mapping[str, str] | None = None) -> Path | None:
    environ = os.environ if env is None else env
    explicit = environ.get("MINDIE_OPENCODE_DB")
    if explicit and os.path.isabs(explicit): return Path(explicit).resolve()
    if isinstance(cfg.get("opencode_db"), str) and os.path.isabs(cfg["opencode_db"]): return Path(cfg["opencode_db"]).resolve()
    native = environ.get("OPENCODE_DB")
    if isinstance(native, str) and native.strip() == ":memory:": return None
    xdg, test_home = environ.get("XDG_DATA_HOME"), environ.get("OPENCODE_TEST_HOME")
    home = Path(test_home).resolve() if test_home and os.path.isabs(test_home) else Path.home()
    base = Path(xdg).resolve() / "opencode" if xdg and os.path.isabs(xdg) else home / ".local" / "share" / "opencode"
    return (base / native.strip()).resolve() if isinstance(native, str) and native.strip() else base / "opencode.db"
def _event(argv: list[str]) -> dict:
    if "--event-json" in argv:
        index = argv.index("--event-json")
        if index + 1 >= len(argv): return {}
        raw = argv[index + 1].encode("utf-8")
    else: raw = sys.stdin.buffer.read(MAX_EVENT + 1)
    if len(raw) > MAX_EVENT: return {}
    try: value = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError): return {}
    return value if isinstance(value, dict) else {}
def _canonical(raw) -> str | None:
    if not isinstance(raw, str) or not os.path.isabs(raw.strip()): return None
    value = os.path.realpath(raw.strip())
    return value.replace("\\", "/").lower() if os.name == "nt" else value
def _valid_stop_envelope(event: dict) -> bool:
    if event.get("parentID") or event.get("forkSessionID") or event.get("stop_hook_active", False) is not False: return False
    etype = event.get("eventType") or event.get("type")
    return not (isinstance(etype, str) and etype.strip() and etype.strip() not in TERMINAL_EVENTS)
def _authorized(event: dict) -> bool:
    if not _valid_stop_envelope(event): return False
    cfg_dir = config_path().parent; community = _read_json(cfg_dir / "mindie-community.json")
    consent = _read_json(Path(community["consent_config"]) if isinstance(community.get("consent_config"), str) and os.path.isabs(community["consent_config"]) else cfg_dir / "mindie-consent.json")
    if community.get("enabled") is not True or consent.get("choice") not in {"contribute", "local-only"}: return False
    raw_roots = community.get("project_roots")
    if not isinstance(raw_roots, list): return False
    candidate = _canonical(event.get("directory") or event.get("workspace_root") or event.get("cwd") or event.get("project_root"))
    roots = [value for value in (_canonical(root) for root in raw_roots) if value]
    return bool(candidate and roots and any(candidate == root or candidate.startswith(root.rstrip("/") + "/") for root in roots))
def _log(receipt: dict) -> None:
    path = state_dir() / "capture.log"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 1024 * 1024: os.replace(path, path.with_suffix(".1"))
        with path.open("a", encoding="utf-8") as stream: stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str)[:4096] + "\n")
    except TimeoutError: raise
    except (OSError, TypeError, ValueError): pass
def _stop(event: dict) -> dict:
    from mindie_harness.paths import ProfileRef
    from mindie_harness.runtime.knowledge_binding import run_stop_capture
    from mindie_harness.sharing import sharing_status
    from mindie_knowledge.loop.activation import Admission
    from opencode_transcript import sync_session_spool
    if not _valid_stop_envelope(event): return {}
    directory = event.get("directory") or event.get("cwd") or event.get("workspace_root") or event.get("project_root")
    profile = ProfileRef.resolve("oc", plugin_root=PLUGIN_ROOT, config_path=config_path(), data_dir=_data_home())
    status = sharing_status(profile, cwd=directory)
    if status.get("tier") not in {"contribute", "local-only"} or not status.get("cwd_in_scope"): return {}
    session = event.get("sessionID") or event.get("session_id")
    if not isinstance(session, str) or not session.strip(): return {}
    session = session.strip()
    if profile.admission_db.is_file() and Admission(profile.admission_db).inspect(session).get("status") == "inactive": return {}
    lease = Admission(profile.admission_db).active_lease(session) if profile.admission_db.is_file() else None
    community = _read_json(profile.config_path.parent / "mindie-community.json")
    enabled_at = community.get("enabled_at") if type(community.get("enabled_at")) in (int, float) else 0.0
    boundary = max(float(enabled_at), float((lease or {}).get("activated_at") or 0.0))
    db_file = _opencode_db(_read_json(profile.config_path))
    if isinstance(event.get("opencodeDb"), str) and os.path.isabs(event["opencodeDb"]) and not os.environ.get("MINDIE_OPENCODE_DB") and os.environ.get("OPENCODE_DB", "").strip() != ":memory:":
        db_file = Path(event["opencodeDb"])
    if db_file is None: return {}
    try: spool = sync_session_spool(session, db_path=db_file, spool_dir=profile.state_dir / "transcripts", not_before=boundary, private_paths=(directory, str(Path.home())), expected_directory=directory)
    except ValueError as exc:
        if "subagent or forked" in str(exc) or "directory mismatch" in str(exc): return {}
        raise
    if spool is None: return {}
    det_turn = next((str(v).strip() for v in (event.get("turn_id"), event.get("turnID")) if isinstance(v, str) and v.strip()), "")
    event_payload: dict[str, object] = dict(hook_event_name="Stop", harness="opencode", session_id=session, cwd=directory, transcript_path=str(spool.resolve()))
    if det_turn: event_payload["identity_kind"] = "turn"; event_payload["turn_id"] = det_turn[:256]
    else:
        notif_id = next((str(v).strip() for v in (event.get("event_id"), event.get("eventID"), event.get("notificationID")) if isinstance(v, str) and v.strip()), "") or f"{session}:idle:{int(time.time() * 1000)}"
        event_payload["identity_kind"] = "notification"; event_payload["event_id"] = notif_id[:256]
    receipt = run_stop_capture(profile, event_payload, plugin_root=PLUGIN_ROOT, timeout_sec=6.0)
    result = receipt if isinstance(receipt, dict) else {"stage": "invalid_receipt"}
    try: _log({"time": time.time(), "receipt": {key: result.get(key) for key in ("stage", "reason", "capture_id", "duplicate")}})
    except TimeoutError: raise
    except Exception as exc: result = {**result, "receipt_unconfirmed": True, "receipt_warning": str(exc)}
    return result
def _summary(argv: list[str]) -> int:
    from mindie_knowledge.materials import summarizer
    if "--identity" in argv:
        implementation = {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        value = summarizer.policy_identity(model=os.environ.get("MINDIE_SUMMARY_MODEL") or "default", effort="low", implementation=implementation)
        value["capabilities"] = []; value["fingerprint"] = summarizer.digest({key: item for key, item in value.items() if key != "fingerprint"})
        sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n"); return 0
    raw = sys.stdin.buffer.read(MAX_EVENT + 1)
    if len(raw) > MAX_EVENT: raise SystemExit("summary request exceeds byte limit")
    request = json.loads(raw.decode("utf-8")); blocks = request.get("blocks") or []
    prompt = summarizer.render_prompt([dict(role="human", content=summarizer.canonical({"block_id": block["block_id"], "text": block["text"]})) for block in blocks])
    endpoint = _read_json(state_dir() / "summary-endpoint.json")
    url = os.environ.get("MINDIE_SUMMARY_ENDPOINT") or endpoint.get("url")
    token = os.environ.get("MINDIE_SUMMARY_TOKEN") or endpoint.get("token") or ""
    started, transmitted = time.monotonic(), False
    try:
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"): raise OSError("summary endpoint is unavailable")
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl): return None
        payload = json.dumps(dict(model=os.environ.get("MINDIE_SUMMARY_MODEL") or endpoint.get("model") or "default", prompt=prompt, response_schema=summarizer.output_schema([block["block_id"] for block in blocks])), ensure_ascii=False).encode()
        request_obj = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}, method="POST")
        transmitted = True
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request_obj, timeout=120.0) as response:
            envelope = json.loads(response.read(summarizer.MAX_RESPONSE_BYTES * 2).decode("utf-8"))
        text = envelope.get("result") if isinstance(envelope, dict) else None
        parsed = json.loads(text) if isinstance(text, str) else text
        outcome = summarizer.outcome(request, status="returned", result=parsed, raw_result=json.dumps(parsed) if parsed is not None else None, usage=envelope.get("usage") if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict) else None, usage_known=isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict), model_calls=1, elapsed_ms=int((time.monotonic() - started) * 1000))
    except urllib.error.HTTPError as exc:
        called = exc.code >= 500
        outcome = summarizer.outcome(request, status="outcome_unknown" if called else "failed", model_calls=1 if called else 0, elapsed_ms=int((time.monotonic() - started) * 1000), error="deadline" if called else "configuration", error_reason="native_deadline" if called else "configuration_invalid")
    except (urllib.error.URLError, ConnectionError) as exc:
        sent = transmitted and not isinstance(getattr(exc, "reason", None), ConnectionRefusedError)
        outcome = summarizer.outcome(request, status="outcome_unknown" if sent else "failed", model_calls=1 if sent else 0, elapsed_ms=int((time.monotonic() - started) * 1000), error="deadline", error_reason="native_deadline")
    except TimeoutError:
        outcome = summarizer.outcome(request, status="outcome_unknown" if transmitted else "failed", model_calls=1 if transmitted else 0, elapsed_ms=int((time.monotonic() - started) * 1000), error="deadline", error_reason="native_deadline")
    except Exception:
        outcome = summarizer.outcome(request, status="outcome_unknown" if transmitted else "failed", model_calls=1 if transmitted else 0, elapsed_ms=int((time.monotonic() - started) * 1000), error="native", error_reason="native_failure")
    sys.stdout.write(json.dumps(outcome, ensure_ascii=False) + "\n"); return 0
def _enqueue() -> None:
    try: _write_json(state_dir() / "pending-update-check.json", {"schema": "mindie-pending-update/1", "host": "oc", "reason": "session_start", "requested_at": time.time()})
    except TimeoutError: raise
    except OSError: pass
def _norm_exe(raw: str | Path) -> Path:
    p = Path(raw).expanduser(); return p.parent.resolve() / p.name
def _stdio() -> dict:
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except TimeoutError: raise
    except Exception: return {}
def _validated_python(cfg: dict) -> str | None:
    if isinstance(rel := cfg.get("component_release"), str) and rel:
        rj = _read_json(Path(rel) / "runtime.json")
        if (rj.get("validation") or {}).get("status") == "validated" and _is_usable_python(rj.get("python")): return str(rj["python"])
    if _is_usable_python(cfg.get("python")): return str(cfg["python"])
    best_at, best_py = -1.0, None
    for root in (_data_home() / "runtimes" / "oc" / "generations", _data_home() / "runtimes" / "oc"):
        if not root.is_dir(): continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith(".") or child.name == "generations": continue
            rj = _read_json(child / "runtime.json"); val = rj.get("validation") or {}
            stamp = float(val.get("validated_at") or 0.0) if type(val.get("validated_at")) in (int, float) else 0.0
            if val.get("status") == "validated" and _is_usable_python(rj.get("python")) and stamp >= best_at: best_at, best_py = stamp, str(rj["python"])
    return best_py
def _reexec(argv: list[str], cfg: dict) -> int | None:
    if os.environ.get("_MINDIE_OC_REEXEC") == "1": return None
    readonly = argv and argv[0] in {"setup", "sharing", "doctor", "update"} and any(t in {"-h", "--help"} or t.startswith("--dry") for t in argv)
    env_py = os.environ.get("MINDIE_OC_PYTHON")
    target = env_py if _is_usable_python(env_py) else _validated_python(cfg)
    if _is_usable_python(target) and _norm_exe(str(target)) != _norm_exe(sys.executable):
        return subprocess.run([str(target), "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv], **_stdio(), env=dict(os.environ, _MINDIE_OC_REEXEC="1"), creationflags=CREATE_NO_WINDOW).returncode
    if _is_usable_python(target) or readonly: os.environ["_MINDIE_OC_REEXEC"] = "1"; return None
    seed = PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz"; verify_seed_bundle()
    parent = PLUGIN_ROOT.parents[1]; provision_root = parent if (parent / "pyproject.toml").is_file() else PLUGIN_ROOT
    try:
        proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(seed), "--host", "oc", "--plugin-root", str(provision_root), "--config", str(config_path())], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SEED_TIMEOUT_SEC, env=dict(os.environ, PYTHONIOENCODING="utf-8"), creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"seed provisioning timed out after {SEED_TIMEOUT_SEC:.0f}s\n"); return 124
    if proc.returncode != 0: sys.stderr.write((proc.stderr or proc.stdout or "seed provisioning failed")[-2000:]); return proc.returncode
    python = None
    try:
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
        if isinstance(parsed, dict): python = parsed.get("python")
    except (ValueError, IndexError): pass
    if not _is_usable_python(python): python = _validated_python(_read_json(config_path()))
    if not _is_usable_python(python): return 1
    return subprocess.run([str(python), "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv], **_stdio(), env=dict(os.environ, _MINDIE_OC_REEXEC="1"), creationflags=CREATE_NO_WINDOW).returncode
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv: raise SystemExit("mindie_launch requires: hook | summary | mcp | setup | sharing | doctor | update")
    if argv[0] == "hook":
        op = argv[1].lower().replace("_", "-") if len(argv) > 1 else ""
        if op == "session-start": _enqueue(); sys.stdout.write("{}\n"); return 0
        if op != "stop": sys.stdout.write("{}\n"); return 0
        event = _event(argv)
        if not _authorized(event): sys.stdout.write("{}\n"); return 0
        argv = ["hook", "stop", "--event-json", json.dumps(event, ensure_ascii=False)]
    if argv[0] in {"hook", "summary", "mcp", "setup", "sharing", "doctor", "update"}:
        code = _reexec(argv, _read_json(config_path()))
        if code is not None: return code
    if argv[0] == "hook": _stop(_event(argv)); sys.stdout.write("{}\n"); return 0
    if argv[0] == "summary": return _summary(argv[1:])
    if argv[0] == "mcp": import mcp_server; return mcp_server.main(argv[1:])
    import setup; return setup.main(argv[1:] if argv[0] == "setup" else argv)
if __name__ == "__main__": raise SystemExit(main())
