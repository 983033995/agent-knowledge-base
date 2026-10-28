"""Read-only native memory/session adapters. No client or vault writes.

Session records are message-sized chunks, split further at 24,000 characters.
Native message IDs win; line/turn ordinals are stable append-only fallbacks.
Unknown formats fail explicitly; known non-dialogue events are intentionally skipped.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from urllib.parse import unquote, urlparse

from akb import parse_frontmatter, redact_text

SOURCES = ('qoder', 'codex', 'workbuddy', 'deepseek', 'cursor')
_CHUNK = 24000
_MAX_LINE = 64 * 1024 * 1024
# The public API returns a list, not an iterator. Reject an oversized result
# explicitly rather than allocating gigabytes or silently retaining only a tail.
_MAX_RESULT = 64 * 1024 * 1024
_MEMORY_TYPES = {'user', 'feedback', 'project', 'reference', 'experience', 'index'}


class SourceReadError(ValueError):
    """Unsupported, malformed, inaccessible, or unsafe source data."""


def _excluded(name):
    name = name.lower()
    return (name in {'.git', 'git', 'backup', 'backups', '__pycache__'}
            or name.startswith(('backup-', 'backup_', '.backup'))
            or name.endswith(('.bak', '.backup', '~')))


def _safe(path, home):
    try:
        relative = path.relative_to(home)
        if any(_excluded(part) for part in relative.parts):
            return False
        current = home
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        path.resolve().relative_to(home)
        return path.is_file()
    except (OSError, ValueError):
        return False


def _files(root, home, recursive=True):
    try:
        relative = root.relative_to(home)
    except ValueError:
        return
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _excluded(part):
            return
    if not root.is_dir():
        return
    # Never traverse symlink directories, even when they happen to point inward.
    def walk_error(error):
        raise SourceReadError('Cannot enumerate source directory') from error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not _excluded(d)
                         and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if _safe(path, home):
                yield path
        if not recursive:
            break


def _format(path):
    if path.name.endswith(('.jsonl.zst', '.jsonl.zstd')):
        return 'jsonl.zstd'
    return {'.md': 'markdown', '.jsonl': 'jsonl', '.ndjson': 'ndjson',
            '.db': 'sqlite', '.vscdb': 'cursor-sqlite'}.get(path.suffix.lower())


def _absolute(value):
    if isinstance(value, dict):
        if value.get('scheme') not in (None, 'file'):
            return None
        value = value.get('fsPath') or value.get('path') or value.get('external')
    if not isinstance(value, str) or not value.strip():
        return None
    if value.startswith('file://'):
        parsed = urlparse(value)
        if parsed.netloc not in ('', 'localhost'):
            return None
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    return str(path.resolve()) if path.is_absolute() else None


def _workspace(data):
    for key in ('cwd', 'project_path', 'workspacePath', 'workspaceRoot', 'folderPath'):
        result = _absolute(data.get(key))
        if result:
            return result
    for key in ('directories', 'workspace-directories', 'workspaceUris'):
        values = data.get(key)
        if isinstance(values, list) and len(values) == 1:
            result = _absolute(values[0])
            if result:
                return result
    workspace = data.get('workspaceIdentifier')
    if isinstance(workspace, dict):
        return _absolute(workspace.get('uri'))
    return None


@contextlib.contextmanager
def _database(path):
    # Do not use immutable=1: live Cursor databases can have committed WAL data.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _composers(path):
    with _database(path) as connection:
        for key, raw in connection.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY key"):
            try:
                data = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise SourceReadError('Malformed Cursor composer data') from exc
            if not isinstance(data, dict):
                raise SourceReadError('Cursor composer must be an object')
            yield key.split(':', 1)[1], data


def discover_sources(home: Path | None = None, source: str = 'all',
                     kind: str = 'all', *, metadata_only: bool = False) -> list[dict]:
    """Enumerate only the five documented client roots, never arbitrary home files.

    Cursor native memory storage is not established; rules are never memories.
    Unknown DeepSeek memory databases remain visible as unsupported SQLite input.
    """
    if source not in (*SOURCES, 'all') or kind not in ('memory', 'session', 'all'):
        raise SourceReadError('Invalid source or kind selector')
    home = Path(home or Path.home()).expanduser().resolve()
    found = []

    def add(client, category, path, **extra):
        fmt = _format(path)
        if fmt and _safe(path, home):
            found.append(dict(source=client, kind=category, path=str(path.resolve()),
                              format=fmt, project_path=None, **extra))

    for client in SOURCES:
        if source not in ('all', client):
            continue
        root = home / { 'qoder': '.qoder', 'codex': '.codex',
                       'workbuddy': '.workbuddy', 'deepseek': '.dsh',
                       'cursor': '.cursor'}[client]
        if kind in ('all', 'memory'):
            roots = {'qoder': ['memory'], 'codex': ['memories', 'memories_v2'],
                     'workbuddy': ['memory'], 'deepseek': ['memory'], 'cursor': []}[client]
            for relative in roots:
                for path in _files(root / relative, home):
                    if path.suffix.lower() == '.md' or (client == 'deepseek' and path.suffix == '.db'):
                        add(client, 'memory', path)
            if client == 'workbuddy':
                add(client, 'memory', root / 'MEMORY.md')
            if client == 'qoder':
                for project in sorted((root / 'projects').glob('*')):
                    for path in _files(project / 'memory', home):
                        if path.suffix.lower() == '.md':
                            add(client, 'memory', path)
        if kind not in ('all', 'session'):
            continue
        roots = {'qoder': ['projects'], 'codex': ['sessions', 'archived_sessions'],
                 'workbuddy': ['projects'], 'deepseek': ['sessions'],
                 'cursor': ['projects']}[client]
        exports = set()
        cursor_candidates = {}
        for relative in roots:
            for path in _files(root / relative, home):
                if client == 'qoder' and len(path.relative_to(root / 'projects').parts) != 2:
                    continue
                if client == 'cursor' and ('agent-transcripts' not in path.parts
                        or 'compaction' in path.relative_to(root).parts
                        or (path.suffix.lower() == '.md' and
                            (path.stem.lower() == 'index' or re.fullmatch(r'segment_\d+', path.stem, re.I)))):
                    continue
                if _format(path) not in ('jsonl', 'jsonl.zstd', 'ndjson', 'markdown'):
                    continue
                if client != 'cursor' and path.suffix == '.md':
                    continue
                if client == 'cursor':
                    # Exported session ID is the filename, not the encoded project directory.
                    sid = path.stem
                    exports.add(sid)
                    previous = cursor_candidates.get(sid)
                    if previous is None or path.suffix == '.jsonl':
                        cursor_candidates[sid] = path
                else:
                    add(client, 'session', path)
        if client == 'cursor':
            for path in cursor_candidates.values():
                add(client, 'session', path)
            database = home / 'Library/Application Support/Cursor/User/globalStorage/state.vscdb'
            if _safe(database, home):
                try:
                    if metadata_only:
                        with _database(database) as connection:
                            composers = [(row[0].split(':', 1)[1], {}) for row in connection.execute("SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY key")]
                    else:
                        composers = _composers(database)
                    for sid, data in composers:
                        if sid not in exports:
                            add(client, 'session', database, composer_id=sid)
                            found[-1]['project_path'] = _workspace(data)
                except (sqlite3.Error, OSError) as exc:
                    raise SourceReadError('Cannot enumerate Cursor database') from exc
    return sorted(found, key=lambda item: (item['source'], item['kind'], item['path'],
                                           item.get('composer_id', '')))


def inventory_sources(home: Path | None = None, source: str = 'all', kind: str = 'all') -> dict:
    """Counts include discoverable unsupported formats; no text is read into inventory."""
    items = discover_sources(home, source, kind)
    counts = {}
    for item in items:
        key = (item['source'], item['kind'], item['format'])
        counts[key] = counts.get(key, 0) + 1
    return {'count': len(items), 'counts': [dict(source=s, kind=k, format=f, count=n)
                                          for (s, k, f), n in sorted(counts.items())]}


def _zstd_chunks(path):
    """libzstd streaming fallback with fixed-size input/output buffers."""
    name = ctypes.util.find_library('zstd')
    if not name:
        raise SourceReadError('Zstandard needs compression.zstd, zstd CLI, or libzstd')
    lib = ctypes.CDLL(name)

    class Buffer(ctypes.Structure):
        _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t),
                    ('pos', ctypes.c_size_t)]

    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(Buffer),
                                          ctypes.POINTER(Buffer)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    stream = lib.ZSTD_createDStream()
    if not stream:
        raise SourceReadError('Cannot allocate Zstandard decoder')
    try:
        if lib.ZSTD_isError(lib.ZSTD_initDStream(stream)):
            raise SourceReadError('Cannot initialize Zstandard decoder')
        remaining, seen = 1, False
        with path.open('rb') as handle:
            while True:
                block = handle.read(65536)
                if not block:
                    break
                seen = True
                raw = ctypes.create_string_buffer(block)
                incoming = Buffer(ctypes.cast(raw, ctypes.c_void_p), len(block), 0)
                while incoming.pos < incoming.size:
                    out = ctypes.create_string_buffer(131072)
                    outgoing = Buffer(ctypes.cast(out, ctypes.c_void_p), len(out), 0)
                    remaining = lib.ZSTD_decompressStream(stream, ctypes.byref(outgoing),
                                                         ctypes.byref(incoming))
                    if lib.ZSTD_isError(remaining):
                        raise SourceReadError('Invalid Zstandard stream')
                    if outgoing.pos:
                        yield out.raw[:outgoing.pos]
        if not seen or remaining:
            raise SourceReadError('Empty or truncated Zstandard stream')
    finally:
        lib.ZSTD_freeDStream(stream)


class _ChunkReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks, self.pending = iter(chunks), b''

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.pending:
            self.pending = next(self.chunks, b'')
        size = min(len(buffer), len(self.pending))
        buffer[:size], self.pending = self.pending[:size], self.pending[size:]
        return size

    def close(self):
        self.chunks.close()
        super().close()


@contextlib.contextmanager
def _text_stream(path, compressed=False):
    if not compressed:
        with path.open('r', encoding='utf-8-sig') as handle:
            yield handle
        return
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        with zstd.open(path, 'rt', encoding='utf-8-sig') as handle:
            yield handle
    elif shutil.which('zstd'):
        process = subprocess.Popen([shutil.which('zstd'), '-dc', '--', str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with io.TextIOWrapper(process.stdout, encoding='utf-8-sig') as handle:
                yield handle
            if process.wait() != 0:
                raise SourceReadError('Zstandard command failed')
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
    else:
        with io.TextIOWrapper(io.BufferedReader(_ChunkReader(_zstd_chunks(path))),
                              encoding='utf-8-sig') as handle:
            yield handle


def _lines(handle):
    number = 0
    while True:
        line = handle.readline(_MAX_LINE + 1)
        if not line:
            return
        number += 1
        if len(line) > _MAX_LINE:
            raise SourceReadError('Source line exceeds safe decoding limit')
        yield number, line


def _plain(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(filter(None, (_plain(block) for block in value)))
    if isinstance(value, dict) and value.get('type') in ('text', 'input_text', 'output_text'):
        return value.get('text', '') if isinstance(value.get('text'), str) else ''
    return ''


def _clean(text, role=None):
    # Remove injected blocks, not a genuine user request following the block.
    tags = {'system-reminder', 'environment_context', 'user_instructions', 'user_info',
            'attached_files', 'tool_result', 'think', 'thinking', 'akb-recalled-context'}
    token = re.compile(r'<(/?)([a-z][a-z0-9_-]*)\b[^>]*>', re.I)
    stack, output, offset = [], [], 0
    for match in token.finditer(text):
        name = match[2].lower()
        if name not in tags:
            continue
        if not stack:
            output.append(text[offset:match.start()])
        if not match[1] and not match[0].endswith('/>'):
            stack.append(name)
        elif match[1] and stack and name == stack[-1]:
            stack.pop()
        offset = match.end()
    if not stack:
        output.append(text[offset:])
    text = ''.join(output)
    stripped = text.lstrip()
    if role == 'user' and (re.match(r'<(?:system-reminder|environment_context|user_instructions)\b',
                                   stripped, re.I)
                           or re.match(r'#\s*(?:AGENTS\.md|Instructions for|Contextual Instructions)',
                                       stripped, re.I)
                           or stripped.startswith(('<INSTRUCTIONS>', '[System]'))):
        return ''
    return redact_text(text.strip())


def _record(item, record_id, content, project_path=None, title=None, memory_type='session', **metadata):
    project_path = _absolute(project_path or item.get('project_path'))
    return dict(source=item['source'], kind=item['kind'], source_path=str(Path(item['path']).resolve()),
                record_id=str(record_id), title=_clean(title or ('Session' if memory_type == 'session' else Path(item['path']).stem)),
                content=content, project=Path(project_path).name if project_path else '',
                project_path=project_path, memory_type=memory_type, metadata=metadata)


def _messages(item, identifier, role, content, project_path, **metadata):
    text = _clean(_plain(content), role)
    if not text:
        return []
    records = []
    for offset in range(0, len(text), _CHUNK):
        part = offset // _CHUNK
        rid = str(identifier) if part == 0 else f'{identifier}:part:{part}'
        records.append(_record(item, rid, f'{role}:\n{text[offset:offset + _CHUNK]}',
                               project_path, role=role, **metadata))
    return records


def _project_memory_workspace(item):
    path = Path(item['path'])
    if item['source'] != 'qoder' or path.parent.name != 'memory' or path.parent.parent.parent.name != 'projects':
        return None
    candidates = set()
    for transcript in path.parent.parent.glob('*.jsonl'):
        with _text_stream(transcript) as handle:
            for number, line in _lines(handle):
                if number > 30:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                workspace = _workspace(row)
                if workspace:
                    candidates.add(workspace)
                if row.get('type') == 'workspace-directories':
                    break
    return next(iter(candidates)) if len(candidates) == 1 else None


def _qoder_memory_type(text):
    match = re.search(r'^metadata:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+type:\s*[\"\']?(user|feedback|project|reference)\b', text, re.M)
    return match.group(1) if match else None


def _deepseek_memory_database(item):
    # Select only normalized text and provenance: never embedding, history, audit,
    # or unrelated tables. mode=ro deliberately preserves visibility of live WAL.
    required = {'id', 'type', 'title', 'content', '_full_content', 'forgotten',
                'archived', 'agent_scope', 'workspace_scope', 'sensitivity',
                'epistemic_status', 'source', 'evidence'}
    columns = sorted(required)
    records, retained = [], 0
    with _database(Path(item['path'])) as connection:
        actual = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
        if not required <= actual:
            raise SourceReadError('Unsupported DeepSeek memories schema')
        cursor = connection.execute('SELECT ' + ', '.join('"' + key + '"' for key in columns)
                                    + ' FROM memories ORDER BY id')
        while True:
            batch = cursor.fetchmany(64)
            if not batch:
                break
            for values in batch:
                row = dict(zip(columns, values))
                if not isinstance(row['id'], (str, int)) or not str(row['id']).strip():
                    raise SourceReadError('DeepSeek memory ID missing')
                for key in ('title', 'content', '_full_content', 'type'):
                    if row[key] is not None and not isinstance(row[key], str):
                        raise SourceReadError('Unsupported DeepSeek memory text shape')
                for key in ('forgotten', 'archived'):
                    if row[key] not in (None, 0, 1):
                        raise SourceReadError('Unsupported DeepSeek memory status shape')
                metadata = {}
                for key in ('agent_scope', 'workspace_scope', 'sensitivity',
                            'epistemic_status', 'source', 'evidence'):
                    value = row[key]
                    if value is not None:
                        if not isinstance(value, str):
                            raise SourceReadError('Unsupported DeepSeek memory provenance shape')
                        metadata[key] = _clean(value)
                metadata['status'] = 'revoked' if row['forgotten'] or row['archived'] else 'active'
                native_type = row['type'] or ''
                memory_type = native_type if native_type in _MEMORY_TYPES - {'index'} else 'experience'
                if native_type != memory_type:
                    metadata['native_type'] = (native_type if re.fullmatch(r'[\w.-]{1,80}', native_type)
                                               else 'unknown')
                scope = (row['workspace_scope'] or '').strip()
                project_path = _absolute(scope)
                global_scopes = {'global', 'all', '*'}
                named_scope = scope if scope and not project_path and scope.lower() not in global_scopes else ''
                reasons = []
                if not scope or (memory_type == 'project' and not project_path):
                    reasons.append('unresolved_project_scope')
                if native_type not in _MEMORY_TYPES - {'index'}:
                    reasons.append('unknown_native_type')
                if (row['agent_scope'] or '').strip().lower() not in global_scopes:
                    reasons.append('restricted_or_unknown_agent_scope')
                if (row['sensitivity'] or '').strip().lower() not in {'public', 'normal', 'standard'}:
                    reasons.append('private_or_unknown_sensitivity')
                if named_scope:
                    reasons.append('unresolved_project_scope')
                if reasons:
                    metadata['quarantine_reason'] = ';'.join(dict.fromkeys(reasons))
                    memory_type = 'experience'
                content = _clean(row['_full_content'] or row['content'] or '')
                record = _record(item, 'memory:' + str(row['id']), content, project_path,
                                 title=row['title'], memory_type=memory_type, **metadata)
                if named_scope:
                    record['project'] = _clean(named_scope)
                retained += len(json.dumps(record, ensure_ascii=False).encode('utf-8'))
                if retained > _MAX_RESULT:
                    raise SourceReadError('Memory exceeds safe 64 MiB normalized result limit')
                records.append(record)
    return records


def _workbuddy_sections(item, body):
    # Explicit H2 topics only; no semantic type inference or LLM processing.
    headings = list(re.finditer(r'^##[ \t]+(.+?)[ \t]*#*[ \t]*$', body, re.M))
    if not headings:
        return None
    records, ordinals = [], {}
    preamble = _clean(body[:headings[0].start()])
    if preamble:
        records.append(_record(item, 'memory', preamble, memory_type='experience'))
    for number, heading in enumerate(headings):
        title = _clean(heading[1])
        slug = re.sub(r'[^\w-]+', '-', title.lower()).strip('-') or 'topic'
        ordinals[slug] = ordinals.get(slug, 0) + 1
        end = headings[number + 1].start() if number + 1 < len(headings) else len(body)
        content = _clean(body[heading.start():end])
        records.append(_record(item, f'topic:{slug}:{ordinals[slug]}', content,
                               title=title, memory_type='experience'))
    return records


def _memory(item):
    if item['source'] == 'deepseek' and item['format'] == 'sqlite':
        return _deepseek_memory_database(item)
    if item['format'] != 'markdown':
        raise SourceReadError('Native memory database schema is not confirmed')
    with _text_stream(Path(item['path'])) as handle:
        parts, retained = [], 0
        for _, line in _lines(handle):
            retained += len(line.encode('utf-8'))
            if retained > _MAX_RESULT:
                raise SourceReadError('Memory exceeds safe 64 MiB read limit')
            parts.append(line)
        text = ''.join(parts)
    front, body = parse_frontmatter(text.replace('\r\n', '\n'))
    nested = front.get('metadata', {})
    memory_type = nested.get('type') if isinstance(nested, dict) else None
    memory_type = memory_type or _qoder_memory_type(text.split('\n---', 1)[0]) or front.get('metadata.type') or front.get('type') or 'experience'
    stem = Path(item['path']).stem.lower()
    if (stem == 'index' or (item['source'] == 'qoder' and stem == 'memory')
            or (item['source'] == 'codex' and stem in ('memory_summary', 'raw_memories'))):
        memory_type = 'index'
    if memory_type not in _MEMORY_TYPES:
        memory_type = 'experience'
    content = _clean(body)
    if not content:
        return []
    # Indexes must remain inventory records even without a resolvable workspace.
    if memory_type == 'index':
        return [_record(item, 'memory', content, memory_type='index')]
    if item['source'] == 'workbuddy' and Path(item['path']).name == 'MEMORY.md':
        sections = _workbuddy_sections(item, body)
        if sections is not None:
            return sections
    project_path = _workspace(front) or _absolute(front.get('workspace_scope')) or _project_memory_workspace(item)
    path = Path(item['path'])
    project_file = (item['source'] == 'qoder' and path.parent.name == 'memory'
                    and path.parent.parent.parent.name == 'projects')
    named_project = front.get('project') or front.get('workspace_scope')
    unresolved = not project_path and (memory_type == 'project' or project_file
                                      or bool(named_project)
                                      or (item['source'] == 'deepseek' and stem == 'projects'))
    metadata = {'quarantine_reason': 'unresolved_project_scope'} if unresolved else {}
    record = _record(item, 'memory', content, project_path, title=front.get('title') or front.get('name'),
                     memory_type='experience' if unresolved else memory_type, **metadata)
    if named_project and not record['project']:
        record['project'] = _clean(str(named_project))
    return [record]


def _jsonl(item):
    path = Path(item['path'])
    records, project_path, session_id = [], item.get('project_path'), None
    retained = 0
    with _text_stream(path, item['format'] == 'jsonl.zstd') as handle:
        for number, line in _lines(handle):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise SourceReadError(f'Malformed JSON at line {number}') from exc
            if not isinstance(row, dict):
                raise SourceReadError(f'JSON object required at line {number}')
            project_path = _workspace(row) or project_path
            session_id = row.get('sessionId') or session_id
            client, event = item['source'], row.get('type')
            message = None
            if client == 'codex':
                payload = row.get('payload')
                if not isinstance(payload, dict):
                    raise SourceReadError(f'Codex payload required at line {number}')
                if event == 'session_meta':
                    session_id = payload.get('id') or session_id
                    project_path = _workspace(payload) or project_path
                if event == 'response_item' and payload.get('type') == 'message':
                    message = payload
                    if payload.get('channel') not in (None, 'final', 'commentary'):
                        message = None
            elif client == 'deepseek':
                if event == 'session':
                    session_id = row.get('id') or session_id
                data = row.get('data', {})
                if not isinstance(data, dict):
                    raise SourceReadError(f'DeepSeek data object required at line {number}')
                if event == 'user/message' and isinstance(data.get('source'), dict) and data['source'].get('kind') == 'user':
                    message = dict(data, role='user')
                elif event == 'assistant/message':
                    message = data.get('message')
                if message is not None and not isinstance(message, dict):
                    raise SourceReadError(f'DeepSeek message object required at line {number}')
            else:
                nested = row.get('message')
                if (client == 'workbuddy' and 'commitSeq' in row
                        and not any(key in row for key in ('message', 'role', 'content'))
                        and event not in ('user', 'assistant', 'message')):
                    continue  # Native checkpoint NDJSON, not dialogue.
                if isinstance(nested, dict):
                    if client == 'workbuddy' and 'content' not in nested:
                        known = {'usage', 'stop_reason', 'stop_sequence', 'finish_reason', 'status'}
                        if nested and set(nested) <= known:
                            if row.get('role') in ('user', 'assistant') and 'content' in row:
                                message = row
                            elif event in ('function_call', 'function_call_output', 'tool_result', 'tool_use') or (
                                    not row.get('role') and event not in ('user', 'assistant', 'message')):
                                continue
                            else:
                                raise SourceReadError(f'Message content missing at line {number}')
                    if message is None:
                        message = dict(nested)
                        message.setdefault('role', row.get('role') or (event if event in ('user', 'assistant') else None))
                elif row.get('role') in ('user', 'assistant') and (client == 'cursor' or event == 'message'):
                    message = row
            if message is None:
                if not event and not row.get('role'):
                    raise SourceReadError(f'Unrecognized session schema at line {number}')
                continue
            role = message.get('role')
            if role not in ('user', 'assistant'):
                continue
            if 'content' not in message:
                raise SourceReadError(f'Message content missing at line {number}')
            identifier = (row.get('uuid') or message.get('id') or row.get('id')
                          or (f'seq:{row["seq"]}' if 'seq' in row else f'line:{number}'))
            chunks = _messages(item, identifier, role, message['content'], project_path)
            retained += sum(len(chunk['content'].encode('utf-8')) for chunk in chunks)
            if retained > _MAX_RESULT:
                raise SourceReadError('Session exceeds safe 64 MiB normalized result limit')
            records.extend(chunks)
    # Metadata can appear after a message, so reconcile only safe workspace/session fields.
    for record in records:
        if not record['project_path'] and project_path:
            record['project_path'] = _absolute(project_path)
            record['project'] = Path(record['project_path']).name if record['project_path'] else ''
        if session_id:
            record['metadata']['session_id'] = redact_text(str(session_id))
    return records


def _cursor_database(item):
    sid = item.get('composer_id')
    if not sid:
        raise SourceReadError('Cursor database input requires composer_id')
    with _database(Path(item['path'])) as connection:
        row = connection.execute('SELECT value FROM cursorDiskKV WHERE key = ?',
                                 ('composerData:' + sid,)).fetchone()
        if row is None:
            raise SourceReadError('Cursor composer no longer exists')
        composer = json.loads(row[0])
        if not isinstance(composer, dict):
            raise SourceReadError('Malformed Cursor composer')
        project_path = _workspace(composer) or item.get('project_path')
        headers = composer.get('fullConversationHeadersOnly')
        conversation = composer.get('conversation')
        if headers is None and conversation is None:
            if composer.get('conversationMap') == {}:
                return []
            raise SourceReadError('Unsupported Cursor conversation schema')
        entries = headers if headers is not None else conversation
        if not isinstance(entries, list):
            raise SourceReadError('Cursor conversation must be a list')
        records = []
        for number, header in enumerate(entries):
            if not isinstance(header, dict):
                raise SourceReadError('Malformed Cursor bubble header')
            bubble_id = header.get('bubbleId')
            bubble = header
            if headers is not None:
                if not bubble_id:
                    raise SourceReadError('Cursor bubble ID missing')
                row = connection.execute('SELECT value FROM cursorDiskKV WHERE key = ?',
                                         (f'bubbleId:{sid}:{bubble_id}',)).fetchone()
                if row is None:
                    raise SourceReadError('Cursor bubble data missing')
                bubble = json.loads(row[0])
            if not isinstance(bubble, dict):
                raise SourceReadError('Malformed Cursor bubble')
            role = {1: 'user', 2: 'assistant'}.get(bubble.get('type'))
            if role is None:
                continue
            workspace = _workspace(bubble) or project_path
            records.extend(_messages(item, f'{sid}:{bubble_id or number}', role,
                                     bubble.get('text', ''), workspace, session_id=redact_text(sid)))
        return records


def _cursor_markdown(item):
    records, role, lines, turn = [], None, [], 0
    with _text_stream(Path(item['path'])) as handle:
        for _, line in _lines(handle):
            match = re.fullmatch(r'(?:#{1,3}\s*)?(user|assistant):?\s*', line.strip(), re.I)
            if match:
                if role is not None:
                    records.extend(_messages(item, f'turn:{turn}', role, ''.join(lines), None))
                role, lines, turn = match[1].lower(), [], turn + 1
            elif role is not None:
                lines.append(line)
            elif line.strip():
                raise SourceReadError('Unconfirmed Cursor Markdown transcript schema')
    if role is not None:
        records.extend(_messages(item, f'turn:{turn}', role, ''.join(lines), None))
    return records


def read_source(item: dict) -> list[dict]:
    """Normalize one discovered item; never return raw tool/system/reasoning data."""
    if item.get('source') not in SOURCES or item.get('kind') not in ('memory', 'session'):
        raise SourceReadError('Invalid source descriptor')
    path = Path(item.get('path', ''))
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SourceReadError('Source must be an existing absolute regular file')
    try:
        if item['kind'] == 'memory':
            return _memory(item)
        fmt = item.get('format')
        if fmt in ('jsonl', 'ndjson', 'jsonl.zstd'):
            return _jsonl(item)
        if fmt == 'cursor-sqlite' and item['source'] == 'cursor':
            return _cursor_database(item)
        if fmt == 'markdown' and item['source'] == 'cursor':
            return _cursor_markdown(item)
        raise SourceReadError('Unsupported source format')
    except SourceReadError:
        raise
    except Exception as exc:
        # Exception messages can contain private source lines, so expose only type.
        raise SourceReadError(f'Cannot read source ({type(exc).__name__})') from exc
