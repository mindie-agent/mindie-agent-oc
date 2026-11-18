#!/usr/bin/env python3
"""Pure-stdlib launcher, hook dispatcher, and summary bridge for OpenCode (`oc`).
[Role] Dispatches `hook`, `summary`, `mcp`, `setup`, `sharing`, `doctor`, and `update`; re-execs into the active profile interpreter or cold-provisions via `mindie-harness-seed.pyz`.
[Invariants] Fast paths stay pure stdlib (`session-start`, `pre-tool-use`, passive `stop`); passive/entitled stop checks saved consent and `project_roots` before importing runtime packages or SQLite; in-runtime stop preserves explicit revocation and deterministic `turn_id`.
[Timeouts & Locks] `capture_hook(timeout=6.0)`; summary HTTP 120s; seed provisioning follows its own 240s build budget.
[Side-Effect Policy] Writes only MindIE-owned `pending-update-check.json`, `capture.log`, and authorized transcript spool; never mutates host OpenCode state.
"""
from __future__ import annotations
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT / "scripts") not in sys.path: sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MAX_EVENT = 512 * 1024
def _config_home() -> Path:
    raw = os.environ.get("MINDIE_CONFIG_HOME")
    return Path(raw).resolve() if raw and os.path.isabs(raw) else Path.home() / ".config" / "mindie-agent"
def _data_home() -> Path:
    raw = os.environ.get("MINDIE_DATA_HOME")
    return Path(raw).resolve() if raw and os.path.isabs(raw) else Path.home() / ".local" / "share" / "mindie-agent"
def config_path() -> Path:
    raw = os.environ.get("MINDIE_AGENT_CONFIG")
    return Path(raw).resolve() if raw else _config_home() / "oc.json"
def state_dir() -> Path:
    cfg = _read_json(config_path())
    raw = cfg.get("state_dir")
    return Path(raw).resolve() if isinstance(raw, str) and os.path.isabs(raw) else _data_home() / "state" / "oc"
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
    finally:
        tmp.unlink(missing_ok=True)
def verify_seed_bundle(plugin_dir: str | Path | None = None) -> dict:
    base = Path(plugin_dir).resolve() if plugin_dir is not None else PLUGIN_ROOT
    pyz, lock_path = base / "assets" / "mindie-harness-seed.pyz", base / "assets" / "seed-lock.json"
    if not lock_path.is_file() or not pyz.is_file():
        raise ValueError(f"Missing seed archive or lock under {base / 'assets'}")
    lock = _read_json(lock_path)
    expected_sha, expected_size = str(lock.get("sha256") or "").lower(), lock.get("size_bytes")
    if lock.get("schema") != "mindie-harness-seed/1" or len(expected_sha) != 64 or type(expected_size) is not int or expected_size <= 0:
        raise ValueError(f"Invalid seed-lock schema or digest in {lock_path}")
    digest, size = hashlib.sha256(), 0
    with pyz.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
            size += len(chunk)
    if size != expected_size or digest.hexdigest() != expected_sha:
        raise ValueError(f"Seed integrity mismatch for {pyz}")
    return lock
def _opencode_db(cfg: dict) -> Path:
    explicit = os.environ.get("MINDIE_OPENCODE_DB")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    if isinstance(cfg.get("opencode_db"), str) and os.path.isabs(cfg["opencode_db"]):
        return Path(cfg["opencode_db"]).resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).resolve() / "opencode" if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "share" / "opencode"
    native = os.environ.get("OPENCODE_DB")
    return base / native.strip() if isinstance(native, str) and native.strip() and native.strip() != ":memory:" else base / "opencode.db"
def _event(argv: list[str]) -> dict:
    if "--event-json" in argv:
        index = argv.index("--event-json")
        if index + 1 >= len(argv):
            return {}
        raw = argv[index + 1].encode("utf-8")
    else:
        raw = sys.stdin.buffer.read(MAX_EVENT + 1)
    if len(raw) > MAX_EVENT:
        return {}
    try:
        value = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
def _canonical(raw) -> str | None:
    if not isinstance(raw, str) or not os.path.isabs(raw.strip()):
        return None
    value = os.path.realpath(raw.strip())
    return value.replace("\\", "/").lower() if os.name == "nt" else value
def _authorized(event: dict) -> bool:
    if event.get("parentID") or event.get("forkSessionID"):
        return False
    community = _read_json(config_path().parent / "mindie-community.json")
    consent = _read_json(Path(community["consent_config"]) if isinstance(community.get("consent_config"), str) and os.path.isabs(community["consent_config"]) else config_path().parent / "mindie-consent.json")
    if community.get("enabled") is not True or consent.get("choice") not in {"contribute", "local-only"}:
        return False
    raw_roots = community.get("project_roots")
    if not isinstance(raw_roots, list):
        return False
    candidate = _canonical(event.get("directory") or event.get("workspace_root") or event.get("cwd") or event.get("project_root"))
    roots = [value for value in (_canonical(root) for root in raw_roots) if value]
    if candidate is None or not roots:
        return False
    return any(candidate == root or candidate.startswith(root.rstrip("/") + "/") for root in roots)
def _prepare_schema(engine_path: Path) -> None:
    engine = _read_json(engine_path)
    if not isinstance(engine.get("root"), str) or not isinstance(engine.get("domain"), str):
        return
    from mindie_knowledge.loop.handoff import _store_path, prepare_schema
    if not _store_path(engine).is_file():
        prepare_schema(str(engine_path))
def _log(receipt: dict) -> None:
    path = state_dir() / "capture.log"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 1024 * 1024: os.replace(path, path.with_suffix(".1"))
        with path.open("a", encoding="utf-8") as stream: stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True)[:4096] + "\n")
    except TimeoutError: raise
    except OSError: pass
def _stop(event: dict) -> dict:
    from mindie_harness.paths import ProfileRef
    from mindie_harness.sharing import sharing_status
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.cli import capture_hook
    from opencode_transcript import capture_source, sync_session_spool
    if event.get("parentID") or event.get("forkSessionID"): return {}
    directory = event.get("directory")
    profile = ProfileRef.resolve("oc", plugin_root=PLUGIN_ROOT, config_path=config_path(), data_dir=_data_home())
    status = sharing_status(profile, cwd=directory)
    if status.get("tier") not in {"contribute", "local-only"} or not status.get("cwd_in_scope"):
        return {}
    session = event.get("sessionID")
    if not isinstance(session, str) or not session.strip():
        return {}
    admission = Admission(profile.admission_db)
    if admission.inspect(session).get("status") == "inactive":
        return {}
    lease = admission.active_lease(session) or admission.activate(session, project_root=directory, root_session=session, not_before=0.0)
    community = _read_json(config_path().parent / "mindie-community.json")
    enabled_at = community.get("enabled_at") if type(community.get("enabled_at")) in (int, float) else 0.0
    boundary = max(float(enabled_at), float(lease.get("activated_at") or 0.0))
    db_file = _opencode_db(_read_json(config_path()))
    if isinstance(event.get("opencodeDb"), str) and os.path.isabs(event["opencodeDb"]) and not os.environ.get("MINDIE_OPENCODE_DB"):
        db_file = Path(event["opencodeDb"])
    try:
        spool = sync_session_spool(session, db_path=db_file, spool_dir=state_dir() / "transcripts", not_before=boundary, private_paths=(directory, str(Path.home())), expected_directory=directory)
    except ValueError as exc:
        if "subagent or forked" in str(exc) or "directory mismatch" in str(exc):
            return {}
        raise
    if spool is None:
        return {}
    engine_raw = _read_json(config_path()).get("engine_config")
    engine_path = Path(engine_raw) if isinstance(engine_raw, str) and os.path.isabs(engine_raw) else config_path().with_name("oc.engine.json")
    _prepare_schema(engine_path)
    source = capture_source(spool, session, binding=lease)
    forwarded = dict(hook_event_name="Stop", identity_kind="turn", session_id=session, turn_id=str(event.get("eventID") or f"{session}:idle:{int(time.time() * 1000)}")[:256], mindie_activation=lease.get("token"), harness="opencode", transcript_path=source["transcript_path"])
    receipt = capture_hook(str(engine_path), forwarded, timeout=6.0)
    result = receipt if isinstance(receipt, dict) else {"stage": "invalid_receipt"}
    _log({"time": time.time(), "receipt": {key: result.get(key) for key in ("stage", "reason", "capture_id", "duplicate")}})
    return result
def _summary(argv: list[str]) -> int:
    from mindie_knowledge.materials import summarizer
    if "--identity" in argv:
        implementation = {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        value = summarizer.policy_identity(model=os.environ.get("MINDIE_SUMMARY_MODEL") or "default", effort="low", implementation=implementation)
        value["capabilities"] = []
        value["fingerprint"] = summarizer.digest({key: item for key, item in value.items() if key != "fingerprint"})
        sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
        return 0
    raw = sys.stdin.buffer.read(MAX_EVENT + 1)
    if len(raw) > MAX_EVENT: raise SystemExit("summary request exceeds byte limit")
    request = json.loads(raw.decode("utf-8"))
    blocks = request.get("blocks") or []
    prompt = summarizer.render_prompt([dict(role="human", content=summarizer.canonical({"block_id": block["block_id"], "text": block["text"]})) for block in blocks])
    endpoint = _read_json(state_dir() / "summary-endpoint.json")
    url = os.environ.get("MINDIE_SUMMARY_ENDPOINT") or endpoint.get("url")
    token = os.environ.get("MINDIE_SUMMARY_TOKEN") or endpoint.get("token") or ""
    started, transmitted = time.monotonic(), False
    try:
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise OSError("summary endpoint is unavailable")
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
    sys.stdout.write(json.dumps(outcome, ensure_ascii=False) + "\n")
    return 0
def _enqueue() -> None:
    try:
        _write_json(state_dir() / "pending-update-check.json", {"reason": "session_start", "at": time.time()})
    except TimeoutError: raise
    except OSError: pass
def _norm_exe(raw: str | Path) -> Path:
    p = Path(raw).expanduser()
    return p.parent.resolve() / p.name
def _stdio() -> dict:
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except Exception: return {}
def _reexec(argv: list[str], cfg: dict) -> int | None:
    if os.environ.get("_MINDIE_OC_REEXEC") == "1": return None
    readonly = argv and argv[0] in {"setup", "sharing", "doctor", "update"} and any(t in {"-h", "--help"} or t.startswith("--dry") for t in argv)
    target = os.environ.get("MINDIE_OC_PYTHON") if os.path.isfile(os.environ.get("MINDIE_OC_PYTHON", "")) else cfg.get("python")
    if isinstance(target, str) and Path(target).is_file() and _norm_exe(target) != _norm_exe(sys.executable):
        return subprocess.run([target, "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv], **_stdio(), env=dict(os.environ, _MINDIE_OC_REEXEC="1"), creationflags=CREATE_NO_WINDOW).returncode
    if (isinstance(cfg.get("python"), str) and Path(cfg["python"]).is_file()) or readonly: os.environ["_MINDIE_OC_REEXEC"] = "1"; return None
    seed = PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz"
    verify_seed_bundle()
    parent = PLUGIN_ROOT.parents[1]
    provision_root = parent if (parent / "pyproject.toml").is_file() else PLUGIN_ROOT
    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(seed), "--host", "oc", "--plugin-root", str(provision_root), "--config", str(config_path())], capture_output=True, text=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"), creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        sys.stderr.write((proc.stderr or proc.stdout or "seed provisioning failed")[-2000:])
        return proc.returncode
    try:
        python = json.loads(proc.stdout.strip().splitlines()[-1]).get("python")
    except (ValueError, IndexError, AttributeError):
        python = _read_json(config_path()).get("python")
    if not isinstance(python, str) or not Path(python).is_file():
        return 1
    return subprocess.run([python, "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv], **_stdio(), env=dict(os.environ, _MINDIE_OC_REEXEC="1"), creationflags=CREATE_NO_WINDOW).returncode
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires: hook | summary | mcp | setup | sharing | doctor | update")
    if argv[0] == "hook" and len(argv) > 1:
        op = argv[1]
        if op == "session-start": _enqueue(); sys.stdout.write("{}\n"); return 0
        if op == "pre-tool-use": sys.stdout.write("{}\n"); return 0
        if op == "stop":
            event = _event(argv)
            if not _authorized(event):
                sys.stdout.write("{}\n")
                return 0
            argv = ["hook", "stop", "--event-json", json.dumps(event, ensure_ascii=False)]
    if argv[0] in {"hook", "summary", "mcp", "setup", "sharing", "doctor", "update"}:
        code = _reexec(argv, _read_json(config_path()))
        if code is not None:
            return code
    if argv[0] == "hook":
        _stop(_event(argv))
        sys.stdout.write("{}\n")
        return 0
    if argv[0] == "summary":
        return _summary(argv[1:])
    if argv[0] == "mcp":
        import mcp_server
        return mcp_server.main(argv[1:])
    import setup
    return setup.main(argv[1:] if argv[0] == "setup" else argv)
if __name__ == "__main__":
    raise SystemExit(main())
