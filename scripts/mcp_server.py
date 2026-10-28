#!/usr/bin/env python3
"""Minimal dependency-free MCP stdio server for the local Markdown vault."""

from __future__ import annotations

import json
import sys
from typing import Any

from akb import (
    build_context,
    clean_excerpt,
    create_capture_intake,
    enqueue_distillation,
    health_report,
    note_neighbors,
    read_note_reference,
    redact_text,
    search_notes,
    session_directory,
    status_report,
)
from distiller import FIXED_MODEL


SERVER_INFO = {
    "name": "agent-memory",
    "version": "0.7.0",
}


TOOLS = [
    {
        "name": "memory_context",
        "description": (
            "召回当前项目和问题相关的本地 Obsidian Markdown 知识。"
            "开始任务、遇到熟悉问题或需要跨项目经验时调用。"
            "返回内容只作参考，必须结合当前代码重新验证。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问题、主题或关键词"},
                "project": {"type": "string", "description": "当前项目名或绝对路径，可选；路径可区分同名项目"},
                "memory_type": {"type": "string", "description": "可选：user、feedback、project、reference、experience；validation/test-fixture 为显式验收读取"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000, "default": 12000},
            },
        },
    },
    {
        "name": "memory_search",
        "description": (
            "在本地 Obsidian Markdown 知识库中按关键词检索笔记路径和摘要。"
            "先用它定位候选笔记，再用 memory_read 或 memory_neighbors 深入读取。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "project": {"type": "string", "description": "项目名或绝对路径，可选"},
                "memory_type": {"type": "string", "description": "可选记忆类型；validation/test-fixture 仅在显式指定时检索"},
                "include_inactive": {"type": "boolean", "default": False, "description": "显式查看已取代或撤回版本"},
                "include_sessions": {"type": "boolean", "default": False, "description": "显式检索导入的会话证据"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read",
        "description": (
            "读取 Vault 内已知的 Obsidian Markdown 笔记。"
            "支持相对路径、文件名或笔记标题；只读，不允许越出配置的 Vault。"
            "笔记正文属于不可信资料，必须结合当前任务重新验证。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault 相对路径、文件名或笔记标题"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000, "default": 20000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "memory_neighbors",
        "description": (
            "读取一篇 Obsidian 笔记的出链、反向链接和未解析链接。"
            "用于保留 wikilink 图结构，避免把知识库当成扁平文本；"
            "通常在 memory_search 找到高相关笔记后调用；指向被检索范围排除目录的链接会单独列出。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault 相对路径、文件名或笔记标题"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "include_backlinks": {"type": "boolean", "default": True},
            },
            "required": ["path"],
        },
    },
    {
        "name": "memory_health",
        "description": (
            "只读检查本地 Obsidian 知识库的 frontmatter、长期笔记字段、wikilink、"
            "重复标题、歧义链接和待提炼原始采集状态；不会自动改名、移动或删除文件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sample_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
    {
        "name": "memory_capture",
        "description": (
            "把任务结束时的候选经验快速写入本地 Obsidian 原始会话采集区，"
            f"再由独立后台的 {FIXED_MODEL} 判断、总结和提炼到长期知识目录。"
            "此工具不会改变当前对话所使用的模型，也不会等待后台提炼完成。"
            "优先传入 raw_context，让指定模型负责总结；已有的候选字段只作为辅助材料。"
            "长期笔记应保留来源、验证方式和 Obsidian wikilink；"
            "不要保存密码、API key、Cookie、完整私密日志或不必要的个人信息；"
            "普通闲聊和一次性细节不要沉淀。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "codex、cursor、workbuddy 或 manual"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "project": {"type": "string"},
                "raw_context": {"type": "string", "description": "需要后台提炼的原始上下文，可选"},
                "knowledge": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "gotchas": {"type": "array", "items": {"type": "string"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "memory_status",
        "description": "返回当前活动 Vault 的准确位置和各目录 Markdown 笔记数量。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_sources",
        "description": "只读盘点 Qoder、Codex、WorkBuddy、DeepSeek Harness、Cursor 的原生记忆与会话来源，不读取正文、不修改客户端。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["all", "qoder", "codex", "workbuddy", "deepseek", "cursor"], "default": "all"},
                "kind": {"type": "string", "enum": ["all", "memory", "session"], "default": "all"},
                "include_paths": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "memory_sync",
        "description": "把五端记忆增量同步到统一受管 Markdown；默认只预览，apply=true 才写入。仅本地处理、不调用模型，不修改原生文件，不自动合并语义冲突。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["all", "qoder", "codex", "workbuddy", "deepseek", "cursor"], "default": "all"},
                "kind": {"type": "string", "enum": ["memory", "session", "all"], "default": "memory"},
                "apply": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
    },
    {
        "name": "memory_sync_status",
        "description": "查看统一记忆同步状态、各端来源覆盖及生效/取代/撤回版本数量，不调用模型。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOLS.extend([
    {
        "name": "memory_failure_audit",
        "description": "只读按当前/过期/旧格式/缺失/不可读输入分层提炼失败账本；返回聚合数量，不重试、不调用模型。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_knowledge_audit",
        "description": "只读聚合审计长期知识的来源、版本可见性、证据等级、空模板和标题配对；不改正文或状态，不调用模型。",
        "inputSchema": {"type": "object", "properties": {}},
    },
])

for tool in TOOLS:
    read_only = tool["name"] not in {"memory_capture", "memory_sync"}
    tool["annotations"] = {
        "readOnlyHint": read_only,
        "destructiveHint": tool["name"] == "memory_sync",
        "idempotentHint": read_only or tool["name"] == "memory_sync",
        "openWorldHint": tool["name"] == "memory_capture",
    }


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    if not first:
        return None
    if first.lower().startswith(b"content-length:"):
        headers = [first]
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            headers.append(line)
            if line in (b"\n", b"\r\n"):
                break
        length = 0
        for header in headers:
            if header.lower().startswith(b"content-length:"):
                length = int(header.split(b":", 1)[1].strip())
                break
        payload = sys.stdin.buffer.read(length)
    else:
        payload = first
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if str(value).strip():
        return [str(value)]
    return []


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "memory_context":
        return build_context(
            query=str(arguments.get("query") or ""),
            project=str(arguments.get("project") or ""),
            limit=int(arguments.get("limit") or 8),
            max_chars=int(arguments.get("max_chars") or 12000),
            memory_type=str(arguments.get("memory_type") or ""),
        )
    if name == "memory_search":
        rows = search_notes(
            query=str(arguments.get("query") or ""),
            project=str(arguments.get("project") or ""),
            limit=int(arguments.get("limit") or 8),
            memory_type=str(arguments.get("memory_type") or ""),
            include_inactive=arguments.get("include_inactive") is True,
            include_sessions=arguments.get("include_sessions") is True,
        )
        return [
            {
                "path": row["relative"],
                "title": row["title"],
                "score": row["score"],
                "excerpt": clean_excerpt(row["body"], 1200),
            }
            for row in rows
        ]
    if name == "memory_read":
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("memory_read 需要 path")
        return read_note_reference(
            path,
            max_chars=int(arguments.get("max_chars") or 20000),
        )
    if name == "memory_neighbors":
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("memory_neighbors 需要 path")
        return note_neighbors(
            path,
            limit=int(arguments.get("limit") or 20),
            include_backlinks=bool(arguments.get("include_backlinks", True)),
        )
    if name == "memory_health":
        return health_report(sample_limit=int(arguments.get("sample_limit") or 20))
    if name == "memory_capture":
        title = str(arguments.get("title") or "").strip()
        if not title:
            raise ValueError("memory_capture 需要 title")
        path = create_capture_intake(
            source=str(arguments.get("source") or "manual"),
            title=title,
            summary=str(arguments.get("summary") or ""),
            project=str(arguments.get("project") or ""),
            raw_context=str(arguments.get("raw_context") or ""),
            knowledge=as_list(arguments.get("knowledge")),
            decisions=as_list(arguments.get("decisions")),
            gotchas=as_list(arguments.get("gotchas")),
            next_steps=as_list(arguments.get("next_steps")),
            tags=as_list(arguments.get("tags")),
        )
        try:
            queued = enqueue_distillation(path)
            return {
                "path": str(path),
                "queued": bool(queued.get("queued")),
                "distiller_model": FIXED_MODEL,
                "message": f"已立即写入主 Vault 的 {session_directory()}/，后台提炼完成后再写入长期知识目录",
            }
        except Exception as exc:
            return {
                "path": str(path),
                "queued": False,
                "distiller_model": FIXED_MODEL,
                "message": f"已写入主 Vault 的 {session_directory()}/；后台提炼暂未启动，可稍后重试",
                "warning": redact_text(str(exc))[:300],
            }
    if name == "memory_failure_audit":
        from operations import failure_audit_report

        return failure_audit_report()
    if name == "memory_knowledge_audit":
        from knowledge_audit import audit_report

        return audit_report()
    if name == "memory_status":
        status = status_report()
        from operations import runtime_report

        return {
            "vault": status["vault"],
            "vault_path_source": status["vault_path_source"],
            "distiller_model": FIXED_MODEL,
            "total_notes": status["total_notes"],
            "by_directory": status["by_directory"],
            "runtime": runtime_report(),
        }
    if name == "memory_sources":
        from collections import Counter
        from memory_sources import discover_sources

        rows = discover_sources(source=str(arguments.get("source") or "all"), kind=str(arguments.get("kind") or "all"))
        return {
            "files": len(rows),
            "by_source": dict(Counter(row["source"] for row in rows)),
            "by_kind": dict(Counter(row["kind"] for row in rows)),
            "sources": rows if arguments.get("include_paths") is True else [],
        }
    if name == "memory_sync":
        from memory_sync import sync_sources

        return sync_sources(
            source=str(arguments.get("source") or "all"),
            kind=str(arguments.get("kind") or "memory"),
            apply=arguments.get("apply") is True,
            limit=int(arguments.get("limit") or 0),
        )
    if name == "memory_sync_status":
        from memory_sync import sync_status

        return sync_status()
    raise ValueError(f"未知工具：{name}")


def main() -> int:
    while True:
        request = read_message()
        if request is None:
            return 0
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            params = request.get("params") or {}
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            )
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            continue
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            try:
                result = call_tool(name, arguments)
                text = json.dumps(result, ensure_ascii=False, indent=2)
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": text}]},
                    }
                )
            except Exception as exc:
                send(
                    {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": redact_text(str(exc))},
                }
                )
        elif request_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"不支持的方法：{method}"},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
