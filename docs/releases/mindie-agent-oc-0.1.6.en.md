# mindie-agent-oc 0.1.6

[简体中文](mindie-agent-oc-0.1.6.md)

Fix #6: `/mindie-agent <task>` returns HTTP 500 on OpenCode 2.2.7.

- Both normal activation and Python gesture-failure forwarding use flat PromptInput, preserving attachments, agents, skills and delivery without overriding the authoritative sessionID/text.
- No exception-driven retry: a request may already have been admitted. Regression assertions cover both paths and one attempt on failure.
- Correct installation guidance: 2.2.7 rejects direct tarball URLs; use the GitHub plugin channel.
- Runtime dependency pins are unchanged from 0.1.5.

Baseline reproduction used Windows Lenovo and official OpenCode 2.2.7 with plugin 0.1.5. Task commands failed with Missing key at text, whereas empty/status commands returned 204. Prefix and flat prompt admission passed with model execution disabled. This is not NPU or complete model-task validation.
