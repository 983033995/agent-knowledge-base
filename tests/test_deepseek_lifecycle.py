from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import deepseek_lifecycle_bridge as bridge


class DeepSeekBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {'AKB_DISTILLER_STATE_DIR': str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.payload = {'cwd': str(self.base), 'session_id': 'synthetic',
                        'events': [{'role': 'user', 'text': 'synthetic-public'}]}

    def test_recall_contract(self):
        with patch.object(bridge.lifecycle, 'recall', return_value='scoped') as recall:
            self.assertEqual(bridge.dispatch('recall', self.payload), {'context': 'scoped'})
            recall.assert_called_once_with(str(self.base))
        with self.assertRaises(ValueError):
            bridge.dispatch('recall', {'cwd': 'relative'})

    def test_capture_native_transcript_idempotence_and_privacy(self):
        self.payload['events'] += [
            {'role': 'tool', 'text': 'hidden-tool'},
            {'role': 'assistant', 'text': '<akb-recalled-context>old-recall</akb-recalled-context>public-answer'},
            {'role': 'assistant', 'text': 'api_key=sk-123456789012345678901234567890'},
        ]
        def capture(source, event):
            self.assertEqual(source, 'deepseek')
            self.assertEqual(event['cwd'], str(self.base))
            path = Path(event['transcript_path'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn('hidden-tool', text)
            self.assertNotIn('old-recall', text)
            self.assertNotIn('sk-123456789012345678901234567890', text)
            self.assertIn('public-answer', text)
            return {'captured': True, 'queued': True}
        with patch.object(bridge.lifecycle, 'capture', side_effect=capture) as mock:
            self.assertTrue(bridge.dispatch('capture', self.payload)['queued'])
            self.assertEqual(bridge.dispatch('capture', self.payload)['reason'], 'duplicate')
            mock.assert_called_once()

    def test_native_seq_survives_sliding_window(self):
        self.payload['events'] = [
            {'role': 'user', 'text': 'first', 'id': 'seq:8'},
            {'role': 'assistant', 'text': 'answer', 'id': 'seq:13'},
        ]
        snapshots = []
        def capture(source, event):
            rows = [json.loads(line) for line in Path(event['transcript_path']).read_text().splitlines()]
            snapshots.append(rows)
            descriptor = {'source': source, 'format': 'jsonl', 'path': event['transcript_path'],
                          'project_path': event['cwd'], 'kind': 'session'}
            parsed = bridge.memory_sources.read_source(descriptor)
            self.assertEqual([r['record_id'] for r in parsed],
                             [f"seq:{r['seq']}" for r in rows])
            return {'captured': True, 'queued': True}
        with patch.object(bridge.lifecycle, 'capture', side_effect=capture):
            bridge.dispatch('capture', self.payload)
            self.payload['events'].pop(0)
            bridge.dispatch('capture', self.payload)
        self.assertEqual(snapshots[0][1]['seq'], snapshots[1][0]['seq'])
        self.assertNotIn('id', snapshots[1][0])

    def test_failed_capture_retries_and_bounds(self):
        self.payload['events'][0]['text'] = 'x' * 50000
        with patch.object(bridge.lifecycle, 'capture', return_value={'captured': False}) as mock:
            bridge.dispatch('capture', self.payload)
            bridge.dispatch('capture', self.payload)
            self.assertEqual(mock.call_count, 2)
        self.assertEqual(len(bridge.clean_events(self.payload['events'])[0]['text']), 18000)

    def test_enqueue_failure_is_retryable_and_unclosed_injection_is_removed(self):
        self.payload['events'] += [{'role': 'assistant', 'text':
                                   '<akb-recalled-context>private injected content'}]
        self.assertNotIn('private injected content', str(bridge.clean_events(self.payload['events'])))
        with patch.object(bridge.lifecycle, 'capture', return_value={'captured': True, 'queued': False}) as mock:
            bridge.dispatch('capture', self.payload)
            bridge.dispatch('capture', self.payload)
            self.assertEqual(mock.call_count, 2)

    def test_stdin_limit_and_sanitized_error(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/deepseek_lifecycle_bridge.py'), 'recall'],
                                input='private-not-json', text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('private-not-json', result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for plugin contract tests')
    def test_node_plugin_event_scope_privacy_reentry_and_timeout(self):
        module = (ROOT / 'integrations/deepseek-memory.mjs').as_uri()
        source = '''
import assert from 'node:assert/strict';
import { apply, publicEvent, runBridge } from MODULE;
const handlers = new Map();
const calls = [];
const warnings = [];
const ctx = { on: (key, cb) => handlers.set(key, cb), logger: { warn: x => warnings.push(x) } };
const session = { id: 'synthetic', header: { cwd: '/tmp/synthetic-project' } };
const other = { id: 'other', header: { cwd: '/tmp/other-project' } };
let failures = 0;
const fake = async (action, payload) => {
  calls.push({ action, payload });
  if (action === 'recall') return { context: payload.cwd + '{{literal}}' };
  if (failures-- > 0) throw new Error('private failure');
  return { captured: true, queued: true };
};
apply(ctx, {}, fake);
const emit = handlers.get('session/event');
assert.equal(publicEvent({type:'tool/result', data:{text:'secret'}}), null);
assert.equal(publicEvent({type:'user/message', data:{source:{kind:'system'},content:'secret'}}), null);
emit(session, {type:'user/message',data:{source:{kind:'user'},content:[{type:'text',text:'public-user'},{type:'thinking',text:'secret'}]}});
emit(session, {type:'assistant/message',data:{message:{role:'assistant',content:[{type:'thinking',text:'secret'},{type:'tool-call',text:'secret'},{type:'text',text:'public-answer'}]}}});
emit(session, {type:'turn/end'});
emit(session, {type:'turn/end'});
handlers.get('session/disposed')(session);
await handlers.get('session/flush')(session);
assert.equal(calls.filter(x=>x.action==='capture').length,1);
assert(!JSON.stringify(calls).includes('secret'));
const assemble = handlers.get('system-prompt/assemble');
const next = async () => ({contexts:[{name:'existing',text:'preserve'}]});
const result = await assemble({}, {agent:{session}}, next);
assert.equal(result.contexts[0].text,'preserve');
assert.equal(result.contexts[1].interpolate,false);
assert(result.contexts[1].text.includes('{{literal}}'));
const different = await assemble({}, {agent:{session:other}}, next);
assert(different.contexts[1].text.startsWith('/tmp/other-project'));
await assemble({}, {agent:{session}}, next);
assert.equal(calls.filter(x=>x.action==='recall').length,2);
failures=1;
emit(session,{type:'user/message',data:{source:{kind:'user'},content:'new-public'}});
await handlers.get('session/flush')(session);
await handlers.get('session/flush')(session);
assert.equal(calls.filter(x=>x.action==='capture').length,3);
assert.equal(warnings.length,1);
assert(!warnings[0].includes('private failure'));
const before=calls.length;
emit({id:'bad',header:{cwd:'relative'}},{type:'turn/end'});
await new Promise(r=>setImmediate(r));
assert.equal(calls.length,before);
await assert.rejects(runBridge('recall', {cwd:'/tmp'},0), /bridge_timeout/);
await assert.rejects(runBridge('recall', {cwd:'relative'}), /bridge_failed:ValueError/);
assert.equal(publicEvent({type:'assistant/message',seq:13,data:{message:{role:'assistant',content:'answer'}}}).id,'seq:13');
console.log('plugin contracts passed');
'''.replace('MODULE', json.dumps(module))
        result = subprocess.run([shutil.which('node'), '--input-type=module'], input=source,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('plugin contracts passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
