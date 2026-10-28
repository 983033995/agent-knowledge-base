from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

import akb
from state_store import StateStore, read_json_object


MEMORY_TYPES = {"user", "feedback", "project", "reference", "experience"}
INACTIVE_STATUSES = {"superseded", "revoked", "conflict", "ignored"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_directory() -> Path:
    configured = os.environ.get("AKB_SYNC_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return akb.ROOT / ".distiller" / "memory-sync" / digest(str(akb.vault_path().resolve()))[:16]


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "vault": str(akb.vault_path().resolve()), "records": {}, "notes": {}}


def sync_store() -> StateStore:
    override = os.environ.get("AKB_STATE_DB")
    if override:
        path = Path(override)
    elif os.environ.get("AKB_SYNC_STATE_DIR"):
        path = state_directory() / "state.sqlite3"
    else:
        from distiller import load_settings

        directory = Path(load_settings()["state_dir"])
        path = (directory.parent if directory.name == "history-batch" else directory) / "state.sqlite3"
    return StateStore(path)


def sync_namespace() -> str:
    return "sync:" + str(state_directory().resolve())


def _validate_state(value: dict[str, Any]) -> dict[str, Any]:
    if (
        value.get("version") != 1
        or value.get("vault") != str(akb.vault_path().resolve())
        or not isinstance(value.get("records"), dict)
        or not isinstance(value.get("notes"), dict)
        or not isinstance(value.get("files", {}), dict)
    ):
        raise ValueError("统一记忆清单版本或 Vault 不匹配，已停止同步")
    return value


def _legacy_state() -> dict[str, Any] | None:
    path = state_directory() / "manifest.json"
    if not path.exists():
        return None
    try:
        return _validate_state(read_json_object(path))
    except ValueError as exc:
        raise ValueError("统一记忆清单无法读取，已停止同步，未覆盖现有状态") from exc


def _load_state() -> dict[str, Any]:
    with sync_store().read() as transaction:
        if transaction is not None:
            value = transaction.get_sync_state(sync_namespace())
            if value is not None:
                return _validate_state(value)
            if transaction.migration(sync_namespace(), "sync-json-v1") is not None:
                return _empty_state()
    return _legacy_state() or _empty_state()


def _load_pending() -> dict[str, Any] | None:
    with sync_store().read() as transaction:
        if transaction is not None:
            pending = transaction.get_document(sync_namespace(), "pending")
            if pending is not None:
                return pending
            if transaction.migration(sync_namespace(), "sync-json-v1") is not None:
                return None
    path = state_directory() / "pending.json"
    return read_json_object(path) if path.exists() else None


def migrate_state() -> dict[str, Any]:
    with sync_store().transaction() as transaction:
        namespace = sync_namespace()
        previous = transaction.migration(namespace, "sync-json-v1")
        if previous is not None:
            return {**previous, "already_imported": True}
        legacy = _legacy_state()
        pending_path = state_directory() / "pending.json"
        pending = read_json_object(pending_path) if pending_path.exists() else None
        if pending is not None:
            _validate_state(pending.get("state", {}))
            if not isinstance(pending.get("contents"), dict) or not isinstance(pending.get("refs"), dict):
                raise ValueError("待恢复同步事务无效，已停止迁移")
        if legacy is not None:
            transaction.put_sync_state(namespace, legacy)
        if pending is not None:
            transaction.put_document(namespace, "pending", pending)
        result = {"manifest": legacy is not None, "pending": pending is not None}
        transaction.mark_migration(namespace, "sync-json-v1", result)
        return {**result, "already_imported": False}


@contextmanager
def _sync_lock() -> Iterator[None]:
    import fcntl

    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "sync.lock").open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("统一记忆同步正在运行，请等待当前同步完成") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _project_key(record: dict[str, Any]) -> str:
    path = str(record.get("project_path") or "").strip()
    if path:
        return str(Path(path).expanduser().resolve())
    return str(record.get("project") or "").strip()


def _identity(record: dict[str, Any]) -> str:
    values = [record["source"], record["kind"], record["source_path"], record["record_id"]]
    return digest(json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def _normalize(record: dict[str, Any]) -> dict[str, Any]:
    required = ("source", "kind", "source_path", "record_id", "content")
    if any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
        raise ValueError("来源适配器返回了缺失身份或正文的记录")
    kind = record["kind"]
    if kind not in {"memory", "session"}:
        raise ValueError("未知记忆来源类型")
    memory_type = str(record.get("memory_type") or ("session" if kind == "session" else "experience"))
    if memory_type == "index":
        return {**record, "memory_type": "index"}
    if kind == "memory" and memory_type not in MEMORY_TYPES:
        memory_type = "experience"
    content = akb.redact_text(record["content"]).strip()
    result = {
        "source": record["source"],
        "kind": kind,
        "source_path": str(Path(record["source_path"]).expanduser().resolve()),
        "record_id": record["record_id"],
        "title": akb.redact_text(str(record.get("title") or Path(record["source_path"]).stem)),
        "content": content,
        "project": akb.redact_text(str(record.get("project") or "").strip()),
        "project_path": str(record.get("project_path") or "").strip(),
        "memory_type": memory_type,
    }
    native_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    result["quarantine_reason"] = akb.redact_text(str(native_metadata.get("quarantine_reason") or ""))
    result["source_status"] = "revoked" if native_metadata.get("status") in {"revoked", "archived", "forgotten"} else (
        "conflict" if result["quarantine_reason"] else "active"
    )
    result["native_metadata"] = akb.redact_value({key: native_metadata[key] for key in (
        "agent_scope", "workspace_scope", "sensitivity", "epistemic_status", "source", "evidence", "native_type",
    ) if key in native_metadata})
    result["project_key"] = _project_key(result)
    result["content_hash"] = digest(content)
    result["identity"] = _identity(result)
    result["note_key"] = digest(json.dumps(
        [kind, memory_type, result["project_key"], result["content_hash"], result["quarantine_reason"]], ensure_ascii=False,
    ))
    return result


def _note_path(record: dict[str, Any]) -> str:
    directory = akb.session_directory() if record["kind"] == "session" else akb.knowledge_directory()
    return str(Path(directory) / "_unified-memory" / f"{record['note_key']}.md")


def _safe_managed_path(relative: str) -> Path:
    target = akb._safe_note_target("", relative)
    allowed = [Path(akb.knowledge_directory()) / "_unified-memory", Path(akb.session_directory()) / "_unified-memory"]
    rel = target.relative_to(akb.vault_path().resolve())
    if not any(rel.is_relative_to(parent) for parent in allowed):
        raise ValueError("统一记忆清单引用了受管目录之外的文件")
    return target


def _metadata_hash(metadata: dict[str, Any]) -> str:
    return digest(json.dumps({key: value for key, value in metadata.items() if key != "managed_metadata_hash"}, ensure_ascii=False, sort_keys=True))


def _validate_managed(path: Path) -> tuple[dict[str, Any], str]:
    metadata, body = akb.read_note(path)
    if (
        metadata.get("managed_by") != "akb-sync"
        or digest(body.strip()) != metadata.get("content_hash")
        or _metadata_hash(metadata) != metadata.get("managed_metadata_hash")
    ):
        raise ValueError("受管记忆存在非受管文件或人工修改，已停止同步")
    return metadata, body


def _metadata(note: dict[str, Any], refs: list[dict[str, Any]], old: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "imported-memory" if note["kind"] == "memory" else "session-evidence",
        "title": note["title"],
        "memory_type": note["memory_type"],
        "scope": "project" if note["project_key"] else "user",
        "project": note["project"],
        "project_path": note["project_path"],
        "project_key": note["project_key"],
        "status": note.get("status", "active"),
        "managed_by": "akb-sync",
        "content_hash": note["content_hash"],
        "source": "unified-memory",
        "sources": sorted({ref["source"] for ref in refs if ref["status"] == "active"}),
        "source_refs": [{key: ref[key] for key in ("source", "source_path", "record_id", "status", "native_metadata") if key in ref} for ref in refs],
        "source_count": sum(ref["status"] == "active" for ref in refs),
        "created": old.get("created") or akb.iso_now(),
        "updated": akb.iso_now(),
        "tags": ["agent-memory", "imported", f"type/{note['memory_type']}"],
        "conflict_candidates": note.get("conflict_candidates", []),
        "quarantine_reason": note.get("quarantine_reason", ""),
    }


def _write_managed(note: dict[str, Any], refs: list[dict[str, Any]], content: str | None) -> None:
    path = _safe_managed_path(note["path"])
    old_metadata: dict[str, Any] = {}
    old_body = ""
    if path.exists():
        old_metadata, old_body = _validate_managed(path)
    body = content if content is not None else old_body.strip()
    if not body:
        raise ValueError("受管记忆正文缺失，不能从索引重建正文")
    metadata = _metadata(note, refs, old_metadata)
    if old_metadata and all(old_metadata.get(key) == value for key, value in metadata.items() if key != "updated"):
        return
    metadata["managed_metadata_hash"] = _metadata_hash(metadata)
    with akb.note_lock(path):
        if path.exists():
            _validate_managed(path)
        akb.atomic_write_text(path, akb.render_frontmatter(metadata) + body.strip() + "\n")


def _sync(
    *, source: str, kind: str, home: Path | None, apply: bool, limit: int,
) -> dict[str, Any]:
    from memory_sources import SourceReadError, discover_sources, read_source

    if source not in {"all", "qoder", "codex", "workbuddy", "deepseek", "cursor"}:
        raise ValueError("不支持的 Agent 来源")
    if kind not in {"all", "memory", "session"}:
        raise ValueError("来源类型必须为 memory、session 或 all")
    if limit < 0:
        raise ValueError("limit 不可为负数")
    original = _load_state()
    state = copy.deepcopy(original)
    source_home = str((home or Path.home()).expanduser().resolve())
    if state.get("source_home") and state["source_home"] != source_home:
        raise ValueError("来源 Home 与既有同步清单不匹配，已停止同步")
    state["source_home"] = source_home
    state.setdefault("files", {})
    discovered = discover_sources(home=home, source=source, kind=kind)
    items = discovered[:limit] if limit else discovered
    partial = len(items) < len(discovered)
    successful_paths: set[tuple[str, str, str]] = set()
    failed_paths: set[tuple[str, str, str]] = set()
    seen: set[str] = set()
    reconciled_ids: set[str] = set()
    contents: dict[str, str] = {}
    stats: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for item in items:
        path_key = (item["source"], item["kind"], str(Path(item["path"]).expanduser().resolve()))
        file_key = digest(json.dumps([*path_key, item.get("composer_id", "")], ensure_ascii=False))
        try:
            signature = _file_signature(Path(path_key[2]))
            previous_file = state["files"].get(file_key)
            if previous_file and previous_file.get("signature") == signature:
                cached_ids = previous_file.get("record_ids", [])
                if all(identity in state["records"] and state["records"][identity]["status"] == state["records"][identity].get("native_status", "active") for identity in cached_ids):
                    seen.update(cached_ids)
                    successful_paths.add(path_key)
                    stats["unchanged"] += len(cached_ids)
                    stats["unchanged_files"] += 1
                    continue
            batch = [_normalize(record) for record in read_source(item)]
            if _file_signature(Path(path_key[2])) != signature:
                raise ValueError("来源读取期间发生变化，本轮保留旧版本，下次重试")
            identities = [record.get("identity") for record in batch if record.get("memory_type") != "index"]
            if len(set(identities)) != len(identities):
                raise ValueError("来源适配器返回重复记录身份")
        except (SourceReadError, OSError, ValueError) as exc:
            failed_paths.add(path_key)
            stats["failed_files"] += 1
            errors.append({"source": item["source"], "path": akb.redact_text(item["path"]), "error": akb.redact_text(str(exc))[:300]})
            continue
        successful_paths.add(path_key)
        if previous_file:
            reconciled_ids.update(previous_file.get("record_ids", []))
        state["files"][file_key] = {
            "signature": signature, "record_ids": identities,
            "source": item["source"], "kind": item["kind"], "source_path": path_key[2],
            "composer_id": item.get("composer_id", ""), "format": item["format"],
        }
        stats["read_files"] += 1
        for record in batch:
            if record["memory_type"] == "index":
                stats["skipped_indexes"] += 1
                continue
            identity = record["identity"]
            seen.add(identity)
            previous = state["records"].get(identity)
            if previous and previous["note_key"] == record["note_key"] and previous["status"] == record["source_status"]:
                stats["unchanged"] += 1
            else:
                stats["updated" if previous else "imported"] += 1
            if previous and previous["note_key"] != record["note_key"]:
                state["notes"][previous["note_key"]]["status"] = "superseded"
            state["records"][identity] = {key: record[key] for key in (
                "source", "kind", "source_path", "record_id", "content_hash", "note_key", "project_key", "native_metadata",
            )}
            state["records"][identity]["status"] = record["source_status"]
            state["records"][identity]["native_status"] = record["source_status"]
            if record["note_key"] not in state["notes"]:
                state["notes"][record["note_key"]] = {key: record[key] for key in (
                    "title", "kind", "memory_type", "project", "project_path", "project_key", "content_hash", "quarantine_reason",
                )}
                state["notes"][record["note_key"]]["path"] = _note_path(record)
                stats["new_notes"] += 1
            elif not previous or previous["note_key"] != record["note_key"]:
                stats["deduplicated"] += 1
            state["notes"][record["note_key"]]["status"] = record["source_status"]
            contents[record["note_key"]] = record["content"]
    discovered_paths = {(item["source"], item["kind"], str(Path(item["path"]).expanduser().resolve())) for item in discovered}
    if not partial:
        for file_state in state["files"].values():
            if file_state.get("format") != "cursor-sqlite" or not file_state.get("composer_id"):
                continue
            path_key = (file_state["source"], file_state["kind"], file_state["source_path"])
            if source not in {"all", path_key[0]} or kind not in {"all", path_key[1]}:
                continue
            if _cursor_composer_missing(Path(path_key[2]), file_state["composer_id"]):
                reconciled_ids.update(file_state["record_ids"])
    for identity, record in state["records"].items():
        if identity in seen or record["status"] != "active":
            continue
        if source != "all" and record["source"] != source:
            continue
        if kind != "all" and record["kind"] != kind:
            continue
        path_key = (record["source"], record["kind"], record["source_path"])
        if path_key in failed_paths:
            continue
        # Partial scans and temporarily unavailable runtime roots cannot revoke evidence.
        root_available = _source_root_available(record["source"], home)
        missing_file = False
        if not partial and path_key not in discovered_paths and root_available:
            try:
                Path(record["source_path"]).stat()
            except FileNotFoundError:
                missing_file = True
            except OSError:
                pass
        if identity in reconciled_ids or missing_file:
            record["status"] = "revoked"
            stats["revoked_sources"] += 1
    active_refs: dict[str, list[dict[str, Any]]] = {}
    all_refs: dict[str, list[dict[str, Any]]] = {}
    for record in state["records"].values():
        all_refs.setdefault(record["note_key"], []).append(record)
        if record["status"] == "active":
            active_refs.setdefault(record["note_key"], []).append(record)
    for key, note in state["notes"].items():
        if key in active_refs:
            note["status"] = "active"
        elif note.get("status") not in {"superseded", "conflict"}:
            note["status"] = "revoked"
    groups: dict[tuple[str, str, str], list[str]] = {}
    for key, note in state["notes"].items():
        note["conflict_candidates"] = []
        if note["status"] == "active" and note["kind"] == "memory":
            groups.setdefault((note["project_key"], note["memory_type"], note["title"].strip().casefold()), []).append(key)
    for keys in groups.values():
        if len(keys) > 1:
            for key in keys:
                state["notes"][key]["conflict_candidates"] = [state["notes"][other]["path"] for other in keys if other != key]
    stats["conflict_candidates"] = sum(len(keys) for keys in groups.values() if len(keys) > 1)
    stats["active_notes"] = sum(note["status"] == "active" for note in state["notes"].values())
    stats["source_files"] = len(discovered)
    stats["selected_files"] = len(items)
    if apply:
        # Preflight every affected note before writing any of them.
        for key, note in state["notes"].items():
            path = _safe_managed_path(note["path"])
            if path.exists():
                _validate_managed(path)
            elif key not in contents:
                raise ValueError("受管记忆文件缺失，请恢复文件后再同步")
        state["last_sync"] = {"at": akb.iso_now(), "source": source, "kind": kind, "partial": partial, "stats": dict(stats)}
        transaction = {"state": state, "contents": contents, "refs": all_refs}
        with sync_store().transaction() as database:
            database.put_document(sync_namespace(), "pending", transaction)
        _publish(transaction)
    return {
        "applied": apply, "dry_run": not apply, "source": source, "kind": kind, "partial": partial,
        "stats": dict(stats), "errors": errors, "cloud_requests": 0,
    }


def _publish(transaction: dict[str, Any]) -> None:
    state = transaction["state"]
    if state.get("vault") != str(akb.vault_path().resolve()):
        raise ValueError("待恢复同步事务的 Vault 不匹配")
    for key, note in state["notes"].items():
        _write_managed(note, transaction["refs"].get(key, []), transaction["contents"].get(key))
    with sync_store().transaction() as transaction:
        transaction.put_sync_state(sync_namespace(), state)
        transaction.delete_document(sync_namespace(), "pending")


def _recover_pending() -> None:
    pending = _load_pending()
    if pending is not None:
        _publish(pending)


def _cursor_composer_missing(path: Path, composer_id: str) -> bool:
    import sqlite3

    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT 1 FROM cursorDiskKV WHERE key = ?", ("composerData:" + composer_id,)).fetchone() is None
    except (OSError, sqlite3.Error):
        return False


def _file_signature(path: Path) -> list[int]:
    value = path.stat()
    signature = [value.st_mtime_ns, value.st_size]
    if path.suffix in {".db", ".sqlite", ".vscdb"}:
        wal = Path(str(path) + "-wal")
        if wal.exists():
            value = wal.stat()
            signature.extend([value.st_mtime_ns, value.st_size])
    return signature


def _source_root_available(source: str, home: Path | None) -> bool:
    root = home or Path.home()
    directory = {"qoder": ".qoder", "codex": ".codex", "workbuddy": ".workbuddy", "deepseek": ".dsh", "cursor": ".cursor"}[source]
    return (root / directory).is_dir()


def sync_sources(
    *, source: str = "all", kind: str = "memory", home: Path | None = None,
    apply: bool = False, limit: int = 0,
) -> dict[str, Any]:
    if source not in {"all", "qoder", "codex", "workbuddy", "deepseek", "cursor"} or kind not in {"all", "memory", "session"} or limit < 0:
        raise ValueError("同步来源、类型或数量无效")
    if apply:
        with _sync_lock():
            migrate_state()
            _recover_pending()
            return _sync(source=source, kind=kind, home=home, apply=True, limit=limit)
    if _load_pending() is not None:
        raise ValueError("存在中断的同步事务，请先使用 --apply 恢复后再预览")
    return _sync(source=source, kind=kind, home=home, apply=False, limit=limit)


def published_notes() -> set[str]:
    if _load_pending() is not None:
        return set()
    state = _load_state()
    return {note["path"] for note in state["notes"].values() if note["status"] == "active"}


def published_note(relative: str) -> bool:
    return relative in published_notes()


def sync_status() -> dict[str, Any]:
    state = _load_state()
    return {
        "vault": state["vault"], "last_sync": state.get("last_sync"),
        "backend": "sqlite", "database": str(sync_store().db_path),
        "records": len(state["records"]), "notes": len(state["notes"]),
        "by_status": dict(Counter(note["status"] for note in state["notes"].values())),
        "by_source": dict(Counter(record["source"] for record in state["records"].values() if record["status"] == "active")),
    }
