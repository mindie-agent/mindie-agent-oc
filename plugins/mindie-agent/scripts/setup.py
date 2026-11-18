#!/usr/bin/env python3
"""Thin profile, sharing, doctor, update, and release-metadata CLI for OpenCode (`oc`).
[Role] Writes MindIE-owned `oc.engine.json`/`oc.json`, delegates `sharing`/`doctor`/`update` to `mindie_harness`, and validates `release-metadata`.
[Invariants] `--help`/`-h`/`--dry-run` write nothing; `~/.config/opencode` and `~/.local/share/opencode` are never touched; release metadata requires a full 40-char SHA.
[Timeouts & Locks] Local atomic `os.replace` writes; runtime bootstrap, supervision, and generation locks are delegated to `mindie-harness`.
[Side-Effect Policy] Writes only MindIE-owned config under `MINDIE_CONFIG_HOME`; prints release metadata to stdout.
"""
from __future__ import annotations
import argparse, ast, json, os, re, shutil, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REPOSITORY = "mindie-agent/mindie-agent-oc"
def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
def _config_home() -> Path:
    raw = os.environ.get("MINDIE_CONFIG_HOME")
    return Path(raw).resolve() if raw and os.path.isabs(raw) else Path.home() / ".config" / "mindie-agent"
def _data_home() -> Path:
    raw = os.environ.get("MINDIE_DATA_HOME")
    return Path(raw).resolve() if raw and os.path.isabs(raw) else Path.home() / ".local" / "share" / "mindie-agent"
def _cfg_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    raw = os.environ.get("MINDIE_AGENT_CONFIG")
    return Path(raw).resolve() if raw else _config_home() / "oc.json"
def _init(config: str | None, workspace: str | None, domain: str) -> dict:
    cfg_file = _cfg_path(config)
    engine_file = cfg_file.with_name("oc.engine.json")
    state = _data_home() / "state" / "oc"
    python = _read(cfg_file).get("python") or sys.executable
    version = _read(PLUGIN_ROOT / "plugin.json").get("version")
    validation = {"native_version": version, "workspace_root": str(Path(workspace).resolve()) if workspace else None}
    engine = dict(
        schema="mindie-engine-config/1", harness="opencode", domain=domain,
        root=str(_data_home() / "domains"), state_dir=str(state / "engine"),
        admission_path=str(state / "admission.sqlite3"),
        community_config=str(cfg_file.parent / "mindie-community.json"),
        settings_path=str(cfg_file.parent / "mindie-community.json"),
        transcript_adapter=str(HERE / "opencode_transcript.py"),
        summary_command=[python, "-I", "-X", "utf8", str(HERE / "mindie_launch.py"), "summary"],
        redactor_executable=shutil.which("gitleaks"),
        product_validation=validation,
    )
    adapter = dict(
        schema="mindie-adapter-config/1", harness="opencode", python=python,
        runtime_scripts=str(HERE), source_root=str(PLUGIN_ROOT.parents[1]),
        engine_config=str(engine_file), state_dir=str(state),
        admission_path=str(state / "admission.sqlite3"), domain=domain,
        product_validation=validation,
    )
    _write(engine_file, engine)
    _write(cfg_file, adapter)
    return {"schema": "mindie-oc-setup/1", "status": "initialized", "config": str(cfg_file), "engine_config": str(engine_file), "harness": "opencode"}
def _delegate(cmd: str, rest: list[str], config: str | None) -> int:
    from mindie_harness.api import run_host; return run_host(["--host", "oc", "--plugin-root", str(PLUGIN_ROOT), "--config", str(_cfg_path(config)), "--data-dir", str(_data_home()), cmd, *rest])
def _has_symbol(path: Path, symbol: str) -> bool:
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    for segment in symbol.split("."):
        node = next((item for item in nodes if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == segment), None)
        if node is None:
            return False
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
                rel, _, symbol = reference.partition(":")
                target = (root / rel).resolve()
                if not target.is_relative_to(root) or not target.is_file() or (symbol and target.suffix == ".py" and not _has_symbol(target, symbol)):
                    raise ValueError(f"registered code path missing for {interface}: {reference}")
            for reference in meta["test_ids"]:
                rel, _, symbol = reference.partition("::")
                target = (tests_root / Path(rel).relative_to("tests") if Path(rel).parts[:1] == ("tests",) else tests_root / rel).resolve()
                if not symbol or not target.is_relative_to(tests_root) or not target.is_file() or not _has_symbol(target, symbol):
                    raise ValueError(f"registered contract test missing for {interface}: {reference}")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release source requires a full 40-char immutable commit SHA")
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
    parser = _build_parser()
    incoming = list(sys.argv[1:] if argv is None else argv)
    dry = any(token in {"--dry", "--dry-", "--dry-r", "--dry-ru", "--dry-run"} for token in incoming)
    incoming = [token for token in incoming if token not in {"--dry", "--dry-", "--dry-r", "--dry-ru", "--dry-run"}]
    args = parser.parse_args(incoming)
    if dry:
        print(json.dumps({"schema": "mindie-oc-setup/1", "status": "dry-run", "bootstrap": False, "cmd": args.cmd or "init", "uv": shutil.which("uv"), "gh": shutil.which("gh")}, sort_keys=True)); return 0
    if args.cmd in {"sharing", "doctor", "update"}:
        rest = [token for token in incoming if token != args.cmd]
        return _delegate(args.cmd, rest, getattr(args, "config", None))
    if args.cmd == "release-metadata":
        print(json.dumps(_release_metadata(args.source, args.tests, args.revision), sort_keys=True)); return 0
    print(json.dumps(_init(getattr(args, "config", None), getattr(args, "workspace_root", None), getattr(args, "domain", "vllm-ascend")), sort_keys=True))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
