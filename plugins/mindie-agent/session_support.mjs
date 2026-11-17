/**
 * [Role] Session-support helpers for the OpenCode v2 host adapter (`session_support.mjs`).
 * [Invariants]
 * - `admissionMtimeSignature` only reads `admission.sqlite3` metadata and the per-session pending-activation file; it never opens or mutates the database.
 * - `safeGetSession` never throws: a missing or failing `ctx.session.get` degrades to `null` with one stderr warning.
 * - `renderSkillSystemBlock` strips YAML frontmatter from `SKILL.md` and embeds only the host-verified interpreter and MindIE-owned CLI paths; it never injects model-supplied text.
 * - `parseModelSpec` accepts only `provider/id[#variant]` and returns `undefined` for anything else (zero side effects).
 * [Timeouts & Locks] Pure `statSync`/`readFileSync` reads plus one awaited host call; no timer, lock, or subprocess.
 * [Side-Effect Policy] Read-only against the host: writes nothing, spawns nothing, and only emits a stderr diagnostic line on failure.
 */

import fs from "node:fs";
import path from "node:path";
import { admissionPath, resolveBootstrapPython, stateDir } from "./paths.mjs";

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

export function renderSkillSystemBlock(sessionID, state) {
    const py = resolveBootstrapPython();
    let skillBody = "";
    if (typeof state.skill_path === "string" && fs.existsSync(state.skill_path)) {
        skillBody = stripSkillFrontmatter(fs.readFileSync(state.skill_path, "utf8"));
    }
    return [
        `<mindie-agent-skill session="${sessionID}">`,
        "MindIE Agent knowledge flywheel is ACTIVE for this OpenCode main session.",
        "Host-verified execution bindings:",
        `- OPENCODE_SESSION_ID=${sessionID}`,
        `- Python runtime: ${py}`,
        `- Private Knowledge CLI: ${state.knowledge_cli}`,
        `- RSI Loop helper: ${state.rsi_loop}`,
        `- Experiment Ledger helper: ${state.experiment_ledger}`,
        `Shell commands created in this session automatically receive OPENCODE_SESSION_ID=${sessionID}.`,
        "",
        skillBody.trim(),
        "</mindie-agent-skill>",
    ].join("\n");
}
