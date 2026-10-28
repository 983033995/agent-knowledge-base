from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import akb
import memory_sources
import distiller


SOURCES = {"codex", "qoder", "cursor", "workbuddy", "deepseek"}


def recall(cwd: str, query: str = "") -> str:
    if not cwd or not Path(cwd).is_absolute():
        return ""
    project_path = str(Path(cwd).expanduser().resolve())
    rows = akb.search_notes(
        query=query, project=project_path, limit=3,
        directories=[akb.knowledge_directory(), akb.patterns_directory()],
    )
    if not rows:
        return ""
    context = akb.build_context(
        query=query, project=project_path, limit=3, max_chars=2800,
        directories=[akb.knowledge_directory(), akb.patterns_directory()],
    )
    return "<akb-recalled-context>\n" + context + "\n</akb-recalled-context>"


def capture(source: str, event: dict[str, Any]) -> dict[str, Any]:
    if source not in SOURCES:
        raise ValueError("未知生命周期来源")
    raw_path = event.get("transcript_path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        return {"captured": False, "reason": "missing_transcript"}
    original_path = Path(raw_path).expanduser()
    if original_path.is_symlink():
        return {"captured": False, "reason": "unsafe_transcript"}
    path = original_path.resolve()
    if not path.is_file():
        return {"captured": False, "reason": "transcript_unavailable"}
    cwd = str(event.get("cwd") or "")
    if not cwd or not Path(cwd).is_absolute():
        return {"captured": False, "reason": "missing_workspace"}
    workspace = str(Path(cwd).expanduser().resolve())
    stat_before = path.stat()
    descriptor = {
        "source": source, "kind": "session", "path": str(path),
        "format": memory_sources._format(path), "project_path": workspace,
    }
    rows = memory_sources.read_source(descriptor)
    if any(row.get("project_path") and str(Path(row["project_path"]).resolve()) != workspace for row in rows):
        return {"captured": False, "reason": "workspace_mismatch"}
    stat_after = path.stat()
    if (stat_before.st_mtime_ns, stat_before.st_size) != (stat_after.st_mtime_ns, stat_after.st_size):
        return {"captured": False, "reason": "transcript_changed"}
    session_id = str(event.get("session_id") or event.get("conversation_id") or hashlib.sha256(str(path).encode()).hexdigest())
    identity = hashlib.sha256(json.dumps([source, session_id, workspace], ensure_ascii=False).encode()).hexdigest()
    store = distiller.queue_store(distiller.load_settings())
    namespace = 'lifecycle:' + str(akb.vault_path().resolve())
    with akb.note_lock(store.db_path.parent / ('cursor-' + identity)):
        with store.transaction() as transaction:
            cursor = transaction.get_document(namespace, identity) or {'messages': {}, 'mtime_ns': 0}
        if cursor.get('mtime_ns', 0) > stat_before.st_mtime_ns:
            return {'captured': False, 'reason': 'older_snapshot'}
        seen = dict(cursor['messages'])
        budget = int(akb.load_config().get('session_excerpt_chars', 18000))
        if not cursor.get('initialized'):
            size, cutoff = 0, len(rows)
            for index in range(len(rows) - 1, -1, -1):
                length = len(str(rows[index].get('content') or ''))
                if size and size + length > budget:
                    break
                size += length
                cutoff = index
            for row in rows[:cutoff]:
                text = str(row.get('content') or '').strip()
                seen[str(row['record_id'])] = hashlib.sha256(text.encode()).hexdigest()
            with store.transaction() as transaction:
                transaction.put_document(namespace, identity, {'messages': seen, 'mtime_ns': 0, 'initialized': True})
        fresh = []
        for row in rows:
            text = str(row.get('content') or '').strip()
            key = str(row['record_id'])
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            if text and seen.get(key) != content_hash:
                fresh.append((key, content_hash, text))
        if not fresh:
            return {'captured': False, 'reason': 'no_new_dialogue'}
        budget = int(akb.load_config().get('session_excerpt_chars', 18000))
        batches, batch, size = [], [], 0
        for key, value, text in fresh:
            for offset in range(0, len(text), budget):
                part = text[offset:offset + budget]
                part_key = key if len(text) <= budget else f'{key}:capture-part:{offset // budget}'
                if seen.get(part_key) == value:
                    continue
                if batch and size + len(part) + 2 > budget:
                    batches.append(batch)
                    batch, size = [], 0
                batch.append((part_key, value, part))
                size += len(part) + 2
        if batch:
            batches.append(batch)
        paths = []
        for batch in batches:
            batch_hash = hashlib.sha256(json.dumps([(key, value) for key, value, _ in batch]).encode()).hexdigest()
            note = akb.create_session_intake(
                source=source, title=f'{source} 自动会话采集',
                content='\n\n'.join(text for _, _, text in batch),
                project=Path(workspace).name, session_id=session_id,
                filename=f'lifecycle-{source}-{identity[:20]}-{batch_hash[:20]}.md',
                extra_metadata={
                    'capture_kind': 'lifecycle', 'project_path': workspace, 'project_key': workspace,
                    'scope': 'project', 'transcript_mtime_ns': stat_before.st_mtime_ns,
                    'transcript_path': str(path),
                },
            )
            queued = akb.enqueue_distillation(note)
            if not queued.get('queued') and queued.get('reason') != 'already_distilled':
                return {'captured': True, 'queued': False, 'path': str(note)}
            paths.append(str(note))
            seen.update({key: value for key, value, _ in batch})
            with store.transaction() as transaction:
                transaction.put_document(namespace, identity, {'messages': seen, 'mtime_ns': stat_before.st_mtime_ns, 'initialized': True})
        seen.update({key: value for key, value, _ in fresh})
        with store.transaction() as transaction:
            transaction.put_document(namespace, identity, {'messages': seen, 'mtime_ns': stat_before.st_mtime_ns, 'initialized': True})
        return {'captured': bool(paths), 'queued': bool(paths), 'path': paths[-1] if paths else None, 'paths': paths}
