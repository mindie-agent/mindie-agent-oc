/**
 * [Role] Canonical MindIE-owned path resolution for the OpenCode (`oc`) JS host plugin.
 * [Invariants] Byte-for-byte aligned with `scripts/paths.py`. Never mutates `~/.config/opencode` or `~/.local/share/opencode`; all MindIE state lives under MindIE-owned `configHome`, `dataHome`, and `stateDir`.
 * [Timeouts & Locks] Pure path resolution and bounded local JSON reads (`<= 64 KiB`); cold Windows fallback interpreter discovery probes candidates with `<= 2.5s` `spawnSync` (`windowsHide: true`) and caches in-memory.
 * [Side-Effect Policy] Read-only except when `atomicWriteJson` is explicitly called on a MindIE-owned path.
 */

import { spawnSync } from "node:child_process";
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

export function parseSemverTuple(raw) {
    if (typeof raw !== "string") return null;
    const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(raw.trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

export function isNewerSemver(candidate, active) {
    const c = parseSemverTuple(candidate);
    const a = parseSemverTuple(active);
    if (!c) return false;
    if (!a) return true;
    for (let i = 0; i < 3; i += 1) {
        if (c[i] !== a[i]) return c[i] > a[i];
    }
    return false;
}

export function pidAlive(pid) {
    if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) {
        return false;
    }
    try {
        process.kill(pid, 0);
        return true;
    } catch (err) {
        return err?.code === "EPERM";
    }
}

export function readJsonSafe(targetPath) {
    try {
        const stat = fs.lstatSync(targetPath);
        if (!stat.isFile() || stat.size > MAX_CONFIG_BYTES) {
            process.stderr.write(`MindIE OpenCode config ignored (non-regular or oversize): ${targetPath}\n`);
            return null;
        }
        const raw = fs.readFileSync(targetPath, "utf8");
        const clean = raw.charCodeAt(0) === 0xFEFF ? raw.slice(1) : raw;
        const parsed = JSON.parse(clean);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            process.stderr.write(`MindIE OpenCode config ignored (not a JSON object): ${targetPath}\n`);
            return null;
        }
        return parsed;
    } catch (err) {
        if (err && err.code === "ENOENT") {
            return null;
        }
        process.stderr.write(
            `MindIE OpenCode config read failed (${targetPath}): ${err instanceof Error ? err.message : String(err)}\n`,
        );
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

export function admissionPath(explicitConfig = null) {
    const cfg = configPath(explicitConfig);
    const data = readJsonSafe(cfg);
    if (data && typeof data.admission_path === "string" && path.isAbsolute(data.admission_path)) {
        return data.admission_path;
    }
    return path.join(stateDir(explicitConfig), "admission.sqlite3");
}

export function summaryEndpointPath(explicitConfig = null) {
    return path.join(stateDir(explicitConfig), "summary-endpoint.json");
}

export function opencodeDbPath(explicitConfig = null) {
    const explicit = process.env.MINDIE_OPENCODE_DB;
    if (explicit && path.isAbsolute(explicit)) {
        return path.resolve(explicit);
    }
    const cfg = readJsonSafe(configPath(explicitConfig));
    if (cfg && typeof cfg.opencode_db === "string" && path.isAbsolute(cfg.opencode_db)) {
        return path.resolve(cfg.opencode_db);
    }
    const xdgData = process.env.XDG_DATA_HOME;
    let baseDir;
    if (xdgData && path.isAbsolute(xdgData)) {
        baseDir = path.join(path.resolve(xdgData), "opencode");
    } else {
        const testHome = process.env.OPENCODE_TEST_HOME;
        const homeDir = testHome && path.isAbsolute(testHome)
            ? path.resolve(testHome)
            : os.homedir();
        baseDir = path.join(path.resolve(homeDir, ".local", "share"), "opencode");
    }
    const nativeEnvDb = process.env.OPENCODE_DB;
    if (typeof nativeEnvDb === "string" && nativeEnvDb.trim() && nativeEnvDb.trim() !== ":memory:") {
        return path.resolve(baseDir, nativeEnvDb.trim());
    }
    return path.join(baseDir, "opencode.db");
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

const WIN_HOST_EXT_RE = /\.(?:exe|cmd|bat|ps1|com)$/i;
const WSL_DRVFS_RE = /^\/mnt\/[a-zA-Z](?:\/|$)/;
let coldWinPythonCache = { key: "", value: "" };

export function inWslJs() {
    if (process.platform === "win32") return false;
    if (process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP) return true;
    try {
        return fs.existsSync("/proc/sys/fs/binfmt_misc/WSLInterop");
    } catch {
        return false;
    }
}

const PY_INTERP_RE = /^(?:python|pypy)(?:\d+(?:\.\d+)*)?$/i;
const WIN_SHIM_BODY_RE = /(?:[a-z0-9_./"'$\\-]+\.(?:exe|cmd|bat)\b|\b(?:cygpath|wslpath|powershell(?:\.exe)?|cmd\.exe)\b|\\windows\\)/i;

function isWslLinuxExecutableJs(cand) {
    let fd = -1;
    try {
        fd = fs.openSync(cand, "r");
        const st = fs.fstatSync(fd);
        if (!st.isFile() || st.size < 4) return false;
        const buf = Buffer.alloc(Math.min(512, st.size));
        const read = fs.readSync(fd, buf, 0, buf.length, 0);
        if (read >= 4 && buf[0] === 0x7f && buf[1] === 0x45 && buf[2] === 0x4c && buf[3] === 0x46) {
            return true;
        }
        if (read < 2 || buf[0] !== 0x23 || buf[1] !== 0x21) return false;
        const text = buf.subarray(0, read).toString("latin1");
        const shebang = text.split(/\r?\n/, 1)[0].slice(2).trim();
        const parts = shebang.split(/\s+/).filter(Boolean);
        if (!parts.length || /^[a-zA-Z]:[\\/]/.test(parts[0])) return false;
        const lowShebang = shebang.toLowerCase();
        if (lowShebang.includes(".exe") || lowShebang.includes(".cmd") || lowShebang.includes(".bat") || lowShebang.includes("powershell") || lowShebang.includes("\\windows\\")) {
            return false;
        }
        let interp = parts[0];
        if (interp.endsWith("/env")) {
            interp = parts.slice(1).find((tok) => !tok.startsWith("-")) || "";
        }
        const interpBase = interp.slice(interp.lastIndexOf("/") + 1);
        if (!interpBase) return false;
        if (PY_INTERP_RE.test(interpBase)) return true;
        return !WIN_SHIM_BODY_RE.test(text);
    } catch {
        return false;
    } finally {
        if (fd >= 0) {
            try { fs.closeSync(fd); } catch {}
        }
    }
}

export function isUsableBootstrapPython(cand) {
    if (typeof cand !== "string" || !cand.trim()) return false;
    const norm = cand.replace(/\\/g, "/").toLowerCase();
    const base = norm.slice(norm.lastIndexOf("/") + 1);
    if (base === "pythonw.exe" || base === "pythonw" || norm.includes("/windowsapps/")) {
        return false;
    }
    if (process.platform !== "win32") {
        if (WIN_HOST_EXT_RE.test(norm)) return false;
        if (inWslJs() && WSL_DRVFS_RE.test(cand)) {
            return isWslLinuxExecutableJs(cand);
        }
    }
    return fs.existsSync(cand);
}

function collectMatchingSubdirs(parentDir, prefix, relExe) {
    const out = [];
    if (!parentDir) return out;
    try {
        const entries = fs.readdirSync(parentDir, { withFileTypes: true })
            .filter((d) => d.isDirectory() && d.name.toLowerCase().startsWith(prefix))
            .map((d) => d.name)
            .sort()
            .reverse();
        for (const name of entries) {
            const full = path.join(parentDir, name, relExe);
            if (fs.existsSync(full)) out.push(full);
        }
    } catch {}
    return out;
}

function resolveColdWindowsPython() {
    const localApp = process.env.LOCALAPPDATA || "";
    const appData = process.env.APPDATA || "";
    const userProf = process.env.USERPROFILE || os.homedir();
    const pathEnv = process.env.PATH || process.env.Path || "";
    const cacheKey = `${localApp}|${appData}|${userProf}|${pathEnv}`;
    if (coldWinPythonCache.key === cacheKey && coldWinPythonCache.value && fs.existsSync(coldWinPythonCache.value)) {
        return coldWinPythonCache.value;
    }
    const candidates = [
        ...collectMatchingSubdirs(localApp ? path.join(localApp, "Programs", "uv-python") : "", "cpython-3.", "python.exe"),
        ...collectMatchingSubdirs(appData ? path.join(appData, "uv", "python") : "", "cpython-3.", "python.exe"),
        ...collectMatchingSubdirs(localApp ? path.join(localApp, "uv", "python") : "", "cpython-3.", "python.exe"),
        ...collectMatchingSubdirs(localApp ? path.join(localApp, "Programs", "Python") : "", "python3", "python.exe"),
    ];
    for (const dir of pathEnv.split(path.delimiter)) {
        if (!dir || dir.toLowerCase().includes("\\windowsapps")) continue;
        for (const exe of ["python.exe", "python3.exe"]) {
            const cand = path.join(dir, exe);
            if (fs.existsSync(cand)) candidates.push(cand);
        }
    }
    const seen = new Set();
    for (const cand of candidates) {
        const normKey = cand.toLowerCase();
        if (seen.has(normKey)) continue;
        seen.add(normKey);
        if (seen.size > 10) break;
        const probe = spawnSync(
            cand,
            ["-S", "-c", "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)"],
            { timeout: 2500, windowsHide: true, stdio: "ignore" },
        );
        if (!probe.error && probe.status === 0) {
            coldWinPythonCache = { key: cacheKey, value: cand };
            return cand;
        }
    }
    const uvCands = [];
    for (const dir of pathEnv.split(path.delimiter)) {
        if (dir && !dir.toLowerCase().includes("\\windowsapps") && fs.existsSync(path.join(dir, "uv.exe"))) {
            uvCands.push(path.join(dir, "uv.exe"));
        }
    }
    if (localApp) {
        uvCands.push(path.join(localApp, "Microsoft", "WinGet", "Links", "uv.exe"));
        uvCands.push(...collectMatchingSubdirs(path.join(localApp, "Microsoft", "WinGet", "Packages"), "astral-sh.uv_", "uv.exe"));
    }
    if (userProf) uvCands.push(path.join(userProf, ".local", "bin", "uv.exe"));
    const seenUv = new Set();
    for (const uvExe of uvCands) {
        const uvKey = uvExe.toLowerCase();
        if (seenUv.has(uvKey) || !fs.existsSync(uvExe)) continue;
        seenUv.add(uvKey);
        if (seenUv.size > 4) break;
        const found = spawnSync(uvExe, ["python", "find", ">=3.11"], { timeout: 2000, windowsHide: true, encoding: "utf8" });
        const pyPath = !found.error && found.status === 0 ? String(found.stdout || "").trim() : "";
        if (!pyPath || !isUsableBootstrapPython(pyPath)) continue;
        const pyProbe = spawnSync(
            pyPath,
            ["-S", "-c", "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)"],
            { timeout: 2000, windowsHide: true, stdio: "ignore" },
        );
        if (!pyProbe.error && pyProbe.status === 0) {
            coldWinPythonCache = { key: cacheKey, value: pyPath };
            return pyPath;
        }
    }
    return "python";
}

export function resolveBootstrapPython(explicitConfig = null) {
    if (process.env.MINDIE_OC_PYTHON && isUsableBootstrapPython(process.env.MINDIE_OC_PYTHON)) {
        return process.env.MINDIE_OC_PYTHON;
    }
    const cfg = readJsonSafe(configPath(explicitConfig));
    if (cfg && typeof cfg.python === "string" && isUsableBootstrapPython(cfg.python)) {
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
        if (isUsableBootstrapPython(cand)) {
            return cand;
        }
    }
    for (const sibling of ["oc.json", "agy.json", "wb.json", "codex.json", "cc.json", "kimi.json"]) {
        const scfg = readJsonSafe(path.join(configHome(), sibling));
        if (scfg && typeof scfg.python === "string" && isUsableBootstrapPython(scfg.python)) {
            return scfg.python;
        }
    }
    return isWin ? resolveColdWindowsPython() : "python3";
}
