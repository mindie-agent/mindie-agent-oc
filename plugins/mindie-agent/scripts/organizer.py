#!/usr/bin/env python3
"""Isolated OpenCode (`oc`) summary and C6 distillation worker (`organizer.py`).

[Role]
Executes a single bounded model invocation to summarize redacted transcript
blocks (`mindie-summary-request/1`) or perform explicit C6 distillation
(`mindie-distill-request/1`), emitting one `mindie-summary-outcome/1` JSON
envelope on stdout.

[Invariants]
- Exit 0 indicates the outcome envelope was delivered on stdout; never retries
  uncertain/billed model calls (`outcome_unknown`) or substitutes fallback
  summaries.
- Supports `--identity` for `mindie_knowledge.materials.summarizer.validate_identity`.
- Connects first to the Bearer-authenticated loopback summary endpoint
  (`summary-endpoint.json` / `MINDIE_SUMMARY_ENDPOINT`, backed by `ctx.generate.text`
  inside `opencode_host.mjs`), bypassing HTTP proxies for `127.0.0.1` / `localhost`.
- Falls back to standalone `opencode run --standalone --archived --auto` in an
  isolated scratch profile (`summary_scratch.create_profile`) when no loopback
  server is running.

[Timeouts & Locks]
- Uses `mindie_knowledge.loop.summary_budget.native_timeout()` reserving `5.0s`
  for cleanup inside the parent worker deadline.

[Side-Effect Policy]
- Creates and cleans up one private temporary profile; never modifies user
  workspaces, material stores, or publication state directly.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from mindie_knowledge.loop import summary_scratch
from mindie_knowledge.loop.summary_budget import native_timeout
from mindie_knowledge.materials import summarizer

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from oc_native_compat import ensure_oc_native_support
from paths import opencode_cli_bin, opencode_db_path, state_dir

ensure_oc_native_support()

_JSONC_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')


def _loads_jsonc(raw_text: str) -> object:
    cleaned = _JSONC_TOKEN_RE.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", raw_text)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def _seed_scratch_opencode_auth_db(src_db: Path, dst_db: Path) -> None:
    if not src_db.is_file():
        return
    try:
        src = sqlite3.connect(f"file:{src_db.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0)
        try:
            ddl_rows = src.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
            ).fetchall()
            dst_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            dst = sqlite3.connect(str(dst_db), timeout=2.0)
            try:
                with dst:
                    for (ddl,) in ddl_rows:
                        dst.execute(ddl)
                    for tbl in ("migration", "credential", "account", "account_state", "control_account"):
                        if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                            rows = src.execute(f'SELECT * FROM "{tbl}"').fetchall()
                            if rows:
                                ph = ",".join("?" for _ in rows[0])
                                dst.executemany(f'INSERT INTO "{tbl}" VALUES ({ph})', rows)
            finally:
                dst.close()
            os.chmod(dst_db, 0o600)
        finally:
            src.close()
    except TimeoutError:
        raise
    except (OSError, sqlite3.Error):
        pass

SUMMARY_MODEL = os.environ.get("MINDIE_SUMMARY_MODEL") or "default"
SUMMARY_EFFORT = "low"
MAX_DISTILL_REQUEST_BYTES = 512 * 1024
DISTILL_REQUEST_SCHEMA = "mindie-distill-request/1"
CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL | re.IGNORECASE)


class NativeFailure(RuntimeError):
    def __init__(self, message: str, *, reason: str = "native_failure"):
        super().__init__(message)
        self.reason = reason


class NativeStartError(OSError):
    pass


def _strip_code_fence(text: str) -> str:
    match = CODE_FENCE_RE.match(text)
    return match.group(1).strip() if match is not None else text.strip()


def _distill_capability() -> tuple[list[str], Path | None]:
    try:
        from mindie_knowledge.graded import distill
    except ImportError:
        return [], None
    required = ("build_prompt", "decision_schema", "parse_validate_result", "validate_decision")
    if (
        getattr(distill, "REQUEST_SCHEMA", None) != DISTILL_REQUEST_SCHEMA
        or not all(callable(getattr(distill, name, None)) for name in required)
    ):
        return [], None
    return [DISTILL_REQUEST_SCHEMA], Path(distill.__file__)


def identity(model: str | None = None) -> dict:
    selected_model = model or os.environ.get("MINDIE_SUMMARY_MODEL") or SUMMARY_MODEL
    sources = [Path(__file__), Path(summarizer.__file__), Path(summary_scratch.__file__)]
    capabilities, distill_source = _distill_capability()
    if distill_source is not None:
        sources.append(distill_source)
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    implementation["native_command"] = os.environ.get("MINDIE_OC_BIN") or opencode_cli_bin() or "opencode"
    value = summarizer.policy_identity(
        model=selected_model,
        effort=SUMMARY_EFFORT,
        implementation=implementation,
    )
    value["capabilities"] = capabilities
    value["fingerprint"] = summarizer.digest({k: v for k, v in value.items() if k != "fingerprint"})
    return value


def _resolve_endpoint() -> dict | None:
    env_url = os.environ.get("MINDIE_SUMMARY_ENDPOINT")
    if env_url:
        return dict(
            url=env_url,
            token=os.environ.get("MINDIE_SUMMARY_TOKEN", ""),
            model=os.environ.get("MINDIE_SUMMARY_MODEL") or SUMMARY_MODEL,
        )
    ep_file = state_dir() / "summary-endpoint.json"
    if ep_file.is_file():
        try:
            data = json.loads(ep_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("url"), str):
                return data
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return None
    return None


class NativeSummaryModel:
    """Single-call model bridge for LangMem and C6 distillation; never retries."""

    def __init__(self, requested_model: str | None = None):
        self.calls = 0
        self.requested_model = requested_model
        self.raw_result: str | None = None
        self.usage: dict | None = None
        self.usage_known = False
        self.transmitted = False
        self.cleanup_failed = False

    def _call_once(self, prompt_text: str, schema: dict) -> str:
        self.calls += 1
        if self.calls != 1:
            raise NativeFailure("worker attempted more than one model call", reason="native_extra_turn")
        effective_model = self.requested_model or SUMMARY_MODEL
        directory, core_owned = summary_scratch.create_profile(prefix="mindie-oc-summary-")
        try:
            endpoint = _resolve_endpoint()
            if endpoint is not None:
                url = endpoint["url"]
                headers = {"Content-Type": "application/json"}
                if endpoint.get("token"):
                    headers["Authorization"] = f"Bearer {endpoint['token']}"
                ep_model = (
                    effective_model
                    if effective_model and effective_model != "default"
                    else (endpoint.get("model") or SUMMARY_MODEL)
                )
                payload = json.dumps(
                    dict(
                        model=ep_model,
                        prompt=prompt_text,
                        response_schema=schema,
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                budget = native_timeout(max_seconds=120.0)
                opener = (
                    urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    if ("127.0.0.1" in url or "localhost" in url)
                    else urllib.request.build_opener()
                )
                refused_stale_ep = False
                try:
                    self.transmitted = True
                    with opener.open(req, timeout=budget) as resp:
                        raw_bytes = resp.read(summarizer.MAX_RESPONSE_BYTES * 2)
                except urllib.error.HTTPError as exc:
                    raise TimeoutError(f"summary endpoint HTTP {exc.code} after transmission") from exc
                except urllib.error.URLError as exc:
                    if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
                        self.transmitted = False
                        if os.environ.get("MINDIE_SUMMARY_ENDPOINT") or opencode_cli_bin() is None:
                            raise NativeStartError("summary endpoint connection refused") from exc
                        try:
                            (state_dir() / "summary-endpoint.json").unlink(missing_ok=True)
                        except TimeoutError:
                            raise
                        except OSError:
                            pass
                        refused_stale_ep = True
                    else:
                        raise TimeoutError("summary endpoint request failed after transmission") from exc

                if not refused_stale_ep:
                    envelope = json.loads(raw_bytes.decode("utf-8"))
                    if not isinstance(envelope, dict):
                        raise summarizer.SummaryResultError("invalid endpoint response", reason="result_json_invalid")
                    if isinstance(envelope.get("usage"), dict):
                        self.usage = summarizer.clean_usage(envelope["usage"])
                        self.usage_known = True
                    text = envelope.get("result")
                    if isinstance(text, dict):
                        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    if not isinstance(text, str) or not text.strip():
                        raise summarizer.SummaryResultError("empty summary response", reason="result_json_invalid")
                    cleaned = _strip_code_fence(text)
                    self.raw_result = cleaned
                    return cleaned

            cli_bin = opencode_cli_bin()
            if cli_bin is None:
                raise NativeStartError("OpenCode CLI binary is not available and no summary endpoint is configured")

            scratch_cfg = Path(directory) / "xdg-config"
            scratch_state = Path(directory) / "xdg-state"
            scratch_data = Path(directory) / "xdg-data"
            scratch_cache = Path(directory) / "xdg-cache"
            (scratch_cfg / "opencode").mkdir(mode=0o700, parents=True, exist_ok=True)
            (scratch_data / "opencode").mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch_state.mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch_cache.mkdir(mode=0o700, parents=True, exist_ok=True)

            # Copy read-only provider/auth settings into the ephemeral scratch profile
            # without touching or inheriting the host OpenCode SQLite database or plugins.
            scratch_oc_cfg: dict = {"plugin": []}
            orig_cfg_base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
            for cfg_name in ("opencode.json", "opencode.jsonc"):
                orig_oc_cfg = orig_cfg_base / "opencode" / cfg_name
                if orig_oc_cfg.is_file():
                    try:
                        parsed_orig = _loads_jsonc(orig_oc_cfg.read_text(encoding="utf-8"))
                        if isinstance(parsed_orig, dict):
                            for k in ("provider", "model", "small_model"):
                                if k in parsed_orig:
                                    scratch_oc_cfg[k] = parsed_orig[k]
                        break
                    except TimeoutError:
                        raise
                    except (OSError, ValueError):
                        pass
            (scratch_cfg / "opencode" / "opencode.json").write_text(
                json.dumps(scratch_oc_cfg, indent=2) + "\n",
                encoding="utf-8",
            )

            orig_data_base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
            orig_auth = orig_data_base / "opencode" / "auth.json"
            if orig_auth.is_file():
                try:
                    dst_auth = scratch_data / "opencode" / "auth.json"
                    shutil.copyfile(orig_auth, dst_auth)
                    os.chmod(dst_auth, 0o600)
                except TimeoutError:
                    raise
                except OSError:
                    pass
            _seed_scratch_opencode_auth_db(opencode_db_path(), scratch_data / "opencode" / "opencode.db")

            env = dict(os.environ)
            for inherited_key in (
                "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR",
                "OPENCODE_CONFIG_CONTENT",
                "OPENCODE_SESSION_ID",
                "MINDIE_OPENCODE_DB",
            ):
                env.pop(inherited_key, None)
            env["PWD"] = str(directory)
            env["XDG_CONFIG_HOME"] = str(scratch_cfg)
            env["XDG_STATE_HOME"] = str(scratch_state)
            env["XDG_DATA_HOME"] = str(scratch_data)
            env["XDG_CACHE_HOME"] = str(scratch_cache)
            env["OPENCODE_DB"] = str(scratch_data / "opencode" / "opencode.db")

            full_prompt = (
                f"{prompt_text}\n\n"
                "Return ONLY a single valid JSON object matching this JSON Schema (no markdown fences, no commentary):\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
            argv = [cli_bin, "run", "--standalone", "--archived", "--auto"]
            if effective_model and effective_model != "default":
                argv.extend(["-m", effective_model])
            argv.append(full_prompt)

            budget = native_timeout(max_seconds=120.0)
            self.transmitted = True
            proc = bounded_run(
                argv,
                "",
                timeout=budget,
                max_output=summarizer.MAX_RESPONSE_BYTES * 2,
                env=env,
                cwd=directory,
            )
            self.cleanup_failed = proc.cleanup_failed
            text = _strip_code_fence(proc.checked_stdout())
            if not text:
                raise summarizer.SummaryResultError("empty CLI summary response", reason="result_json_invalid")
            self.raw_result = text
            return text
        finally:
            if not core_owned:
                try:
                    shutil.rmtree(directory)
                except TimeoutError:
                    raise
                except OSError:
                    self.cleanup_failed = True

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        data = [dict(role=m.type, content=m.content) for m in messages]
        prompt = summarizer.render_prompt(data)
        expected_ids = [m.id for m in messages if getattr(m, "id", None)]
        schema = summarizer.output_schema(expected_ids or None)
        raw = self._call_once(prompt, schema)
        return AIMessage(content=raw)


def execute_request(request: dict) -> dict:
    started = time.monotonic()
    model = NativeSummaryModel()
    try:
        summarizer.validate_request(request)
        model.requested_model = request["policy_identity"].get("model")
        current_id = identity(model.requested_model)
        if current_id["fingerprint"] != request["policy_identity"].get("fingerprint"):
            return summarizer.outcome(
                request,
                status="failed",
                model_calls=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="configuration",
                error_reason="worker_policy_changed",
                billing_status="not_called",
            )
        if request.get("schema_version") == DISTILL_REQUEST_SCHEMA:
            from mindie_knowledge.graded import distill

            prompt = distill.build_prompt(request)
            schema = distill.decision_schema()
            raw = model._call_once(prompt, schema)
            decision = distill.parse_validate_result(raw, request)
            return summarizer.outcome(
                request,
                status="returned",
                result=decision,
                raw_result=model.raw_result,
                usage=model.usage,
                usage_known=model.usage_known,
                model_calls=model.calls,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                cleanup_failed=model.cleanup_failed,
            )

        result = summarizer.summarize_batch(request, model)
        return summarizer.outcome(
            request,
            status="returned",
            result=result,
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=model.calls,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            cleanup_failed=model.cleanup_failed,
        )
    except NativeStartError:
        return summarizer.outcome(
            request,
            status="failed",
            model_calls=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="native",
            error_reason="native_start_failed",
            cleanup_failed=model.cleanup_failed,
            billing_status="not_called",
        )
    except summarizer.SummaryResultError as exc:
        return summarizer.outcome(
            request,
            status="failed" if not model.transmitted or model.raw_result is not None else "outcome_unknown",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="invalid_result",
            error_reason=exc.reason,
            cleanup_failed=model.cleanup_failed,
        )
    except TimeoutError:
        return summarizer.outcome(
            request,
            status="outcome_unknown" if model.transmitted else "failed",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="deadline",
            error_reason="native_deadline",
            cleanup_failed=model.cleanup_failed,
        )
    except Exception:
        return summarizer.outcome(
            request,
            status="outcome_unknown" if model.transmitted else "failed",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="native",
            error_reason="native_failure",
            cleanup_failed=model.cleanup_failed,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--identity" in argv:
        sys.stdout.buffer.write((json.dumps(identity(), ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    raw = sys.stdin.buffer.read(MAX_DISTILL_REQUEST_BYTES + 1)
    if len(raw) > MAX_DISTILL_REQUEST_BYTES:
        raise SystemExit("summary request exceeds byte limit")
    request = json.loads(raw.decode("utf-8"))
    envelope = execute_request(request)
    sys.stdout.buffer.write((json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
