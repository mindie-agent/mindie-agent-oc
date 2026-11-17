# mindie-agent-oc 0.1.9：显式禁用模型自动唤醒 `SKILL.md` 与剥离运行时安装引导边界

[English](mindie-agent-oc-0.1.9.en.md)

- **显式禁用语义自动唤醒与纯净上下文注入（`disable-model-invocation: true`）**：在 `plugins/mindie-agent/skills/mindie-agent/SKILL.md` Frontmatter 中声明 `disable-model-invocation: true`，并在 `session_support.mjs`（`renderSkillSystemBlock`）中自动剥离 YAML Frontmatter 后仅向已激活会话注入纯净 Markdown 指引。
- **安装引导与运行时技能严格分离**：将首次工作区配置、GitHub 登录引导与用户须知从运行时 `skills/mindie-agent/references/` 迁出至仓库级 `docs/install/`（`workspace-setup.md`、`github-authentication.md`、`user-notice.md`），彻底清除 `SKILL.md` 中的安装、授权询问与 Star 引导内容。
