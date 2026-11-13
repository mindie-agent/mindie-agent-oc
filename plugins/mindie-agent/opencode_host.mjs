/**
 * [Role] Native OpenCode v2 (`@opencode-ai/plugin` >= 2.2.0) host adapter for MindIE Agent (`oc`).
 * [Invariants]
 * - Default MCP (`mindie-remote-dev`, `mindie-tools`) registers zero `knowledge_*` tools; MCP execution budget is 660000ms (>= 600s + 60s cleanup grace).
 * - Passive Default: unactivated sessions incur zero subprocess spawns on ordinary prompts/contexts (`!fs.existsSync(admission.sqlite3)` fast-path) and zero system-prompt additions.
 * - Session Isolation: `SKILL.md` system guidance, `OPENCODE_SESSION_ID` shell env, and `session.idle` capture activate strictly per-`sessionID` after an explicit `/mindie-agent` or `$mindie-agent` gesture in a top-level session (`!parentID && !forkSessionID`).
 * - Zero Console Popup (P1.5): every `spawn` uses `windowsHide: true`, and Windows background spawns never combine `detached: true` with `windowsHide: true`.
 * [Timeouts & Locks] Hook subprocess calls enforce a 10500ms outer Node deadline (`>= HOOK_TOTAL 10.0s`); loopback summary server binds `127.0.0.1:0` with `unref()`.
 * [Side-Effect Policy] Delegates state mutations to MindIE Python scripts (`mindie_launch.py`, `capture_stop.py`) and writes only `summary-endpoint.json` under MindIE `stateDir()`.
 */

import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import {
    admissionPath,
    atomicWriteJson,
    configPath,
    dataHome,
    opencodeDbPath,
    pluginRoot,
    readJsonSafe,
    resolveBootstrapPython,
    stateDir,
    summaryEndpointPath,
} from "./paths.mjs";

const GESTURE_PREFIX_RE = /^\s*(?:\/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?=\s|$)/;
const MCP_WIRE_RE = /^(?:mcp[_:])?(?:mindie[-_]agent[-_:])?(mindie[-_]remote[-_]dev|mindie[-_]tools)[_:]([A-Za-z0-9_.-]+)$/;
const SHELL_TOKEN_INLINE_RE = /\r?\n# __mindie_oc_sid:([0-9a-f]{24})(?=$|['"\s])/;
const TERMINAL_SESSION_EVENTS = new Set([
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.idle",
]);
const HOOK_NODE_TIMEOUT_MS = 10500;
const MAX_HOOK_STDOUT_BYTES = 1024 * 1024;
const SHELL_QUEUE_TTL_MS = 30000;
const CAPTURE_COALESCE_MS = 250;
const BG_UPGRADE_BACKOFF_MS = 60000;
const MAX_SUMMARY_BODY_BYTES = 512 * 1024;
const FORWARDED_ENV_KEYS = [
    "MINDIE_CONFIG_HOME",
    "MINDIE_DATA_HOME",
    "MINDIE_AGENT_CONFIG",
    "MINDIE_DIAGNOSTICS_ROOT",
    "MINDIE_OPENCODE_DB",
    "MINDIE_OC_INSTALLED_ROOT",
    "MINDIE_OC_PYTHON",
    "MINDIE_OC_BIN",
    "MINDIE_SUMMARY_MODEL",
    "MINDIE_SUMMARY_ENDPOINT",
    "MINDIE_SUMMARY_TOKEN",
    "OPENCODE_DB",
    "OPENCODE_TEST_HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
];

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

function resolveInstalledCheckoutRoot() {
    const explicit = process.env.MINDIE_OC_INSTALLED_ROOT;
    if (explicit && path.isAbsolute(explicit)) {
        const resolved = path.resolve(explicit);
        if (fs.existsSync(path.join(resolved, "plugins", "mindie-agent", "plugin.json"))) {
            return resolved;
        }
    }
    return path.resolve(pluginRoot(), "..", "..");
}

function maybeTriggerBackgroundPluginSync() {
    if (process.env.MINDIE_CONFIG_HOME && process.env.MINDIE_OC_ALLOW_BG_UPDATE !== "1") {
        return false;
    }
    try {
        const installedRoot = resolveInstalledCheckoutRoot();
        const manifest = readJsonSafe(path.join(installedRoot, "plugins", "mindie-agent", "plugin.json"));
        const cfgFile = configPath();
        const activeCfg = readJsonSafe(cfgFile);
        const pluginVer = manifest?.version;
        const activeVer = activeCfg?.product_validation?.native_version;
        if (activeCfg && !isNewerSemver(pluginVer, activeVer)) {
            return false;
        }
        const statusFile = path.join(dataHome(), "oc-component-update-status.json");
        const prevStatus = readJsonSafe(statusFile);
        const nowSec = Date.now() / 1000;
        if (prevStatus && typeof prevStatus.checked_at === "number" && (nowSec - prevStatus.checked_at) * 1000 < BG_UPGRADE_BACKOFF_MS) {
            return false;
        }
        atomicWriteJson(statusFile, { checked_at: nowSec, status: "in_progress" });
        const py = resolveBootstrapPython();
        const candUpdatePy = path.join(installedRoot, "plugins", "mindie-agent", "scripts", "component_update.py");
        const updatePy = fs.existsSync(candUpdatePy)
            ? candUpdatePy
            : path.join(pluginRoot(), "scripts", "component_update.py");
        const child = spawn(py, ["-I", updatePy, "--config", cfgFile, "--source", installedRoot, "--non-blocking"], {
            env: { ...process.env, ...buildMindieEnv(), MINDIE_OC_INSTALLED_ROOT: installedRoot },
            stdio: ["ignore", "ignore", "ignore"],
            windowsHide: true,
            detached: process.platform !== "win32",
        });
        child.on("error", () => {});
        child.unref();
        return true;
    } catch {
        return false;
    }
}

function buildMindieEnv() {
    const env = {
        OC_PLUGIN_ROOT: pluginRoot(),
        PYTHONIOENCODING: "utf-8",
    };
    for (const key of FORWARDED_ENV_KEYS) {
        const val = process.env[key];
        if (typeof val === "string" && val.length > 0) {
            env[key] = val;
        }
    }
    return env;
}

function admissionMtimeSignature(sessionID = null) {
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
    } catch {
        sig = null;
    }
    if (typeof sessionID === "string" && sessionID.length > 0 && !sessionID.includes("/") && !sessionID.includes("\\")) {
        try {
            const pStat = fs.statSync(path.join(stateDir(), "pending-activations", `${sessionID}.json`));
            return `${sig || "0"}:p${pStat.mtimeMs}`;
        } catch {}
    }
    return sig;
}

function killHookTree(child) {
    if (process.platform !== "win32" && typeof child?.pid === "number" && child.pid > 0) {
        try {
            process.kill(-child.pid, "SIGKILL");
            return;
        } catch {
            // Fall through to direct child kill.
        }
    }
    try {
        child.kill("SIGKILL");
    } catch {
        // Process already exited.
    }
}

function runHook(op, payload) {
    const py = resolveBootstrapPython();
    const launchPy = path.join(pluginRoot(), "scripts", "mindie_launch.py");
    const childEnv = { ...process.env, ...buildMindieEnv() };
    return new Promise((resolve, reject) => {
        const child = spawn(py, ["-I", launchPy, "hook", op], {
            env: childEnv,
            stdio: ["pipe", "pipe", "pipe"],
            windowsHide: true,
            detached: process.platform !== "win32",
        });
        let stdout = "";
        let stdoutOverflow = false;
        let stderr = "";
        let settled = false;
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            killHookTree(child);
            reject(new Error(`MindIE OpenCode hook '${op}' timed out after ${HOOK_NODE_TIMEOUT_MS}ms`));
        }, HOOK_NODE_TIMEOUT_MS);
        if (typeof timer.unref === "function") {
            timer.unref();
        }
        child.stdout.setEncoding("utf8");
        child.stderr.setEncoding("utf8");
        child.stdout.on("data", (chunk) => {
            if (stdout.length + chunk.length <= MAX_HOOK_STDOUT_BYTES) {
                stdout += chunk;
            } else {
                stdoutOverflow = true;
            }
        });
        child.stderr.on("data", (chunk) => {
            if (stderr.length < 8192) stderr += chunk;
        });
        child.on("error", (err) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(err);
        });
        child.on("close", (code) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            if (code !== 0) {
                reject(new Error(`MindIE OpenCode hook '${op}' exited with ${code}: ${stderr.trim() || stdout.trim()}`));
                return;
            }
            if (stdoutOverflow) {
                reject(new Error(`MindIE OpenCode hook '${op}' exceeded ${MAX_HOOK_STDOUT_BYTES} stdout byte cap`));
                return;
            }
            try {
                const line = stdout.trim();
                resolve(line ? JSON.parse(line) : {});
            } catch (err) {
                reject(new Error(`Invalid JSON from MindIE hook '${op}': ${err.message}`));
            }
        });
        child.stdin.on("error", () => {});
        child.stdin.end(JSON.stringify(payload ?? {}));
    });
}

function spawnDetachedCapture(payload) {
    const py = resolveBootstrapPython();
    const capturePy = path.join(pluginRoot(), "scripts", "capture_stop.py");
    const child = spawn(py, ["-I", capturePy, "--event-json", JSON.stringify(payload ?? {})], {
        env: { ...process.env, ...buildMindieEnv() },
        stdio: "ignore",
        windowsHide: true,
        detached: process.platform !== "win32",
    });
    child.on("error", () => {});
    child.unref();
}

function parseModelSpec(rawModel) {
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

async function safeGetSession(ctx, sessionID) {
    if (!sessionID || typeof ctx?.session?.get !== "function") {
        return null;
    }
    try {
        return await ctx.session.get({ sessionID });
    } catch {
        return null;
    }
}

function renderSkillSystemBlock(sessionID, state) {
    const py = resolveBootstrapPython();
    let skillBody = "";
    if (typeof state.skill_path === "string" && fs.existsSync(state.skill_path)) {
        skillBody = fs.readFileSync(state.skill_path, "utf8");
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

export default {
    id: "mindie-agent",
    async setup(ctx) {
        let disposed = false;
        let summaryServer = null;
        let summaryServerPromise = null;
        const sessionStateCache = new Map();
        const pendingShellQueue = [];
        const pendingShellTokens = new Map();
        const recentCaptureBySession = new Map();

        const defaultDirectory = () =>
            ctx?.location?.directory || ctx?.location?.worktree || process.cwd();

        async function ensureSummaryServer() {
            if (disposed || typeof ctx?.generate?.text !== "function") {
                return null;
            }
            if (summaryServer) {
                return summaryServer;
            }
            if (summaryServerPromise) {
                return await summaryServerPromise;
            }
            summaryServerPromise = new Promise((resolve) => {
                const token = crypto.randomBytes(24).toString("hex");
                const server = http.createServer(async (req, res) => {
                    if (req.method !== "POST") {
                        res.writeHead(405, { "Content-Type": "application/json" });
                        res.end(JSON.stringify({ error: "method_not_allowed" }));
                        return;
                    }
                    const auth = req.headers.authorization || "";
                    if (auth !== `Bearer ${token}`) {
                        res.writeHead(401, { "Content-Type": "application/json" });
                        res.end(JSON.stringify({ error: "unauthorized" }));
                        return;
                    }
                    let totalBytes = 0;
                    const chunks = [];
                    req.on("data", (chunk) => {
                        totalBytes += chunk.length;
                        if (totalBytes <= MAX_SUMMARY_BODY_BYTES) {
                            chunks.push(chunk);
                        }
                    });
                    req.on("end", async () => {
                        if (totalBytes > MAX_SUMMARY_BODY_BYTES) {
                            res.writeHead(413, { "Content-Type": "application/json" });
                            res.end(JSON.stringify({ error: "payload_too_large" }));
                            return;
                        }
                        try {
                            const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
                            const basePrompt = typeof body.prompt === "string" ? body.prompt : "";
                            const fullPrompt = body.response_schema
                                ? `${basePrompt}\n\nReturn ONLY a single valid JSON object matching this JSON Schema (no markdown fences, no commentary):\n${JSON.stringify(body.response_schema)}`
                                : basePrompt;
                            const modelSpec = parseModelSpec(body.model);
                            const genArgs = modelSpec
                                ? { prompt: fullPrompt, model: modelSpec }
                                : { prompt: fullPrompt };
                            const out = await ctx.generate.text(genArgs);
                            res.writeHead(200, { "Content-Type": "application/json" });
                            res.end(JSON.stringify({ result: out?.text ?? "" }));
                        } catch (err) {
                            res.writeHead(502, { "Content-Type": "application/json" });
                            res.end(JSON.stringify({ error: String(err?.message || err) }));
                        }
                    });
                });
                server.on("error", () => {
                    summaryServerPromise = null;
                    resolve(null);
                });
                server.listen(0, "127.0.0.1", () => {
                    if (typeof server.unref === "function") {
                        server.unref();
                    }
                    const addr = server.address();
                    if (addr && typeof addr === "object" && addr.port) {
                        const epPath = summaryEndpointPath();
                        try {
                            atomicWriteJson(epPath, {
                                url: `http://127.0.0.1:${addr.port}/v1/summary`,
                                token,
                                pid: process.pid,
                                model: process.env.MINDIE_SUMMARY_MODEL || "default",
                            });
                        } catch {
                            // Endpoint file write failure leaves organizer CLI fallback intact.
                        }
                        summaryServer = { server, endpointPath: epPath };
                        resolve(summaryServer);
                    } else {
                        resolve(null);
                    }
                });
            });
            return await summaryServerPromise;
        }

        // 1. Register deterministic MCP surfaces (`mindie-remote-dev` & `mindie-tools`).
        async function refreshMcpSurfaces() {
            if (disposed || !ctx?.mcp || typeof ctx.mcp.transform !== "function") {
                return;
            }
            const py = resolveBootstrapPython();
            const launchPy = path.join(pluginRoot(), "scripts", "mindie_launch.py");
            const mcpEnv = buildMindieEnv();
            const mcpTimeout = { startup: 30000, catalog: 30000, execution: 660000 };
            await ctx.mcp.transform((editor) => {
                editor.set("mindie-remote-dev", {
                    type: "local",
                    command: [py, "-I", launchPy, "mcp", "remote"],
                    environment: mcpEnv,
                    timeout: mcpTimeout,
                });
                editor.set("mindie-tools", {
                    type: "local",
                    command: [py, "-I", launchPy, "mcp", "tools"],
                    environment: mcpEnv,
                    timeout: mcpTimeout,
                });
            });
            if (typeof ctx.mcp.reload === "function") {
                await ctx.mcp.reload();
            }
        }

        let bootstrapPollTimer = null;
        function watchBootstrapReady() {
            if (disposed || bootstrapPollTimer) return;
            const startedAt = Date.now();
            const cfgFile = configPath();
            bootstrapPollTimer = setInterval(async () => {
                if (disposed || Date.now() - startedAt > 560000) {
                    clearInterval(bootstrapPollTimer);
                    bootstrapPollTimer = null;
                    return;
                }
                if (fs.existsSync(cfgFile)) {
                    clearInterval(bootstrapPollTimer);
                    bootstrapPollTimer = null;
                    try {
                        await refreshMcpSurfaces();
                    } catch {
                        // Ignore reload error during shutdown.
                    }
                }
            }, 1500);
            if (typeof bootstrapPollTimer.unref === "function") {
                bootstrapPollTimer.unref();
            }
        }

        await refreshMcpSurfaces();
        maybeTriggerBackgroundPluginSync();

        // 2. Register `/mindie-agent` native slash command.
        if (ctx?.command && typeof ctx.command.transform === "function") {
            await ctx.command.transform((editor) => {
                editor.add({
                    name: "mindie-agent",
                    description: "Activate, inspect, or deactivate the MindIE Agent knowledge flywheel for this OpenCode session",
                    execute: async ({ sessionID, prompt, delivery }) => {
                        const rawArg = typeof prompt?.text === "string" ? prompt.text.trim() : "";
                        const gestureText = GESTURE_PREFIX_RE.test(rawArg)
                            ? rawArg
                            : rawArg
                                ? `/mindie-agent ${rawArg}`
                                : "/mindie-agent";
                        const info = await safeGetSession(ctx, sessionID);
                        const res = await runHook("gesture", {
                            sessionID,
                            directory: info?.directory || defaultDirectory(),
                            parentID: info?.parentID,
                            forkSessionID: info?.forkSessionID,
                            prompt: gestureText,
                        });
                        sessionStateCache.delete(sessionID);
                        if (res.activated) {
                            await ensureSummaryServer();
                            if (res.bootstrapping) watchBootstrapReady();
                        }
                        if (res.action === "activate" && typeof res.task_prompt === "string" && res.task_prompt.trim()) {
                            await ctx.session.prompt({
                                sessionID,
                                prompt: { ...prompt, text: res.task_prompt },
                                ...(delivery ? { delivery } : {}),
                            });
                            return;
                        }
                        if (typeof ctx?.session?.synthetic === "function" && res.message) {
                            await ctx.session.synthetic({
                                sessionID,
                                text: res.message,
                                resume: false,
                            });
                        }
                    },
                });
            });
            if (typeof ctx.command.reload === "function") {
                await ctx.command.reload();
            }
        }

        // 3. Session prompt hook for `/mindie-agent` or `$mindie-agent` prefixes in direct prompts.
        if (ctx?.session && typeof ctx.session.hook === "function") {
            ctx.session.hook("prompt", async ({ sessionID, prompt }) => {
                const rawText = typeof prompt?.text === "string" ? prompt.text : "";
                if (!GESTURE_PREFIX_RE.test(rawText)) {
                    return;
                }
                const info = await safeGetSession(ctx, sessionID);
                const res = await runHook("gesture", {
                    sessionID,
                    directory: info?.directory || defaultDirectory(),
                    parentID: info?.parentID,
                    forkSessionID: info?.forkSessionID,
                    prompt: rawText,
                });
                sessionStateCache.delete(sessionID);
                if (!res.matched) {
                    return;
                }
                if (res.activated) {
                    await ensureSummaryServer();
                    if (res.bootstrapping) watchBootstrapReady();
                }
                if (typeof res.task_prompt === "string" && res.task_prompt.trim()) {
                    prompt.text = res.task_prompt;
                } else if (res.message) {
                    prompt.text = `[MindIE Session Control] ${res.message}\nAcknowledge this status in one short sentence without calling tools.`;
                }
            });

            // 4. Session context hook: inject `SKILL.md` strictly into activated sessions.
            ctx.session.hook("context", async ({ sessionID, system }) => {
                const sig = admissionMtimeSignature(sessionID);
                if (sig === null) {
                    sessionStateCache.clear();
                    return;
                }
                let state = null;
                const cached = sessionStateCache.get(sessionID);
                if (cached && cached.sig === sig) {
                    state = cached.state;
                } else {
                    const info = await safeGetSession(ctx, sessionID);
                    state = await runHook("session-state", {
                        sessionID,
                        directory: info?.directory || defaultDirectory(),
                        parentID: info?.parentID,
                        forkSessionID: info?.forkSessionID,
                    });
                    sessionStateCache.set(sessionID, { sig, state });
                }
                if (!state?.activated || !Array.isArray(system)) {
                    return;
                }
                await ensureSummaryServer();
                const marker = `<mindie-agent-skill session="${sessionID}">`;
                const alreadyPresent = system.some(
                    (part) => typeof part?.text === "string" && part.text.includes(marker),
                );
                if (!alreadyPresent) {
                    system.push({
                        type: "text",
                        text: renderSkillSystemBlock(sessionID, state),
                    });
                }
            });
        }

        // 5. Tool hook: publish one-shot MCP binds & correlate shell commands with `sessionID`.
        if (ctx?.tool && typeof ctx.tool.hook === "function") {
            ctx.tool.hook("execute.before", async ({ tool, sessionID, id, input }) => {
                if (typeof tool === "string" && MCP_WIRE_RE.test(tool)) {
                    const info = await safeGetSession(ctx, sessionID);
                    if (input && typeof input === "object" && !Array.isArray(input)) {
                        try {
                            delete input._mindie_bind_id;
                        } catch {}
                    }
                    const cleanInput =
                        input && typeof input === "object" && !Array.isArray(input) ? { ...input } : {};
                    delete cleanInput._mindie_bind_id;
                    const res = await runHook("pre-tool-use", {
                        sessionID,
                        id,
                        tool,
                        input: cleanInput,
                        directory: info?.directory || defaultDirectory(),
                    });
                    if (
                        res?.bound &&
                        typeof res.bind_id === "string" &&
                        input &&
                        typeof input === "object" &&
                        !Array.isArray(input) &&
                        Object.isExtensible(input)
                    ) {
                        input._mindie_bind_id = res.bind_id;
                    }
                    return;
                }
                if (typeof sessionID === "string" && input && typeof input === "object" && typeof input.command === "string") {
                    const now = Date.now();
                    for (const [tok, meta] of pendingShellTokens.entries()) {
                        if (meta.expiresAt <= now) {
                            pendingShellTokens.delete(tok);
                        }
                    }
                    while (pendingShellQueue.length > 0 && pendingShellQueue[0].expiresAt <= now) {
                        pendingShellQueue.shift();
                    }
                    const origCmd = input.command;
                    const token = crypto.randomBytes(12).toString("hex");
                    const expiresAt = now + SHELL_QUEUE_TTL_MS;
                    pendingShellTokens.set(token, { sessionID, command: origCmd, expiresAt });
                    pendingShellQueue.push({ token, command: origCmd, sessionID, expiresAt });
                    if (Object.isExtensible(input)) {
                        try {
                            input.command = `${origCmd}\n# __mindie_oc_sid:${token}`;
                        } catch {
                            // Frozen input property; unambiguous queue fallback remains active.
                        }
                    }
                }
            });

            ctx.tool.hook("execute.after", ({ input }) => {
                if (input && typeof input === "object" && typeof input.command === "string") {
                    const m = SHELL_TOKEN_INLINE_RE.exec(input.command);
                    if (m && Object.isExtensible(input)) {
                        try {
                            input.command = input.command.replace(SHELL_TOKEN_INLINE_RE, "");
                        } catch {
                            // Ignore read-only command field.
                        }
                    }
                }
            });
        }

        // 6. Shell hook: inject `OPENCODE_SESSION_ID` into session-scoped child shells.
        if (ctx?.shell && typeof ctx.shell.hook === "function") {
            ctx.shell.hook("create.before", (shellInit) => {
                const env = shellInit?.env;
                if (!env || typeof env !== "object") {
                    return;
                }
                const forwarded = buildMindieEnv();
                for (const [k, v] of Object.entries(forwarded)) {
                    if (env[k] === undefined) {
                        env[k] = v;
                    }
                }
                const now = Date.now();
                for (const [tok, meta] of pendingShellTokens.entries()) {
                    if (meta.expiresAt <= now) {
                        pendingShellTokens.delete(tok);
                    }
                }
                while (pendingShellQueue.length > 0 && pendingShellQueue[0].expiresAt <= now) {
                    pendingShellQueue.shift();
                }

                const rawCommand = typeof shellInit.command === "string" ? shellInit.command : "";
                const tokenMatch = SHELL_TOKEN_INLINE_RE.exec(rawCommand);
                if (tokenMatch) {
                    shellInit.command = rawCommand.replace(SHELL_TOKEN_INLINE_RE, "");
                    const token = tokenMatch[1];
                    const entry = pendingShellTokens.get(token);
                    pendingShellTokens.delete(token);
                    const qIdx = pendingShellQueue.findIndex((item) => item.token === token);
                    if (qIdx >= 0) {
                        pendingShellQueue.splice(qIdx, 1);
                    }
                    if (entry && entry.expiresAt > now) {
                        env.OPENCODE_SESSION_ID = entry.sessionID;
                    } else {
                        delete env.OPENCODE_SESSION_ID;
                    }
                    return;
                }

                // Fallback for non-mutating test harnesses: bind only when command matches a single session unambiguously.
                const matchingIndices = [];
                for (let i = 0; i < pendingShellQueue.length; i += 1) {
                    const cand = pendingShellQueue[i].command;
                    if (cand === rawCommand || (cand.length > 0 && rawCommand.includes(cand))) {
                        matchingIndices.push(i);
                    }
                }
                const distinctSessions = new Set(matchingIndices.map((i) => pendingShellQueue[i].sessionID));
                if (distinctSessions.size === 1) {
                    const [matched] = pendingShellQueue.splice(matchingIndices[0], 1);
                    pendingShellTokens.delete(matched.token);
                    env.OPENCODE_SESSION_ID = matched.sessionID;
                } else {
                    delete env.OPENCODE_SESSION_ID;
                }
            });
        }

        // 7. Event stream subscription: await subscription setup synchronously so zero standalone turn events race startup.
        if (ctx?.event && typeof ctx.event.subscribe === "function") {
            let stream = null;
            try {
                stream = await ctx.event.subscribe();
            } catch {
                stream = null;
            }
            if (stream && typeof stream[Symbol.asyncIterator] === "function") {
                (async () => {
                    try {
                        for await (const event of stream) {
                            if (disposed) break;
                            const evType = event?.type;
                            if (!TERMINAL_SESSION_EVENTS.has(evType)) continue;
                            if (evType === "session.execution.interrupted" && event?.data?.reason === "shutdown") continue;
                            const sessionID = event?.data?.sessionID ?? event?.properties?.sessionID;
                            if (typeof sessionID !== "string" || admissionMtimeSignature(sessionID) === null) continue;
                            const now = Date.now();
                            const explicitId = typeof event?.id === "string" && event.id ? event.id : null;
                            const prev = recentCaptureBySession.get(sessionID);
                            if (prev && ((explicitId && prev.eventID === explicitId) || now - prev.at < CAPTURE_COALESCE_MS)) continue;
                            const eventID = explicitId || `${sessionID}:${evType}:${now}`;
                            recentCaptureBySession.set(sessionID, { eventID, at: now });
                            if (recentCaptureBySession.size > 256) {
                                const oldestKey = recentCaptureBySession.keys().next().value;
                                if (oldestKey !== undefined) recentCaptureBySession.delete(oldestKey);
                            }
                            spawnDetachedCapture({ sessionID, eventID, eventType: evType, opencodeDb: opencodeDbPath() });
                        }
                    } catch {
                        // Stream closed on host shutdown.
                    }
                })();
            }
        }

        return async () => {
            disposed = true;
            if (bootstrapPollTimer) {
                clearInterval(bootstrapPollTimer);
                bootstrapPollTimer = null;
            }
            sessionStateCache.clear();
            pendingShellQueue.length = 0;
            pendingShellTokens.clear();
            recentCaptureBySession.clear();
            if (summaryServer) {
                try {
                    summaryServer.server.close();
                } catch {
                    // Best-effort close.
                }
                try {
                    if (fs.existsSync(summaryServer.endpointPath)) {
                        fs.unlinkSync(summaryServer.endpointPath);
                    }
                } catch {
                    // Best-effort unlink.
                }
                summaryServer = null;
            }
        };
    },
};
