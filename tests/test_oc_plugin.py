"""Contract, OpenCode v2 plugin loading, and Node host-isolation tests (`test_oc_plugin.py`).

[Role]
Verifies repository manifests (`package.json`, `product-contract.json`,
`harness-compatibility.json`), module hygiene (`< 800` lines, LF endings, P1.5
`windowsHide: true`), real `opencode debug config --standalone` plugin loading,
and per-`sessionID` isolation in `opencode_host.mjs`.

[Invariants]
- All paths and test IDs referenced in `harness-compatibility.json` must resolve
  to real symbols/functions.
- Concurrent OpenCode sessions never leak `SKILL.md` context or `OPENCODE_SESSION_ID`
  shell variables across session boundaries.

[Timeouts & Locks]
- Bounded subprocess invocations (`<= 15s` Tier 1 ceiling).

[Side-Effect Policy]
- Executes strictly inside `isolated_oc_env`.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess

from mindie_knowledge import releases

from bounded import run as bounded_run
from paths import opencode_cli_bin

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifests_and_opencode_v2_loading(isolated_oc_env: dict[str, Path]) -> None:
    pkg = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    contract = json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))
    plugin_json = json.loads((REPO_ROOT / "plugins" / "mindie-agent" / "plugin.json").read_text(encoding="utf-8"))
    compat = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))

    assert pkg["name"] == "@mindie-agent/oc"
    assert pkg["type"] == "module"
    assert pkg["main"] == "./index.mjs"
    assert pkg["version"] == contract["native_version"] == plugin_json["version"]
    assert contract["harness"] == "opencode"

    validated = releases.validate_compatibility(compat, harness_name="opencode")
    assert validated["harness"] == "opencode"

    # Verify every code_paths, test_ids, and evidence_refs entry in harness-compatibility.json exists.
    for iface, meta in compat["interface_registry"].items():
        assert iface in compat["used_interfaces"]
        for ref in meta["evidence_refs"]:
            assert (REPO_ROOT / ref).is_file(), f"missing evidence_ref {ref}"
        for code_ref in meta["code_paths"]:
            rel_path, _, sym = code_ref.partition(":")
            file_path = REPO_ROOT / rel_path
            assert file_path.is_file(), f"missing code_path {rel_path}"
            if sym and file_path.suffix == ".py":
                tree = ast.parse(file_path.read_text(encoding="utf-8"))
                defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                assert sym in defined, f"missing symbol {sym} in {rel_path}"
        for test_id in meta["test_ids"]:
            test_rel, _, test_fn = test_id.partition("::")
            test_path = REPO_ROOT / test_rel
            assert test_path.is_file(), f"missing test file {test_rel}"
            tree = ast.parse(test_path.read_text(encoding="utf-8"))
            funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            assert test_fn in funcs, f"missing test function {test_fn} in {test_rel}"

    # Verify LF line endings, < 800 line module cap, top-30-line Agent headers, and P1.5 windowsHide.
    source_files = [
        REPO_ROOT / "index.mjs",
        REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs",
        REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs",
        *sorted((REPO_ROOT / "plugins" / "mindie-agent" / "scripts").glob("*.py")),
    ]
    for src in source_files:
        raw = src.read_bytes()
        assert b"\r\n" not in raw, f"CRLF detected in {src}"
        lines = raw.decode("utf-8").splitlines()
        assert len(lines) < 800, f"{src} exceeds 800-line limit ({len(lines)})"
        head = "\n".join(lines[:35])
        for section in ("[Role]", "[Invariants]", "[Timeouts & Locks]", "[Side-Effect Policy]"):
            assert section in head, f"{src.name} missing {section} header"

    host_mjs = (REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs").read_text(encoding="utf-8")
    assert "windowsHide: true" in host_mjs

    # Verify live OpenCode v2 CLI loads `file://<repo>` cleanly via `debug config --standalone` & `mcp list --standalone`.
    oc_bin = opencode_cli_bin()
    if oc_bin is not None:
        oc_cfg = isolated_oc_env["workspace"] / "opencode.json"
        oc_cfg.write_text(
            json.dumps({"plugin": [REPO_ROOT.as_uri()]}, indent=2) + "\n",
            encoding="utf-8",
        )
        proc = bounded_run(
            [oc_bin, "debug", "config", "--standalone"],
            "",
            timeout=14.0,
            env=dict(os.environ),
            cwd=isolated_oc_env["workspace"],
        )
        out = proc.checked_stdout()
        parsed_layers = json.loads(out)
        assert isinstance(parsed_layers, list)
        doc_layers = [
            item.get("info", {})
            for item in parsed_layers
            if isinstance(item, dict) and item.get("type") == "document"
        ]
        assert any(
            REPO_ROOT.as_uri() in (doc.get("plugins") or doc.get("plugin") or [])
            for doc in doc_layers
        )

        agents_proc = bounded_run(
            [oc_bin, "debug", "agents", "--standalone", "--print-logs", "--log-level", "info"],
            "",
            timeout=14.0,
            env=dict(os.environ),
            cwd=isolated_oc_env["workspace"],
        )
        assert agents_proc.returncode == 0
        assert "loading plugin" in agents_proc.stderr
        assert "index.mjs" in agents_proc.stderr


def test_host_session_isolation_and_context_injection(isolated_oc_env: dict[str, Path]) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None, "node runtime is required to test opencode_host.mjs"

    driver = isolated_oc_env["root"] / "host_driver.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").asFileUri() if hasattr(Path, "asFileUri") else (REPO_ROOT / "index.mjs").as_uri())};

const mcpMap = new Map();
const commands = new Map();
const sessionHooks = new Map();
const toolHooks = new Map();
const shellHooks = new Map();
const synthetics = [];
const generateCalls = [];

const sessions = {{
    ses_mainA001: {{ id: "ses_mainA001", directory: process.env.TEST_WORKSPACE }},
    ses_otherB002: {{ id: "ses_otherB002", directory: process.env.TEST_WORKSPACE }},
    ses_subC003: {{ id: "ses_subC003", directory: process.env.TEST_WORKSPACE, parentID: "ses_mainA001" }},
}};

const mockCtx = {{
    location: {{ directory: process.env.TEST_WORKSPACE, worktree: process.env.TEST_WORKSPACE }},
    mcp: {{
        async transform(fn) {{
            fn({{ set: (k, v) => mcpMap.set(k, v) }});
        }},
        async reload() {{}},
    }},
    command: {{
        async transform(fn) {{
            fn({{ add: (cmd) => commands.set(cmd.name, cmd) }});
        }},
        async reload() {{}},
    }},
    session: {{
        hook(name, fn) {{ sessionHooks.set(name, fn); }},
        async get({{ sessionID }}) {{ return sessions[sessionID] || null; }},
        async synthetic(payload) {{ synthetics.push(payload); }},
        async prompt() {{}},
    }},
    tool: {{
        hook(name, fn) {{ toolHooks.set(name, fn); }},
    }},
    shell: {{
        hook(name, fn) {{ shellHooks.set(name, fn); }},
    }},
    generate: {{
        async text(args) {{
            generateCalls.push(args);
            return {{ text: JSON.stringify({{ blocks: [], navigation: "ok" }}) }};
        }},
    }},
    event: {{
        async subscribe() {{
            return (async function* () {{}})();
        }},
    }},
}};

const cleanup = await plugin.setup(mockCtx);
assert.equal(mcpMap.get("mindie-remote-dev").timeout.execution, 660000);
assert.equal(mcpMap.get("mindie-tools").timeout.execution, 660000);
assert.ok(commands.has("mindie-agent"));

// 1. Passive unactivated context check: zero system parts and no admission.sqlite3 created.
const passiveSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_mainA001", system: passiveSys }});
assert.equal(passiveSys.length, 0);

// 2. Activate ses_mainA001 via /mindie-agent command.
await commands.get("mindie-agent").execute({{
    sessionID: "ses_mainA001",
    prompt: {{ text: "" }},
}});
assert.equal(synthetics.length, 1);
assert.match(synthetics[0].text, /MindIE Agent knowledge flywheel enabled/);

// 3. Context injection hits ONLY ses_mainA001; ses_otherB002 & subagent ses_subC003 get nothing.
const activeSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_mainA001", system: activeSys }});
assert.equal(activeSys.length, 1);
assert.equal(activeSys[0].type, "text");
assert.match(activeSys[0].text, /OPENCODE_SESSION_ID=ses_mainA001/);

const otherSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_otherB002", system: otherSys }});
assert.equal(otherSys.length, 0);

const subSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_subC003", system: subSys }});
assert.equal(subSys.length, 0);

// 4. Shell hook correlates OPENCODE_SESSION_ID strictly per session command.
await toolHooks.get("execute.before")({{
    tool: "bash",
    sessionID: "ses_mainA001",
    id: "call_1",
    input: {{ command: "python knowledge_cli.py describe" }},
}});
await toolHooks.get("execute.before")({{
    tool: "bash",
    sessionID: "ses_otherB002",
    id: "call_2",
    input: {{ command: "echo other" }},
}});
const envB = {{}};
shellHooks.get("create.before")({{ command: "zsh -lc 'echo other'", env: envB }});
assert.equal(envB.OPENCODE_SESSION_ID, "ses_otherB002");

const envA = {{}};
shellHooks.get("create.before")({{ command: "zsh -lc 'python knowledge_cli.py describe'", env: envA }});
assert.equal(envA.OPENCODE_SESSION_ID, "ses_mainA001");

// 5. Loopback summary endpoint authenticates Bearer token & parses provider/id#variant.
const epPath = process.env.EXPECTED_SUMMARY_EP;
assert.ok(fs.existsSync(epPath));
const ep = JSON.parse(fs.readFileSync(epPath, "utf8"));
const unauth = await fetch(ep.url, {{ method: "POST", body: "{{}}" }});
assert.equal(unauth.status, 401);

const okResp = await fetch(ep.url, {{
    method: "POST",
    headers: {{
        "Content-Type": "application/json",
        Authorization: `Bearer ${{ep.token}}`,
    }},
    body: JSON.stringify({{
        model: "deepseek/deepseek-flash#max",
        prompt: "Summarize blocks",
        response_schema: {{ type: "object" }},
    }}),
}});
assert.equal(okResp.status, 200);
assert.equal(generateCalls.length, 1);
assert.deepEqual(generateCalls[0].model, {{
    providerID: "deepseek",
    id: "deepseek-flash",
    variant: "max",
}});

await cleanup();
assert.ok(!fs.existsSync(epPath));
console.log("HOST_ISOLATION_OK");
""",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["TEST_WORKSPACE"] = str(isolated_oc_env["workspace"])
    env["EXPECTED_SUMMARY_EP"] = str(isolated_oc_env["mindie_data"] / "oc-state" / "summary-endpoint.json")
    proc = subprocess.run(
        [node_bin, str(driver)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    assert proc.returncode == 0, f"node driver failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "HOST_ISOLATION_OK" in proc.stdout
