/**
 * [Role] Root OpenCode v2 plugin entry shim (`@mindie-agent/oc`).
 * [Invariants] Resolves the paired active plugin payload recorded in `oc.json` when valid, falling back cleanly to the checkout's `./plugins/mindie-agent/opencode_host.mjs`.
 * [Timeouts & Locks] Synchronous local JSON manifest check (`<= 64 KiB`) plus dynamic ES module import; zero subprocesses at module load.
 * [Side-Effect Policy] Read-only at import time; delegates `setup(ctx)` to `opencode_host.mjs`.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { configPath, readJsonSafe } from "./plugins/mindie-agent/paths.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL_HOST = path.join(HERE, "plugins", "mindie-agent", "opencode_host.mjs");

function resolveActiveHostModule() {
    const cfgFile = configPath();
    const active = readJsonSafe(cfgFile);
    if (active && typeof active === "object") {
        const sourceRoot = active.source_root;
        const expectedVersion = active.product_validation?.native_version;
        if (typeof sourceRoot === "string" && path.isAbsolute(sourceRoot) && typeof expectedVersion === "string") {
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
