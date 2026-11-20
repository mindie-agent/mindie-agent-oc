"""OpenCode v2 plugin loading, path parity, setup preservation, and release metadata tests.

[Role]
Verifies manifests (`v0.1.12`), `harness-compatibility.json` references, `.mcp.json`
`uv` launcher, seed integrity, `release-metadata`, module headers/LF, Windows
`APPDATA`/`LOCALAPPDATA` & POSIX `XDG_*` path parity, `pending-update-check.json`
schema, `setup.py` 9-domain key preservation, and pure-Node host hooks.

[Invariants]
- Compatibility `code_paths`/`test_ids` resolve to real files and symbols.
- `opencode_host.mjs`, `mindie_launch.py`, `setup.py`, and `ProfileRef.resolve("oc")`
  resolve identical config/data paths on Windows and POSIX.
- Subagent/forked sessions receive zero skill context; `_mindie_bind_id` never
  survives a MindIE MCP tool input; every child spawn is windowless.

[Timeouts & Locks]
- Bounded Node drivers (`< 15s`) plus local JSON/AST inspection.

[Side-Effect Policy]
- Writes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

import mindie_launch
import setup as oc_setup

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
HEADERS = ("[Role]", "[Invariants]", "[Timeouts & Locks]", "[Side-Effect Policy]")
EXPECTED_DOMAINS = {
    "vllm-ascend",
    "operators",
    "xllm",
    "mindie-sd",
    "infer-core",
    "infer-distributed",
    "infer-observability",
    "infer-ops-compat",
    "agent-engineering",
}


def test_plugin_manifests_and_opencode_v2_loading(isolated_oc_env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    import tomllib

    pkg = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    contract = json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))
    plugin_json = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    compat = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))
    pyproject_doc = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = pyproject_doc["version"]
    assert pyproject_doc["requires-python"] == ">=3.11,<3.15"
    assert "mindie-harness>=0.1.6" in pyproject_doc["dependencies"]
    assert pkg["name"] == "@mindie-agent/oc" and pkg["main"] == "./index.mjs"
    assert pkg["version"] == contract["native_version"] == plugin_json["version"] == version == "0.1.12"
    assert contract["harness"] == compat["harness"] == "opencode"
    assert contract["components"]["mindie-harness"] == "0.1.6"
    assert len(contract["components"]) == 9

    from mindie_knowledge.releases import validate_compatibility

    validated = validate_compatibility(compat, harness_name="opencode")
    assert validated["harness"] == "opencode"
    for interface in validated["used_interfaces"]:
        entry = validated["interface_registry"][interface]
        for reference in entry["code_paths"]:
            rel, _, symbol = reference.partition(":")
            target = REPO_ROOT / rel
            assert target.is_file(), f"missing code_path {reference}"
            if symbol and target.suffix == ".py":
                tree = ast.parse(target.read_text(encoding="utf-8"))
                assert symbol in {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for reference in entry["test_ids"]:
            rel, _, symbol = reference.partition("::")
            target = REPO_ROOT / rel
            assert target.is_file(), f"missing test file {reference}"
            assert symbol in {node.name for node in ast.walk(ast.parse(target.read_text(encoding="utf-8"))) if isinstance(node, ast.FunctionDef)}

    files = [REPO_ROOT / "index.mjs", PLUGIN_ROOT / "opencode_host.mjs", *sorted((PLUGIN_ROOT / "scripts").glob("*.py"))]
    for path in files:
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        lines = raw.decode("utf-8").splitlines()
        head = "\n".join(lines[:30])
        for marker in HEADERS:
            assert marker in head, f"{path.name} missing {marker}"
    assert sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files) <= 1200

    host = (PLUGIN_ROOT / "opencode_host.mjs").read_text(encoding="utf-8")
    assert "windowsHide: true" in host and "detached: true" not in host
    assert 'detached: process.platform !== "win32"' in host
    assert "paths.mjs" not in host and "session_support.mjs" not in host
    assert "knowledge_cli.py" not in host and "rsi_loop.py" not in host

    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert mcp["mindie-remote-dev"]["command"] == "uv"
    assert mcp["mindie-tools"]["command"] == "uv"
    assert mcp["mindie-remote-dev"]["args"] == ["run", "--no-project", "--python", "3.13", "python", "-B", "-X", "utf8", "${OC_PLUGIN_ROOT}/scripts/mindie_launch.py", "mcp", "remote"]
    assert mcp["mindie-tools"]["args"] == ["run", "--no-project", "--python", "3.13", "python", "-B", "-X", "utf8", "${OC_PLUGIN_ROOT}/scripts/mindie_launch.py", "mcp", "tools"]
    assert mcp["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "oc"

    lock = mindie_launch.verify_seed_bundle(PLUGIN_ROOT)
    assert lock["schema"] == "mindie-harness-seed/1" and lock["version"] == "0.1.6"

    skill = (PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]
    assert re.search(r'^name:\s*"?mindie-agent"?$', frontmatter, re.MULTILINE)
    assert "disable-model-invocation" not in skill and "/mindie-agent" not in skill and "$mindie-agent" not in skill
    for forbidden in ("workspace-setup.md", "github-authentication.md", "user-notice.md", "(out_tensor, metrics_dict)"):
        for doc in [PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md", *sorted((PLUGIN_ROOT / "skills" / "mindie-agent" / "references").glob("*.md"))]:
            assert forbidden not in doc.read_text(encoding="utf-8")

    assert oc_setup.main(["release-metadata", "--source", str(REPO_ROOT), "--tests", str(REPO_ROOT / "tests"), "--revision", "a" * 40]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mindie-plugin-release/1" and payload["version"] == "0.1.12"
    assert payload["source"]["sha"] == "a" * 40

    assert oc_setup.main(["init", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry-run"


def test_windows_and_posix_path_parity_pending_update_and_setup_preservation(isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from mindie_harness.paths import ProfileRef

    node = shutil.which("node")
    assert node is not None

    monkeypatch.delenv("MINDIE_CONFIG_HOME", raising=False)
    monkeypatch.delenv("MINDIE_DATA_HOME", raising=False)
    monkeypatch.delenv("MINDIE_AGENT_CONFIG", raising=False)

    prof = ProfileRef.resolve("oc", plugin_root=PLUGIN_ROOT)
    assert mindie_launch.config_path() == oc_setup._cfg_path() == prof.config_path
    assert mindie_launch._data_home() == oc_setup._data_home() == prof.data_dir

    parity_driver = isolated_oc_env["root"] / "parity_driver.mjs"
    cold_plugin = isolated_oc_env["root"] / "cold-pkg" / "plugins" / "mindie-agent"
    (cold_plugin / "scripts").mkdir(parents=True)
    parity_driver.write_text(
        f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import {{ configHome, configPath, dataHome, enqueuePendingUpdateCheck, opencodeDbPath, resolveLaunchCommand, resolvePython }} from {json.dumps((PLUGIN_ROOT / "opencode_host.mjs").as_uri())};

const currentCfg = configPath(null, process.env, process.platform);
const currentData = dataHome(process.env, process.platform);
const winExplicit = {{ USERPROFILE: "C:\\\\Users\\\\alice", APPDATA: "D:\\\\RoamingCfg", LOCALAPPDATA: "D:\\\\LocalData" }};
assert.equal(configHome(winExplicit, "win32"), "D:\\\\RoamingCfg\\\\mindie-agent");
assert.equal(dataHome(winExplicit, "win32"), "D:\\\\LocalData\\\\mindie-agent");
const winFallback = {{ USERPROFILE: "C:\\\\Users\\\\alice" }};
assert.equal(configHome(winFallback, "win32"), "C:\\\\Users\\\\alice\\\\AppData\\\\Roaming\\\\mindie-agent");
assert.equal(dataHome(winFallback, "win32"), "C:\\\\Users\\\\alice\\\\AppData\\\\Local\\\\mindie-agent");
const posixEnv = {{ HOME: "/home/alice" }};
assert.equal(configHome(posixEnv, "linux"), "/home/alice/.config/mindie-agent");
assert.equal(dataHome(posixEnv, "linux"), "/home/alice/.local/share/mindie-agent");

const testHomeDir = {json.dumps(str(isolated_oc_env["root"] / "oc-test-home"))};
fs.mkdirSync(testHomeDir, {{ recursive: true }});
assert.equal(opencodeDbPath({{ OPENCODE_DB: ":memory:", OPENCODE_TEST_HOME: testHomeDir }}), null);
const jsTestHomeDb = opencodeDbPath({{ OPENCODE_TEST_HOME: testHomeDir }});

const stubPy = path.join(testHomeDir, "zero_stub_python.exe");
fs.writeFileSync(stubPy, "");
const coldCfg = {json.dumps(str(isolated_oc_env["root"] / "cold-cfg"))};
const coldData = {json.dumps(str(isolated_oc_env["root"] / "cold-data"))};
const coldEnv = {{ MINDIE_CONFIG_HOME: coldCfg, MINDIE_DATA_HOME: coldData, OC_PLUGIN_ROOT: {json.dumps(str(cold_plugin))}, MINDIE_OC_PYTHON: stubPy }};
assert.equal(resolvePython(coldEnv, "win32"), null);
const coldCmd = resolveLaunchCommand(["mcp", "tools"], coldEnv, "win32");
assert.deepEqual(coldCmd.slice(0, 8), ["uv", "run", "--no-project", "--python", "3.13", "python", "-B", "-X"]);
assert.equal(coldCmd.at(-2), "mcp");
assert.equal(coldCmd.at(-1), "tools");

const genDir = path.join(coldData, "runtimes", "oc", "generations", "gen_valid");
fs.mkdirSync(genDir, {{ recursive: true }});
const realPy = {json.dumps(str(isolated_oc_env["python"]))};
fs.writeFileSync(path.join(genDir, "runtime.json"), JSON.stringify({{ python: realPy, validation: {{ status: "validated", validated_at: 100 }} }}), "utf8");
delete coldEnv.MINDIE_OC_PYTHON;
const genCmd = resolveLaunchCommand(["mcp", "remote"], coldEnv, process.platform);
assert.equal(genCmd[0], realPy);

const marker = enqueuePendingUpdateCheck("unit_test", coldEnv, process.platform);
const markerDoc = JSON.parse(fs.readFileSync(marker, "utf8"));
assert.equal(markerDoc.schema, "mindie-pending-update/1");
assert.equal(markerDoc.host, "oc");
assert.equal(typeof markerDoc.requested_at, "number");
assert.deepEqual(fs.readdirSync(path.dirname(marker)).filter((n) => n.endsWith(".tmp")), []);

process.stdout.write(JSON.stringify({{ currentCfg, currentData, jsTestHomeDb, stubPy }}) + "\\n");
""",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(parity_driver)], capture_output=True, text=True, env=dict(os.environ), timeout=30)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    js_paths = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(js_paths["currentCfg"]) == prof.config_path
    assert Path(js_paths["currentData"]) == prof.data_dir
    assert mindie_launch._opencode_db({}, {"OPENCODE_DB": ":memory:", "OPENCODE_TEST_HOME": str(isolated_oc_env["root"] / "oc-test-home")}) is None
    assert mindie_launch._opencode_db({}, {"OPENCODE_TEST_HOME": str(isolated_oc_env["root"] / "oc-test-home")}) == Path(js_paths["jsTestHomeDb"])
    assert not mindie_launch._is_usable_python(js_paths["stubPy"], is_win=True)
    assert not mindie_launch._is_usable_python("C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe", is_win=True)

    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(isolated_oc_env["mindie_cfg"]))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(isolated_oc_env["mindie_data"]))
    assert mindie_launch.main(["hook", "session-start"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {}
    py_marker = isolated_oc_env["mindie_data"] / "state" / "oc" / "pending-update-check.json"
    py_doc = json.loads(py_marker.read_text(encoding="utf-8"))
    assert py_doc["schema"] == "mindie-pending-update/1" and py_doc["host"] == "oc" and isinstance(py_doc["requested_at"], float)

    cfg_file = isolated_oc_env["mindie_cfg"] / "oc.json"
    preserved_cmd = [str(isolated_oc_env["python"]), "-I", "-X", "utf8", "-m", "mindie_profiler"]
    cfg_file.write_text(
        json.dumps(
            {
                "schema": "mindie-adapter-config/1",
                "python": str(isolated_oc_env["python"]),
                "component_release": str(isolated_oc_env["mindie_data"] / "runtimes" / "oc" / "gen_keep"),
                "component_commands": {"profiler": preserved_cmd},
                "product_validation": {"schema": "mindie-candidate-check/1", "status": "validated", "custom_flag": True},
            }
        ),
        encoding="utf-8",
    )
    assert oc_setup.main(["init", "--workspace-root", str(isolated_oc_env["workspace"]), "--domain", "operators"]) == 0
    init_out = json.loads(capsys.readouterr().out)
    assert init_out["status"] == "initialized"
    saved_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved_cfg["component_release"] == str(isolated_oc_env["mindie_data"] / "runtimes" / "oc" / "gen_keep")
    assert saved_cfg["component_commands"]["profiler"] == preserved_cmd
    assert saved_cfg["product_validation"]["custom_flag"] is True
    assert saved_cfg["product_validation"]["native_version"] == "0.1.12"
    assert saved_cfg["domain"] == "vllm-ascend" and saved_cfg["preferred_domain"] == "operators"
    assert Path(saved_cfg["engine_config"]).name == "vllm-ascend.engine.json"
    assert set(saved_cfg["domain_configs"]) == EXPECTED_DOMAINS
    for dom_path in saved_cfg["domain_configs"].values():
        assert Path(dom_path).is_file()

    import mindie_harness.runtime.knowledge_binding as kb_mod

    monkeypatch.setattr(kb_mod, "ensure_knowledge_binding", lambda *_a, **_k: {"status": "skipped", "reason": "knowledge_not_installed"})
    with pytest.raises(RuntimeError, match="knowledge binding failed"):
        oc_setup.main(["init"])

    seed_gen = isolated_oc_env["mindie_data"] / "runtimes" / "oc" / "generations" / "gen_seed"
    seed_gen.mkdir(parents=True, exist_ok=True)
    (seed_gen / "runtime.json").write_text(json.dumps({"python": str(isolated_oc_env["python"]), "validation": {"status": "validated", "validated_at": 200}}), encoding="utf-8")
    cfg_file.unlink(missing_ok=True)
    monkeypatch.delenv("MINDIE_OC_PYTHON", raising=False)
    monkeypatch.delenv("_MINDIE_OC_REEXEC", raising=False)
    reexec_calls: list[list[str]] = []
    monkeypatch.setattr(mindie_launch, "verify_seed_bundle", lambda *_a, **_k: {"schema": "mindie-harness-seed/1"})
    monkeypatch.setattr(mindie_launch, "_validated_python", lambda cfg: str(isolated_oc_env["python"]) if reexec_calls else None)

    def _fake_run(cmd, **_kw):
        reexec_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"provisioned"}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert mindie_launch._reexec(["doctor"], {}) == 0
    assert len(reexec_calls) == 2 and reexec_calls[1][0] == str(isolated_oc_env["python"])


def test_host_session_isolation_and_context_injection(isolated_oc_env: dict[str, Path]) -> None:
    node = shutil.which("node")
    assert node is not None
    workspace = str(isolated_oc_env["workspace"])
    driver = isolated_oc_env["root"] / "host_driver.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").as_uri())};

const mcp = new Map();
const hooks = {{ session: new Map(), tool: new Map(), shell: new Map() }};
const calls = [];
const sessions = {{
  ses_main_a001: {{ id: "ses_main_a001", directory: process.env.TEST_WORKSPACE }},
  ses_sub_b002: {{ id: "ses_sub_b002", directory: process.env.TEST_WORKSPACE, parentID: "ses_main_a001" }},
  ses_fork_c003: {{ id: "ses_fork_c003", directory: process.env.TEST_WORKSPACE, forkSessionID: "ses_main_a001" }},
}};
const ctx = {{
  location: {{ directory: process.env.TEST_WORKSPACE }},
  mcp: {{ async transform(fn) {{ fn({{ set: (k, v) => mcp.set(k, v) }}); }}, async reload() {{}} }},
  session: {{ hook(n, fn) {{ hooks.session.set(n, fn); }}, async get({{ sessionID }}) {{ return sessions[sessionID] || null; }} }},
  tool: {{ hook(n, fn) {{ hooks.tool.set(n, fn); }} }},
  shell: {{ hook(n, fn) {{ hooks.shell.set(n, fn); }} }},
  generate: {{ async text(args) {{ calls.push(args); return {{ text: "{{\\"blocks\\":[],\\"navigation\\":\\"ok\\"}}" }}; }} }},
}};
const cleanup = await plugin.setup(ctx);
assert.equal(mcp.get("mindie-remote-dev").timeout.execution, 660000);
assert.equal(mcp.get("mindie-remote-dev").environment.REMOTE_DEV_HARNESS_PROFILE, "oc");
assert.equal(mcp.get("mindie-tools").timeout.execution, 660000);
assert.equal(mcp.get("mindie-tools").environment.REMOTE_DEV_HARNESS_PROFILE, undefined);

const sys = [];
await hooks.session.get("context")({{ sessionID: "ses_main_a001", system: sys }});
assert.equal(sys.length, 1);
assert.match(sys[0].text, /OPENCODE_SESSION_ID=ses_main_a001/);
assert.ok(sys[0].text.includes("Keep the user's task moving"));
assert.ok(!sys[0].text.startsWith("---"));
for (const skipped of ["ses_sub_b002", "ses_fork_c003"]) {{
  const parts = [];
  await hooks.session.get("context")({{ sessionID: skipped, system: parts }});
  assert.equal(parts.length, 0);
}}

const input = {{ _mindie_bind_id: "forged", query: "ACL 507015" }};
await hooks.tool.get("execute.before")({{ tool: "mcp_mindie-tools_knowledge_query", sessionID: "ses_main_a001", input }});
assert.equal(input._mindie_bind_id, undefined);

await hooks.tool.get("execute.before")({{ tool: "bash", sessionID: "ses_main_a001", input: {{ command: "python train.py" }} }});
const envA = {{}};
hooks.shell.get("create.before")({{ command: "zsh -lc 'python train.py'", env: envA }});
assert.equal(envA.OPENCODE_SESSION_ID, "ses_main_a001");

const endpoint = path.join(process.env.MINDIE_DATA_HOME, "state", "oc", "summary-endpoint.json");
assert.ok(fs.existsSync(endpoint));
const ep = JSON.parse(fs.readFileSync(endpoint, "utf8"));
const unauthorized = await fetch(ep.url, {{ method: "POST", body: "{{}}" }});
assert.equal(unauthorized.status, 401);
const response = await fetch(ep.url, {{ method: "POST", headers: {{ "Content-Type": "application/json", Authorization: `Bearer ${{ep.token}}` }}, body: JSON.stringify({{ model: "deepseek/deepseek-flash#max", prompt: "Summarize" }}) }});
assert.equal(response.status, 200);
assert.equal(calls.length, 1);
assert.deepEqual(calls[0].model, {{ providerID: "deepseek", id: "deepseek-flash", variant: "max" }});
await cleanup();
""",
        encoding="utf-8",
    )
    env = dict(os.environ, TEST_WORKSPACE=workspace, MINDIE_OC_ALLOW_BG_UPDATE="0")
    result = subprocess.run([node, str(driver)], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
