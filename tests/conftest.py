"""Hermetic isolation fixtures and pre-0.1.5 `knowledge_binding` test shim for `mindie-agent-oc`.

[Role]
Redirects `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, every `XDG_*` root,
`MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`,
`OPENCODE_TEST_HOME`, `REMOTE_DEV_AUTH_DIR`, `UV_CACHE_DIR`, and `TMPDIR` inside
`tmp_path`, and registers a faithful `mindie_harness.runtime.knowledge_binding`
shim only when running locally against `mindie-harness < 0.1.5`.

[Invariants]
- No test reads host `~/.config/*`, `~/.local/share/*`, or host OpenCode state.
- When `mindie-harness >= 0.1.5` is installed, its real `knowledge_binding` is used untouched.

[Timeouts & Locks]
- Filesystem-only setup per test (`< 10ms`).

[Side-Effect Policy]
- Writes strictly inside `pytest`'s `tmp_path`.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
import types
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _install_knowledge_binding_shim_if_needed() -> None:
    if importlib.util.find_spec("mindie_harness.runtime.knowledge_binding") is not None:
        return
    import mindie_harness.runtime as mh_runtime
    from mindie_harness.atomic import atomic_write_json, atomic_write_text, read_json_safe
    from mindie_harness.locks import FileLock, LockTimeoutError
    from mindie_harness.paths import ProfileRef, assert_wal_safe_path

    shim = types.ModuleType("mindie_harness.runtime.knowledge_binding")
    shim.DEFAULT_DOMAIN = "vllm-ascend"
    shim.HOST_TRANSCRIPT_CANDIDATES = {
        "codex": ("scripts/codex_transcript.py", "plugins/mindie-agent/scripts/codex_transcript.py"),
        "cc": ("scripts/transcript.py",),
        "kimi": ("scripts/kimi_transcript.py",),
        "dsh": ("scripts/dsh_transcript.py",),
        "agy": ("scripts/agy_transcript.py", "plugins/mindie-agent/scripts/agy_transcript.py"),
        "wb": ("scripts/wb_transcript.py", "plugins/mindie-agent/scripts/wb_transcript.py"),
        "oc": ("scripts/opencode_transcript.py", "plugins/mindie-agent/scripts/opencode_transcript.py"),
    }

    def _is_inside(candidate: Path, root: Path) -> bool:
        try:
            c_norm = os.path.normcase(str(candidate.resolve(strict=False)))
            r_norm = os.path.normcase(str(root.resolve(strict=False)))
            if os.path.splitdrive(c_norm)[0] != os.path.splitdrive(r_norm)[0]:
                return False
            return os.path.commonpath((c_norm, r_norm)) == r_norm
        except (OSError, ValueError):
            return False

    def deterministic_redactor_path(profile: ProfileRef) -> Path:
        system = platform.system().lower() or ("windows" if os.name == "nt" else sys.platform)
        arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "x64")
        binary = "gitleaks.exe" if os.name == "nt" else "gitleaks"
        return (profile.data_dir / "cache" / "gitleaks" / "8.30.1" / f"{system}-{arch}" / binary).resolve()

    def _resolve_adapter(host: str, p_root: Path, prev_eng: Mapping[str, Any]) -> Path:
        for rel in shim.HOST_TRANSCRIPT_CANDIDATES.get(host, ()):
            cand = (p_root / rel).resolve()
            if cand.is_file():
                return cand
        prev = prev_eng.get("transcript_adapter")
        if isinstance(prev, str) and Path(prev).is_absolute() and Path(prev).is_file():
            return Path(prev).resolve()
        return (SCRIPTS_DIR / "opencode_transcript.py").resolve()

    def ensure_knowledge_binding(
        profile: ProfileRef,
        *,
        plugin_root: Path | None = None,
        python_executable: str | Path | None = None,
        install_redactor: bool = False,
        retire_previous: bool = False,
        include_default_feed: bool = False,
        already_locked: bool = False,
    ) -> dict[str, Any]:
        _ = install_redactor
        from mindie_knowledge.loop import domain_catalog
        from mindie_knowledge.loop.cli import config_at, validate_config
        from mindie_knowledge.loop.handoff import prepare_schema
        from mindie_knowledge.loop.lifecycle import inspect_retirement

        eff_root = Path(plugin_root).resolve() if plugin_root is not None else profile.plugin_root.resolve()
        eff_py = Path(python_executable) if python_executable is not None else Path(sys.executable)
        lock = None
        if not already_locked:
            lock = FileLock(profile.config_path.with_name(f".{profile.config_path.name}.update.lock"), exclusive=True, timeout=5.0)
            try:
                lock.acquire()
            except LockTimeoutError:
                return {"status": "deferred", "reason": "lock_timeout", "reportable": False}
        try:
            raw_cfg = read_json_safe(profile.config_path)
            current_cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
            prev_eng_raw = current_cfg.get("engine_config")
            prev_eng = read_json_safe(prev_eng_raw) if isinstance(prev_eng_raw, str) and prev_eng_raw.strip() else None
            if not isinstance(prev_eng, dict):
                prev_eng = {}
            knowledge_root = (profile.data_dir / "knowledge").resolve(strict=False)
            assert_wal_safe_path(knowledge_root)
            assert_wal_safe_path(profile.admission_db.parent)
            adapter_py = _resolve_adapter(profile.host, eff_root, prev_eng)
            found_gitleaks = shutil.which("gitleaks")
            redactor_bin = str(Path(found_gitleaks).resolve()) if found_gitleaks else str(deterministic_redactor_path(profile))
            comm_path = (profile.config_path.parent / "mindie-community.json").resolve(strict=False)
            consent_path = profile.consent_path.resolve(strict=False)
            admission_path = profile.admission_db.resolve(strict=False)
            default_engine: dict[str, Any] = {
                "root": str(knowledge_root),
                "domain": shim.DEFAULT_DOMAIN,
                "domain_catalog": domain_catalog.SCHEMA,
                "capture_mode": "public-transcript",
                "transcript_adapter": str(adapter_py),
                "redactor_executable": redactor_bin,
                "admission_path": str(admission_path),
                "community_config": str(comm_path),
                "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated", "python": str(eff_py)},
            }
            if include_default_feed:
                default_engine["feeds"] = [domain_catalog.get_domain_config(shim.DEFAULT_DOMAIN).to_feed()]
            validate_config(default_engine)
            default_text = json.dumps(default_engine, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            engines_root = (profile.config_path.parent / "engines" / profile.host).resolve(strict=False)
            authorities_root = (profile.config_path.parent / "authorities" / profile.host).resolve(strict=False)
            default_hash = hashlib.sha256(default_text.encode("utf-8")).hexdigest()[:20]
            default_engine_path = engines_root / default_hash / f"{shim.DEFAULT_DOMAIN}.engine.json"
            if not default_engine_path.is_file() or default_engine_path.read_text(encoding="utf-8") != default_text:
                atomic_write_text(default_engine_path, default_text)
            prepare_schema(default_engine_path)

            sanitized_routes: dict[str, str] = {}
            raw_routes = current_cfg.get("domain_configs")
            if isinstance(raw_routes, Mapping):
                for dom, route_raw in raw_routes.items():
                    if not isinstance(dom, str) or dom == shim.DEFAULT_DOMAIN or not isinstance(route_raw, str) or not Path(route_raw).is_absolute():
                        continue
                    route_path = Path(route_raw)
                    if not route_path.is_file() or _is_inside(route_path, profile.generations_dir):
                        continue
                    try:
                        if inspect_retirement(route_path) is None and config_at(route_path).get("domain") == dom:
                            sanitized_routes[dom] = str(route_path.resolve())
                    except TimeoutError:
                        raise
                    except Exception:
                        continue

            catalog_report = domain_catalog.prepare(
                {"engine_config": str(default_engine_path), "domain_configs": sanitized_routes},
                default_engine,
                config_directory=str(engines_root),
                authority_directory=str(authorities_root),
                consent_config=str(consent_path),
                public_feeds=True,
            )
            new_routes = dict(catalog_report["domain_configs"])
            new_routes[shim.DEFAULT_DOMAIN] = str(default_engine_path)

            retirement_report = None
            if retire_previous:
                retire_prev_routes: dict[str, str] = {}
                if isinstance(raw_routes, Mapping):
                    for dom, route_raw in raw_routes.items():
                        if isinstance(dom, str) and isinstance(route_raw, str) and Path(route_raw).is_file():
                            try:
                                if config_at(route_raw).get("domain") == dom:
                                    retire_prev_routes[dom] = str(Path(route_raw).resolve())
                            except TimeoutError:
                                raise
                            except Exception:
                                continue
                retire_doc: dict[str, Any] = {"domain_configs": retire_prev_routes}
                if isinstance(prev_eng_raw, str) and Path(prev_eng_raw).is_file():
                    try:
                        config_at(prev_eng_raw)
                        retire_doc["engine_config"] = str(Path(prev_eng_raw).resolve())
                    except TimeoutError:
                        raise
                    except Exception:
                        pass
                retirement_report = domain_catalog.retire_changed(
                    retire_doc,
                    {"engine_config": str(default_engine_path), "domain_configs": new_routes},
                    timeout=15.0,
                    include_default=True,
                )
            binding_fields = {
                "host": profile.host,
                "engine_config": str(default_engine_path),
                "domain_configs": new_routes,
                "community_config": str(comm_path),
                "consent_config": str(consent_path),
                "admission_path": str(admission_path),
            }
            if not already_locked:
                merged_cfg = dict(current_cfg)
                merged_cfg.setdefault("python", str(eff_py))
                merged_cfg.setdefault("source_root", str(eff_root))
                merged_cfg.update(binding_fields)
                merged_cfg["updated_at"] = time.time()
                atomic_write_json(profile.config_path, merged_cfg)
            return {
                "status": "configured",
                "engine_config": str(default_engine_path),
                "domain_configs": new_routes,
                "domains": sorted(new_routes),
                "transcript_adapter": str(adapter_py),
                "standalone_adapter": False,
                "redactor_executable": redactor_bin,
                "binding_fields": binding_fields,
                "retirement": retirement_report,
                "reportable": False,
            }
        finally:
            if lock is not None:
                lock.release()

    def _authorized_scope(profile: ProfileRef, event: Mapping[str, Any]) -> tuple[str | None, str]:
        cfg = read_json_safe(profile.config_path)
        cfg_doc = cfg if isinstance(cfg, Mapping) else {}
        comm_raw = cfg_doc.get("community_config")
        comm_path = Path(comm_raw) if isinstance(comm_raw, str) and os.path.isabs(comm_raw) else (profile.config_path.parent / "mindie-community.json")
        comm = read_json_safe(comm_path)
        if not isinstance(comm, Mapping) or comm.get("schema") != "mindie-community-config/1" or comm.get("enabled") is not True:
            return None, "sharing-disabled"
        cons_raw = comm.get("consent_config") or cfg_doc.get("consent_config")
        cons_path = Path(cons_raw) if isinstance(cons_raw, str) and os.path.isabs(cons_raw) else profile.consent_path
        cons = read_json_safe(cons_path)
        if (
            not isinstance(cons, Mapping)
            or cons.get("schema") != "mindie-consent/1"
            or cons.get("choice") not in {"contribute", "local-only"}
            or not isinstance(cons.get("choice_revision"), str)
            or not cons["choice_revision"].strip()
        ):
            return None, "sharing-disabled"
        roots_raw = comm.get("project_roots")
        if not isinstance(roots_raw, list) or not roots_raw:
            return None, "sharing-disabled"
        roots = [Path(r).expanduser().resolve(strict=False) for r in roots_raw if isinstance(r, str) and os.path.isabs(r)]
        for key in ("cwd", "workspace", "workspace_root", "project_root", "directory"):
            item = event.get(key)
            if isinstance(item, str) and item.strip() and os.path.isabs(item):
                cand = Path(item).expanduser().resolve(strict=False)
                for root in roots:
                    if _is_inside(cand, root):
                        return str(root), "authorized"
                return None, "out-of-scope"
        return None, "missing-cwd"

    def run_stop_capture(
        profile: ProfileRef,
        event: Mapping[str, Any],
        *,
        plugin_root: Path | None = None,
        timeout_sec: float = 6.0,
    ) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            return {"stage": "inert", "reason": "not-stop"}
        if event.get("stop_hook_active", False) is not False:
            return {"stage": "inert", "reason": "recursive-stop"}
        matched_root, reason = _authorized_scope(profile, event)
        if matched_root is None:
            return {"stage": "inert", "reason": reason}
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import config_at
        from mindie_knowledge.loop.handoff import accept_stop

        session_id = str(event.get("session_id") or "").strip()
        if not session_id:
            return {"stage": "rejected", "reason": "missing-identity"}
        cfg = read_json_safe(profile.config_path) or {}
        eng_raw = cfg.get("engine_config")
        eng_path = Path(eng_raw) if isinstance(eng_raw, str) and Path(eng_raw).is_file() else None
        if eng_path is None:
            ensured = ensure_knowledge_binding(profile, plugin_root=plugin_root)
            eng_path = Path(ensured["engine_config"])
        engine_cfg = config_at(eng_path)
        deadline = time.monotonic() + max(1.0, float(timeout_sec))
        admission = Admission(engine_cfg["admission_path"])
        inspected = admission.inspect(session_id, deadline=deadline)
        if inspected.get("status") == "inactive":
            return {"stage": "inert", "reason": "revoked"}
        lease = admission.active_lease(session_id, deadline=deadline)
        if (lease is None or not lease.get("entry_enabled")) and inspected.get("status") == "missing":
            lease = admission.activate(session_id, project_root=matched_root, root_session=session_id, not_before=0.0)
        if lease is None or not lease.get("token") or not lease.get("entry_enabled"):
            return {"stage": "inert", "reason": "not-activated"}
        stop_event = dict(event)
        stop_event["hook_event_name"] = "Stop"
        stop_event["session_id"] = session_id
        stop_event.setdefault("harness", profile.host)
        stop_event["mindie_activation"] = str(lease["token"])
        return accept_stop(eng_path, stop_event, deadline=deadline)

    shim.deterministic_redactor_path = deterministic_redactor_path
    shim.ensure_knowledge_binding = ensure_knowledge_binding
    shim.run_stop_capture = run_stop_capture
    shim.has_knowledge_package = lambda _py=None: True
    sys.modules["mindie_harness.runtime.knowledge_binding"] = shim
    setattr(mh_runtime, "knowledge_binding", shim)

    import mindie_harness.mcp.knowledge_rpc as mh_krpc
    from mindie_harness.mcp.receipts import apply_receipt_honesty

    def _v015_try_in_process(operation: str, arguments: dict[str, Any], *, profile: ProfileRef, session_id: str, cwd: Path | None, deadline: Any = None) -> dict[str, Any] | None:
        _ = cwd
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import config_at, ensure_service
        from mindie_knowledge.loop.domain_routes import select_engine_config
        from mindie_knowledge.loop.transport import rpc

        method = operation.removeprefix("knowledge_")
        admission = lease = None
        submitted = False
        stage = "config"
        try:
            adapter_doc = read_json_safe(profile.config_path) or {}
            default = Path(adapter_doc.get("engine_config") or profile.config_path.with_name(profile.config_path.stem + ".engine.json"))
            engine = config_at(default)
            routes = dict(adapter_doc.get("domain_configs") or {})
            routes[engine["domain"]] = str(default.resolve())
            refs = ([arguments["ref"]] if arguments.get("ref") else []) + list(arguments.get("refs") or [])
            selected = select_engine_config(default, domain=arguments.get("domain"), ref=refs[0] if refs else None, domain_configs=routes)
            payload = dict(arguments)
            payload.pop("domain", None)
            if method in {"query", "explain", "feedback", "distill"}:
                payload["_session_id"] = session_id
            if method == "distill":
                stage = "authorization"
                admission = Admission(config_at(selected)["admission_path"])
                lease = admission.active_lease(session_id)
                if not lease or not lease.get("token") or not lease.get("entry_enabled"):
                    return mh_krpc._error_result("knowledge_authorization_required", "Distillation requires existing scoped capture authorization.")
                payload["_activation"] = lease["token"]
            elif method in {"query", "explain", "feedback"}:
                payload["_session_verified"] = True
            stage = "service"
            connection = ensure_service(selected)
            stage = "rpc"
            submitted = True
            raw = rpc(connection, method, payload, timeout=110.0 if method in {"sync", "distill"} else 35.0)
        except TimeoutError:
            raise
        except Exception as exc:
            out = mh_krpc._error_result(f"knowledge_{stage}_failed", f"{type(exc).__name__}: {exc}")
            out["structuredContent"].update(stage=stage, submission_state="sent" if submitted else "not_sent")
            return out
        if method == "distill":
            from mindie_knowledge.graded.distill import result_view

            raw, is_error = result_view(raw)
            result = mh_krpc._normalize_result(raw)
            result["isError"] = is_error
        else:
            result = mh_krpc._normalize_result(raw if isinstance(raw, Mapping) else {"results": raw})
        if admission is not None and lease is not None:
            try:
                admission.finish(session_id, lease["token"], not result["isError"])
            except Exception as exc:
                if result["isError"] is not True:
                    result = apply_receipt_honesty(result, exc)
        return result

    mh_krpc._try_in_process = _v015_try_in_process


_install_knowledge_binding_shim_if_needed()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    return PLUGIN_ROOT


@pytest.fixture()
def isolated_oc_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    mindie_cfg = tmp_path / "mindie-config"
    mindie_data = tmp_path / "mindie-data"
    mindie_diag = tmp_path / "mindie-diag"
    xdg_cfg, xdg_data, xdg_state, xdg_cache = (tmp_path / name for name in ("xdg-config", "xdg-data", "xdg-state", "xdg-cache"))
    workspace = tmp_path / "workspace"
    opencode_db = xdg_data / "opencode" / "opencode.db"
    appdata, localappdata = tmp_path / "appdata" / "Roaming", tmp_path / "appdata" / "Local"
    for directory in (home, appdata, localappdata, mindie_cfg, mindie_data, mindie_diag, xdg_cfg / "opencode", xdg_data / "opencode", xdg_state, xdg_cache, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    env_map = {
        "HOME": home, "USERPROFILE": home, "APPDATA": appdata, "LOCALAPPDATA": localappdata,
        "MINDIE_CONFIG_HOME": mindie_cfg, "MINDIE_DATA_HOME": mindie_data,
        "MINDIE_DIAGNOSTICS_ROOT": mindie_diag, "MINDIE_DIAGNOSTICS_CONFIG": mindie_cfg / "diagnostics.json",
        "MINDIE_OPENCODE_DB": opencode_db, "MINDIE_OC_PYTHON": python,
        "OC_PLUGIN_ROOT": PLUGIN_ROOT,
        "XDG_CONFIG_HOME": xdg_cfg, "XDG_DATA_HOME": xdg_data, "XDG_STATE_HOME": xdg_state, "XDG_CACHE_HOME": xdg_cache,
        "XDG_RUNTIME_DIR": xdg_state / "runtime", "OPENCODE_TEST_HOME": home,
        "REMOTE_DEV_AUTH_DIR": tmp_path / "remote-auth", "UV_CACHE_DIR": tmp_path / "uv-cache",
        "TMPDIR": tmp_path / "tmp", "TEMP": tmp_path / "tmp", "TMP": tmp_path / "tmp",
    }
    for key, value in env_map.items():
        if key in {"REMOTE_DEV_AUTH_DIR", "UV_CACHE_DIR", "TMPDIR"}:
            Path(value).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(key, str(value))
    for key in ("MINDIE_AGENT_CONFIG", "MINDIE_OC_ALLOW_BG_UPDATE", "MINDIE_OC_INSTALLED_ROOT", "OPENCODE_SESSION_ID", "MINDIE_SUMMARY_ENDPOINT", "MINDIE_SUMMARY_TOKEN", "MINDIE_SUMMARY_MODEL", "_MINDIE_OC_REEXEC", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "KIMI_SHARE_DIR", "DSH_HOME", "WORKBUDDY_HOME"):
        monkeypatch.delenv(key, raising=False)
    return {
        "root": tmp_path, "home": home, "appdata": appdata, "localappdata": localappdata,
        "mindie_cfg": mindie_cfg, "mindie_data": mindie_data,
        "mindie_diag": mindie_diag, "xdg_cfg": xdg_cfg, "xdg_data": xdg_data, "xdg_state": xdg_state,
        "workspace": workspace, "opencode_db": opencode_db, "python": python,
    }
