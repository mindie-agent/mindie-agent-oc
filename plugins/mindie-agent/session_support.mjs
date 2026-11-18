/**
 * [Role] Session-support helpers for the OpenCode v2 host adapter (`session_support.mjs`).
 * [Invariants]
 * - `mayCaptureCommunitySession` evaluates `mindie-community.json`, `mindie-consent.json`, and `project_roots` in pure Node.js without opening SQLite or spawning subprocesses.
 * - `safeGetSession` never throws: a missing or failing `ctx.session.get` degrades to `null` with one stderr warning.
 * - `renderSkillSystemBlock` caches stripped `SKILL.md` in memory and embeds only host-verified paths; it never spawns Python or injects model-supplied text.
 * - `parseModelSpec` accepts only `provider/id[#variant]` and returns `undefined` for anything else (zero side effects).
 * [Timeouts & Locks] Pure in-memory/local metadata reads plus one awaited host call; no timer, lock, or subprocess.
 * [Side-Effect Policy] Read-only against the host: writes nothing, spawns nothing, and only emits a stderr diagnostic line on failure.
 */

import fs from "node:fs";
import path from "node:path";
import {
    admissionPath,
    communityConfigPath,
    pluginRoot,
    readJsonSafe,
    resolveBootstrapPython,
    stateDir,
} from "./paths.mjs";

let cachedSkill = { path: "", mtimeMs: -1, body: "" };

export function admissionMtimeSignature(sessionID = null) {
    const base = admissionPath();
    let sig = null;
    try {
        const mainStat = fs.statSync(base);
        let walMtime = 0;
        try {
            walMtime = fs.statSync(`${base}-wal`).mtimeMs;
        } catch {
            walMtime = 0;
        }
        sig = `${mainStat.mtimeMs}:${walMtime}`;
    } catch (err) {
        if (err && err.code !== "ENOENT") {
            process.stderr.write(`MindIE OpenCode admission stat failed (${base}): ${err.message || err}\n`);
            sig = "unreadable";
        }
    }
    if (typeof sessionID === "string" && sessionID.length > 0 && !sessionID.includes("/") && !sessionID.includes("\\")) {
        try {
            const pStat = fs.statSync(path.join(stateDir(), "pending-activations", `${sessionID}.json`));
            return `${sig || "0"}:p${pStat.mtimeMs}`;
        } catch {}
    }
    return sig;
}

export function parseModelSpec(rawModel) {
    if (typeof rawModel !== "string") return undefined;
    const trimmed = rawModel.trim();
    if (!trimmed || trimmed === "default") return undefined;
    const [providerAndId, variant] = trimmed.split("#", 2);
    const slashIdx = providerAndId.indexOf("/");
    if (slashIdx <= 0 || slashIdx >= providerAndId.length - 1) return undefined;
    const spec = {
        providerID: providerAndId.slice(0, slashIdx),
        id: providerAndId.slice(slashIdx + 1),
    };
    if (variant) {
        spec.variant = variant;
    }
    return spec;
}

export async function safeGetSession(ctx, sessionID) {
    if (!sessionID || typeof ctx?.session?.get !== "function") {
        return null;
    }
    try {
        return await ctx.session.get({ sessionID });
    } catch (err) {
        process.stderr.write(`MindIE OpenCode session.get warning (${sessionID}): ${err?.message || err}\n`);
        return null;
    }
}

export function stripSkillFrontmatter(markdown) {
    if (typeof markdown !== "string") return "";
    return markdown.replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "");
}

export function loadCachedSkillBody(skillPath = path.join(pluginRoot(), "skills", "mindie-agent", "SKILL.md")) {
    try {
        const stat = fs.statSync(skillPath);
        if (cachedSkill.path === skillPath && cachedSkill.mtimeMs === stat.mtimeMs) {
            return cachedSkill.body;
        }
        const raw = fs.readFileSync(skillPath, "utf8");
        const body = stripSkillFrontmatter(raw).trim();
        cachedSkill = { path: skillPath, mtimeMs: stat.mtimeMs, body };
        return body;
    } catch {
        return "";
    }
}

function canonicalScopePath(rawPath) {
    if (typeof rawPath !== "string" || !path.isAbsolute(rawPath.trim())) {
        return null;
    }
    const resolved = path.resolve(rawPath.trim());
    let real = resolved;
    try {
        real = fs.realpathSync(resolved);
    } catch {
        real = resolved;
    }
    return process.platform === "win32" ? real.replace(/\\/g, "/").toLowerCase() : real;
}

export function mayCaptureCommunitySession(directory, explicitConfig = null) {
    const commFile = communityConfigPath(explicitConfig);
    if (!fs.existsSync(commFile)) {
        return false;
    }
    const comm = readJsonSafe(commFile);
    if (!comm || comm.schema !== "mindie-community-config/1" || comm.enabled !== true) {
        return false;
    }
    if (typeof comm.generation !== "string" || !comm.generation.length) {
        return false;
    }
    if (typeof comm.enabled_at !== "number" || !Number.isFinite(comm.enabled_at) || comm.enabled_at <= 0) {
        return false;
    }
    if (!Array.isArray(comm.project_roots) || comm.project_roots.length === 0) {
        return false;
    }
    if (typeof comm.consent_config !== "string" || !path.isAbsolute(comm.consent_config)) {
        return false;
    }
    const consent = readJsonSafe(comm.consent_config);
    if (!consent || consent.schema !== "mindie-consent/1" || consent.choice !== "contribute") {
        return false;
    }
    const cand = canonicalScopePath(directory);
    if (!cand) {
        return false;
    }
    for (const rawRoot of comm.project_roots) {
        const root = canonicalScopePath(rawRoot);
        if (!root) continue;
        const prefix = root.endsWith("/") ? root : `${root}/`;
        if (cand === root || cand.startsWith(prefix)) {
            return true;
        }
    }
    return false;
}

export function renderSkillSystemBlock(sessionID, state = null) {
    const py = resolveBootstrapPython();
    const scriptsDir = path.join(pluginRoot(), "scripts");
    const skillPath = typeof state?.skill_path === "string"
        ? state.skill_path
        : path.join(pluginRoot(), "skills", "mindie-agent", "SKILL.md");
    const knowledgeCli = typeof state?.knowledge_cli === "string"
        ? state.knowledge_cli
        : path.join(scriptsDir, "knowledge_cli.py");
    const rsiLoop = typeof state?.rsi_loop === "string"
        ? state.rsi_loop
        : path.join(scriptsDir, "rsi_loop.py");
    const skillBody = loadCachedSkillBody(skillPath);
    return [
        `<mindie-agent-skill session="${sessionID}">`,
        "MindIE Agent capabilities are enabled for this OpenCode main session.",
        "Host-verified execution bindings:",
        `- OPENCODE_SESSION_ID=${sessionID}`,
        `- Python runtime: ${py}`,
        `- Knowledge CLI: ${knowledgeCli}`,
        `- RSI Loop helper: ${rsiLoop}`,
        `Shell commands created in this session automatically receive OPENCODE_SESSION_ID=${sessionID}.`,
        "",
        skillBody,
        "</mindie-agent-skill>",
    ].join("\n");
}
