# Agent Knowledge Base 优化与重构计划

状态：阶段 A/B/C 已落地（2026-09-04）。阶段 D 仍延后。
基准日期：2026-09-04 / 落地：2026-09-04
约束：**不引入未约定技术栈**（不加 sqlite / FTS / 向量库 / MCP SDK / 包管理器）。继续 Python 标准库 + 现有 JSON/Markdown 文件布局。
原则：**最小改动、可回滚、先修热路径、后拆模块。** 对外契约（CLI 子命令、MCP 工具名、笔记 frontmatter、Vault 路径解析顺序）默认保持兼容。

---

## 1. 现状梳理

### 1.1 模块职责

| 模块 | 行数 | 实际职责 | 问题 |
|------|------|----------|------|
| `scripts/akb.py` | 1520 | 配置、Vault 发现、脱敏、frontmatter、检索、wikilink、health、写入、Hook、CLI | God object，读写混在一处 |
| `scripts/distiller.py` | 665 | 入队、worker、鉴权、调 Gemini、写长期笔记、失败隔离 | 锁与重试过弱 |
| `scripts/mcp_server.py` | 335 | stdio JSON-RPC，7 个 memory_* 工具 | 协议自研，热路径直接打满检索 |
| `scripts/history_batch.py` | 565 | Codex JSONL 回填，独立队列 | 零测试；与 distiller 共享 worker |
| `tests/test_akb.py` | 427 | 12 个单测，覆盖脱敏/链接/ingest 竞态/MCP initialize/distiller 写回 | 无性能、无 history_batch、无失败重试 |
| `bin/akb` | 4 | 转调 `/usr/bin/python3 scripts/akb.py` | 钉死系统 Python 3.9.6 |
| `config.json` / `distiller.json` | — | 活动 Vault 与提炼端点 | 与代码内英文默认目录不一致 |
| `vault/` | 3838 md | README 称 staging，实际是完整历史副本 | 双事实源 |
| `.distiller/` | 运行态 | queue / failed / log / history-batch | 无 PID 锁、无失败回收 |

入口关系：

```
Codex Hooks / Cursor / WorkBuddy / CLI
        │
        ▼
   scripts/akb.py          ◄── 唯一共享内核
        │
        ├── mcp_server.py  （import akb）
        ├── distiller.py   （import akb）
        └── history_batch.py（import akb + distiller）
```

### 1.2 依赖关系（热路径）

| 调用方 | 打到的函数 | 实测/约束 |
|--------|------------|-----------|
| MCP `memory_search` / `memory_context` | `search_notes` → 每篇 `read_note` | 活动 Vault 1051 篇，一次搜索 **29.4s 后被杀** |
| Codex `SessionStart` | `hook_command` → `search_notes` + `build_context` | Hook timeout **10s**，必超时 |
| MCP `memory_neighbors` / `memory_health` | `iter_notes` + `iter_all_notes` 全量再读 | 比 search 更重 |
| `memory_capture` / Hook Stop | `create_*_intake` + `enqueue_distillation` | enqueue 再 `subprocess` 调 distiller，timeout 4s |
| distiller worker | `O_EXCL` 空文件锁 + 重试 1 次 | 崩溃锁残留；网络错误直接 failed |

### 1.3 重复逻辑

| 逻辑 | 出现位置 | 建议 |
|------|----------|------|
| `now` / `iso_now` | `akb.py`、`distiller.py`、`history_batch.py` | 阶段 C 收到 `akb.py`，暂不拆文件 |
| `truthy` / `_truthy` | `akb.py`、`distiller.py` | 同上 |
| 原子写 JSON | `distiller.py`、`history_batch.py`（无 fsync） | 阶段 B 让 history_batch 复用 distiller 的写函数 |
| 原子写文本 | 仅 `akb.py`（有 fsync） | 保持 |
| 队列锁 `O_EXCL` | distiller worker、history_batch run | 阶段 B 抽 `_acquire_lock` |
| 正文抽取 `content_text` / `block_text` / `flatten_content` | distiller / history_batch / akb | 阶段 C 再合并，不阻塞 P0 |
| 英文目录默认值 | `NOTE_DIRS`、`session_directory()` 等 | 阶段 C 改为读取 `config.json` 必填项 |

### 1.4 性能与可维护性痛点（已核实）

1. **检索无索引**：`search_notes` 打开全部 Markdown，整篇小写拼接后子串打分，结果列表还带着全文。
2. **Hook 与检索抢同一条慢路径**：SessionStart 用项目名当 query 再 `build_context(max_chars=9000)`。
3. **提炼失败不自愈**：`.distiller/failed` 现有 23 条，主因 `CLIProxyAPI Connection refused`；只重试 1 次；没有 `retry-failed`。
4. **Worker 锁会卡死**：锁文件无 PID，进程崩溃后新 worker 直接退出。
5. **enqueue 覆盖**：同 note 的队列文件被后一次整份覆盖，settle 窗口内可能丢 attempts。
6. **双目录 schema**：代码默认 `90-Sessions` / `20-Knowledge`，配置与主 Vault 用 `00-Inbox/_agent-sessions` / `10-技术笔记`。
7. **仓库内 `vault/` 3838 篇**与活动 Vault 并行，心智模型分叉。
8. **无 git / 无根 `.gitignore`**，脚本与运行态、历史 md 混在一起。
9. **测试未锁热路径**：12/12 通过，但都在临时小 Vault 上，发现不了 29s 问题。

### 1.5 明确不改的边界

- MCP 工具名、入参字段名、返回 JSON 顶层键。
- `memory_capture`「先落盘、不等待模型」语义。
- 提炼模型固定 `gemini-3.7-flash-high`，端点仍走本机 CLIProxyAPI。
- Obsidian Markdown 仍是事实源，不引入第二套存储。
- 不把 `vault/` 里的历史笔记搬进活动 Vault（避免污染主库）。
- 不在本轮改 Codex 的 Headroom hook（那是集成文件，另案处理）。

---

## 2. 重构目标与范围

### 2.1 目标（按优先级）

| ID | 目标 | 范围 | 时机 |
|----|------|------|------|
| G1 | 召回热路径可在 Hook 时限内完成 | `search_notes` / `build_context` / SessionStart | **立即** |
| G2 | 提炼失败可恢复，worker 不再被残留锁卡死 | `distiller.py` + `akb distill-retry` | **立即** |
| G3 | 运行态与源码隔离，改动能回滚 | `.gitignore`、索引文件放 `.distiller/` | **立即** |
| G4 | 目录约定单一来源 | `configured_directory*` 默认值与文档 | 随后 |
| G5 | `akb.py` 按职责切开，重复工具函数合并 | 文件拆分，**不改公开函数名** | 延后 |
| G6 | 知识去重、MCP 协议加固、history_batch 测试 | 质量与覆盖 | 延后 |

### 2.2 本轮不做

- sqlite / 倒排库 / ripgrep 子进程 / 向量检索。
- 把四个脚本改成 installable package 或加 `pyproject.toml` 依赖。
- 切换官方 MCP SDK。
- 自动合并/删除已有重复笔记。
- `git init`（仓库是否纳入版本库由你决定；计划只准备 `.gitignore`）。
- 迁移或删除 `vault/` 内 3838 篇历史文件。

---

## 3. 分阶段实施

回滚总策略：每阶段只改 1～2 个文件；新行为用环境变量关闭；索引/锁文件坏了必须能降级回现行全量扫描。

推荐环境变量（全部可选，默认走新行为）：

| 变量 | 作用 | 关闭后 |
|------|------|--------|
| `AKB_SEARCH_CACHE=0` | 关闭检索缓存 | 现行全量读盘 |
| `AKB_HOOK_LIGHT=0` | SessionStart 恢复全量 context | 现行 6 条 / 9000 字 |
| `AKB_DISTILLER_STALE_LOCK=0` | 不回收残留锁 | 现行 O_EXCL |
| `AKB_DISTILLER_MAX_ATTEMPTS` | 默认改为 5；设回 `2` 即旧行为 | 旧：失败 1 次就进 failed |

---

### 阶段 A — 立即：检索可用来（G1 + G3 的索引部分）

**改动文件**：`scripts/akb.py`（主）、`tests/test_akb.py`  
**不改**：MCP schema、config.json 键、Vault 目录布局。

#### A1. 进程内 + 落盘的 mtime 索引

在 `search_notes` / `note_neighbors` 的读路径前加缓存，**不改打分公式**。

缓存条目（JSON，放 `.distiller/search-index.json`）：

```json
{
  "vault": "<resolved vault path>",
  "generated_at": "...",
  "notes": {
    "10-技术笔记/foo.md": {
      "mtime_ns": 0,
      "size": 0,
      "title": "...",
      "project": "...",
      "tags": "...",
      "haystack": "title project tags body lowercase",
      "excerpt": "前 1200 字"
    }
  }
}
```

规则：

1. 先 `iter_notes()` 拿路径和 `st_mtime_ns`（已实测约 0.06s）。
2. mtime/size 未变 → 用缓存，不 `read_text`。
3. 变化或新文件 → `read_note` 后写回缓存。
4. 索引损坏或 vault 路径变化 → 丢弃重建。
5. `search_notes` 结果仍返回 `path/relative/metadata/body/title/score/modified`，`body` 改为 excerpt（默认 1200 字）。**打分仍用完整 haystack**，分数应与现在同序；仅截断返回正文。
6. `AKB_SEARCH_CACHE=0` 时走旧代码路径（可用 `if` 包一层，旧函数体保留为 `_search_notes_uncached`）。

#### A2. SessionStart 降载

`hook_command("session-start")`：

- 只搜 `knowledge_directory` + `patterns_directory`（在 `search_notes` 增加可选 `directories=`，默认 `None` = 现行范围）。
- `limit=3`，`build_context(..., max_chars=3000)`。
- 项目名为空则不搜，直接返回 0。
- `AKB_HOOK_LIGHT=0` 恢复 6 / 9000。

MCP `memory_context` **不改默认参数**，避免 Cursor/WorkBuddy 行为变化。

#### A3. `.gitignore`（源码隔离）

仓库根新增：

```
.DS_Store
__pycache__/
*.pyc
.distiller/
.codegraph/
.workbuddy/
```

**不**把 `vault/` 写入 ignore，除非你确认它只是 archive。本阶段只隔离运行态。

#### 预期收益

- MCP 长驻进程：二次搜索从 29s 降到毫秒级。
- CLI 冷启动：一次扫盘建索引，之后按 mtime 增量。
- SessionStart 有机会在 10s timeout 内返回。

#### 风险与兼容

| 风险 | 缓解 |
|------|------|
| excerpt 截断导致 CLI `--excerpt` 看不到文末 | CLI 默认 excerpt=0 仍打印 title；需要全文时走 `memory_read` / `read_note_reference` |
| 索引与文件短暂不一致 | 以 mtime_ns+size 为准，写笔记后下次检索刷新 |
| `.distiller/search-index.json` 变大 | 1051 篇 haystack 预估数 MB，可接受；不进 git |
| 打分顺序微调 | 公式不变；用同一组 query 做前后 top-8 对比 |

#### 验收标准

- 现有 12 个单测全部通过。
- 新增：缓存命中不再读文件（可用临时 Vault 改 mtime 断言）。
- 新增：`AKB_SEARCH_CACHE=0` 与缓存开启时，同一 query 的 `relative` 排序一致（允许并列分数因 mtime 造成的稳定排序差异，需固定 mtime）。
- 活动 Vault 基准：`search_notes("hatch-pet 精灵图", limit=8)` **冷启动 < 3s，热启动 < 200ms**，进程不被杀。
- SessionStart 在测试夹具中 `max_chars<=3000` 且调用次数可测。

#### 验证方式

```bash
/usr/bin/python3 -m unittest tests.test_akb -v
AKB_VAULT="/Volumes/13759427003/资源存储/Obsidian Vault" \
  /usr/bin/python3 -c "import time,sys; sys.path.insert(0,'scripts'); import akb; \
  t=time.perf_counter(); akb.search_notes('hatch-pet 精灵图', limit=8); print('cold', time.perf_counter()-t); \
  t=time.perf_counter(); akb.search_notes('hatch-pet 精灵图', limit=8); print('hot', time.perf_counter()-t)"
```

**代码审查要点**

- 打分循环不得改权重数字（12/3/10/8/7/-1）。
- 缓存文件必须经 `atomic_write_text` 或与 distiller 同级的 replace，禁止直接 `write_text` 半截 JSON。
- 失败降级：读索引异常只能重建，不能抛到 MCP。
- 不得把 API key 写入 haystack 前要先 `redact_text`（与现在 `note_text` 一致）。

**回滚**：设 `AKB_SEARCH_CACHE=0` 和 `AKB_HOOK_LIGHT=0`；或还原 `akb.py` 该次提交。删除 `.distiller/search-index.json` 即可重建。

---

### 阶段 B — 立即：提炼可恢复（G2）

**改动文件**：`scripts/distiller.py`、`scripts/akb.py`（CLI 子命令）、`tests/test_akb.py`  
**不改**：Gemini prompt、输出 JSON schema、长期笔记目录。

#### B1. PID 锁

`worker.lock` 写入 `{"pid": n, "started_at": "..."}`。

- 锁不存在 → 创建。
- 锁存在且 PID 仍在（`os.kill(pid, 0)`）→ 退出 0（与现在相同）。
- 锁存在但 PID 不在 → 删除后抢锁。
- `AKB_DISTILLER_STALE_LOCK=0` 时保持现行「文件在就退出」。

#### B2. 瞬时错误与失败回收

- `max_attempts` 默认 5（可用 `AKB_DISTILLER_MAX_ATTEMPTS` 覆盖）。
- 错误分类：`Connection refused` / `reset` / `closed` / timeout / HTTP 5xx → **不改**笔记 `status` 为 `distillation-failed`，留在 queue，`not_before` 指数退避（30s、2min、10min，上限 30min）。
- 耗尽 attempts 才进 `failed/` 并打 `distillation-failed`。
- 新增 `distiller.py retry-failed`：把 `failed/*.json` 移回 queue，`attempts=0`。
- `akb distill-retry` 转调上述命令；`akb distill-status` 增加 `last_error` 样例（最多 3 条，已 redact）。

#### B3. enqueue 合并而不是覆盖

同 `queue_key` 已有文件时：

- 保留较小的 `queued_at`、较大的 `not_before`。
- `attempts` 取已有值（不重置为 0，避免无限重试；retry-failed 才重置）。

#### 预期收益

- CLIProxyAPI 重启后队列自动继续，不必手删 failed。
- worker 崩溃不再永久停摆。
- 23 条存量 failed 可用一次 `distill-retry` 回收。

#### 风险与兼容

| 风险 | 缓解 |
|------|------|
| 退避导致「看起来没动」 | status 输出 `next_not_before` |
| PID 复用误杀锁 | 锁内带 `started_at`，PID 存在但启动时间早于锁则视为活进程，不抢 |
| 网络抖动时反复打模型 | attempts 上限 5 + settle_seconds 仍在 |
| history_batch 共用 worker | 它已设独立 `AKB_DISTILLER_STATE_DIR`，锁按 state_dir 隔离，行为一致即可 |

#### 验收标准

- 现有 distiller 单测仍通过（假 HTTP 服务器那条）。
- 新增：制造残留锁（PID=1 或不存在的 PID）后 `worker()` 能启动。
- 新增：模拟 `Connection refused` 两次，笔记 status 仍为 `distilling` 或 `needs-distillation`，条目留在 queue。
- 新增：`retry-failed` 把 failed 移回 queue 且 `akb distill-status` 的 `failed` 变 0。
- 不调用真实 Gemini（继续用测试内 HTTP handler）。

#### 验证方式

```bash
/usr/bin/python3 -m unittest tests.test_akb -v
/usr/bin/python3 scripts/distiller.py status
# 确认 CLIProxyAPI 可用后再：
/usr/bin/python3 scripts/distiller.py retry-failed
```

**代码审查要点**

- 禁止 `except Exception` 把解析错误也当成可重试网络错误。
- 笔记 status 的合法迁移：`needs-distillation|distilling|distillation-failed` → 成功才 `distilled*`。
- 日志继续 `redact_text`。
- 不在 enqueue 热路径里探测 endpoint（避免 Hook 的 4s subprocess 再加一次网络）。

**回滚**：环境变量恢复旧 attempts/锁行为；或还原 `distiller.py`。queue 文件格式仍是 `{note, queued_at, not_before, attempts}`，旧 worker 能读。

---

### 阶段 C — 随后：约定单一化与小拆分（G4，部分 G5）

只在 A/B 验收通过后做。每次一个主题，可独立回滚。

#### C1. 目录默认值与配置同源

- `session_directory()` 等函数：`config.json` 缺键时 **读文件内已有键失败再报错**，不再静默回落到 `90-Sessions`。
- 测试继续用 `AKB_VAULT` 临时目录，并在 setUp 写一份最小 `config.json` 或通过现有环境覆盖。
- MCP 成功文案改为 `session_directory()`，不要写死中文路径。
- **不改**活动 Vault 里已有文件路径。

验收：缺 `session_directory` 的配置在测试中抛 `RuntimeError`；有配置时行为与现在完全一致。

#### C2. 重复工具函数收到 `akb.py`

`iso_now` / `truthy`：`distiller.py` 与 `history_batch.py` 改为 `from akb import iso_now`（akb 已有 `_truthy`，可升为公开 `truthy`）。  
原子写 JSON：history_batch 改调 `distiller.atomic_write_json`。  
**不拆文件。**

验收：import 循环检查（akb 不得 import distiller，现状已满足）；单测全绿。

#### C3. `bin/akb` 解释器

改为 `exec python3 "$SCRIPT_DIR/../scripts/akb.py" "$@"`（PATH 中的 python3），README 注明 `>=3.9`。  
若你本机 GUI MCP 仍缺 PATH，`integrations/mcp.json.example` 继续写绝对路径，那是另一条问题。

风险：WorkBuddy/Codex 若依赖 `/usr/bin/python3` 的 3.9 行为。验收：`python3 -m unittest` 与 `/usr/bin/python3 -m unittest` 都跑一遍。

#### C4. 文档对齐

README / `vault/README.md` / 集成说明：活动入口是主 Vault 中文目录；`vault/` 标注为 **历史 archive，不参与默认召回**。不删文件。

---

### 阶段 D — 延后：结构重构与质量（G5 / G6）

仅当 C 稳定、且 `akb.py` 还要继续涨时再做。

| 项 | 做法 | 为什么延后 |
|----|------|------------|
| 拆 `akb.py` 为 `scripts/akb_lib/{config,redact,search,notes,hooks}.py` + 薄 `akb.py` 再导出 | 公开符号名不变，`from akb import search_notes` 仍可用 | 现在 1520 行可维护；拆文件不修 29s 问题 |
| `health_report` / backlinks 改「可选全量」 | MCP `memory_health` 加 `quick=true` 默认只统计目录计数 | 要改 MCP 默认，需你确认 |
| history_batch 单测 | 用假 jsonl 测 chunk / 过滤 / 幂等文件名 | 不阻塞日常召回 |
| 提炼去重 | `distilled_notes` 前按 title casefold 命中则 overwrite 同 hash 前缀文件（已有 `overwrite=True`） | 需产品规则：何时合并 |
| 脱敏补 JWT/Cookie | 只加正则，保持现有 marker | 误伤面要单独测 |
| MCP Content-Length 上限 | 例如 1MB | 协议边角，不是主痛点 |

**仍然不做**：sqlite FTS、向量库、官方 MCP SDK、自动删除重复笔记、把 `vault/` 迁入主库。

---

## 4. 立即 vs 延后清单

**立即（阶段 A + B，建议同一迭代连续做，但分两个提交）**

1. `search_notes` mtime 缓存 + 结果 body 改 excerpt  
2. SessionStart 轻量召回  
3. 根 `.gitignore`  
4. distiller PID 锁 + 网络错误退避 + `retry-failed`  
5. enqueue 合并字段  
6. 补对应单测与一条活动 Vault 计时命令  

**随后（阶段 C）**

7. 目录默认值不再回落英文路径  
8. 三个文件的 `iso_now`/`truthy` 去重  
9. `bin/akb` 用 `python3`  
10. README / vault README 标明 archive  

**延后（阶段 D）**

11. 拆包、health 默认降载、history_batch 测试、去重、脱敏扩展、MCP 成帧加固  

**明确不做**

12. 新存储引擎、新依赖、迁移 3838 篇历史 md、改 MCP 工具名、改提炼模型  

---

## 5. 提交与回滚切分

建议提交粒度（尚未建 git 时，按补丁理解）：

| 提交 | 内容 | 回滚单位 |
|------|------|----------|
| 1 | `.gitignore` | 删文件 |
| 2 | `akb.py` 检索缓存 + Hook 降载 + 测试 | 还原该文件；或两个环境变量 |
| 3 | `distiller.py` 锁/重试/retry-failed + `akb.py` CLI + 测试 | 还原两文件；queue JSON 仍兼容 |
| 4 | 目录默认值与文案（阶段 C） | 还原；已写入的笔记路径不变 |

每个提交后必须：`python3 -m unittest tests.test_akb -v` 全绿。

---

## 6. 回归矩阵

| 场景 | 必须保持 |
|------|----------|
| `memory_capture` | 立即返回；先写 Inbox；不调模型 |
| `ingest_transcript` 同 session | 仍只更新同一文件（已有单测） |
| 脱敏 | `sk-` / Bearer / 私钥仍替换；读路径再 redact 一遍 |
| wikilink 歧义 | `AmbiguousNoteReference` 仍进 health / neighbors |
| distiller 成功路径 | 仍写 knowledge + pattern + decision 三类笔记 |
| 固定模型 | 请求里 `model` 仍是 `gemini-3.7-flash-high` |
| Vault 发现 | `AKB_VAULT` > 活动 Obsidian > `config.json` 顺序不变 |

| 允许变化 | 说明 |
|----------|------|
| `search_notes` 返回的 `body` 变短 | 全文改走 `read_note_reference` |
| SessionStart 注入字数变少 | 用环境变量可恢复 |
| failed 目录暂时变少 | retry 后应下降，属预期 |
| `.distiller/search-index.json` 新增 | 运行态，已 ignore |

---

## 7. 基准与审查清单（实施时用）

**基准（阶段 A 前后各跑一次，记数字）**

- 活动 Vault：`iter_notes` 条数、cold/hot `search_notes` 秒数、是否 SIGKILL。
- 临时 20 篇 Vault：确认缓存不改变 top-N `relative`。

**审查（每个提交）**

- [ ] 无新第三方 import  
- [ ] 无 MCP 工具改名  
- [ ] 无默认目录被静默写到另一套 schema（阶段 C 之前尤其不要动 `NOTE_DIRS` 使用者的写入路径）  
- [ ] 热路径异常有降级，不把异常抛给 Codex Hook（Hook 已 swallow Exception，保持）  
- [ ] 测试未改成依赖真实网络或真实主 Vault  

---

## 8. 建议实施顺序（执行时）

1. 先 A1 缓存，用活动 Vault 计时证明不再 29s。  
2. 再 A2 Hook 降载。  
3. 再 B 锁与 retry；CLIProxyAPI 在线时对 failed 做一次 `retry-failed`（这是运维动作，需你确认后再跑）。  
4. 停下来看召回和沉淀是否恢复，再考虑 C。  

阶段 A/B 的代码改动集中在 `akb.py` + `distiller.py` + 测试，不碰 `vault/` 和主 Vault 里的笔记内容。
