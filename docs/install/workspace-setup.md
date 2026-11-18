# Development workspace setup (OpenCode)

Run this flow once during installation or first-use setup. It reuses saved
workspace, sharing, and SSH decisions; ordinary tasks, headless runs, and
updates do not repeat onboarding.

## 1. Install

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host oc
```

`gh` is optional and never blocks installation. When `gh` is missing or logged
out, the installer recommends local-only sharing and continues without GitHub
API calls.

## 2. Three dynamic `question` prompts

The installer uses OpenCode's native **`question`** tool in the user's language.
Questions are generated dynamically from environment facts:

1. **Workspace**: initialize `vllm-ascend-workspace` in the current directory,
   keep the current directory, or select an existing workspace directory.
   For a new directory, clone
   `https://github.com/mindie-agent/vllm-ascend-workspace.git` and then follow
   the checkout's `AGENTS.md`; for reuse, inspect the checkout first and never
   overwrite existing files.
2. **Community sharing**: run `gh auth status` first. When authenticated, ask
   whether to enable community sharing and recommend `contribute`; otherwise
   recommend `local-only`. Both tiers capture locally; only `contribute`
   reports sanitized results. Persist the choice with
   `setup.py sharing --enable --project-root "$WORKSPACE_ROOT"`,
   `setup.py sharing --local-only --project-root "$WORKSPACE_ROOT"`, or
   `setup.py sharing --disable`.
3. **Star postscript (single line, optional)**: "If this plugin helps, would you
   give mindie-agent a Star ⭐?" Only run
   `gh api --hostname github.com --silent -X PUT /user/starred/mindie-agent/mindie-agent`
   after an explicit yes.

## 3. One-step SSH NPU connection

In terminal mode the installer asks for host, user, and authentication in one
step and saves the endpoint with
`remote_auth(action="setup", save_default=True, discover=True)`. Append
`--password` at the very end of the command to be prompted for a hidden
password instead of pasting it into the command line. Never store passwords in
endpoint records.

## 4. Readiness check

Run the read-only `mindie_doctor` tool (or `setup.py doctor`) before the first
NPU task. It reports runtime pins, prerequisites, remote connectivity, and
diagnostics without mutating state. If any MindIE tool is unavailable, report
the exact failure once and continue through native `ssh`/`scp` or local
scripts.

OpenCode treats the project directory (`opencode "<absolute-path>"`) as the
active project root; never edit `~/.local/share/opencode/opencode.db` to fake
project registration.
