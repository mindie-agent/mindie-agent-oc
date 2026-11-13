# Development workspace setup (OpenCode)

Run this flow during interactive installation and first-use setup, immediately after
presenting the MindIE Agent user notice. Reuse saved creation, initialization and
sharing decisions; ordinary tasks, headless `--auto` runs, and updates do not repeat
onboarding.

1. Recommend [mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git).
   When `<python> -I "<plugin_root>/scripts/setup.py" sharing --status` reports
   `needs_onboarding_prompt: true` in an interactive OpenCode session, call OpenCode's
   native **`question`** tool in the user's language to ask:
   - Local Development Workspace `vllm-ascend-workspace`:
     `在当前目录初始化 vllm-ascend-workspace` / `Initialize vllm-ascend-workspace in current directory` vs `继续使用当前目录作为工作区` / `Keep current directory as workspace` vs `指定已有工作区目录` / `Use an existing workspace directory`
   - Community Knowledge Sharing & Automated Fault Reporting:
     `开启社区共享与故障上报` / `Enable Community Sharing & Fault Reporting` vs `仅保持本地模式` / `Keep Local-Only Mode`
   - `顺手给 MindIE Agent 点个 GitHub Star 吗？ ⭐` / `Would you like to give mindie-agent a Star on GitHub ⭐?`:
     `好，点个 Star ⭐` / `Sure, star it ⭐` vs `先不用` / `Maybe later`
2. Before downloads or fork setup, follow [GitHub Sign-In](github-authentication.md).
3. For a new directory, run
   `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"`.
   For reuse, inspect the selected checkout first and preserve existing files.
4. OpenCode treats the project directory (`opencode "<absolute-path>"`) as the active
   project root (`ctx.location.directory`). Never mutate `~/.local/share/opencode/opencode.db`
   directly to fake project registration; report the workspace path so the user can open
   `opencode "<absolute-path>"` directly when switching directories.
5. For scaffold initialization, read the checkout's `AGENTS.md`, `README.md` and
   `.agents/scripts/README.md`, and run
   `uv run --no-project --python 3.13 python .agents/scripts/repo_init.py --no-input`
   (or `--github-user <account> --no-input` on first fork setup).
6. Persist the user's sharing choice via
   `<python> -I "<plugin_root>/scripts/setup.py" sharing --enable --project-root "$WORKSPACE_ROOT"`
   (or `--disable`), and if Star was chosen, run
   `gh api --hostname github.com --silent -X PUT /user/starred/mindie-agent/mindie-agent`.
