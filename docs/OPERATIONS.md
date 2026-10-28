# 运维与恢复

## 只读诊断

```sh
akb status
akb health
akb runtime-status
akb failure-audit
akb knowledge-audit
akb distill-retry --dry-run --error-contains "HTTP 400" --limit 3
```

failure-audit 返回聚合分类，不输出会话正文、原始错误或私人路径；分类优先级包括越界、缺失、不可读、审计中变化、已成功、过期、旧格式、当前。它不证明上游已经恢复。knowledge-audit 汇总配置搜索范围，包含长期笔记之外的收件箱与其他可搜索目录；generated 字段才是生成笔记的专项计数。

队列为零时 worker 不运行正常。发现本地来源不等于客户端连接；never_synced 不等于生命周期没有采集。两条链路应分别检查。

## 提炼与恢复边界

新笔记的 evidence_level=model-extracted 表示模型整理，必须对照来源和实际结果复核。不要把助手声称执行、计划或模型自评当成验证凭证。

格式不合格响应进入失败账本。发布恢复使用独立计数与退避，耗尽后隔离。保留的 publication 指针和人工修改供修复后恢复；不要删掉或改写人工内容来过检查。普通重复 enqueue 不应自动重新开启隔离的恢复窗口，使用显式 retry。

```sh
AKB_DISTILLER_AUTOSTART=0 akb distill-retry --error-contains "发布恢复失败" --limit 1
```

这条命令会改变运行队列，执行前必须先检查 dry-run 和输入范围。关闭 autostart 后仅排队；不把“入队成功”称为模型成功。历史回填使用独立命名空间，旧批次 scope 未验证时不能恢复。

## 配置与安装恢复

源码检出继续使用本机 config.json/distiller.json，既有用户 Hook 的脚本路径可保留。安装版默认状态在 ~/.local/share/agent-knowledge-base，AKB_HOME 可覆盖。AKB_CONFIG 与 AKB_DISTILLER_CONFIG 指向具体文件，AKB_VAULT 对正文位置拥有最高优先级。显式用户配置拥有其 Vault，安装启动器不会再强行覆盖。

构建 wheel 只包含源码和配置示例，默认关闭提炼、不自动跟随 Obsidian。要连接现有 Vault 和模型，应明确配置绝对路径及外发范围，再启用；不把默认安装指向个人主库。

更新前保存当前 commit 和配置原字节。代码回滚用新分支或恢复明确文件，不 reset/force-push，不回滚或删除运行数据。SQLite schema 本轮未升级。

卸载安装包可用 `python -m pip uninstall agent-knowledge-base-local`；数据和本机配置保留。Hook/MCP snippets 只是模板，合并时保留已有条目；卸载时仅移除 AKB 对应条目，不覆盖整个客户端文件。客户端信任和加载状态按各端正常流程验证，不自动放宽权限。
