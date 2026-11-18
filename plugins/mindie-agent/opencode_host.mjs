/**
 * [Role] Windowless OpenCode v2 host adapter for MindIE Agent (`oc`): MCP registration, background update, pure-Node context/skill injection, shell session binding, and consent-first turn capture.
 * [Invariants] Zero subprocess per prompt; context skill only for top-level sessions; community + consent + canonical `project_roots` checked in pure Node before capture; no `_mindie_bind_id` injection; every child process uses `windowsHide: true` and never unconditional detach on win32.
 * [Timeouts & Locks] Loopback summary server binds `127.0.0.1:0` with a bearer token and `unref()`; capture dedup window is `250ms`; shell correlation entries expire after `30s`.
 * [Side-Effect Policy] Writes only `summary-endpoint.json` under MindIE `stateDir`; delegates consent-authorized capture and update checks to `mindie_launch.py`.
 */
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const MAX_CONFIG_BYTES = 65536, MAX_SUMMARY_BYTES = 512 * 1024, COALESCE_MS = 250, SHELL_TTL_MS = 30000;
const MCP_RE = /^(?:mcp[_:])?(?:mindie[-_]agent[-_:])?(mindie[-_]remote[-_]dev|mindie[-_]tools)[_:]/;
const TOKEN_RE = /\r?\n# __mindie_oc_sid:([0-9a-f]{12})(?=$|['"\s])/;
const ENV_KEYS = ["MINDIE_CONFIG_HOME", "MINDIE_DATA_HOME", "MINDIE_AGENT_CONFIG", "MINDIE_DIAGNOSTICS_ROOT", "MINDIE_OPENCODE_DB", "MINDIE_OC_INSTALLED_ROOT", "MINDIE_OC_PYTHON", "MINDIE_OC_BIN", "MINDIE_SUMMARY_MODEL", "OPENCODE_DB", "OPENCODE_TEST_HOME", "PATH", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"];
let skillCache = { mtimeMs: -1, body: "" };
const abs = (value) => typeof value === "string" && path.isAbsolute(value);
export function pluginRoot() { const value = process.env.OC_PLUGIN_ROOT; return abs(value) ? path.resolve(value) : HERE; }
function configHome() { const value = process.env.MINDIE_CONFIG_HOME; return abs(value) ? path.resolve(value) : path.resolve(os.homedir(), ".config", "mindie-agent"); }
export function dataHome() { const value = process.env.MINDIE_DATA_HOME; return abs(value) ? path.resolve(value) : path.resolve(os.homedir(), ".local", "share", "mindie-agent"); }
export function configPath(explicit = null) { if (explicit) return path.resolve(String(explicit)); if (process.env.MINDIE_AGENT_CONFIG) return path.resolve(process.env.MINDIE_AGENT_CONFIG); return path.join(configHome(), "oc.json"); }
function stateDir() { const cfg = readJsonSafe(configPath()); return cfg && abs(cfg.state_dir) ? cfg.state_dir : path.join(dataHome(), "state", "oc"); }
export function opencodeDbPath() {
    if (abs(process.env.MINDIE_OPENCODE_DB)) return path.resolve(process.env.MINDIE_OPENCODE_DB);
    const cfg = readJsonSafe(configPath()); if (cfg && abs(cfg.opencode_db)) return path.resolve(cfg.opencode_db);
    const native = process.env.OPENCODE_DB; if (typeof native === "string" && native.trim() === ":memory:") return null;
    const xdg = process.env.XDG_DATA_HOME, home = abs(process.env.OPENCODE_TEST_HOME) ? process.env.OPENCODE_TEST_HOME : os.homedir();
    const base = abs(xdg) ? path.join(path.resolve(xdg), "opencode") : path.join(home, ".local", "share", "opencode");
    return typeof native === "string" && native.trim() ? path.resolve(base, native.trim()) : path.join(base, "opencode.db");
}
export function readJsonSafe(target) {
    try {
        const stat = fs.statSync(target); if (!stat.isFile() || stat.size > MAX_CONFIG_BYTES) return null;
        const parsed = JSON.parse(fs.readFileSync(target, "utf8").replace(/^\uFEFF/, ""));
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    } catch (err) { if (err?.code !== "ENOENT") process.stderr.write(`MindIE OpenCode config read failed (${target}): ${err?.message || err}\n`); return null; }
}
export function isNewerSemver(candidate, active) {
    const c = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(candidate ?? "").trim()), a = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(active ?? "").trim());
    if (!c) return false; if (!a) return true;
    for (let i = 1; i <= 3; i += 1) if (Number(c[i]) !== Number(a[i])) return Number(c[i]) > Number(a[i]);
    return false;
}
function python() {
    if (process.env.MINDIE_OC_PYTHON) return process.env.MINDIE_OC_PYTHON;
    const cfg = readJsonSafe(configPath()); if (cfg && typeof cfg.python === "string" && cfg.python && fs.existsSync(cfg.python)) return cfg.python;
    const rel = process.platform === "win32" ? path.join("Scripts", "python.exe") : path.join("bin", "python");
    for (const candidate of [path.join(dataHome(), "oc-venv", rel), path.join(pluginRoot(), "..", "..", ".venv", rel)]) if (fs.existsSync(candidate)) return candidate;
    for (const sibling of ["oc.json", "agy.json", "wb.json", "codex.json", "cc.json", "kimi.json"]) { const other = readJsonSafe(path.join(configHome(), sibling)); if (other && typeof other.python === "string" && other.python && fs.existsSync(other.python)) return other.python; }
    return process.platform === "win32" ? "python" : "python3";
}
function mindieEnv() { const env = { OC_PLUGIN_ROOT: pluginRoot(), PYTHONIOENCODING: "utf-8" }; for (const key of ENV_KEYS) if (process.env[key]) env[key] = process.env[key]; return env; }
function writeJson(target, payload) {
    const dir = path.dirname(path.resolve(target)), tmp = path.join(dir, `.${path.basename(target)}.${process.pid}.${Date.now()}.tmp`); fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    try { fs.writeFileSync(tmp, `${JSON.stringify(payload, null, 2)}\n`, { encoding: "utf8", mode: 0o600 }); fs.renameSync(tmp, target); }
    catch (err) { try { fs.unlinkSync(tmp); } catch {} throw err; }
}
function modelSpec(raw) {
    if (typeof raw !== "string" || !raw.trim() || raw.trim() === "default") return undefined;
    const [ref, variant] = raw.trim().split("#", 2), slash = ref.indexOf("/"); if (slash <= 0 || slash >= ref.length - 1) return undefined;
    const spec = { providerID: ref.slice(0, slash), id: ref.slice(slash + 1) }; if (variant) spec.variant = variant; return spec;
}
function canonical(raw) {
    if (!abs(raw)) return null;
    let value = path.resolve(raw.trim()); try { value = fs.realpathSync(value); } catch {}
    return process.platform === "win32" ? value.replace(/\\/g, "/").toLowerCase() : value;
}
function scope() {
    const dir = path.dirname(configPath()), community = readJsonSafe(path.join(dir, "mindie-community.json"));
    if (!community || community.enabled !== true || !Array.isArray(community.project_roots)) return null;
    const consent = readJsonSafe(abs(community.consent_config) ? community.consent_config : path.join(dir, "mindie-consent.json"));
    if (!consent || !["contribute", "local-only"].includes(consent.choice)) return null;
    const roots = community.project_roots.map(canonical).filter(Boolean); return roots.length ? roots : null;
}
function inScope(directory, roots) { const candidate = canonical(directory); return Boolean(candidate) && roots.some((root) => candidate === root || candidate.startsWith(root.endsWith("/") ? root : `${root}/`)); }
function skillText() {
    const target = path.join(pluginRoot(), "skills", "mindie-agent", "SKILL.md");
    try { const stat = fs.statSync(target); if (skillCache.mtimeMs !== stat.mtimeMs) skillCache = { mtimeMs: stat.mtimeMs, body: fs.readFileSync(target, "utf8").replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "").trim() }; return skillCache.body; } catch { return ""; }
}
function skillBlock(sessionID) { return [`<mindie-agent-skill session="${sessionID}">`, "MindIE Agent capabilities are enabled for this OpenCode main session.", `- OPENCODE_SESSION_ID=${sessionID}`, `- Python runtime: ${python()}`, `- Host adapter: ${pluginRoot()}`, "Shell commands created in this session automatically receive OPENCODE_SESSION_ID.", "", skillText(), "</mindie-agent-skill>"].join("\n"); }
async function getSession(ctx, sessionID) {
    if (!sessionID || typeof ctx?.session?.get !== "function") return null;
    try { return await ctx.session.get({ sessionID }); } catch (err) { process.stderr.write(`MindIE OpenCode session.get warning (${sessionID}): ${err?.message || err}\n`); return null; }
}
export default {
    id: "mindie-agent",
    async setup(ctx) {
        let disposed = false, server = null, serverPromise = null;
        const pending = new Map(), recent = new Map();
        async function ensureSummary() {
            if (disposed || typeof ctx?.generate?.text !== "function") return null;
            if (server) return server; if (serverPromise) return serverPromise;
            serverPromise = new Promise((resolve) => {
                const token = crypto.randomBytes(24).toString("hex");
                const httpServer = http.createServer((req, res) => {
                    const send = (status, payload) => { res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(payload)); };
                    if (req.method !== "POST") return send(405, { error: "method_not_allowed" });
                    if (req.headers.authorization !== `Bearer ${token}`) return send(401, { error: "unauthorized" });
                    const chunks = []; let total = 0;
                    req.on("data", (chunk) => { total += chunk.length; if (total <= MAX_SUMMARY_BYTES) chunks.push(chunk); });
                    req.on("end", async () => {
                        if (total > MAX_SUMMARY_BYTES) return send(413, { error: "payload_too_large" });
                        try {
                            const body = JSON.parse(Buffer.concat(chunks).toString("utf8")), base = typeof body.prompt === "string" ? body.prompt : "";
                            const prompt = body.response_schema ? `${base}\n\nReturn ONLY a single valid JSON object matching this JSON Schema (no markdown fences, no commentary):\n${JSON.stringify(body.response_schema)}` : base;
                            const model = modelSpec(body.model), out = await ctx.generate.text(model ? { prompt, model } : { prompt });
                            send(200, { result: out?.text ?? "" });
                        } catch (err) { send(502, { error: String(err?.message || err) }); }
                    });
                });
                httpServer.on("error", (err) => { process.stderr.write(`MindIE OpenCode summary endpoint error: ${err?.message || err}\n`); serverPromise = null; resolve(null); });
                httpServer.listen(0, "127.0.0.1", () => {
                    if (typeof httpServer.unref === "function") httpServer.unref();
                    const address = httpServer.address(); if (!address || typeof address !== "object" || !address.port) return resolve(null);
                    const endpoint = path.join(stateDir(), "summary-endpoint.json");
                    try { writeJson(endpoint, { url: `http://127.0.0.1:${address.port}/v1/summary`, token, pid: process.pid, model: process.env.MINDIE_SUMMARY_MODEL || "default" }); }
                    catch (err) { process.stderr.write(`MindIE OpenCode summary endpoint write failed (${endpoint}): ${err?.message || err}\n`); }
                    server = { httpServer, endpoint, token }; resolve(server);
                });
            });
            return serverPromise;
        }
        function register() {
            if (!ctx?.mcp || typeof ctx.mcp.transform !== "function") return Promise.resolve();
            const launch = path.join(pluginRoot(), "scripts", "mindie_launch.py"), env = { ...mindieEnv(), REMOTE_DEV_STATE_DIR: path.join(stateDir(), "remote") }, timeout = { startup: 270000, catalog: 30000, execution: 660000 };
            return Promise.resolve(ctx.mcp.transform((editor) => {
                editor.set("mindie-remote-dev", { type: "local", command: [python(), "-I", "-X", "utf8", launch, "mcp", "remote"], environment: { ...env, REMOTE_DEV_HARNESS_PROFILE: "oc" }, timeout });
                editor.set("mindie-tools", { type: "local", command: [python(), "-I", "-X", "utf8", launch, "mcp", "tools"], environment: env, timeout });
            })).then(() => (typeof ctx.mcp.reload === "function" ? ctx.mcp.reload() : undefined));
        }
        function spawnChild(args, label) {
            try {
                const child = spawn(python(), ["-I", "-X", "utf8", path.join(pluginRoot(), "scripts", "mindie_launch.py"), ...args], { env: { ...process.env, ...mindieEnv() }, stdio: "ignore", windowsHide: true, detached: process.platform !== "win32" });
                child.on("error", (err) => process.stderr.write(`MindIE OpenCode ${label} spawn failed: ${err?.message || err}\n`)); child.unref();
            } catch (err) { process.stderr.write(`MindIE OpenCode ${label} warning: ${err?.message || err}\n`); }
        }
        function queue(sessionID, command) {
            const now = Date.now(); for (const [token, entry] of pending) if (entry.expiresAt <= now) pending.delete(token);
            const token = crypto.randomBytes(6).toString("hex"); pending.set(token, { sessionID, command, expiresAt: now + SHELL_TTL_MS });
            while (pending.size > 64) pending.delete(pending.keys().next().value); return token;
        }
        await register();
        if (!process.env.MINDIE_CONFIG_HOME || process.env.MINDIE_OC_ALLOW_BG_UPDATE === "1") spawnChild(["update", "--background"], "update check");
        if (ctx?.session && typeof ctx.session.hook === "function") {
            ctx.session.hook("context", async ({ sessionID, system }) => {
                if (!Array.isArray(system) || typeof sessionID !== "string" || !sessionID) return;
                const info = await getSession(ctx, sessionID); if (!info || info.parentID || info.forkSessionID) return;
                await ensureSummary();
                const marker = `<mindie-agent-skill session="${sessionID}">`;
                if (!system.some((part) => typeof part?.text === "string" && part.text.includes(marker))) system.push({ type: "text", text: skillBlock(sessionID) });
            });
        }
        if (ctx?.tool && typeof ctx.tool.hook === "function") {
            ctx.tool.hook("execute.before", ({ tool, sessionID, input }) => {
                if (typeof tool === "string" && MCP_RE.test(tool)) { if (input && typeof input === "object" && !Array.isArray(input)) { try { delete input._mindie_bind_id; } catch {} } return; }
                const isShell = typeof tool !== "string" || /^(?:bash|shell|terminal|exec)$/i.test(tool);
                if (isShell && typeof sessionID === "string" && input && typeof input === "object" && typeof input.command === "string") {
                    const token = queue(sessionID, input.command);
                    if (Object.isExtensible(input)) { try { input.command = `${input.command}\n# __mindie_oc_sid:${token}`; } catch {} }
                }
            });
        }
        if (ctx?.shell && typeof ctx.shell.hook === "function") {
            ctx.shell.hook("create.before", (shellInit) => {
                const env = shellInit?.env; if (!env || typeof env !== "object") return;
                for (const [key, value] of Object.entries(mindieEnv())) if (env[key] === undefined) env[key] = value;
                const now = Date.now(); for (const [token, entry] of pending) if (entry.expiresAt <= now) pending.delete(token);
                const command = typeof shellInit.command === "string" ? shellInit.command : "", match = TOKEN_RE.exec(command);
                if (match) {
                    shellInit.command = command.replace(TOKEN_RE, ""); const entry = pending.get(match[1]); pending.delete(match[1]);
                    if (entry && entry.expiresAt > now) env.OPENCODE_SESSION_ID = entry.sessionID; else delete env.OPENCODE_SESSION_ID; return;
                }
                const matches = [...pending.entries()].filter(([, entry]) => entry.command === command || (entry.command.length > 0 && command.includes(entry.command)));
                if (new Set(matches.map(([, entry]) => entry.sessionID)).size === 1) { env.OPENCODE_SESSION_ID = matches[0][1].sessionID; pending.delete(matches[0][0]); } else delete env.OPENCODE_SESSION_ID;
            });
        }
        if (ctx?.event && typeof ctx.event.subscribe === "function") {
            let stream = null; try { stream = await ctx.event.subscribe(); } catch { stream = null; }
            if (stream && typeof stream[Symbol.asyncIterator] === "function") {
                (async () => {
                    try {
                        for await (const event of stream) {
                            if (disposed) break;
                            const type = event?.type;
                            if (type !== "session.idle" && !(typeof type === "string" && type.startsWith("session.execution."))) continue;
                            if (type === "session.execution.interrupted" && event?.data?.reason === "shutdown") continue;
                            const sessionID = event?.data?.sessionID ?? event?.properties?.sessionID; if (typeof sessionID !== "string" || !sessionID) continue;
                            const roots = scope(); if (!roots) continue;
                            const info = await getSession(ctx, sessionID);
                            if (!info || info.parentID || info.forkSessionID || typeof info.directory !== "string" || !path.isAbsolute(info.directory) || !inScope(info.directory, roots)) continue;
                            const dbPath = opencodeDbPath(); if (!dbPath) continue;
                            const now = Date.now(), explicitID = typeof event?.id === "string" && event.id ? event.id : null, previous = recent.get(sessionID);
                            if (previous && ((explicitID && previous.eventID === explicitID) || now - previous.at < COALESCE_MS)) continue;
                            const eventID = explicitID || `${sessionID}:${type}:${now}`; recent.set(sessionID, { eventID, at: now });
                            if (recent.size > 256) recent.delete(recent.keys().next().value);
                            spawnChild(["hook", "stop", "--event-json", JSON.stringify({ sessionID, directory: info.directory, eventID, eventType: type, opencodeDb: dbPath })], "capture");
                        }
                    } catch (err) { if (!disposed) process.stderr.write(`MindIE OpenCode event stream error: ${err?.message || err}\n`); }
                })();
            }
        }
        return async () => {
            disposed = true; pending.clear(); recent.clear();
            if (server) { try { server.httpServer.close(); } catch {} try { if (readJsonSafe(server.endpoint)?.token === server.token) fs.unlinkSync(server.endpoint); } catch {} server = null; }
        };
    },
};
