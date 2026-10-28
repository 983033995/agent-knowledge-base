# Agent Knowledge Base

这是一个以 Obsidian 可直接打开的 Markdown 文件夹为事实源的本地 Agent 知识库。

目标是让 Codex、Cursor、WorkBuddy 共用同一套：

1. 会话采集：先保存可追溯的会话摘录；
2. 知识提炼：由独立后台调用指定模型，把决策、经验、坑和可复用方案写成长期笔记；
3. 跨项目召回：用同一个本地检索器和 MCP 服务，在新项目中读取旧经验。

## 默认位置

当前以用户的主 Vault 作为唯一日常入口：

    /Volumes/13759427003/资源存储/Obsidian Vault

`vault/` 是历史 archive，不参与默认召回，也不作为日常写入目标。若要固定到非活动 Vault，可在本目录的 config.json 中修改 `vault_path` 并关闭 `auto_discover_obsidian`；绝对路径和相对本目录的路径都支持。命令行入口使用 PATH 中的 `python3`（建议 `>=3.9`）。

运行时还会读取 Obsidian 的活动 Vault 注册表（默认是 `~/Library/Application Support/obsidian/obsidian.json`）。因此 Obsidian 搬家后，只要它在 Obsidian 中作为活动 Vault 打开，MCP 和 Hook 会自动跟随；`AKB_VAULT` 环境变量仍可用于测试或临时覆盖。

## 目录约定

    Obsidian Vault/
    ├── 00-Inbox/_agent-sessions/  Agent 脱敏会话摘录
    ├── 10-技术笔记/               可跨项目复用的知识
    ├── 10-技术笔记/patterns/      已验证的模式、工作流和模板
    └── 99-归档/                   归档，不参与默认召回

原则是：`00-Inbox/_agent-sessions/` 负责可追溯，`10-技术笔记/` 负责可复用。不要把所有聊天全文直接当成知识。

## 本地状态存储

知识正文仍以 Vault 内 Markdown 为唯一事实源。同步来源映射、版本状态、待发布事务、提炼队列、失败记录和历史批次清单使用 Python 标准库 SQLite；默认数据库为本项目 `.distiller/state.sqlite3`，可用 `AKB_STATE_DB` 指定绝对路径。实时、历史批次和各 Vault 的同步状态通过命名空间隔离，检索缓存仍是可重建 JSON。

`akb state-migrate` 会先备份旧状态和已有数据库，再幂等导入旧 JSON；不删除旧文件、不启动 worker、不重试失败任务，也不迁移知识正文。备份及迁移报告保存在 `.distiller/state-backups/`。迁移标记提交后，旧 JSON 只作保留材料，不再作为运行态读取或双写。旧历史批次没有可验证的 Vault 归属时保留为只读，不自动续跑。

状态库使用 `BEGIN IMMEDIATE` 短事务、FULL 同步和 DELETE 日志模式；模型请求期间不持有数据库事务。只读状态查询不会创建缺失数据库；若崩溃日志需要恢复，会提示执行 `state-migrate` 或等待下一次写入恢复。迁移命令遇到活跃 worker、不可判定的 worker 锁或历史回填锁会停止，不强杀进程。

## 已提供的入口

命令行入口：

    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb status
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb health
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb search "缓存失效"
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb context --project my-project --query "发布流程"
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb capture --source manual --title "示例" --summary "可复用经验"
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb distill-status
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb runtime-status
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb distill-retry --dry-run --error-contains "HTTP 400" --limit 3
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb sync-status
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb state-migrate

MCP 服务入口：

    python3 /Volumes/13759427003/skills/agent-knowledge-base/scripts/mcp_server.py

服务提供 memory_context、memory_search、memory_read、memory_neighbors、memory_health、memory_capture、memory_status，以及 memory_sources、memory_sync、memory_sync_status 共十个工具。原有工具名和调用方式保留；来源盘点与同步管理不调用提炼模型，memory_sync 默认仅预览。

`runtime-status` 只读汇总各端来源发现数量、同步范围与新鲜度、待恢复发布及提炼失败分类。发现本地来源不代表客户端连接成功；未同步、局部同步和待恢复状态会明确区分。MCP `memory_status` 的原有字段保留，并追加同一份 `runtime` 诊断。

`distill-retry --dry-run` 仅预览，不创建数据库或锁、不启动 worker。`--error-contains` 按错误文本忽略大小写筛选，`--limit` 限制独立输入数量而非失败文件数；旧正文或作用域版本、已成功输入、读取失败和队列冲突会跳过。去掉 `--dry-run` 才会实际排队，且仍遵循 `AKB_DISTILLER_AUTOSTART`；不会改动不匹配筛选条件的失败记录。

## 固定模型与异步边界

知识提炼固定通过本机 CLIProxyAPI 的 OpenAI-compatible 接口调用：

    endpoint: http://127.0.0.1:8317/v1/chat/completions
    model: gemini-3.8-flash-high

鉴权顺序：`AKB_DISTILLER_API_KEY` / `CLIPROXY_API_KEY` → `~/.cli-proxy-api/config.yaml` 的 `api-keys` → `/Users/zhangheteng/.codex/bin/cliproxy-bearer`。API key 不会写入笔记、命令输出或日志。配置集中在：

    /Volumes/13759427003/skills/agent-knowledge-base/distiller.json

正常对话使用的模型和路由完全不修改。Hook 或 memory_capture 只做两件事：把已经脱敏的材料快速写入 `00-Inbox/_agent-sessions/`，再启动独立后台 worker；worker 等待短暂合并窗口后才调用 Gemini。瞬时网络错误会留在队列并指数退避，耗尽重试后才标记 distillation-failed。

可以查看后台状态，或把失败项重新入队：

    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb distill-status
    /Volumes/13759427003/skills/agent-knowledge-base/bin/akb distill-retry

可选回滚开关（默认走新行为）：

    AKB_SEARCH_CACHE=0      关闭检索 mtime 缓存，恢复全量读盘
    AKB_HOOK_LIGHT=0        SessionStart 恢复 6 条 / 9000 字
    AKB_DISTILLER_STALE_LOCK=0  不回收残留 worker.lock
    AKB_DISTILLER_MAX_ATTEMPTS=2  恢复旧的“失败 1 次即隔离”

长期知识仍只来自 Gemini 提炼结果：

- 10-技术笔记：可复用事实、原理、解决方案和关键取舍；
- 10-技术笔记/patterns：重复工作流、模板和检查清单；
- 00-Inbox/_agent-sessions：原始摘录和提炼状态，作为追溯输入。

## 自动化边界

统一生命周期内核是 `scripts/lifecycle.py`：启动按绝对项目路径召回最多三篇、约3000字符；停止或结束时仅采集新增公开对话，净化后可靠入队。SQLite 消息游标防止重复 Stop/SessionEnd 重放，首次启用只建立历史基线并采集近期窗口，不回填完整旧历史。召回块带专用标记，采集时剥离，防止自反馈沉淀。

- Qoder、Codex、WorkBuddy：用户级 `SessionStart`、`Stop`、`SessionEnd` 调用 `scripts/lifecycle_hook.py <source> start|stop|end`。
- Cursor：用户级 `sessionStart`、`stop`、`sessionEnd` 使用同一适配器，但按 Cursor 协议输出 `additional_context`。
- DeepSeek Harness：`integrations/deepseek-memory.mjs` 接提示组装及会话事件，经 Python bridge 调用统一内核；`session/flush` 等待本地入队，不等待云端提炼。
- 启动召回不调用模型；采集不返回继续执行或阻断指令，不主动唤醒对话。缺失 transcript、工作区不明确、读取期间变化会跳过，不猜项目归属。

已部署用户配置不等于现有会话立即加载。Qoder Desktop、Cursor CLI、隔离 Codex 宿主、隔离 DeepSeek headless，以及使用已安装用户 Hook 命令的 WorkBuddy CLI 持久新会话，均已有自动注入/采集证据。WorkBuddy 启动事件缺少 cwd 时从宿主 CODEBUDDY_PROJECT_DIR 补齐；Codex 日常环境的新命令仍需正常信任审阅。DeepSeek、Cursor、WorkBuddy 的既有桌面会话未据此宣称全部重新加载。所有端仍保留相同 MCP 工具作为按需读取和显式沉淀入口，不用规则提醒冒充确定性 Hook。

## 建议的使用方式

开始任务时调用 memory_context 或 memory_search；找到高相关笔记后，用 memory_read 读取正文，再用 memory_neighbors 沿着 wikilink 和反向链接扩展上下文。需要检查知识库是否变脏时调用 memory_health；它只读报告，不会自动改名、移动或删除文件。

完成一个有长期价值的任务时，调用 memory_capture，把原始上下文或候选结论传给它即可。工具会立即返回，不需要等待 Gemini。若只是一次性操作或没有新知识，不必调用。

这组入口吸收了近期 Obsidian Agent 实践的最小共识：Markdown 是事实源，Properties/frontmatter 用于机器筛选，wikilink 用于关系，搜索后按需展开，不把整个 Vault 全量注入上下文；Hook 和后台提炼负责确定性流程，Agent 负责内容判断。

也可以手动把某个会话摘录排队：

    python3 /Volumes/13759427003/skills/agent-knowledge-base/scripts/distiller.py enqueue --note /path/to/session.md

如果之前因 CLIProxyAPI 暂时不可用、隧道断连或鉴权脚本超时而失败，使用 `akb distill-retry` 或：

    python3 /Volumes/13759427003/skills/agent-knowledge-base/scripts/distiller.py retry-failed

`retry-failed` 会将 SQLite 内仍适用于当前正文与作用域版本的失败记录去重后重新排队，并在默认开启自动启动时拉起 worker。已经迁移的旧 JSON 不参与重试，也不会被删除。关闭自动启动：`AKB_DISTILLER_AUTOSTART=0`。

一次性回填本机 Codex 历史会话可使用：

    python3 /Volumes/13759427003/skills/agent-knowledge-base/scripts/history_batch.py start --include-archived
    python3 /Volumes/13759427003/skills/agent-knowledge-base/scripts/history_batch.py status

历史回填会读取 sessions 和 archived_sessions，过滤系统注入内容，保留真实用户/助手消息并分块提炼。它使用独立的 history-batch 队列，不会阻塞实时沉淀队列。

## 提炼结果与追溯

Gemini 必须返回严格 JSON，包含 keep、title、summary、knowledge、patterns、decisions、gotchas、next_steps。`patterns` 会写入 `10-技术笔记/patterns/`，`decisions` 会单独写入决策条目；keep=false 的会话只标记为无长期价值，不会生成长期笔记。成功的长期笔记 frontmatter 会记录 model: gemini-3.8-flash-high 和来源会话路径。

## 手动复核节奏

每天或每完成一个有价值的任务后，把 `00-Inbox/_agent-sessions/` 中真正有长期价值的内容提炼到：

- 10-技术笔记：事实、原理、解决方案和带背景的决策；
- 10-技术笔记/patterns：可以重复执行的步骤或模板。

长期笔记尽量包含“适用条件、限制、验证方式、来源会话”。不要保存 API key、密码、Cookie、完整个人隐私或不必要的原始日志。

## 官方依据与调研备注

Codex 官方 Hooks 文档：

    https://learn.chatgpt.com/docs/hooks

官方文档明确支持 SessionStart、UserPromptSubmit、Stop、SessionEnd 等生命周期事件，并把“自动总结聊天形成持久记忆”列为 Hooks 用途。Codex 也有独立的 Memories 能力，但本方案将 Obsidian Markdown 作为跨客户端、可迁移的主存储。

本次调研已通过本机 `grok-search-mcp` 的 `grok_x_search` 对 X 做只读检索，并将可落地结论和来源链接写入主 Vault 的：

    10-技术笔记/AI与工具链/Obsidian 使用技巧与运维建议（X 调研）.md

该笔记只把 X 帖子作为经验线索，不替代 Obsidian 官方文档；涉及插件和版本的结论仍需在实际环境中复核。后续如需重新查询，可使用：

    https://x.com/search?q=Obsidian%20knowledge%20base&src=typed_query
    https://x.com/search?q=Obsidian%20second%20brain%20agent%20memory&src=typed_query
