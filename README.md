# Agent Knowledge Base

面向 Codex、Qoder、Cursor、WorkBuddy、DeepSeek Harness 的本地知识中枢。
Markdown 是知识正文的唯一事实源；SQLite 保存队列、版本、来源映射和增量游标。
生命周期 Hook 自动召回/采集，MCP 提供按需读取与显式沉淀。

当前版本：0.7.0。支持 macOS、Linux 和 Python >=3.9；使用 fcntl，尚不支持原生 Windows。
运行时只使用 Python 标准库；DeepSeek 插件契约测试需要 Node。
仓库为私有，当前没有授予开源许可。

## 安装

从仓库目录安装到独立虚拟环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/akb status
```

安装生成 akb、akb-mcp、akb-hook 三个入口。把所在虚拟环境的 bin 加入客户端可见 PATH，或在客户端配置中使用这些入口的绝对路径。

安装版默认使用 ~/.local/share/agent-knowledge-base，默认不跟随活动 Obsidian，也不启用模型提炼。只读 status 不创建缺失目录。
AKB_HOME 可设置独立安装目录；AKB_CONFIG 和 AKB_DISTILLER_CONFIG 可指定用户配置。
安装入口将相对 vault_path/state_dir 按对应配置文件目录解析；源码脚本保留原有按项目根解析的兼容行为。
显式 AKB_VAULT 优先于配置正文位置。

源码方式继续可用：

```sh
python3 scripts/akb.py status
python3 scripts/mcp_server.py
```

已有本机 config.json/distiller.json 不被覆盖。干净检出没有 config.json 时使用 config.example.json 中的隔离 .akb-vault；
它与历史 vault/、用户活动 Obsidian 无关。
指定 AKB_CONFIG 时文件必须存在，错误路径会明确报错。

## 配置现有 Vault

复制配置示例到明确的位置，设置 vault_path 的绝对路径及所需目录，按需打开 auto_discover_obsidian。
要固定到指定库就保持该值为 false。自动发现只在明确开启且存在唯一活动 Vault 时跟随；
AKB_VAULT 永远优先。

默认目录约定：

```text
Vault/
├── 00-Inbox/_agent-sessions/  脱敏会话摘录与提炼状态
├── 10-技术笔记/               可复用候选知识与决策
└── 10-技术笔记/patterns/      可复用模式与流程
```

config.json、distiller.json、vault/、.akb-vault/、.distiller/ 和私人验收宿主均不跟踪，也不包含在 wheel 中。
不要提交凭据、原始会话或完整个人 Vault。

## 日常入口

```sh
akb status
akb search "发布恢复" --project /absolute/path/to/project
akb context --project /absolute/path/to/project --query "作用域版本"
akb health
akb runtime-status
akb failure-audit
akb knowledge-audit
akb distill-retry --dry-run --error-contains "HTTP 400" --limit 3
```

项目绝对路径可区分同名仓库。默认检索排除非活动版本、会话证据和 validation/test-fixture；
测试材料只在显式指定 --memory-type validation 或 test-fixture 时进入检索，include_inactive 不能绕过。
context_role: project-overview 或 project_overview: true 可标记稳定项目概览。

启动上下文只做一次检索，最多三篇、约 2800 字符，读取不调用云模型。
同次检索可复用来源快照，但会检查文件变化；不会跨请求缓存版本有效性。

## 生命周期与 MCP

- Codex、Qoder、WorkBuddy：SessionStart/Stop/SessionEnd 对应 akb-hook SOURCE start/stop/end。
- Cursor：sessionStart/stop/sessionEnd，启动返回 additional_context，工作区归属不唯一时跳过。
- DeepSeek Harness：integrations/deepseek-memory.mjs 通过 Python bridge 接系统提示组装和会话事件；AKB_PYTHON 可指定 Python，默认 PATH 中的 python3。
- Stop/End 只采集新增公开对话，剥离思考、工具内容和旧召回块，落盘入队后返回；不等待云端提炼，也不请求继续对话。

[integrations/mcp.json.example](integrations/mcp.json.example) 与 [Codex Hook 示例](integrations/codex-hooks.json) 使用安装入口。
模板不是完整用户配置，合并时保留现有 Hook；不自动改变信任或权限。源码部署的旧绝对脚本路径仍受支持。
WorkBuddy 的宿主启动事件若缺 cwd，适配命令应从 CODEBUDDY_PROJECT_DIR 补齐；不要猜测项目。
已有会话不一定重新加载配置，需新会话验收。

MCP 提供 12 个工具：

| 类别 | 工具 |
|---|---|
| 读取 | memory_context、memory_search、memory_read、memory_neighbors |
| 诊断 | memory_health、memory_status、memory_sources、memory_sync_status、memory_failure_audit、memory_knowledge_audit |
| 写入 | memory_capture；memory_sync 默认预览，apply=true 才写 |

## 异步提炼与可信度

本项目保留既有固定模型 gemini-3.8-flash-high，通过配置的 OpenAI-compatible /v1/chat/completions 调用。
示例配置默认 enabled=false；启用前明确允许外发的会话范围。
默认本机端点为 http://127.0.0.1:8317/v1/chat/completions。
鉴权来源依次是 AKB_DISTILLER_API_KEY/CLIPROXY_API_KEY、本机 CLIProxyAPI 配置或显式 auth_command；
凭据不写入知识正文。正文在本地不等于提炼完全离线。

输出必须包含 keep、title、summary、knowledge、patterns、decisions、gotchas、next_steps 八字段，
类型分别为布尔、字符串及字符串数组。缺字段、未知字段、重复键、NaN、错误数组项都报错。
为了兼容已有输出，只接受整个响应是一段 JSON 围栏的形式；围栏前后混入文字仍拒绝。
keep=false 只有在结构合法时才会完成为无长期价值。

新生成知识的 evidence_level=model-extracted 表示模型整理，未经独立执行验证；
应结合来源会话、当前代码与实际结果再复用。模型自评、助手声称完成与实际回执不能混为一谈。
维护提示不再输出空的适用条件/验证方式。

正文哈希和作用域指纹保护提交；模型请求期间不持有 SQLite 事务。
永久发布恢复错误按独立计数退避，达到上限后隔离；即使 Markdown 写失败，失败账本仍保留错误。
同版本普通 enqueue 不重开隔离窗口，修复后通过显式 retry 恢复。
不自动覆盖人工修改，不批量重放历史失败。

## 状态与恢复

原生来源同步、生命周期采集和后台提炼是三条不同链路。
来源文件存在不证明客户端已连接；never_synced 不证明 Hook 没有采集；queue=0 时 worker 不运行正常。

failure-audit 按当前输入、旧版本、旧格式、缺失、不可读等情况聚合。
knowledge-audit 汇总配置搜索范围的来源、有效版本、空模板和标题碰撞；标题相同的 knowledge/pattern 配对不是删除依据。
这些命令不写状态、笔记或缓存，不发送会话给模型。

state-migrate 先备份再幂等导入旧 JSON，保留旧文件，不启动 worker；遇到活跃/未知锁状态会停止。
SQLite 使用短 BEGIN IMMEDIATE 事务、FULL 同步和 DELETE journal；只读访问不创建缺失数据库。
历史回填仍保留独立命名空间，未验证旧批次所属 Vault 时不能自动恢复。

细节见 [运维与恢复](docs/OPERATIONS.md)。

## 测试、评测与构建

```sh
python3 -B scripts/run_tests.py
python3 -B scripts/retrieval_benchmark.py
python3 -B scripts/retrieval_benchmark.py --split holdout
python3 -B scripts/validate_lifecycle.py
python3 -m pip install build
python3 -m build --wheel
python3 scripts/check_distribution.py
```

测试运行器强制临时 Vault、状态、缓存和模型配置，关闭后台自动启动。
离线检索基准使用 120 条人工合成查询、15 篇笔记及独立留出标记，包含中文/英文 symbol 混合、无答案、同名项目、旧版本和夹具隔离。
成绩只作为回归基线，不能代表生产相关率或用户收益。
validate_lifecycle 证明五来源的子进程适配器契约，明确不等同于五端原生客户端验收。

CI 在干净 Ubuntu 上运行 Python 3.9/3.14、Node 22、上述隔离验证和 wheel 构建/安装，不使用生产密钥。
没有 npm/PyPI 发布或公开部署流程。

[发展计划](docs/ROADMAP.md) · [版本记录](CHANGELOG.md) · [验证记录](docs/VALIDATION.md)。
