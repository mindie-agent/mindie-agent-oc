"""Contract, OpenCode v2 plugin loading, and Node host-isolation tests (`test_oc_plugin.py`).

[Role]
Verifies repository manifests (`package.json`, `product-contract.json`,
`harness-compatibility.json`), module hygiene (`< 800` lines, LF endings, P1.5
`windowsHide: true`), silent `SKILL.md` (`<= 30` lines, zero command exposure),
real `opencode debug config --standalone` plugin loading, pure-Node `SKILL.md`
context injection, and pure-Node consent-first `session.idle` gating.

[Invariants]
- All paths and test IDs referenced in `harness-compatibility.json` must resolve
  to real symbols/functions.
- Zero user-facing `/mindie-agent` or `$mindie-agent` command mentions appear in
  manifests, runtime skill docs, or README/install docs.
- Concurrent OpenCode sessions never leak `SKILL.md` context into subagents or
  leak `OPENCODE_SESSION_ID` shell variables across session boundaries.

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
import re
import shutil
import subprocess

from mindie_knowledge import releases

from bounded import run as bounded_run
from paths import opencode_cli_bin

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_COMMAND_RE = re.compile(r"(?<![A-Za-z0-9_./}-])(?:/|\$)mindie-agent(?![-/A-Za-z0-9_])")


def test_plugin_manifests_and_opencode_v2_loading(isolated_oc_env: dict[str, Path]) -> None:
    pkg = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    contract = json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))
    plugin_json = json.loads((REPO_ROOT / "plugins" / "mindie-agent" / "plugin.json").read_text(encoding="utf-8"))
    compat = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))

    import tomllib

    expected_ver = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert pkg["name"] == "@mindie-agent/oc"
    assert pkg["type"] == "module"
    assert pkg["main"] == "./index.mjs"
    assert pkg["version"] == contract["native_version"] == plugin_json["version"] == expected_ver
    assert contract["harness"] == "opencode"

    validated = releases.validate_compatibility(compat, harness_name="opencode")
    assert validated["harness"] == "opencode"
    assert "command.transform" not in compat["used_interfaces"]
    assert "session.hook.prompt" not in compat["used_interfaces"]

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
        REPO_ROOT / "plugins" / "mindie-agent" / "session_support.mjs",
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
    assert "command.transform" not in host_mjs
    assert 'hook("prompt"' not in host_mjs and "hook('prompt'" not in host_mjs

    install_docs = REPO_ROOT / "docs" / "install"
    skill_dir = REPO_ROOT / "plugins" / "mindie-agent" / "skills" / "mindie-agent"
    refs_dir = skill_dir / "references"
    for doc_name in ("workspace-setup.md", "user-notice.md", "github-authentication.md"):
        assert (install_docs / doc_name).is_file()
        assert not (refs_dir / doc_name).exists()
    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    import yaml

    frontmatter = yaml.safe_load(skill_text.split("---", 2)[1])
    assert isinstance(frontmatter, dict) and frontmatter.get("name") == "mindie-agent"
    assert "disable-model-invocation" not in frontmatter and frontmatter.get("disable-model-invocation") is not True
    assert "disableModelInvocation" not in frontmatter and frontmatter.get("disableModelInvocation") is not True
    assert "disable-model-invocation" not in skill_text
    assert len(skill_text.splitlines()) <= 30, f"SKILL.md exceeds 30 lines ({len(skill_text.splitlines())})"
    for runtime_doc in (skill_dir / "SKILL.md", *sorted(refs_dir.glob("*.md"))):
        body = runtime_doc.read_text(encoding="utf-8")
        for forbidden_install_term in (
            "workspace-setup.md",
            "github-authentication.md",
            "user-notice.md",
            "needs_onboarding_prompt",
            "vllm-ascend-workspace",
            "gh auth login",
            "/user/starred/",
            "setup.py",
        ):
            assert forbidden_install_term not in body, f"{forbidden_install_term!r} leaked into {runtime_doc}"

    user_surface_files = [
        REPO_ROOT / "package.json",
        REPO_ROOT / "plugins" / "mindie-agent" / "plugin.json",
        REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs",
        skill_dir / "SKILL.md",
        *sorted(refs_dir.glob("*.md")),
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
        *sorted(install_docs.glob("*.md")),
    ]
    for surface in user_surface_files:
        txt = surface.read_text(encoding="utf-8")
        assert USER_COMMAND_RE.search(txt) is None, f"User command leaked in {surface}"

    for readme_path in (REPO_ROOT / "README.md", REPO_ROOT / "README.en.md"):
        readme_text = readme_path.read_text(encoding="utf-8")
        assert "docs/install/user-notice.md" in readme_text
        assert "docs/install/workspace-setup.md" in readme_text
        assert "docs/install/github-authentication.md" in readme_text
        assert "skills/mindie-agent/references/" not in readme_text

    # Verify live OpenCode v2 CLI loads `file://<repo>` cleanly via `debug config --standalone` & `debug agents --standalone`.
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
            timeout=30.0,
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
            timeout=30.0,
            env=dict(os.environ),
            cwd=isolated_oc_env["workspace"],
        )
        assert agents_proc.returncode == 0
        assert "loading plugin" in agents_proc.stderr
        assert "index.mjs" in agents_proc.stderr


def test_host_session_isolation_and_context_injection(isolated_oc_env: dict[str, Path]) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None, "node runtime is required to test opencode_host.mjs"

    outside_ws = isolated_oc_env["root"] / "outside-workspace"
    outside_ws.mkdir(parents=True, exist_ok=True)
    capture_spy = isolated_oc_env["root"] / "capture_spy"
    capture_log = isolated_oc_env["root"] / "capture_spawns.jsonl"
    if os.name != "nt":
        capture_spy.write_text(
            f"#!{isolated_oc_env['python']}\n"
            "import pathlib, sys\n"
            f"pathlib.Path({str(capture_log)!r}).write_text(sys.argv[-1] + '\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        capture_spy.chmod(0o755)

    driver = isolated_oc_env["root"] / "host_driver.mjs"
    driver.write_text(
        f"""
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import plugin from {json.dumps((REPO_ROOT / "index.mjs").as_uri())};

const mcpMap = new Map();
const commands = new Map();
const sessionHooks = new Map();
const toolHooks = new Map();
const shellHooks = new Map();
const generateCalls = [];
let emitEvent = null;

const sessions = {{
    ses_mainA001: {{ id: "ses_mainA001", directory: process.env.TEST_WORKSPACE }},
    ses_otherB002: {{ id: "ses_otherB002", directory: {json.dumps(str(outside_ws))} }},
    ses_subC003: {{ id: "ses_subC003", directory: process.env.TEST_WORKSPACE, parentID: "ses_mainA001" }},
    ses_forkD004: {{ id: "ses_forkD004", directory: process.env.TEST_WORKSPACE, forkSessionID: "ses_mainA001" }},
    ses_nodirE005: {{ id: "ses_nodirE005" }},
}};
let sessionGetCalls = 0;

const eventQueue = [];
let eventWaiter = null;
function pushEvent(ev) {{
    if (eventWaiter) {{
        const w = eventWaiter;
        eventWaiter = null;
        w({{ value: ev, done: false }});
    }} else {{
        eventQueue.push(ev);
    }}
}}

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
        async get({{ sessionID }}) {{
            sessionGetCalls += 1;
            return sessions[sessionID] || null;
        }},
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
            return {{
                [Symbol.asyncIterator]() {{
                    return {{
                        next() {{
                            if (eventQueue.length > 0) {{
                                const nextEv = eventQueue.shift();
                                if (nextEv === null) return Promise.resolve({{ value: undefined, done: true }});
                                return Promise.resolve({{ value: nextEv, done: false }});
                            }}
                            return new Promise((resolve) => {{ eventWaiter = resolve; }});
                        }},
                    }};
                }},
            }};
        }},
    }},
}};

const cleanup = await plugin.setup(mockCtx);
assert.equal(mcpMap.get("mindie-remote-dev").timeout.execution, 660000);
assert.equal(mcpMap.get("mindie-tools").timeout.execution, 660000);

// 1. Zero user commands or prompt gesture hooks registered.
assert.equal(commands.size, 0);
assert.equal(sessionHooks.has("prompt"), false);

// 2. Pure-Node context injection works with zero Python subprocesses & zero admission.sqlite3 created.
const origPy = process.env.MINDIE_OC_PYTHON;
process.env.MINDIE_OC_PYTHON = "/nonexistent-python-proving-zero-subprocess";
const activeSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_mainA001", system: activeSys }});
process.env.MINDIE_OC_PYTHON = origPy || "";
assert.equal(activeSys.length, 1);
assert.equal(activeSys[0].type, "text");
assert.match(activeSys[0].text, /OPENCODE_SESSION_ID=ses_mainA001/);
assert.ok(activeSys[0].text.includes("# MindIE Agent (OpenCode Unified Entry)"));
assert.ok(!activeSys[0].text.includes("name: mindie-agent"));
assert.ok(!fs.existsSync(path.join(process.env.MINDIE_DATA_HOME, "oc-state", "admission.sqlite3")));

// Subagent, forked, and null-metadata sessions receive zero SKILL.md system context.
const subSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_subC003", system: subSys }});
assert.equal(subSys.length, 0);
const forkSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_forkD004", system: forkSys }});
assert.equal(forkSys.length, 0);
const nullSys = [];
await sessionHooks.get("context")({{ sessionID: "ses_unknown_null", system: nullSys }});
assert.equal(nullSys.length, 0);

// 3. Shell hook correlates OPENCODE_SESSION_ID strictly per session command.
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

// 4. Loopback summary endpoint started lazily on context hook & authenticates Bearer token.
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

// 5. Pure-Node consent-first idle capture: checks local consent BEFORE session.get & never substitutes ctx.directory!
if (process.platform !== "win32") {{
    process.env.MINDIE_OC_PYTHON = {json.dumps(str(capture_spy))};
    const callsBeforeOff = sessionGetCalls;
    pushEvent({{ type: "session.idle", id: "ev_off_1", data: {{ sessionID: "ses_mainA001" }} }});
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(sessionGetCalls, callsBeforeOff);
    assert.equal(fs.existsSync({json.dumps(str(capture_log))}), false);

    // Enable consent + community strictly for TEST_WORKSPACE.
    const cfgDir = process.env.MINDIE_CONFIG_HOME;
    fs.mkdirSync(cfgDir, {{ recursive: true }});
    const consentPath = path.join(cfgDir, "mindie-consent.json");
    fs.writeFileSync(consentPath, JSON.stringify({{ schema: "mindie-consent/1", choice: "contribute", updated_at: 1700000000 }}));
    fs.writeFileSync(path.join(cfgDir, "mindie-community.json"), JSON.stringify({{
        schema: "mindie-community-config/1",
        enabled: true,
        generation: "gen_test_01",
        enabled_at: 1700000000,
        project_roots: [process.env.TEST_WORKSPACE],
        consent_config: consentPath,
    }}));

    // Null session or session missing directory MUST NOT substitute ctx.location.directory!
    pushEvent({{ type: "session.idle", id: "ev_null_1", data: {{ sessionID: "ses_unknown_null" }} }});
    pushEvent({{ type: "session.idle", id: "ev_nodir_1", data: {{ sessionID: "ses_nodirE005" }} }});
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(fs.existsSync({json.dumps(str(capture_log))}), false);

    // Out-of-scope session (`ses_otherB002`) -> zero subprocess spawned!
    pushEvent({{ type: "session.idle", id: "ev_out_1", data: {{ sessionID: "ses_otherB002" }} }});
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(fs.existsSync({json.dumps(str(capture_log))}), false);

    // Subagent session (`ses_subC003`) -> zero subprocess spawned!
    pushEvent({{ type: "session.idle", id: "ev_sub_1", data: {{ sessionID: "ses_subC003" }} }});
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(fs.existsSync({json.dumps(str(capture_log))}), false);

    // Authorized in-scope main session (`ses_mainA001`) -> spawns detached capture_stop.py!
    pushEvent({{ type: "session.idle", id: "ev_ok_1", data: {{ sessionID: "ses_mainA001" }} }});
    for (let i = 0; i < 40 && !fs.existsSync({json.dumps(str(capture_log))}); i += 1) {{
        await new Promise((r) => setTimeout(r, 50));
    }}
    assert.equal(fs.existsSync({json.dumps(str(capture_log))}), true);
    const logged = JSON.parse(fs.readFileSync({json.dumps(str(capture_log))}, "utf8").trim());
    assert.equal(logged.sessionID, "ses_mainA001");
    assert.equal(logged.directory, process.env.TEST_WORKSPACE);
}}

pushEvent(null);
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
