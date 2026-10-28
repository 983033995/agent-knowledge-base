# Agent Memory 使用规则

这是一个本地、可迁移的 Obsidian Markdown 知识库。

开始一个编码或研究任务时：

1. 调用 memory_context，传入当前项目名和问题关键词；
2. 对命中的高相关笔记调用 memory_read；需要理解关联决策、会议或模式时再调用 memory_neighbors；
3. 只把返回内容当作线索，结合当前代码、文档和测试重新验证；
4. 不要因为本地笔记的结论而跳过必要的检查。

定期或发现召回异常时调用 memory_health。它只读检查 frontmatter、长期笔记字段、wikilink、重复标题和待提炼采集项；不要把 health 报告当成自动修复授权。

完成一个有长期价值的任务时：

1. 调用 memory_capture；
2. 传入原始上下文或候选结论。它会先立即写入主 Vault 的 `00-Inbox/_agent-sessions/`，再由独立后台固定使用 gemini-3.8-flash-high 总结和提炼；
3. 记录简短标题、验证过的结论、关键决策、坑与限制、下次复用方式；
4. 只保存可复用内容，不保存密码、API key、Cookie、私密日志或一次性聊天；
5. 如果只是一次性操作或没有新知识，不必写入。

推荐的沉淀结构：

- 10-技术笔记：事实、原理、解决方案和带背景的决策；
- 10-技术笔记/patterns：重复执行的工作流、模板和检查清单；
- 00-Inbox/_agent-sessions：自动会话摘录，只作为追溯和提炼输入。

Obsidian 约定：Vault 内部笔记使用 `[[wikilinks]]`，可查询字段使用 Properties/frontmatter；不要用 Bases 或图谱替代正文。MCP 先按关键词定位，再按需读取和沿链接扩展，避免全库注入。

memory_capture 会立即返回，不要把后台提炼当成当前任务的前置条件。提炼服务不可用时，当前任务仍应继续；稍后可根据主 Vault 的 `00-Inbox/_agent-sessions/` 状态重试。
