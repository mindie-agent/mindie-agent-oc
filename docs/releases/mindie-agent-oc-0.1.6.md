# mindie-agent-oc 0.1.6

[English](mindie-agent-oc-0.1.6.en.md)

修复 #6：OpenCode 2.2.7 中 `/mindie-agent <任务>` 返回 HTTP 500。

- 正常激活和 Python 手势失败后的任务转发均使用平铺 PromptInput，保留附件、agents、skills 和 delivery；权威 sessionID/text 不被输入覆盖。
- 不增加异常后重试，避免已接收请求被重复投递。补充两路径及异常仅一次调用的回归断言。
- 更正安装文档：2.2.7 不接受直接 tarball URL，使用 GitHub 插件渠道。
- 依赖组合与 0.1.5 保持一致。

验证范围：Windows Lenovo、官方 OpenCode 2.2.7、插件 0.1.5 已复现带任务命令的 Missing key at text。无参数命令和 status 返回 204；前缀路径及平铺 prompt 的不执行模型输入验证通过。不代表 NPU 或完整模型任务已经验证。
