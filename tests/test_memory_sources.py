"""Fixture-only source adapter tests; no real client/vault access."""
import builtins
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import memory_sources as sources


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.workspace = self.home / 'actual workspace'
        self.workspace.mkdir()

    def write(self, name, text):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def rows(self, name, *rows):
        return self.write(name, ''.join(json.dumps(row) + '\n' for row in rows))

    def descriptor(self, path, source='qoder', kind='session', fmt='jsonl', **extra):
        return dict(source=source, kind=kind, format=fmt, path=str(path), project_path=None, **extra)

    def read(self, path, **kwargs):
        return sources.read_source(self.descriptor(path, **kwargs))

    def test_all_five_schemas_and_native_ids(self):
        cases = [
            ('qoder', '.qoder/projects/encoded/session.jsonl', [
                {'type': 'workspace-directories', 'directories': [str(self.workspace)]},
                {'type': 'user', 'uuid': 'q1', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Keep correction'}]}}], 'q1'),
            ('codex', '.codex/sessions/2026/s.jsonl', [
                {'type': 'session_meta', 'payload': {'id': 'c1', 'cwd': str(self.workspace)}},
                {'type': 'response_item', 'payload': {'type': 'message', 'id': 'c2', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Reply'}]}}], 'c2'),
            ('workbuddy', '.workbuddy/projects/p/s.jsonl', [
                {'type': 'message', 'id': 'w1', 'cwd': str(self.workspace), 'role': 'user', 'content': [{'type': 'text', 'text': 'Correction'}]}], 'w1'),
            ('deepseek', '.dsh/sessions/s/session.jsonl', [
                {'type': 'session', 'id': 'd1', 'cwd': str(self.workspace)},
                {'type': 'user/message', 'data': {'id': 'd2', 'source': {'kind': 'user'}, 'content': [{'type': 'text', 'text': 'Correction'}]}}], 'd2'),
            ('cursor', '.cursor/projects/encoded/agent-transcripts/s.jsonl', [
                {'role': 'user', 'cwd': str(self.workspace), 'message': {'content': [{'type': 'text', 'text': 'Correction'}]}}], 'line:1'),
        ]
        for client, name, rows, identifier in cases:
            with self.subTest(client=client):
                path = self.rows(name, *rows)
                records = self.read(path, source=client)
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record['record_id'], identifier)
                self.assertEqual(record['project_path'], str(self.workspace))
                self.assertEqual(record['project'], 'actual workspace')
                self.assertEqual(record['source_path'], str(path))
                self.assertEqual(record['memory_type'], 'session')
                self.assertEqual(record['source'], client)
                self.assertIsInstance(record['metadata'], dict)
                self.assertEqual(record, self.read(path, source=client)[0])

    def test_memory_frontmatter_index_and_redaction(self):
        for client in sources.SOURCES[:-1]:
            path = self.write(f'{client}.md', '---\nname: Preference\nmetadata:\n  type: feedback\n---\nKeep password=hunter2 private')
            record = self.read(path, source=client, kind='memory', fmt='markdown')[0]
            self.assertEqual(record['memory_type'], 'feedback')
            self.assertEqual(record['record_id'], 'memory')
            self.assertNotIn('hunter2', record['content'])
            self.assertEqual(record['title'], 'Preference')
        for client, name in [('qoder', 'MEMORY.md'), ('codex', 'memory_summary.md'), ('codex', 'raw_memories.md')]:
            record = self.read(self.write(name, '# Index'), source=client, kind='memory', fmt='markdown')[0]
            self.assertEqual(record['memory_type'], 'index')
        record = self.read(self.write('MEMORY.md', '# Actual memory'), source='workbuddy', kind='memory', fmt='markdown')[0]
        self.assertEqual(record['memory_type'], 'experience')

    def test_project_memory_uses_transcript_workspace_and_nested_type(self):
        self.rows('.qoder/projects/encoded/s.jsonl', {'type': 'workspace-directories', 'directories': [str(self.workspace)]})
        path = self.write('.qoder/projects/encoded/memory/preference.md', '---\nname: preference\nmetadata:\n  type: feedback\n---\nUse Chinese')
        record = self.read(path, kind='memory', fmt='markdown')[0]
        self.assertEqual(record['memory_type'], 'feedback')
        self.assertEqual(record['project_path'], str(self.workspace))

    def test_ambiguous_project_memory_is_not_user_scope(self):
        path = self.write('.qoder/projects/encoded/memory/preference.md', '---\nmetadata:\n  type: feedback\n---\nUse Chinese')
        record = self.read(path, kind='memory', fmt='markdown')[0]
        self.assertEqual(record['metadata']['quarantine_reason'], 'unresolved_project_scope')
        self.assertEqual(record['memory_type'], 'experience')
        self.assertIn('Use Chinese', record['content'])

    def test_discovery_scope_and_no_source_writes(self):
        expected = [
            '.qoder/memory/a.md', '.qoder/projects/p/memory/b.md',
            '.qoder/projects/p/s.jsonl', '.codex/memories/nested/a.md',
            '.codex/memories_v2/a.md', '.codex/sessions/y/s.jsonl.zst',
            '.codex/archived_sessions/a.jsonl.zst', '.workbuddy/MEMORY.md',
            '.workbuddy/memory/a.md', '.workbuddy/projects/p/s.ndjson',
            '.dsh/memory/a.db', '.dsh/sessions/id/session.jsonl.zstd',
            '.cursor/projects/p/agent-transcripts/s.jsonl']
        for name in expected:
            self.write(name, '')
        for name in ['elsewhere/a.md', '.cursor/rules/a.md', '.qoder/memory/backup/a.md',
                     '.codex/memories/.git/a.md', '.qoder/projects/p/nested/unrelated.jsonl',
                     '.qoder/memory/a.md.bak']:
            self.write(name, '')
        outside = self.home / 'elsewhere'
        (self.home / '.qoder/memory/link').symlink_to(outside, target_is_directory=True)
        (self.home / '.qoder/memory/link.md').symlink_to(outside / 'a.md')
        before = {str(p): p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()}
        items = sources.discover_sources(self.home)
        after = {str(p): p.stat().st_mtime_ns for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual({i['path'] for i in items}, {str(self.home / p) for p in expected})
        self.assertTrue(all(i['project_path'] is None for i in items))
        self.assertEqual(sources.inventory_sources(self.home)['count'], len(expected))
        self.assertTrue(all(i['kind'] == 'memory' and i['source'] == 'codex'
                            for i in sources.discover_sources(self.home, 'codex', 'memory')))
        for source, kind in [('other', 'all'), ('all', 'rules')]:
            with self.assertRaises(sources.SourceReadError):
                sources.discover_sources(self.home, source, kind)

    def test_filters_system_tools_reasoning_and_keeps_correction(self):
        path = self.rows('q.jsonl',
            {'type': 'user', 'uuid': 'u', 'message': {'role': 'user', 'content': '<system-reminder>AGENTS injected</system-reminder>\nActually use the corrected value'}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'hidden'}, {'type': 'tool_use', 'input': {'secret': 'hidden'}},
                {'type': 'text', 'text': 'public answer'}]}},
            {'type': 'system', 'message': {'role': 'system', 'content': 'private'}},
            {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'large tool output'}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': '# AGENTS.md instructions\nInjected'}})
        content = '\n'.join(r['content'] for r in self.read(path))
        self.assertIn('Actually use the corrected value', content)
        self.assertIn('public answer', content)
        for term in ['AGENTS', 'hidden', 'private', 'large tool output', 'Injected']:
            self.assertNotIn(term, content)

    def test_codex_analysis_and_deepseek_non_user_filtered(self):
        path = self.rows('c.jsonl', {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'channel': 'analysis', 'content': 'private'}})
        self.assertEqual(self.read(path, source='codex'), [])
        path = self.rows('d.jsonl',
            {'type': 'user/message', 'data': {'content': 'internal', 'source': {'kind': 'tool'}}},
            {'type': 'assistant/message', 'seq': 8, 'data': {'message': {'id': 'a1', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'public'}]}, 'stream': ['private']}})
        records = self.read(path, source='deepseek')
        self.assertEqual([r['record_id'] for r in records], ['a1'])
        self.assertNotIn('internal', records[0]['content'])

    def test_empty_bad_format_and_malformed(self):
        path = self.write('empty.jsonl', '')
        self.assertEqual(self.read(path), [])
        self.assertEqual(self.read(self.write('empty.md', ''), kind='memory', fmt='markdown'), [])
        for text in ['{bad', '[]', '{"unknown":1}']:
            with self.subTest(text=text), self.assertRaises(sources.SourceReadError):
                self.read(self.write('invalid.jsonl', text))
        for fmt in ['unsupported', 'sqlite']:
            with self.assertRaises(sources.SourceReadError):
                self.read(path, fmt=fmt)
        with self.assertRaises(sources.SourceReadError):
            self.read(self.write('memory.db', ''), kind='memory', fmt='sqlite')
        with self.assertRaises(sources.SourceReadError):
            self.read(self.write('unsupported.md', '# Unknown transcript'), source='cursor', fmt='markdown')

    def test_long_messages_not_tail_truncated_and_ids_survive_append(self):
        path = self.rows('long.jsonl', {'type': 'user', 'uuid': 'native', 'message': {'role': 'user', 'content': 'A' * 50000}})
        before = self.read(path)
        self.assertEqual(len(before), 3)
        self.assertEqual(sum(len(r['content'].split('\n', 1)[1]) for r in before), 50000)
        self.assertEqual([r['record_id'] for r in before], ['native', 'native:part:1', 'native:part:2'])
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'user', 'uuid': 'new', 'message': {'role': 'user', 'content': 'new'}}) + '\n')
        self.assertEqual(self.read(path)[:3], before)

    def test_zstd_read_and_truncation(self):
        try:
            from compression import zstd
        except ImportError:
            self.skipTest('Fixture compressor needs Python 3.14 compression.zstd')
        path = self.home / 'compressed.jsonl.zst'
        raw = (json.dumps({'type': 'user', 'uuid': 'z1', 'message': {'role': 'user', 'content': 'compressed'}}) + '\n').encode()
        path.write_bytes(zstd.compress(raw))
        self.assertEqual(self.read(path, fmt='jsonl.zstd')[0]['record_id'], 'z1')
        original_import = builtins.__import__

        def legacy_import(name, *args, **kwargs):
            if name == 'compression':
                raise ImportError('Python before 3.14')
            return original_import(name, *args, **kwargs)

        # Exercise the real fallback stream through the public API.
        if sources.ctypes.util.find_library('zstd'):
            self.assertEqual(b''.join(sources._zstd_chunks(path)), raw)
            with patch('builtins.__import__', side_effect=legacy_import), patch.object(sources.shutil, 'which', return_value=None):
                self.assertEqual(self.read(path, fmt='jsonl.zstd')[0]['record_id'], 'z1')
        if sources.shutil.which('zstd'):
            with patch('builtins.__import__', side_effect=legacy_import):
                self.assertEqual(self.read(path, fmt='jsonl.zstd')[0]['record_id'], 'z1')
        with patch('builtins.__import__', side_effect=legacy_import), patch.object(sources.shutil, 'which', return_value=None), patch.object(sources.ctypes.util, 'find_library', return_value=None):
            with self.assertRaises(sources.SourceReadError):
                self.read(path, fmt='jsonl.zstd')
        path.write_bytes(path.read_bytes()[:-3])
        with self.assertRaises(sources.SourceReadError):
            self.read(path, fmt='jsonl.zstd')

    def cursor_db(self):
        path = self.home / 'Library/Application Support/Cursor/User/globalStorage/state.vscdb'
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)')
        return path, connection

    def test_cursor_read_only_database_wal_and_export_dedup(self):
        path, connection = self.cursor_db()
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        composer = {'composerId': 's1', 'workspaceIdentifier': {'uri': {'scheme': 'file', 'path': str(self.workspace)}},
                    'fullConversationHeadersOnly': [{'bubbleId': 'b1'}, {'bubbleId': 'b2'}], 'blobEncryptionKey': 'not-safe'}
        for key, value in [('composerData:s1', composer),
                           ('bubbleId:s1:b1', {'type': 1, 'text': 'user correction', 'allThinkingBlocks': ['secret']}),
                           ('bubbleId:s1:b2', {'type': 2, 'text': 'answer', 'toolResults': ['large']})]:
            connection.execute('INSERT INTO cursorDiskKV VALUES (?, ?)', (key, json.dumps(value)))
        connection.commit()
        before = path.read_bytes()
        items = sources.discover_sources(self.home, 'cursor')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['composer_id'], 's1')
        records = sources.read_source(items[0])
        self.assertEqual([r['record_id'] for r in records], ['s1:b1', 's1:b2'])
        self.assertEqual(records[0]['project_path'], str(self.workspace))
        self.assertNotIn('not-safe', json.dumps(records))
        self.assertNotIn('secret', json.dumps(records))
        self.assertEqual(path.read_bytes(), before)
        with sources._database(path) as read_only:
            with self.assertRaises(sqlite3.OperationalError):
                read_only.execute("INSERT INTO cursorDiskKV VALUES ('x', 'y')")
        self.write('.cursor/projects/p/agent-transcripts/s1.jsonl', '')
        self.write('.cursor/projects/p/agent-transcripts/s1.md', '')
        items = sources.discover_sources(self.home, 'cursor')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['format'], 'jsonl')

    def test_metadata_inventory_never_decodes_cursor_body(self):
        path, connection = self.cursor_db()
        connection.execute('INSERT INTO cursorDiskKV VALUES (?, ?)', ('composerData:one', 'not-json-private-body'))
        connection.commit()
        connection.close()
        with patch.object(sources, '_composers', side_effect=AssertionError('must not read values')):
            items = sources.discover_sources(self.home, source='cursor', metadata_only=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['composer_id'], 'one')
        self.assertIsNone(items[0]['project_path'])

    def test_cursor_missing_bubble_is_error(self):
        path, connection = self.cursor_db()
        connection.execute('INSERT INTO cursorDiskKV VALUES (?, ?)', ('composerData:s1', json.dumps({'fullConversationHeadersOnly': [{'bubbleId': 'missing'}]})))
        connection.commit()
        connection.close()
        with self.assertRaises(sources.SourceReadError):
            sources.read_source(sources.discover_sources(self.home, 'cursor')[0])

    def test_checkpoint_ndjson_is_known_non_dialogue(self):
        path = self.rows('checkpoint.ndjson', {'v': 1, 'requestId': 'r', 'commitSeq': 1, 'ca': 'opaque'})
        self.assertEqual(self.read(path, source='workbuddy', fmt='ndjson'), [])

    def test_workbuddy_metadata_and_unknown_content(self):
        path = self.rows('native.jsonl',
            {'v': 1, 'requestId': 'r', 'commitSeq': 0},
            {'type': 'function_call', 'message': {'usage': {}}},
            {'type': 'message', 'role': 'assistant', 'content': 'answer', 'message': {'usage': {}}},
            {'type': 'message', 'role': 'user', 'content': 'question'})
        self.assertEqual(len(self.read(path, source='workbuddy')), 2)
        for row in [
            {'type': 'message', 'role': 'assistant', 'message': {'usage': {}}},
            {'type': 'message', 'role': 'user', 'message': {'unknown': 'private'}},
            {'commitSeq': 1, 'type': 'message', 'role': 'user', 'message': {}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'usage': {}}},
            {'type': 'user', 'message': {'unknown': 'private'}},
            {'type': 'assistant', 'message': {'usage': {}}},
        ]:
            with self.subTest(shape=list(row)), self.assertRaises(sources.SourceReadError):
                self.read(self.rows('bad.jsonl', row), source='workbuddy')

    def test_cursor_compaction_and_companions_excluded(self):
        base = '.cursor/projects/p/agent-transcripts/'
        for name in ['s.jsonl', 'compaction/INDEX.md', 'compaction/segment_000.md',
                     'INDEX.md', 'segment_000.md', 'segment_000.jsonl']:
            self.write(base + name, '')
        items = sources.discover_sources(self.home, 'cursor')
        self.assertEqual({Path(i['path']).name for i in items}, {'s.jsonl', 'segment_000.jsonl'})

    def native_memory_db(self):
        path = self.home / 'memory.db'
        connection = sqlite3.connect(path)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE memories (id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT, '
                           '_full_content TEXT, forgotten INTEGER, archived INTEGER, agent_scope TEXT, '
                           'workspace_scope TEXT, sensitivity TEXT, epistemic_status TEXT, source TEXT, evidence TEXT, embedding BLOB)')
        self.addCleanup(connection.close)
        return path, connection

    def test_native_memory_database_scopes_status_and_readonly(self):
        path, connection = self.native_memory_db()
        base = ['one', 'user', 'Title', 'short', 'full password=secret', 0, 0,
                '*', str(self.workspace), 'public', 'observation', 'native', '["doc:1"]', b'private embedding']
        rows = [base]
        for identifier, changes in [('archived', {6: 1}), ('forgotten', {5: 1}),
                                    ('named', {8: 'project-name'}),
                                    ('private', {7: 'agent-one', 9: 'private'}),
                                    ('unknown', {1: 'preference', 8: None, 7: None, 9: None})]:
            row = base.copy()
            row[0] = identifier
            for index, value in changes.items():
                row[index] = value
            rows.append(row)
        connection.executemany('INSERT INTO memories VALUES (' + ','.join('?' for _ in base) + ')', rows)
        connection.commit()
        # SQLite read-only readers may update transient SHM reader marks, but
        # must not alter the database, committed WAL, or journal mode.
        before = {p.name: p.read_bytes() for p in (path, Path(str(path) + '-wal'))}
        records = self.read(path, source='deepseek', kind='memory', fmt='sqlite')
        indexed = {r['record_id']: r for r in records}
        self.assertEqual(len(records), 6)
        record = indexed['memory:one']
        self.assertEqual(record['memory_type'], 'user')
        self.assertEqual(record['project_path'], str(self.workspace))
        self.assertEqual(record['metadata']['evidence'], '["doc:1"]')
        self.assertNotIn('secret', record['content'])
        self.assertTrue(record['content'].startswith('full'))
        self.assertNotIn('embedding', json.dumps(records))
        for name in ('archived', 'forgotten'):
            self.assertEqual(indexed['memory:' + name]['metadata']['status'], 'revoked')
        self.assertEqual(indexed['memory:named']['project'], 'project-name')
        for name in ('named', 'private', 'unknown'):
            self.assertEqual(indexed['memory:' + name]['memory_type'], 'experience')
            self.assertTrue(indexed['memory:' + name]['metadata']['quarantine_reason'])
        self.assertEqual(indexed['memory:unknown']['metadata']['native_type'], 'preference')
        self.assertEqual(before, {p.name: p.read_bytes() for p in (path, Path(str(path) + '-wal'))})
        self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        with sources._database(path) as read_only:
            with self.assertRaises(sqlite3.OperationalError):
                read_only.execute('DELETE FROM memories')
        with patch.object(sources, '_MAX_RESULT', 10), self.assertRaisesRegex(sources.SourceReadError, '64 MiB'):
            self.read(path, source='deepseek', kind='memory', fmt='sqlite')

    def test_unknown_native_database_schema_fails(self):
        path = self.home / 'unknown.db'
        connection = sqlite3.connect(path)
        try:
            connection.execute('CREATE TABLE memories (id TEXT, content TEXT)')
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(sources.SourceReadError, 'schema'):
            self.read(path, source='deepseek', kind='memory', fmt='sqlite')

    def test_unresolved_markdown_scope_and_index(self):
        for source, name, text in [
            ('deepseek', 'projects.md', '# Projects\nPrivate project context'),
            ('deepseek', 'named.md', '---\ntype: project\nproject: named\n---\nContext'),
        ]:
            record = self.read(self.write(name, text), source=source, kind='memory', fmt='markdown')[0]
            self.assertEqual(record['memory_type'], 'experience')
            self.assertEqual(record['metadata']['quarantine_reason'], 'unresolved_project_scope')
        self.assertEqual(record['project'], 'named')
        path = self.write('.qoder/projects/p/memory/INDEX.md', '# Index')
        record = self.read(path, kind='memory', fmt='markdown')[0]
        self.assertEqual(record['memory_type'], 'index')
        self.assertNotIn('quarantine_reason', record['metadata'])

    def test_workbuddy_h2_stable_topics(self):
        path = self.write('MEMORY.md', '# Memories\n## Topic A\nFirst\n## Topic A\nSecond\n## 业务\nThird\n')
        records = self.read(path, source='workbuddy', kind='memory', fmt='markdown')
        self.assertEqual([r['record_id'] for r in records], ['memory', 'topic:topic-a:1', 'topic:topic-a:2', 'topic:业务:1'])
        self.assertTrue(all(r['memory_type'] == 'experience' for r in records))
        self.assertNotIn('Second', records[1]['content'])
        before = [r['record_id'] for r in records]
        path.write_text(path.read_text() + '## Extra\nFourth\n')
        self.assertEqual([r['record_id'] for r in self.read(path, source='workbuddy', kind='memory', fmt='markdown')][:4], before)
        migrated = self.write('workbuddy-memory.md', path.read_text())
        self.assertEqual(len(self.read(migrated, kind='memory', fmt='markdown')), 1)

    def test_bounded_stream_and_result_fail_explicitly(self):
        path = self.rows('bounded.jsonl', {'type': 'message', 'id': 'b1', 'role': 'user', 'content': 'text'})
        with patch.object(sources, '_MAX_LINE', 10), self.assertRaises(sources.SourceReadError):
            self.read(path, source='workbuddy')
        with patch.object(sources, '_MAX_RESULT', 2), self.assertRaises(sources.SourceReadError):
            self.read(path, source='workbuddy')

    def test_symlink_client_root_not_traversed(self):
        self.write('outside/memory/a.md', 'not a source')
        (self.home / '.qoder').symlink_to(self.home / 'outside', target_is_directory=True)
        self.assertEqual(sources.discover_sources(self.home, 'qoder'), [])

    def test_memory_id_does_not_change_with_body(self):
        path = self.write('memory.md', 'old body')
        before = self.read(path, kind='memory', fmt='markdown')[0]
        path.write_text('new body')
        after = self.read(path, kind='memory', fmt='markdown')[0]
        self.assertEqual(before['record_id'], after['record_id'])
        self.assertNotEqual(before['content'], after['content'])

    def test_markdown_transcript_roles(self):
        path = self.write('export.md', 'user:\nCorrection\nassistant:\nAccepted\n')
        records = self.read(path, source='cursor', fmt='markdown')
        self.assertEqual([r['record_id'] for r in records], ['turn:1', 'turn:2'])
        self.assertEqual([r['metadata']['role'] for r in records], ['user', 'assistant'])


if __name__ == '__main__':
    unittest.main()
