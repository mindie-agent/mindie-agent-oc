"""Regression tests for OpenCode (`oc`) Windows, PowerShell BOM, WSL2, and transport parity."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import types
import urllib.error

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import component_update
import diagnostic_support
import identity
import paths


def _write_fake_pe(path: Path) -> None:
    buf = bytearray(128)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 64)
    buf[64:68] = b"PE\x00\x00"
    path.write_bytes(bytes(buf))
    path.chmod(0o755)


def test_classify_update_exception_maps_transient_transport_without_masking_local_bugs() -> None:
    from mindie_knowledge.release_http import ReleaseResponseTooLarge
    from mindie_knowledge.releases_io import ReleaseFetchError

    assert diagnostic_support.classify_update_exception(TimeoutError("deadline")) == ("timeout", False, None)
    assert diagnostic_support.classify_update_exception(ssl.SSLError("EOF in violation of protocol")) == (
        "temporary_network",
        False,
        "release_transport_unavailable",
    )
    assert diagnostic_support.classify_update_exception(ssl.SSLCertVerificationError(1, "self-signed")) == (
        "certificate",
        False,
        "release_certificate_error",
    )
    assert diagnostic_support.classify_update_exception(
        urllib.error.URLError(ssl.SSLCertVerificationError(1, "corp-mitm"))
    ) == ("certificate", False, "release_certificate_error")
    assert diagnostic_support.classify_update_exception(http.client.IncompleteRead(b"{")) == (
        "temporary_network",
        False,
        "release_transport_unavailable",
    )
    assert diagnostic_support.classify_update_exception(socket.gaierror(-2, "name not known")) == (
        "temporary_network",
        False,
        "release_transport_unavailable",
    )
    assert diagnostic_support.classify_update_exception(
        ReleaseFetchError("release_rate_limited", status=429)
    ) == ("rate_limited", False, "release_rate_limited")
    assert diagnostic_support.classify_update_exception(
        ReleaseFetchError("invalid_release_json")
    ) == ("temporary_network", False, "invalid_release_json")

    wrapped_local_json = ValueError("MindIE metadata is malformed JSON: product-contract.json")
    wrapped_local_json.__cause__ = json.JSONDecodeError("bad json", "{", 1)
    for local_bug in (
        ReleaseResponseTooLarge("oversize"),
        http.client.InvalidURL("http://bad host"),
        http.client.ImproperConnectionState("double send"),
        http.client.CannotSendRequest("cannot send request"),
        http.client.CannotSendHeader(),
        http.client.ResponseNotReady(),
        http.client.NotConnected("closed"),
        json.JSONDecodeError("bad local json", "{", 1),
        wrapped_local_json,
        ValueError("contract mismatch"),
        RuntimeError("unexpected"),
    ):
        assert diagnostic_support.classify_update_exception(local_bug) == ("update", True, None)


def test_component_update_check_records_non_reportable_transient_tls_error(
    isolated_oc_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple] = []
    orig_failure = diagnostic_support.failure

    def spy_failure(op: str, stage: str, category: str, **kwargs):
        recorded.append((op, stage, category, kwargs.get("reportable"), kwargs.get("reason_code")))
        return orig_failure(op, stage, category, **kwargs)

    monkeypatch.setattr(diagnostic_support, "failure", spy_failure)
    orig_prepare = component_update.prepare
    monkeypatch.setattr(
        component_update,
        "prepare",
        lambda **_kw: (_ for _ in ()).throw(ssl.SSLError("TLS record layer drop")),
    )
    with pytest.raises(ssl.SSLError):
        component_update.check(
            config=paths.config_path(),
            source=REPO_ROOT,
            timeout=30.0,
        )
    assert recorded
    assert recorded[-1] == (
        "component.update",
        "prepare",
        "temporary_network",
        False,
        "release_transport_unavailable",
    )

    # Verify truncated upstream Catalog.read() JSONDecodeError is wrapped into invalid_release_json
    # (temporary_network, reportable=False) at the remote catalog boundary.
    import mindie_knowledge.component_releases as comp_rel
    import mindie_knowledge.component_runtime as comp_rt
    import mindie_knowledge.component_bindings as comp_bind

    def truncated_http_read(_self, _url: str):
        raise json.JSONDecodeError("Expecting value", "{truncated", 1)

    monkeypatch.setattr(comp_rel.Catalog, "read", truncated_http_read)
    monkeypatch.setattr(comp_bind, "required_components", lambda *_a, **_kw: {"knowledge": "0.1.0"})
    monkeypatch.setattr(comp_rt, "source_baseline", lambda *_a, **_kw: {"runtime": {"knowledge": "abc"}})
    monkeypatch.setattr(
        comp_rt,
        "discover",
        lambda _py, _rt, **_kw: _kw["catalog"].read("https://api.github.com/repos/mindie-agent/knowledge/releases"),
    )
    try:
        orig_prepare(source=REPO_ROOT, python=sys.executable, config=paths.config_path(), timeout=30.0)
    except Exception as remote_json_exc:
        assert diagnostic_support.classify_update_exception(remote_json_exc) == (
            "temporary_network",
            False,
            "invalid_release_json",
        )
    else:
        raise AssertionError("Expected ReleaseFetchError('invalid_release_json') from truncated Catalog.read()")


def test_utf8_bom_tolerance_across_python_and_js(tmp_path: Path) -> None:
    bom_cfg = tmp_path / "bom.json"
    bom_cfg.write_bytes(b'\xef\xbb\xbf{"schema":"mindie-adapter-config/1","domain":"vllm-ascend"}\n')
    assert paths.read_json(bom_cfg)["domain"] == "vllm-ascend"

    hook_payload = b'\xef\xbb\xbf{"sessionID":"ses_bom_1234","prompt":"\xef\xbb\xbf/mindie-agent status"}'
    parsed = identity.parse_hook(hook_payload)
    assert parsed["sessionID"] == "ses_bom_1234"
    assert identity.parse_mindie_gesture(parsed) == (True, "status")

    node_bin = shutil.which("node")
    assert node_bin is not None
    paths_uri = (REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs").as_uri()
    js_out = subprocess.run(
        [
            node_bin,
            "--input-type=module",
            "-e",
            f"import {{ readJsonSafe }} from {json.dumps(paths_uri)};"
            f"console.log(JSON.stringify(readJsonSafe({json.dumps(str(bom_cfg))})));",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert json.loads(js_out.stdout.strip())["domain"] == "vllm-ascend"


def test_wsl_binary_filtering_and_windows_interpreter_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None
    pe_dir = tmp_path / "win_bin"
    linux_dir = tmp_path / "linux_bin"
    pe_dir.mkdir()
    linux_dir.mkdir()
    pe_oc = pe_dir / "opencode"
    _write_fake_pe(pe_oc)
    linux_oc = linux_dir / "opencode"
    linux_oc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    linux_oc.chmod(0o755)

    wsl_path = f"/mnt/c/Program Files (x86)/Tool{os.pathsep}{pe_dir}{os.pathsep}{linux_dir}"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setenv("PATH", wsl_path)
    if os.name == "posix":
        assert paths.in_wsl() is True
        assert paths.is_windows_host_binary("/mnt/c/Users/demo/.opencode/bin/opencode") is True
        assert paths.is_windows_host_binary(pe_oc) is True
        assert paths.which_host_binary("opencode") == str(linux_oc)
        monkeypatch.setattr(paths.shutil, "which", lambda _n: "/mnt/c/Users/demo/.opencode/bin/opencode")
        assert paths.which_host_binary("opencode", which_fn=paths.shutil.which) == str(linux_oc)

    # Windows backslash uv/WindowsApps/pythonw filtering & \\?\ same_interpreter.
    win_venv = tmp_path / "oc-venv" / "Scripts"
    win_venv.mkdir(parents=True)
    py_exe = win_venv / "python.exe"
    py_exe.write_text("bin", encoding="utf-8")
    pyw_exe = win_venv / "pythonw.exe"
    pyw_exe.write_text("bin", encoding="utf-8")
    uv_managed = tmp_path / "AppData" / "Roaming" / "uv" / "python" / "cpython-3.13-venv" / "python.exe"
    uv_managed.parent.mkdir(parents=True)
    uv_managed.write_text("bin", encoding="utf-8")
    assert paths.is_usable_venv_python(str(pyw_exe)) is False
    assert paths.is_usable_venv_python(str(uv_managed).replace("/", "\\")) is False
    assert paths.is_usable_venv_python(r"C:\Users\demo\AppData\Local\Microsoft\WindowsApps\venv\python.exe") is False

    # Verify DrvFS Linux console scripts (with uv/distlib .exe normalization), POSIX $basedir wrappers,
    # and TimeoutError propagation during os.close() cleanup.
    drvfs_dir = tmp_path / "drvfs_venv"
    drvfs_dir.mkdir()
    linux_console = drvfs_dir / "mindie-profiler"
    linux_console.write_text(
        "#!/mnt/c/proj/.venv/bin/python3\n"
        "import re, sys\n"
        'sys.argv[0] = re.sub(r"(-script\\.pyw|\\.exe)?$", "", sys.argv[0])\n'
        'if sys.argv[0].endswith(".exe"):\n'
        "    sys.argv[0] = sys.argv[0][:-4]\n",
        encoding="utf-8",
    )
    linux_console.chmod(0o755)
    posix_basedir = drvfs_dir / "opencode-wrapper"
    posix_basedir.write_text('#!/bin/sh\nbasedir=$(dirname "$0")\nexec "$basedir/linux-opencode" "$@"\n', encoding="utf-8")
    posix_basedir.chmod(0o755)
    npm_shim = drvfs_dir / "opencode-npm"
    npm_shim.write_text('#!/bin/sh\nbasedir=$(cygpath -w "$0")\nexec "$basedir/node.exe"\n', encoding="utf-8")
    npm_shim.chmod(0o755)
    assert paths._is_wsl_linux_executable(linux_console) is True
    assert paths._is_wsl_linux_executable(posix_basedir) is True
    assert paths._is_wsl_linux_executable(npm_shim) is False

    real_close = paths.os.close

    def close_with_timeout(fd):
        real_close(fd)
        raise TimeoutError("deadline during close")

    monkeypatch.setattr(paths.os, "close", close_with_timeout)
    try:
        try:
            paths._is_wsl_linux_executable(linux_console)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expected TimeoutError from os.close in _is_wsl_linux_executable")
    finally:
        monkeypatch.setattr(paths.os, "close", real_close)

    fake_nt = types.SimpleNamespace(name="nt", environ=os.environ, path=os.path, fspath=os.fspath)
    monkeypatch.setattr(paths, "os", fake_nt)
    assert paths.same_interpreter(
        r"\\?\c:\Users\中文\oc-venv\Scripts\python.exe",
        r"C:\Users\中文\oc-venv\Scripts\PYTHON.EXE",
    )

    paths_uri = (REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs").as_uri()
    js_wsl = subprocess.run(
        [
            node_bin,
            "--input-type=module",
            "-e",
            f"import {{ isUsableBootstrapPython }} from {json.dumps(paths_uri)};"
            "console.log(JSON.stringify({"
            "winApps: isUsableBootstrapPython('C:\\\\Users\\\\u\\\\AppData\\\\Local\\\\Microsoft\\\\WindowsApps\\\\python.exe'),"
            "pyw: isUsableBootstrapPython('/tmp/pythonw.exe'),"
            "mntC: process.platform === 'win32' ? false : isUsableBootstrapPython('/mnt/c/Python313/python')"
            "}));",
        ],
        env={**os.environ, "WSL_DISTRO_NAME": "Ubuntu-24.04"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert json.loads(js_wsl.stdout.strip()) == {"winApps": False, "pyw": False, "mntC": False}


def test_js_windows_kill_hook_tree_and_cold_uv_fallback_resilience(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    assert node_bin is not None
    host_js = REPO_ROOT / "plugins" / "mindie-agent" / "opencode_host.mjs"
    paths_js = REPO_ROOT / "plugins" / "mindie-agent" / "paths.mjs"
    script = """
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const hostSrc = fs.readFileSync(process.argv[1], "utf8");
const killFn = hostSrc.slice(hostSrc.indexOf("function killHookTree("), hostSrc.indexOf("\\nfunction runHook("));

// 1. Successful synchronous taskkill terminates tree while parent PID is still alive (no premature child.kill).
let events = [];
let parentAlive = true;
vm.runInNewContext(killFn + "\\nkillHookTree(child);", {
    process: { platform: "win32" },
    spawnSync: (cmd, args, opts) => {
        events.push({ cmd, args, windowsHide: opts.windowsHide, parentAlive });
        return { status: 0, error: null };
    },
    child: { pid: 4242, kill: () => { parentAlive = false; events.push({ fallbackKill: true }); } },
});
assert.equal(events.length, 1);
assert.equal(events[0].parentAlive, true);
assert.equal(events[0].windowsHide, true);

// 2. Missing taskkill (ENOENT) does not throw uncaught error and falls back cleanly to child.kill("SIGKILL").
events = [];
vm.runInNewContext(killFn + "\\nkillHookTree(child);", {
    process: { platform: "win32" },
    spawnSync: () => ({ status: null, error: Object.assign(new Error("spawnSync taskkill ENOENT"), { code: "ENOENT" }) }),
    child: { pid: 4242, kill: (sig) => events.push({ fallbackSig: sig }) },
});
assert.deepEqual(events, [{ fallbackSig: "SIGKILL" }]);

// 3. Cold uv fallback continues past a blocked/failing first uv.exe to a healthy second uv.exe.
const pathsSrc = fs.readFileSync(process.argv[2], "utf8");
const coldFn = pathsSrc.slice(pathsSrc.indexOf("function resolveColdWindowsPython("), pathsSrc.indexOf("\\nexport function resolveBootstrapPython("));
const uvCalls = [];
const res = vm.runInNewContext(coldFn + "\\nresolveColdWindowsPython();", {
    process: {
        platform: "win32",
        env: { LOCALAPPDATA: "C:\\\\AppData\\\\Local", APPDATA: "", USERPROFILE: "C:\\\\Users\\\\u", PATH: "C:\\\\uv1;C:\\\\uv2" },
    },
    os: { homedir: () => "C:\\\\Users\\\\u" },
    path: path.win32,
    coldWinPythonCache: { key: "", value: "" },
    collectMatchingSubdirs: () => [],
    isUsableBootstrapPython: (p) => p === "C:\\\\Healthy\\\\python.exe",
    fs: { existsSync: (p) => p === "C:\\\\uv1\\\\uv.exe" || p === "C:\\\\uv2\\\\uv.exe" || p === "C:\\\\Healthy\\\\python.exe" },
    spawnSync: (exe, args) => {
        uvCalls.push(exe);
        if (exe === "C:\\\\uv1\\\\uv.exe") return { status: 1, error: new Error("SAC 4551"), stdout: "" };
        if (exe === "C:\\\\uv2\\\\uv.exe") return { status: 0, error: null, stdout: "C:\\\\Healthy\\\\python.exe\\r\\n" };
        if (exe === "C:\\\\Healthy\\\\python.exe") return { status: 0, error: null, stdout: "" };
        return { status: 1, error: null, stdout: "" };
    },
});
assert.equal(res, "C:\\\\Healthy\\\\python.exe");
assert.deepEqual(uvCalls, ["C:\\\\uv1\\\\uv.exe", "C:\\\\uv2\\\\uv.exe", "C:\\\\Healthy\\\\python.exe"]);
console.log("OK");
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script, str(host_js), str(paths_js)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout.strip() == "OK"
