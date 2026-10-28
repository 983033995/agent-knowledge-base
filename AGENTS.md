# Agent Knowledge Base

- 知识正文在外部 Obsidian Vault；SQLite、队列、游标和缓存是运行态。只为当前任务修改源码，测试不得触碰真实 Vault 或客户端配置。
- 当前支持 macOS/Linux，Python >=3.9；依赖 fcntl，不能把 wheel 标签当成原生 Windows 验收。
- 本机 config.json、distiller.json 保持不跟踪；干净检出使用 config.example.json。主 Vault、vault/、.distiller/、凭据与历史验收宿主不得进入 Git 或构建包。
- 统一隔离测试：`rtk proxy python3 -B scripts/run_tests.py`。检索基准：`rtk proxy python3 -B scripts/retrieval_benchmark.py`。五端适配器契约：`rtk proxy python3 -B scripts/validate_lifecycle.py`。
- 模型提炼输出是候选知识，不是执行证明。固定模型与客户端正常对话路由不得因测试而更改。
- 默认召回不得包含 validation/test-fixture；显式读取仍可用于验收。正文/作用域/发布指针校验必须保留；不得用跨请求缓存隐藏已失效结果。
- 合成适配器契约、历史宿主回执、当前真实宿主验收必须分开报告。合成查询分数不代表生产召回质量。
- 所有生产同步、失败重试、历史回填和客户端重启先核实范围与可恢复性；不要批量重放旧版本或为了消除告警放开旧笔记。
- 不用 `git add .`。改动以逻辑分组提交，修改 package/MCP 版本时同步测试、CHANGELOG 和验证记录。
