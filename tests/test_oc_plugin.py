"""OpenCode v2 plugin loading, skill/context isolation, and release metadata tests.

[Role]
Verifies manifests, `harness-compatibility.json` references, `.mcp.json`,
seed integrity, `release-metadata`, module headers/LF, and pure-Node
`session.hook("context")`, MCP input sanitization, shell binding, and the
loopback summary endpoint.

[Invariants]
- Compatibility `code_paths`/`test_ids` resolve to real files and symbols.
- Subagent/forked sessions receive zero skill context; `_mindie_bind_id` never
  survives a MindIE MCP tool input; every child spawn is windowless.

[Timeouts & Locks]
- One bounded Node driver (`< 15s`) plus local JSON/AST inspection.

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


def test_plugin_manifests_and_opencode_v2_loading(isolated_oc_env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    import tomllib

    pkg = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    contract = json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))
    plugin_json = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    compat = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert pkg["name"] == "@mindie-agent/oc" and pkg["main"] == "./index.mjs"
    assert pkg["version"] == contract["native_version"] == plugin_json["version"] == version == "0.1.11"
    assert contract["harness"] == compat["harness"] == "opencode"

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
    assert "paths.mjs" not in host and "session_support.mjs" not in host
    assert "knowledge_cli.py" not in host and "rsi_loop.py" not in host

    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert mcp["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "oc"
    assert mcp["mindie-remote-dev"]["args"][-2:] == ["mcp", "remote"]
    assert mcp["mindie-tools"]["args"][-2:] == ["mcp", "tools"]

    lock = mindie_launch.verify_seed_bundle(PLUGIN_ROOT)
    assert lock["schema"] == "mindie-harness-seed/1" and lock["version"] == "0.1.3"

    skill = (PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]
    assert re.search(r'^name:\s*"?mindie-agent"?$', frontmatter, re.MULTILINE)
    assert "disable-model-invocation" not in skill and "/mindie-agent" not in skill and "$mindie-agent" not in skill
    for forbidden in ("workspace-setup.md", "github-authentication.md", "user-notice.md", "(out_tensor, metrics_dict)"):
        for doc in [PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md", *sorted((PLUGIN_ROOT / "skills" / "mindie-agent" / "references").glob("*.md"))]:
            assert forbidden not in doc.read_text(encoding="utf-8")

    assert oc_setup.main(["release-metadata", "--source", str(REPO_ROOT), "--tests", str(REPO_ROOT / "tests"), "--revision", "a" * 40]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mindie-plugin-release/1" and payload["version"] == "0.1.11"
    assert payload["source"]["sha"] == "a" * 40

    assert oc_setup.main(["init", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry-run"


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
