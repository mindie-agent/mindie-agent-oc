/**
 * [Role] Canonical MindIE-owned path resolution for the OpenCode (`oc`) JS host plugin.
 * [Invariants] Byte-for-byte aligned with `scripts/paths.py`. Never mutates `~/.config/opencode` or `~/.local/share/opencode`; all MindIE state lives under MindIE-owned `configHome`, `dataHome`, and `stateDir`.
 * [Timeouts & Locks] Pure path resolution and bounded local JSON reads (`<= 64 KiB`); zero subprocesses or network calls.
 * [Side-Effect Policy] Read-only except when `atomicWriteJson` is explicitly called on a MindIE-owned path.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MAX_CONFIG_BYTES = 65536;

export function pluginRoot() {
    const explicit = process.env.OC_PLUGIN_ROOT;
    if (explicit && path.isAbsolute(explicit)) {
        return path.resolve(explicit);
    }
    return HERE;
}

export function configHome() {
    const explicit = process.env.MINDIE_CONFIG_HOME;
    if (explicit && path.isAbsolute(explicit)) {
        return path.resolve(explicit);
    }
    return path.resolve(os.homedir(), ".config", "mindie-agent");
}

export function dataHome() {
    const explicit = process.env.MINDIE_DATA_HOME;
    if (explicit && path.isAbsolute(explicit)) {
        return path.resolve(explicit);
    }
    return path.resolve(os.homedir(), ".local", "share", "mindie-agent");
}

export function configPath(explicit = null) {
    if (explicit) {
        return path.resolve(String(explicit));
    }
    const envPath = process.env.MINDIE_AGENT_CONFIG;
    if (envPath) {
        return path.resolve(envPath);
    }
    return path.join(configHome(), "oc.json");
}

export function readJsonSafe(targetPath) {
    try {
        const stat = fs.statSync(targetPath);
        if (!stat.isFile() || stat.size > MAX_CONFIG_BYTES) {
            return null;
        }
        const raw = fs.readFileSync(targetPath, "utf8");
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

export function stateDir(explicitConfig = null) {
    const cfg = configPath(explicitConfig);
    const data = readJsonSafe(cfg);
    if (data && typeof data.state_dir === "string" && path.isAbsolute(data.state_dir)) {
        return data.state_dir;
    }
    return path.join(dataHome(), "oc-state");
}

export function summaryEndpointPath(explicitConfig = null) {
    return path.join(stateDir(explicitConfig), "summary-endpoint.json");
}

export function opencodeDbPath() {
    const explicit = process.env.MINDIE_OPENCODE_DB;
    if (explicit && path.isAbsolute(explicit)) {
        return path.resolve(explicit);
    }
    const xdgData = process.env.XDG_DATA_HOME;
    if (xdgData && path.isAbsolute(xdgData)) {
        return path.join(path.resolve(xdgData), "opencode", "opencode.db");
    }
    const testHome = process.env.OPENCODE_TEST_HOME;
    const homeDir = testHome && path.isAbsolute(testHome)
        ? path.resolve(testHome)
        : os.homedir();
    return path.join(path.resolve(homeDir, ".local", "share"), "opencode", "opencode.db");
}

export function atomicWriteJson(targetPath, payload) {
    const resolved = path.resolve(targetPath);
    const dir = path.dirname(resolved);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const tmp = path.join(dir, `.${path.basename(resolved)}.${process.pid}.${Date.now()}.tmp`);
    const text = `${JSON.stringify(payload, null, 2)}\n`;
    try {
        fs.writeFileSync(tmp, text, { encoding: "utf8", mode: 0o600 });
        fs.renameSync(tmp, resolved);
    } catch (err) {
        try {
            fs.unlinkSync(tmp);
        } catch {
            // Best-effort cleanup of unrenamed temp file.
        }
        throw err;
    }
    return resolved;
}

export function resolveBootstrapPython(explicitConfig = null) {
    if (process.env.MINDIE_OC_PYTHON && fs.existsSync(process.env.MINDIE_OC_PYTHON)) {
        return process.env.MINDIE_OC_PYTHON;
    }
    const cfg = readJsonSafe(configPath(explicitConfig));
    if (cfg && typeof cfg.python === "string" && fs.existsSync(cfg.python)) {
        return cfg.python;
    }
    const isWin = process.platform === "win32";
    const relPy = isWin ? path.join("Scripts", "python.exe") : path.join("bin", "python");
    const candidates = [
        path.join(dataHome(), "oc-venv", relPy),
        path.join(pluginRoot(), "..", "..", ".venv", relPy),
        path.join(dataHome(), "agy-venv", relPy),
        path.join(dataHome(), "wb-venv", relPy),
    ];
    for (const cand of candidates) {
        if (fs.existsSync(cand)) {
            return cand;
        }
    }
    for (const sibling of ["oc.json", "agy.json", "wb.json", "codex.json", "cc.json", "kimi.json"]) {
        const scfg = readJsonSafe(path.join(configHome(), sibling));
        if (scfg && typeof scfg.python === "string" && fs.existsSync(scfg.python)) {
            return scfg.python;
        }
    }
    return isWin ? "python" : "python3";
}
