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
        json.JSONDecodeError("bad json", "{", 1)
    ) == ("temporary_network", False, "invalid_release_json")

    for local_bug in (
        ReleaseResponseTooLarge("oversize"),
        http.client.InvalidURL("http://bad host"),
        http.client.ImproperConnectionState("double send"),
        http.client.NotConnected("closed"),
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
