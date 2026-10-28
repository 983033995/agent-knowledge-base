#!/usr/bin/env python3
"""一次性完整提炼本机 Codex 历史会话。

只读取 ~/.codex/sessions 和 ~/.codex/archived_sessions 中的 JSONL，
筛选真实 user/assistant 消息，分块写入共享 Vault，再使用独立队列调用
distiller。批处理队列与实时队列分离，因此历史回填不会阻塞当前对话的
知识沉淀。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import akb
import distiller
from akb import iso_now
from state_store import StateStore, read_json_object


ROOT = Path(__file__).resolve().parent.parent
CODEX_ROOT = Path.home() / ".codex"
SESSION_ROOTS = (
    CODEX_ROOT / "sessions",
    CODEX_ROOT / "archived_sessions",
)
DEFAULT_BATCH_ID = f"codex-history-{dt.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
EXCLUDED_USER_PREFIXES = (
    "<system-reminder",
    "<environment_context",
    "<recommended_plugins>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<plugins_instructions>",
    "# AGENTS.md instructions",
    "# Files mentioned by the user:",
    "<image ",
    "</image>",
    "<attached_files>",
    "<user_context>",
)
SESSION_ID_PATTERN = re.compile(
    r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def batch_store() -> StateStore:
    override = os.environ.get("AKB_STATE_DB")
    return StateStore(Path(override) if override else batch_state_dir().parent / "state.sqlite3")


def batch_namespace() -> str:
    return "history:" + str(batch_state_dir().resolve())


def load_manifest() -> dict[str, Any]:
    with batch_store().read() as transaction:
        if transaction is not None:
            value = transaction.get_document(batch_namespace(), "manifest")
            if value is not None:
                return validate_manifest(value)
            if transaction.migration(batch_namespace(), "history-json-v1") is not None:
                return {}
    path = batch_state_dir() / "manifest.json"
    return validate_manifest(read_json_object(path)) if path.exists() else {}


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    vault = value.get("vault")
    if vault and vault != str(akb.vault_path().resolve()):
        raise ValueError("历史批次所属 Vault 与当前 Vault 不一致，已停止操作")
    return value


def migrate_manifest() -> dict[str, Any]:
    with batch_store().transaction() as transaction:
        namespace = batch_namespace()
        previous = transaction.migration(namespace, "history-json-v1")
        if previous is not None:
            return {**previous, "already_imported": True}
        path = batch_state_dir() / "manifest.json"
        if path.exists():
            manifest = validate_manifest(read_json_object(path))
            if not manifest.get("vault"):
                manifest["legacy_scope_unverified"] = True
            transaction.put_document(namespace, "manifest", manifest)
        result = {"manifest": path.exists()}
        transaction.mark_migration(namespace, "history-json-v1", result)
        return {**result, "already_imported": False}


def save_manifest(value: dict[str, Any]) -> None:
    validate_manifest(value)
    if value.get("legacy_scope_unverified"):
        raise ValueError("旧历史批次没有 Vault 归属，保留为只读，不能自动继续回填")
    value = {**value, "vault": str(akb.vault_path().resolve())}
    with batch_store().transaction() as transaction:
        transaction.put_document(batch_namespace(), "manifest", value)


def load_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    path = CODEX_ROOT / "session_index.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return index
    for raw in lines:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id"):
            index[str(value["id"])] = value
    return index


def session_id(path: Path) -> str:
    match = SESSION_ID_PATTERN.search(path.name)
    return match.group(1) if match else akb.short_hash(str(path), length=16)


def first_payload(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            value = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError):
        return {}
    payload = value.get("payload", {}) if isinstance(value, dict) else {}
    return payload if isinstance(payload, dict) else {}


def session_metadata(path: Path, index: dict[str, dict[str, Any]]) -> dict[str, str]:
    sid = session_id(path)
    indexed = index.get(sid, {})
    payload = first_payload(path)
    cwd = str(indexed.get("cwd") or payload.get("cwd") or "")
    thread_name = str(indexed.get("thread_name") or payload.get("thread_name") or "")
    source = str(indexed.get("source") or payload.get("source") or "codex")
    return {
        "session_id": sid,
        "cwd": cwd,
        "project": Path(cwd.rstrip("/")).name if cwd else "unknown-project",
        "thread_name": thread_name or f"Codex 会话 {sid[:12]}",
        "source": source,
    }


def block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return "\n".join(block_text(item) for item in block)
    if isinstance(block, dict):
        if block.get("type") in {"input_image", "image"}:
            return ""
        for key in ("text", "value", "content", "output", "message"):
            if key in block:
                return block_text(block[key])
    return str(block) if block is not None else ""


def clean_user_content(content: Any) -> str:
    blocks = content if isinstance(content, list) else [content]
    selected: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in {"input_image", "image"}:
            continue
        text = block_text(block).strip()
        if not text:
            continue
        if any(text.lstrip().startswith(prefix) for prefix in EXCLUDED_USER_PREFIXES):
            continue
        selected.append(text)
    value = "\n".join(selected)
    for tag in (
        "system-reminder",
        "environment_context",
        "recommended_plugins",
        "skills_instructions",
        "apps_instructions",
        "plugins_instructions",
    ):
        value = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>",
            "",
            value,
            flags=re.DOTALL | re.IGNORECASE,
        )
    return akb.redact_text(value).strip()


def clean_assistant_content(content: Any) -> str:
    return akb.redact_text(block_text(content)).strip()


def read_messages(path: Path) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    last_key = ""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return messages
    with handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload = value.get("payload", {}) if isinstance(value, dict) else {}
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = (
                clean_user_content(payload.get("content"))
                if role == "user"
                else clean_assistant_content(payload.get("content"))
            )
            if not content:
                continue
            key = f"{role}:{content}"
            if key == last_key:
                continue
            messages.append((role, content))
            last_key = key
    return messages


def chunk_messages(messages: list[tuple[str, str]], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for role, text in messages:
        prefix = f"### {role}\n"
        remaining = prefix + text + "\n"
        while remaining:
            available = limit - len(current)
            if available <= len(prefix) + 1 and current:
                chunks.append(current.rstrip())
                current = ""
                available = limit
            if len(remaining) <= available:
                current += remaining
                break
            if current:
                chunks.append(current.rstrip())
                current = ""
                continue
            take = max(1, limit - len(prefix))
            current = prefix + remaining[len(prefix) : len(prefix) + take].rstrip()
            remaining = prefix + remaining[len(prefix) + take :]
            if current:
                chunks.append(current.rstrip())
                current = ""
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def candidate_files(include_archived: bool) -> list[Path]:
    roots = SESSION_ROOTS if include_archived else SESSION_ROOTS[:1]
    files: list[Path] = []
    seen_ids: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            sid = session_id(path)
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            files.append(path)
    return files


def batch_state_dir() -> Path:
    base = Path(distiller.load_settings()["state_dir"])
    return base if base.name == "history-batch" else base / "history-batch"


def state_paths() -> tuple[Path, Path, Path]:
    state = batch_state_dir()
    return state, state / "manifest.json", state / "batch.lock"


def make_manifest(batch_id: str, include_archived: bool, project_filter: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "vault": str(akb.vault_path().resolve()),
        "created_at": iso_now(),
        "scope": {
            "include_archived": include_archived,
            "project_filter": project_filter,
            "roots": [str(path) for path in SESSION_ROOTS if include_archived or path == SESSION_ROOTS[0]],
        },
        "phase": "created",
        "files": {},
        "stats": {
            "sessions_seen": 0,
            "sessions_skipped": 0,
            "sessions_failed": 0,
            "chunks_queued": 0,
            "chunks_existing": 0,
            "messages": 0,
            "message_chars": 0,
        },
    }


def target_note_path(filename: str) -> Path:
    return akb.vault_path() / akb.session_directory() / filename


def enqueue_chunk(
    *,
    batch_id: str,
    path: Path,
    meta: dict[str, str],
    project_filter: str,
    chunk: str,
    chunk_index: int,
    chunk_total: int,
) -> tuple[Path, bool]:
    title = f"{meta['project']}｜{meta['thread_name']}｜第{chunk_index}/{chunk_total}段"
    filename = (
        f"history-{akb.short_hash(f'{batch_id}:{path}', length=16)}"
        f"-{chunk_index:04d}-{akb.slugify(meta['project'], 'project')}.md"
    )
    target = target_note_path(filename)
    existing = False
    if target.exists():
        metadata, _ = akb.read_note(target)
        if metadata.get("history_batch_id") == batch_id:
            existing = True
            if metadata.get("status") in {"distilled", "distilled-no-value"}:
                return target, False
    if not existing:
        target = akb.create_session_intake(
            source="codex-history",
            title=title,
            content=chunk,
            project=meta["project"],
            session_id=f"{meta['session_id']}-part-{chunk_index:04d}",
            filename=filename,
            extra_metadata={
                "history_batch_id": batch_id,
                "history_source_path": str(path),
                "history_project_path": meta["cwd"],
                "history_thread_name": meta["thread_name"],
                "history_chunk": chunk_index,
                "history_chunks": chunk_total,
            },
        )
    distiller.enqueue(target)
    return target, True


def run_batch(include_archived: bool, project_filter: str, batch_id: str) -> int:
    state_dir, manifest_path, lock_path = state_paths()
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        print("history batch is already running", file=sys.stderr)
        return 2

    old_autostart = os.environ.get("AKB_DISTILLER_AUTOSTART")
    old_state_dir = os.environ.get("AKB_DISTILLER_STATE_DIR")
    os.environ["AKB_DISTILLER_AUTOSTART"] = "0"
    os.environ["AKB_DISTILLER_STATE_DIR"] = str(state_dir)
    try:
        migrate_manifest()
        manifest = load_manifest()
        if manifest.get("legacy_scope_unverified"):
            raise ValueError("旧历史批次没有 Vault 归属，不能自动续跑")
        if not manifest or manifest.get("batch_id") != batch_id:
            manifest = make_manifest(batch_id, include_archived, project_filter)
        manifest["phase"] = "reading"
        manifest["started_at"] = manifest.get("started_at") or iso_now()
        save_manifest(manifest)
        index = load_index()
        files = candidate_files(include_archived)
        for path in files:
            meta = session_metadata(path, index)
            if project_filter and project_filter.lower() not in (
                meta["project"] + " " + meta["cwd"] + " " + meta["thread_name"]
            ).lower():
                continue
            signature = f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
            key = str(path)
            previous = manifest["files"].get(key, {})
            if previous.get("signature") == signature and previous.get("status") in {"queued", "done"}:
                manifest["stats"]["sessions_skipped"] += 1
                continue
            try:
                messages = read_messages(path)
                manifest["stats"]["sessions_seen"] += 1
                manifest["stats"]["messages"] += len(messages)
                manifest["stats"]["message_chars"] += sum(len(text) for _, text in messages)
                if not messages:
                    manifest["files"][key] = {
                        "signature": signature,
                        "status": "empty",
                        "session_id": meta["session_id"],
                    }
                    save_manifest(manifest)
                    continue
                settings = distiller.load_settings()
                chunk_limit = max(
                    6000,
                    min(16000, int(settings["max_input_chars"]) - 5000),
                )
                chunks = chunk_messages(messages, chunk_limit)
                queued = 0
                existing = 0
                for index_number, chunk in enumerate(chunks, start=1):
                    _, created = enqueue_chunk(
                        batch_id=batch_id,
                        path=path,
                        meta=meta,
                        project_filter=project_filter,
                        chunk=chunk,
                        chunk_index=index_number,
                        chunk_total=len(chunks),
                    )
                    if created:
                        queued += 1
                    else:
                        existing += 1
                manifest["stats"]["chunks_queued"] += queued
                manifest["stats"]["chunks_existing"] += existing
                manifest["files"][key] = {
                    "signature": signature,
                    "status": "queued",
                    "session_id": meta["session_id"],
                    "project": meta["project"],
                    "thread_name": meta["thread_name"],
                    "chunks": len(chunks),
                    "queued": queued,
                    "existing": existing,
                }
            except Exception as exc:
                manifest["stats"]["sessions_failed"] += 1
                manifest["files"][key] = {
                    "signature": signature,
                    "status": "failed",
                    "session_id": meta["session_id"],
                    "error": akb.redact_text(str(exc))[:300],
                }
            save_manifest(manifest)

        manifest["phase"] = "queued"
        manifest["queued_at"] = iso_now()
        save_manifest(manifest)
        settings = distiller.load_settings()
        distiller.spawn_worker(settings)
        print(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "phase": manifest["phase"],
                    "sessions": manifest["stats"]["sessions_seen"],
                    "chunks_queued": manifest["stats"]["chunks_queued"],
                    "state_dir": str(state_dir),
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        if old_autostart is None:
            os.environ.pop("AKB_DISTILLER_AUTOSTART", None)
        else:
            os.environ["AKB_DISTILLER_AUTOSTART"] = old_autostart
        if old_state_dir is None:
            os.environ.pop("AKB_DISTILLER_STATE_DIR", None)
        else:
            os.environ["AKB_DISTILLER_STATE_DIR"] = old_state_dir
        try:
            lock_path.unlink()
        except OSError:
            pass


def start_batch(include_archived: bool, project_filter: str) -> int:
    state_dir, manifest_path, lock_path = state_paths()
    state_dir.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        print(json.dumps({"started": False, "reason": "already_running"}, ensure_ascii=False))
        return 0
    migrate_manifest()
    batch_id = DEFAULT_BATCH_ID
    manifest = load_manifest()
    if manifest.get("legacy_scope_unverified"):
        raise ValueError("旧历史批次没有 Vault 归属，不能自动续跑")
    if manifest.get("phase") in {"reading", "queued"} and manifest.get("batch_id"):
        batch_id = str(manifest["batch_id"])
    else:
        manifest = make_manifest(batch_id, include_archived, project_filter)
        save_manifest(manifest)
    log_path = state_dir / "history-batch.log"
    log_handle = log_path.open("ab")
    child_env = os.environ.copy()
    child_env["AKB_DISTILLER_STATE_DIR"] = str(state_dir)
    child_env["AKB_DISTILLER_AUTOSTART"] = "0"
    child = None
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--batch-id",
                batch_id,
                "--include-archived" if include_archived else "--active-only",
                "--project",
                project_filter,
            ],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    print(
        json.dumps(
            {
                "started": True,
                "pid": child.pid if child else None,
                "batch_id": batch_id,
                "state_dir": str(state_dir),
                "log": str(log_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def batch_status() -> int:
    state_dir, manifest_path, lock_path = state_paths()
    manifest = load_manifest()
    namespace = "queue:" + str(state_dir.resolve())
    with batch_store().read() as transaction:
        if transaction is not None and transaction.migration(namespace, "json-jobs:" + str(state_dir.resolve())) is not None:
            status = {kind: len(transaction.list_jobs(namespace, kind)) for kind in ("queue", "failed")}
        else:
            status = {kind: len(list((state_dir / kind).glob("*.json"))) for kind in ("queue", "failed")}
    result = {
        "running": lock_path.exists(),
        "batch_id": manifest.get("batch_id"),
        "phase": manifest.get("phase"),
        "legacy_scope_unverified": bool(manifest.get("legacy_scope_unverified")),
        "state_dir": str(state_dir),
        "queue": status["queue"],
        "failed": status["failed"],
        "backend": "sqlite", "database": str(batch_store().db_path),
        "stats": manifest.get("stats", {}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex 历史会话批量提炼")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--include-archived", action="store_true", default=True)
    start.add_argument("--project", default="")
    start.set_defaults(func=lambda args: start_batch(args.include_archived, args.project))

    run = subparsers.add_parser("run")
    run.add_argument("--batch-id", required=True)
    run.add_argument("--include-archived", action="store_true")
    run.add_argument("--active-only", action="store_true")
    run.add_argument("--project", default="")
    run.set_defaults(
        func=lambda args: run_batch(
            include_archived=not args.active_only,
            project_filter=args.project,
            batch_id=args.batch_id,
        )
    )

    status = subparsers.add_parser("status")
    status.set_defaults(func=lambda args: batch_status())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"history batch error: {akb.redact_text(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
