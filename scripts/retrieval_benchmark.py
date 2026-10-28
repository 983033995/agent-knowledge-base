"""Deterministic offline retrieval benchmark for Chinese/code mixed queries.

The benchmark creates an isolated temporary Vault and never reads or writes the
user's active Obsidian Vault. Its corpus is synthetic and its scores are a
regression signal, not a claim about production relevance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import akb


TOPICS: tuple[dict[str, Any], ...] = (
    {
        "id": "lifecycle",
        "title": "SessionStart Hook 会话启动召回",
        "body": "Codex Cursor Qoder WorkBuddy DeepSeek sessionstart sessionend stop lifecycle hook 自动注入上下文 context memory MCP 工具 transcript capture workspace",
        "queries": (
            "Codex SessionStart 自动注入项目记忆",
            "Cursor 新 session 读取历史 context hook",
            "WorkBuddy Stop 和 SessionEnd 怎样增量采集 transcript",
            "会话开始的 hook 如何给 Agent context",
            "Qoder 新对话怎么自动加载旧经验",
            "DeepSeek Harness session event flush 保存新对话",
            "不要求模型调用 memory 工具的启动机制",
            "会话结束 Stop Hook 追加用户公开消息",
            "启动时把旧项目注入上下文 不手动查记忆",
            "新 conversation 从 workspace 召回过往经验",
        ),
    },
    {
        "id": "retry",
        "title": "SQLite 提炼队列指数退避与失败重试",
        "body": "SQLite distiller 队列 worker HTTP 400 HTTP 503 network timeout 临时连接错误 exponential backoff retry failed attempts 入队恢复",
        "queries": (
            "CLIProxyAPI HTTP 503 之后如何重试任务",
            "后台 worker 遇到 network timeout 保留 queue",
            "sqlite distiller failed 记录指数 backoff",
            "短暂上游连接错误别丢掉提炼任务",
            "HTTP400 参数错误和网络错误怎么区分 retry",
            "queue worker attempts 达上限后隔离输入",
            "failed jobs 指定 error contains 小批量恢复",
            "队列暂不可用 later retry 以及状态检查",
            "SQLite 数据库故障后读取待处理队列",
            "从失败记录恢复一个 unchanged markdown note",
        ),
    },
    {
        "id": "scope",
        "title": "项目绝对路径作用域隔离",
        "body": "project_path project_key scope absolute path workspace 同名 basename exact match 精确匹配 cross project isolation 不同仓库不能串读 user memory 可跨项目共享",
        "queries": (
            "同名 demo workspace 用 project_path 分开检索",
            "absolute directory 用于防止项目经验串读",
            "project_key 和 scope 怎样隔离本地 repo",
            "basename 一样但 /one 与 /two 不是一个项目",
            "同名项目 exact match 不采用子串",
            "shared user memory 与 project scoped 知识边界",
            "workspace 移动以后旧绝对路径不应误召回",
            "多个 agent 项目上下文不能 cross project",
            "过滤项目知识用 repository absolute path",
            "只有 user scope 的用户偏好能跨项目共享",
        ),
    },
    {
        "id": "mcp",
        "title": "MCP stdio JSON-RPC 工具服务",
        "body": "MCP server stdio JSON-RPC initialize tools/list tools/call memory_search memory_read memory_neighbors memory_context transport protocol 工具调用",
        "queries": (
            "MCP initialize 后读取 tools/list 方法",
            "JSON-RPC stdio 里的 tools/call memory_search",
            "memory_neighbors 和 memory_read 是哪些 MCP tool",
            "本地 MCP server 不暴露 HTTP 端口",
            "stdio transport 返回 initialize serverInfo",
            "测试 MCP protocol tool annotations",
            "memory_context 搜索知识库 API",
            "如何检查 MCP 工具名和 list response",
            "Agent 客户端怎样调用本地 MCP memory",
            "structured content 之外还有文本 JSON-RPC result",
        ),
    },
    {
        "id": "sync",
        "title": "来源同步增量游标与预览",
        "body": "memory_sync sources native agent memories incremental cursor checksum source discovery apply preview sqlite records notes files pending recovery Codex Qoder Cursor WorkBuddy DeepSeek",
        "queries": (
            "native Codex memories 增量同步到 Markdown",
            "sync 默认 preview 不要写 Vault",
            "SQLite cursor 如何记录 source checksum",
            "Qoder Cursor WorkBuddy 来源发现条目",
            "memory_sync apply 才会实际导入 sessions",
            "pending publication synchronization recovery",
            "同一条来源第二次运行应变成 unchanged",
            "不同 client 的原生知识怎么统一映射",
            "source discovery 不代表 client live connection",
            "never_synced 与 lifecycle automatic capture 有何区别",
        ),
    },
    {
        "id": "privacy",
        "title": "会话脱敏与提示注入边界",
        "body": "redact secrets API key password token cookie authorization private data prompt injection untrusted transcript thinking tool call nested recalled context local vault cloud model",
        "queries": (
            "API key 和 bearer token 写入会话前要 redacted",
            "password cookie secret 不进入长期知识",
            "会话里的 prompt injection 只能作为不可信资料",
            "不要采集 assistant thinking 和 tool call",
            "recalled context 需要剥离避免 self training",
            "个人数据本地保存 但提炼会发送模型 endpoint",
            "authorization header 和 cookie 防止泄漏",
            "untrusted transcript 不能覆写 system prompt",
            "敏感文件名不要写到公开错误日志",
            "本地脱敏不代表普通文本都已去隐私",
        ),
    },
    {
        "id": "versions",
        "title": "正文哈希和作用域版本提交保护",
        "body": "source_input_hash source_context_hash content_hash metadata_fingerprint input_version snapshot claim pending publication atomic commit stale response body changed scope changed superseded output recovery",
        "queries": (
            "源 Markdown 正文变更后旧模型返回要丢弃",
            "source_context_hash 防止 scope change stale output",
            "metadata fingerprint 和 content hash 的作用",
            "pending publication 崩溃后怎样恢复",
            "模型调用期间用户修改了 note 不覆盖新内容",
            "input_version 相同的重复 enqueue 要去重",
            "旧的 distilled output should be superseded",
            "原子更新 Markdown 与队列记录一致性",
            "changed context invalidates an inflight claim",
            "重新提炼时保留旧 source lineage",
        ),
    },
    {
        "id": "wikilinks",
        "title": "Obsidian Wikilink 双向关系读取",
        "body": "Obsidian wikilink backlinks outgoing links ambiguous reference broken links memory_neighbors graph traversal related notes frontmatter markdown",
        "queries": (
            "Obsidian [[note]] wikilink 的反向链接",
            "memory_neighbors 返回 outgoing 和 backlinks",
            "歧义链接需要报告多个候选文件",
            "broken wikilink 目标缺失检查",
            "related notes 怎样保留 Markdown graph",
            "frontmatter properties 与正文 wikilinks 一起读取",
            "知识笔记之间通过双链扩展 context",
            "ambiguous note reference 不应猜一个链接",
            "多个同名 markdown 文件怎么解析",
            "邻居关系查询和全文搜索区别是什么",
        ),
    },
    {
        "id": "ranking",
        "title": "关键词检索排序与上下文预算",
        "body": "search_notes tokenize title match body match score project pattern directory modified recency max_chars build_context excerpt ranking cache mtime no match",
        "queries": (
            "search_notes 标题匹配比 body bonus 高吗",
            "关键词 score 同分按 modified time 排列",
            "Context 最大 max_chars 超限截断",
            "中文 token substring query matching",
            "目录 patterns 权重如何改变 ranking",
            "empty query 如何召回项目概览",
            "mtime cache 失效依据 file size",
            "没有匹配时 context 要返回空结果提示",
            "search limit 最大几条结果",
            "高相关的 title match 在 Top 3 排序",
        ),
    },
    {
        "id": "state",
        "title": "SQLite 状态迁移和只读状态查询",
        "body": "state-migrate SQLite BEGIN IMMEDIATE transaction journal DELETE synchronous FULL backup migration marker read only state database corruption JSON import idempotent worker lock",
        "queries": (
            "state-migrate 迁移 JSON 前先备份 SQLite",
            "BEGIN IMMEDIATE transaction worker lock migration",
            "只读 status 不能创建数据库或 Vault",
            "旧状态 JSON 重复导入应保持 idempotent",
            "SQLite WAL hot journal 崩溃恢复",
            "迁移遇到活跃 worker 要停下不杀进程",
            "journal mode DELETE 和 synchronous FULL",
            "损坏 JSON migration rollback all jobs",
            "历史回填 scope 未验证时只读保留",
            "state database schema version 不支持应报错",
        ),
    },
)

NO_ANSWER_QUERIES = (
    "qzxv violet parachute nowhere 73416",
    "zzqmx nebula recipe quux 99172",
    "xqzv teapot orbital plover 867",
    "vqxqz penguin equation saffron 55129",
    "qzxm lunar origami zephyr 6071",
    "qzxv fabricated melody glorp 721",
    "zzqmx invisible staircase zaffre 291",
    "xqzv volcano cucumber plover 8383",
    "vqxqz marshmallow satellite quux 7770",
    "qzxm zaffre klor 1109",
    "项目火箭味觉飞船",
    "迁移项目气象模型",
)


def benchmark_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for topic in TOPICS:
        for index, query in enumerate(topic["queries"]):
            cases.append({
                "query": query,
                "expected": topic["id"],
                "kind": "answerable",
                "split": "holdout" if index >= 8 else "train",
            })
    for index, query in enumerate(NO_ANSWER_QUERIES):
        cases.append({
            "query": query,
            "expected": None,
            "kind": "no-answer",
            "split": "holdout" if index >= 5 else "train",
        })
    cases.extend((
        {"query": "alpha", "expected": "same-name-one", "kind": "scope", "split": "holdout", "project": "/one/demo"},
        {"query": "beta", "expected": "same-name-two", "kind": "scope", "split": "holdout", "project": "/two/demo"},
        {"query": "alpha", "expected": None, "kind": "scope", "split": "holdout", "project": "/three/demo"},
        {"query": "稳定配置", "expected": "current-version", "kind": "inactive", "split": "holdout"},
        {"query": "akb-benchmark-fixture-unique-74628", "expected": None, "kind": "fixture", "split": "holdout"},
        {"query": "akb-benchmark-fixture-unique-74628", "expected": "validation-fixture", "kind": "fixture-opt-in", "split": "holdout", "memory_type": "validation"},
        {"query": "akb-benchmark-fixture-unique-74628", "expected": None, "kind": "fixture", "split": "holdout", "include_inactive": True},
        {"query": "qzxvplmnr zzqmxvk xyzzqw", "expected": None, "kind": "no-answer", "split": "holdout", "project": "/unknown/repo"},
    ))
    return cases


def _write_corpus(vault: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for topic in TOPICS:
        paths[topic["id"]] = akb.write_note(
            akb.knowledge_directory(), f"{topic['id']}.md",
            {"title": topic["title"], "type": "knowledge", "scope": "user"},
            topic["body"],
        )
    for identity, project_path, content in (
        ("same-name-one", "/one/demo", "数据库 migration from alpha workspace"),
        ("same-name-two", "/two/demo", "数据库 migration from beta workspace"),
    ):
        paths[identity] = akb.write_note(
            akb.knowledge_directory(), f"{identity}.md",
            {"title": "同名项目数据库迁移", "type": "knowledge", "scope": "project",
             "project": "demo", "project_path": project_path}, content,
        )
    for identity, status in (("old-version", "superseded"), ("current-version", "active")):
        paths[identity] = akb.write_note(
            akb.knowledge_directory(), f"{identity}.md",
            {"title": "数据库 migration 稳定配置", "type": "knowledge", "status": status},
            "稳定配置 数据库 migration",
        )
    paths["validation-fixture"] = akb.write_note(
        akb.knowledge_directory(), "validation-fixture.md",
        {"title": "隔离验收夹具", "type": "validation"},
        "akb-benchmark-fixture-unique-74628",
    )
    return paths


@contextmanager
def isolated_corpus() -> Iterator[dict[str, Path]]:
    with tempfile.TemporaryDirectory(prefix="akb-retrieval-benchmark-") as directory:
        root = Path(directory)
        vault = root / "vault"
        config = root / "config.json"
        config.write_text(json.dumps({
            "vault_path": str(vault),
            "auto_discover_obsidian": False,
            "session_directory": "00-Inbox/_agent-sessions",
            "knowledge_directory": "10-技术笔记",
            "patterns_directory": "10-技术笔记/patterns",
            "decisions_directory": "10-技术笔记",
            "managed_directories": ["00-Inbox/_agent-sessions", "10-技术笔记", "10-技术笔记/patterns"],
            "search_all_markdown": False,
            "search_directories": ["00-Inbox/_agent-sessions", "10-技术笔记"],
            "search_exclude_directories": [],
        }), encoding="utf-8")
        environment = {
            "AKB_CONFIG": str(config),
            "AKB_VAULT": str(vault),
            "AKB_SEARCH_INDEX": str(root / "search-index.json"),
            "AKB_STATE_DB": str(root / "state.sqlite3"),
            "AKB_SYNC_STATE_DIR": str(root / "sync"),
            "AKB_DISTILLER_STATE_DIR": str(root / "distiller"),
            "AKB_DISTILLER_CONFIG": str(root / "distiller.json"),
            "AKB_DISTILLER_AUTOSTART": "0",
        }
        Path(environment["AKB_DISTILLER_CONFIG"]).write_text(
            json.dumps({"enabled": False, "state_dir": str(root / "distiller")}), encoding="utf-8",
        )
        with patch.dict(os.environ, environment):
            akb._CONFIG_CACHE = None
            akb._CONFIG_CACHE_KEY = None
            akb._CONFIG_CACHE_REGISTRY_PATH = None
            akb._SEARCH_INDEX_MEM = None
            akb._SEARCH_INDEX_MEM_KEY = None
            try:
                yield _write_corpus(vault)
            finally:
                akb._CONFIG_CACHE = None
                akb._CONFIG_CACHE_KEY = None
                akb._CONFIG_CACHE_REGISTRY_PATH = None
                akb._SEARCH_INDEX_MEM = None
                akb._SEARCH_INDEX_MEM_KEY = None


def _query_results(case: dict[str, Any]) -> list[dict[str, Any]]:
    return akb.search_notes(
        query=case["query"],
        project=case.get("project", ""),
        memory_type=case.get("memory_type", ""),
        include_inactive=bool(case.get("include_inactive", False)),
        limit=3,
    )


def run_benchmark(split: str = "all", *, cjk_fragments: bool = True) -> dict[str, Any]:
    cases = [case for case in benchmark_cases() if split == "all" or case["split"] == split]
    cjk_patch = None if cjk_fragments else patch.object(akb, "_ordered_cjk_fragment_hits", return_value=0)
    with isolated_corpus() as paths:
        if cjk_patch is not None:
            cjk_patch.start()
        corpus_note_count = len(paths)
        try:
            outcomes = []
            for case in cases:
                results = _query_results(case)
                result_ids = [next((key for key, path in paths.items() if path == row["path"]), "") for row in results]
                expected = case["expected"]
                rank = result_ids.index(expected) + 1 if expected in result_ids else None
                outcomes.append({
                    "query": case["query"],
                    "kind": case["kind"],
                    "split": case["split"],
                    "expected": expected,
                    "top3": result_ids,
                    "rank": rank,
                })
        finally:
            if cjk_patch is not None:
                cjk_patch.stop()

    answerable = [item for item in outcomes if item["expected"] is not None]
    no_answer = [item for item in outcomes if item["expected"] is None]
    reciprocal_ranks = [1 / item["rank"] if item["rank"] else 0.0 for item in answerable]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in outcomes:
        grouped.setdefault(item["kind"], []).append(item)
    return {
        "benchmark": "akb-synthetic-retrieval-v1",
        "retrieval_method": "lexical-plus-ordered-cjk-trigrams" if cjk_fragments else "lexical-only-baseline",
        "split": split,
        "query_count": len(outcomes),
        "corpus_note_count": corpus_note_count,
        "answerable_count": len(answerable),
        "top1_accuracy": sum(item["rank"] == 1 for item in answerable) / len(answerable) if answerable else None,
        "recall_at_3": sum(item["rank"] is not None for item in answerable) / len(answerable) if answerable else None,
        "mrr_at_3": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else None,
        "no_answer_count": len(no_answer),
        "no_answer_empty_rate": sum(not item["top3"] for item in no_answer) / len(no_answer) if no_answer else None,
        "special_cases": {
            kind: {
                "count": len(rows),
                "pass_rate": sum(
                    (row["rank"] is not None) if row["expected"] is not None else not row["top3"]
                    for row in rows
                ) / len(rows),
            }
            for kind, rows in grouped.items() if kind not in {"answerable", "no-answer"}
        },
        "failures": [item for item in outcomes if (
            (item["rank"] is None if item["expected"] is not None else bool(item["top3"]))
        )],
        "limitations": [
            "Synthetic, hand-authored corpus and queries; this is not production relevance or user benefit.",
            "Ranking metrics use a single known relevant note per answerable query.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("all", "train", "holdout"), default="all")
    parser.add_argument("--lexical-only", action="store_true", help="compare against token-substring retrieval without CJK fragments")
    args = parser.parse_args(argv)
    print(json.dumps(run_benchmark(args.split, cjk_fragments=not args.lexical_only), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
