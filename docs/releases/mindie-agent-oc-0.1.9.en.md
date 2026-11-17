# mindie-agent-oc 0.1.9: Explicit Manual-Trigger `SKILL.md` Guard and Runtime/Install Boundary Separation

[中文](mindie-agent-oc-0.1.9.md)

- **Explicit Manual-Trigger Enforcement & Clean Frontmatter Stripping (`disable-model-invocation: true`)**: Sets `disable-model-invocation: true` in `plugins/mindie-agent/skills/mindie-agent/SKILL.md` frontmatter and strips YAML frontmatter in `session_support.mjs` (`renderSkillSystemBlock`) so activated sessions receive clean Markdown guidance without metadata noise.
- **Clean Runtime Skill vs. Installation Doc Boundary**: Moves `workspace-setup.md`, `github-authentication.md`, and `user-notice.md` out of `plugins/mindie-agent/skills/mindie-agent/references/` into `docs/install/`, removing all installation, onboarding, `setup.py sharing` prompt, and GitHub Star instructions from runtime `SKILL.md` and skill references.
