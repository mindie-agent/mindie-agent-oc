/**
 * [Role] Root OpenCode v2 plugin entry shim (`@mindie-agent/oc`) that resolves the active thin host module.
 * [Invariants] Preserves the installed checkout root (`MINDIE_OC_INSTALLED_ROOT`) and honors `oc.json` `source_root` recorded either as `plugins/mindie-agent` (rebind) or as the outer repository root; only delegates to a module that exists and matches the recorded native version.
 * [Timeouts & Locks] Synchronous bounded JSON reads (`<= 64 KiB`) and one dynamic ES module import; zero subprocesses at load.
 * [Side-Effect Policy] Read-only at import time; delegates `setup(ctx)` to `opencode_host.mjs`.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { configPath, isNewerSemver, readJsonSafe } from "./plugins/mindie-agent/opencode_host.mjs";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOCAL_PLUGIN_DIR = path.join(HERE, "plugins", "mindie-agent"), LOCAL_HOST = path.join(LOCAL_PLUGIN_DIR, "opencode_host.mjs");
export function resolveActiveHostModule(explicitConfig = null) {
    if (!process.env.MINDIE_OC_INSTALLED_ROOT) {
        process.env.MINDIE_OC_INSTALLED_ROOT = HERE;
    }
    const localVersion = readJsonSafe(path.join(LOCAL_PLUGIN_DIR, "plugin.json"))?.version;
    const active = readJsonSafe(configPath(explicitConfig));
    const sourceRoot = active && typeof active === "object" ? active.source_root : null;
    const expectedVersion = active?.product_validation?.native_version;
    if (
        typeof sourceRoot === "string"
        && path.isAbsolute(sourceRoot)
        && (!expectedVersion || !isNewerSemver(localVersion, expectedVersion))
    ) {
        const candidates = [
            path.join(sourceRoot, "opencode_host.mjs"),
            path.join(sourceRoot, "plugins", "mindie-agent", "opencode_host.mjs"),
        ];
        for (const candidate of candidates) {
            if (!fs.existsSync(candidate)) continue;
            const foundVersion = readJsonSafe(path.join(path.dirname(candidate), "plugin.json"))?.version;
            if (!expectedVersion || foundVersion === expectedVersion) {
                return candidate;
            }
        }
        process.stderr.write(
            `[mindie-agent-oc] Active oc.json payload mismatch at ${sourceRoot} (expected ${expectedVersion ?? "unversioned"}); falling back to checkout host module.\n`,
        );
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
