/**
 * [Role] Root OpenCode v2 plugin entry shim (`@mindie-agent/oc`).
 * [Invariants] Preserves newly installed checkout root (`MINDIE_OC_INSTALLED_ROOT`) for background promotion, and only delegates to `oc.json`'s active `source_root` when its version is >= the installed checkout version.
 * [Timeouts & Locks] Synchronous local JSON manifest check (`<= 64 KiB`) plus dynamic ES module import; zero subprocesses at module load.
 * [Side-Effect Policy] Read-only at import time; delegates `setup(ctx)` to `opencode_host.mjs`.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { configPath, readJsonSafe } from "./plugins/mindie-agent/paths.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL_PLUGIN_DIR = path.join(HERE, "plugins", "mindie-agent");
const LOCAL_HOST = path.join(LOCAL_PLUGIN_DIR, "opencode_host.mjs");

function parseSemverTuple(raw) {
    if (typeof raw !== "string") return null;
    const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(raw.trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

function isNewerSemver(candidate, active) {
    const c = parseSemverTuple(candidate);
    const a = parseSemverTuple(active);
    if (!c) return false;
    if (!a) return true;
    for (let i = 0; i < 3; i += 1) {
        if (c[i] !== a[i]) return c[i] > a[i];
    }
    return false;
}

export function resolveActiveHostModule(explicitConfig = null) {
    if (!process.env.MINDIE_OC_INSTALLED_ROOT) {
        process.env.MINDIE_OC_INSTALLED_ROOT = HERE;
    }
    const localManifest = readJsonSafe(path.join(LOCAL_PLUGIN_DIR, "plugin.json"));
    const localVersion = localManifest?.version;
    const cfgFile = configPath(explicitConfig);
    const active = readJsonSafe(cfgFile);
    if (active && typeof active === "object") {
        const sourceRoot = active.source_root;
        const expectedVersion = active.product_validation?.native_version;
        if (
            typeof sourceRoot === "string" &&
            path.isAbsolute(sourceRoot) &&
            typeof expectedVersion === "string" &&
            !isNewerSemver(localVersion, expectedVersion)
        ) {
            const candidatePluginDir = path.join(sourceRoot, "plugins", "mindie-agent");
            const candidateManifest = readJsonSafe(path.join(candidatePluginDir, "plugin.json"));
            const candidateHost = path.join(candidatePluginDir, "opencode_host.mjs");
            if (candidateManifest?.version === expectedVersion && fs.existsSync(candidateHost)) {
                return candidateHost;
            }
        }
    }
    return LOCAL_HOST;
}

export default {
    id: "mindie-agent",
    async setup(ctx) {
        const hostPath = resolveActiveHostModule();
        const mod = await import(pathToFileURL(hostPath).href);
        const target = mod?.default ?? mod;
        if (typeof target?.setup !== "function") {
            throw new Error(`Invalid MindIE OpenCode host module at ${hostPath}`);
        }
        return await target.setup(ctx);
    },
};
