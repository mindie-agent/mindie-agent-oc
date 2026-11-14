/**
 * [Role] Root OpenCode v2 plugin entry shim (`@mindie-agent/oc`).
 * [Invariants] Preserves newly installed checkout root (`MINDIE_OC_INSTALLED_ROOT`) for background promotion, and only delegates to `oc.json`'s active `source_root` when its version is >= the installed checkout version.
 * [Timeouts & Locks] Synchronous local JSON manifest check (`<= 64 KiB`) plus dynamic ES module import; zero subprocesses at module load.
 * [Side-Effect Policy] Read-only at import time; delegates `setup(ctx)` to `opencode_host.mjs`.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { configPath, isNewerSemver, readJsonSafe } from "./plugins/mindie-agent/paths.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL_PLUGIN_DIR = path.join(HERE, "plugins", "mindie-agent");
const LOCAL_HOST = path.join(LOCAL_PLUGIN_DIR, "opencode_host.mjs");

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
            process.stderr.write(
                `[mindie-agent-oc] Active oc.json payload mismatch at ${sourceRoot} (expected ${expectedVersion}, found ${candidateManifest?.version ?? "missing"}); falling back to checkout host module.\n`,
            );
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
