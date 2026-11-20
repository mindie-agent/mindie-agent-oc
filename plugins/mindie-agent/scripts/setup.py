#!/usr/bin/env python3
"""Thin profile, sharing, doctor, update, and release-metadata CLI for OpenCode (`oc`).
[Role] Provisions MindIE-owned `oc.json` and 9-domain engine configs via `ensure_knowledge_binding`, delegates `sharing`/`doctor`/`update` to `mindie_harness`, and validates `release-metadata`.
[Invariants] `--help`/`-h`/`--dry-run` write nothing; holds `.oc.json.update.lock` once (`already_locked=True`), fails closed unless `status == "configured"`, keeps `engine_config` on `vllm-ascend`, and preserves existing `oc.json` keys.
[Timeouts & Locks] Single `.oc.json.update.lock` (`5.0s`) + atomic `os.replace`; runtime supervision delegates to `mindie-harness`.
[Side-Effect Policy] Writes only MindIE-owned config/engine files under `MINDIE_CONFIG_HOME` / `MINDIE_DATA_HOME`; prints JSON to stdout.
"""
from __future__ import annotations
from collections.abc import Mapping
import argparse, ast, json, os, re, shutil, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REPOSITORY = "mindie-agent/mindie-agent-oc"
def _read(path: Path) -> dict:
    try: value = json.loads(path.read_text(encoding="utf-8-sig"))
    except TimeoutError: raise
    except (OSError, ValueError): return {}
    return value if isinstance(value, dict) else {}
def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally: tmp.unlink(missing_ok=True)
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
def _cfg_path(explicit: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    if explicit: return _norm_abs(explicit)
    environ = os.environ if env is None else env
    raw = environ.get("MINDIE_AGENT_CONFIG")
    return _norm_abs(raw) if raw else _config_home(environ) / "oc.json"
def _usable_py(raw: object) -> bool:
    if not isinstance(raw, str) or not raw.strip() or "\\microsoft\\windowsapps\\" in raw.replace("/", "\\").lower(): return False
    try: st = os.stat(raw); return os.path.isfile(raw) and (os.name != "nt" or st.st_size > 0)
    except TimeoutError: raise
    except OSError: return False
def _init(config: str | None, workspace: str | None, domain: str) -> dict:
    from mindie_harness.locks import FileLock
    from mindie_harness.paths import ProfileRef
    from mindie_harness.runtime.knowledge_binding import DEFAULT_DOMAIN, ensure_knowledge_binding
    profile = ProfileRef.resolve("oc", plugin_root=PLUGIN_ROOT, config_path=config)
    profile.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(profile.config_path.with_name(f".{profile.config_path.name}.update.lock"), exclusive=True, timeout=5.0)
    lock.acquire()
    try:
        existing = _read(profile.config_path)
        python = str(existing["python"]) if _usable_py(existing.get("python")) else sys.executable
        res = ensure_knowledge_binding(profile, plugin_root=PLUGIN_ROOT, python_executable=python, install_redactor=False, retire_previous=True, already_locked=True)
        binding_fields = res.get("binding_fields")
        if res.get("status") != "configured" or not isinstance(binding_fields, Mapping):
            raise RuntimeError(f"knowledge binding failed ({res.get('status')}): {res.get('reason') or 'unconfigured'}")
        version = _read(PLUGIN_ROOT / "plugin.json").get("version")
        prev_val = existing.get("product_validation")
        validation = dict(prev_val) if isinstance(prev_val, Mapping) else {}
        validation["native_version"] = version
        if workspace: validation["workspace_root"] = str(Path(workspace).resolve())
        else: validation.setdefault("workspace_root", None)
        source_root = PLUGIN_ROOT.parents[1] if (PLUGIN_ROOT.parents[1] / "package.json").is_file() else PLUGIN_ROOT
        adapter = dict(existing)
        adapter.update(schema=str(existing.get("schema") or "mindie-adapter-config/1"), host="oc", harness="opencode", python=python, runtime_scripts=str(HERE), source_root=str(source_root), state_dir=str(profile.state_dir), domain=DEFAULT_DOMAIN, product_validation=validation)
        if domain and domain != DEFAULT_DOMAIN: adapter["preferred_domain"] = domain
        adapter.update(binding_fields)
        adapter["updated_at"] = __import__("time").time()
        _write(profile.config_path, adapter)
    finally: lock.release()
    return {"schema": "mindie-oc-setup/1", "status": "initialized", "config": str(profile.config_path), "engine_config": str(adapter["engine_config"]), "domain_configs": dict(adapter.get("domain_configs") or {}), "harness": "opencode"}
def _delegate(cmd: str, rest: list[str], config: str | None) -> int:
    from mindie_harness.api import ProfileRef, run_host
    profile = ProfileRef.resolve("oc", plugin_root=PLUGIN_ROOT, config_path=config)
    return run_host(["--host", "oc", "--plugin-root", str(PLUGIN_ROOT), "--config", str(profile.config_path), "--data-dir", str(profile.data_dir), cmd, *rest])
def _has_symbol(path: Path, symbol: str) -> bool:
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    for segment in symbol.split("."):
        node = next((item for item in nodes if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == segment), None)
        if node is None: return False
        nodes = node.body
    return True
def _release_metadata(source: Path, tests: Path | None, revision: str) -> dict:
    from mindie_knowledge.releases import validate_compatibility, validate_release_metadata
    root = source.resolve()
    compatibility = validate_compatibility(_read(root / "harness-compatibility.json"), harness_name="opencode")
    if tests is not None:
        tests_root = tests.resolve()
        for interface, meta in compatibility["interface_registry"].items():
            for reference in meta["code_paths"]:
                rel, _, symbol = reference.partition(":"); target = (root / rel).resolve()
                if not target.is_relative_to(root) or not target.is_file() or (symbol and target.suffix == ".py" and not _has_symbol(target, symbol)):
                    raise ValueError(f"registered code path missing for {interface}: {reference}")
            for reference in meta["test_ids"]:
                rel, _, symbol = reference.partition("::")
                target = (tests_root / Path(rel).relative_to("tests") if Path(rel).parts[:1] == ("tests",) else tests_root / rel).resolve()
                if not symbol or not target.is_relative_to(tests_root) or not target.is_file() or not _has_symbol(target, symbol):
                    raise ValueError(f"registered contract test missing for {interface}: {reference}")
    if not re.fullmatch(r"[0-9a-f]{40}", revision): raise ValueError("release source requires a full 40-char immutable commit SHA")
    payload = dict(schema="mindie-plugin-release/1", version=_read(root / "product-contract.json")["native_version"], source=dict(repository=REPOSITORY, sha=revision), compatibility=compatibility)
    return validate_release_metadata(payload, repository=REPOSITORY, harness_name="opencode")
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure MindIE Agent for OpenCode"); sub = parser.add_subparsers(dest="cmd")
    init = sub.add_parser("init"); init.add_argument("--config", default=None); init.add_argument("--workspace-root", default=None); init.add_argument("--domain", default="vllm-ascend")
    sharing = sub.add_parser("sharing"); sharing.add_argument("--config", default=None); sharing.add_argument("--status", action="store_true")
    for flag in ("--enable", "--local-only", "--disable"): sharing.add_argument(flag, action="store_true")
    for flag in ("--fork", "--account", "--repository"): sharing.add_argument(flag, default=None)
    sharing.add_argument("--project-root", action="append", default=[])
    doctor = sub.add_parser("doctor"); doctor.add_argument("--config", default=None); doctor.add_argument("--cwd", default=None); doctor.add_argument("--probe-remote", action="store_true"); doctor.add_argument("--timeout", type=float, default=None)
    update = sub.add_parser("update"); update.add_argument("--config", default=None); update.add_argument("--force", action="store_true"); update.add_argument("--offline", action="store_true"); update.add_argument("--background", action="store_true"); update.add_argument("--channel-base-url", default=None)
    release = sub.add_parser("release-metadata"); release.add_argument("--source", type=Path, required=True); release.add_argument("--tests", type=Path, default=None); release.add_argument("--revision", required=True)
    return parser
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser(); incoming = list(sys.argv[1:] if argv is None else argv)
    dry = any(token in {"--dry", "--dry-", "--dry-r", "--dry-ru", "--dry-run"} for token in incoming)
    incoming = [token for token in incoming if token not in {"--dry", "--dry-", "--dry-r", "--dry-ru", "--dry-run"}]
    args = parser.parse_args(incoming)
    if dry:
        print(json.dumps({"schema": "mindie-oc-setup/1", "status": "dry-run", "bootstrap": False, "cmd": args.cmd or "init", "uv": shutil.which("uv"), "gh": shutil.which("gh")}, sort_keys=True)); return 0
    if args.cmd in {"sharing", "doctor", "update"}:
        return _delegate(args.cmd, [token for token in incoming if token != args.cmd], getattr(args, "config", None))
    if args.cmd == "release-metadata":
        print(json.dumps(_release_metadata(args.source, args.tests, args.revision), sort_keys=True)); return 0
    print(json.dumps(_init(getattr(args, "config", None), getattr(args, "workspace_root", None), getattr(args, "domain", "vllm-ascend")), sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
