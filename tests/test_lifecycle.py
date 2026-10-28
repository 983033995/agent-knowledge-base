from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import akb
import lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {
            "AKB_VAULT": str(self.root / "vault"), "AKB_STATE_DB": str(self.root / "state.sqlite3"),
            "AKB_SEARCH_INDEX": str(self.root / "index.json"), "AKB_SYNC_STATE_DIR": str(self.root / "sync"),
            "AKB_DISTILLER_STATE_DIR": str(self.root / "distill"), "AKB_DISTILLER_AUTOSTART": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, akb, "_CONFIG_CACHE", None)
        self.addCleanup(setattr, akb, "_SEARCH_INDEX_MEM", None)

    def transcript(self, text="用户纠正必须保留"):
        path = self.root / "session.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [
            {"type": "workspace-directories", "directories": [str(self.workspace)]},
            {"type": "user", "uuid": "one", "message": {"role": "user", "content": text}},
            {"type": "assistant", "uuid": "two", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "已验证结果"}]}},
        ]) + "\n", encoding="utf-8")
        return path

    def event(self, path):
        return {"cwd": str(self.workspace), "session_id": "session-one", "transcript_path": str(path)}

    def test_capture_preserves_scope_and_filters_thinking(self):
        with patch.object(akb, "enqueue_distillation", return_value={"queued": True}):
            result = lifecycle.capture("qoder", self.event(self.transcript()))
        self.assertTrue(result["queued"])
        metadata, body = akb.read_note(Path(result["path"]))
        self.assertEqual(metadata["project_path"], str(self.workspace))
        self.assertEqual(metadata["source"], "qoder")
        self.assertIn("用户纠正必须保留", body)
        self.assertNotIn("hidden", body)

    def test_recalled_context_is_not_captured_again(self):
        path = self.transcript('<akb-recalled-context>旧知识不应再次提炼</akb-recalled-context>\n新的用户修正')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            result = lifecycle.capture('qoder', self.event(path))
        _, body = akb.read_note(Path(result['path']))
        self.assertNotIn('旧知识不应再次提炼', body)
        self.assertIn('新的用户修正', body)

    def test_duplicate_events_share_input_identity(self):
        event = self.event(self.transcript())
        with patch.object(akb, "enqueue_distillation", return_value={"queued": True}):
            first = lifecycle.capture("qoder", event)
            second = lifecycle.capture("qoder", event)
        self.assertTrue(first['captured'])
        self.assertEqual(second['reason'], 'no_new_dialogue')
        self.assertEqual(len(list((self.root / "vault").rglob("lifecycle-*.md"))), 1)

    def test_append_captures_only_new_messages(self):
        path = self.transcript('OLD_PRIVATE_DIALOGUE')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            lifecycle.capture('qoder', self.event(path))
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'type': 'user', 'uuid': 'three', 'message': {'role': 'user', 'content': 'NEW_DIALOGUE'}}) + '\n')
            result = lifecycle.capture('qoder', self.event(path))
        _, body = akb.read_note(Path(result['path']))
        self.assertIn('NEW_DIALOGUE', body)
        self.assertNotIn('OLD_PRIVATE_DIALOGUE', body)

    def test_nested_and_unclosed_injection_never_reaches_capture(self):
        path = self.transcript('<system-reminder>outer<system-reminder>inner</system-reminder>INJECTED_TAIL</system-reminder>SAFE')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            result = lifecycle.capture('qoder', self.event(path))
        _, body = akb.read_note(Path(result['path']))
        self.assertNotIn('INJECTED_TAIL', body)
        self.assertIn('SAFE', body)
        self.assertEqual(lifecycle.memory_sources._clean('SAFE<system-reminder>hidden'), 'SAFE')

    def test_missing_workspace_or_transcript_never_writes(self):
        self.assertFalse(lifecycle.capture("qoder", {})["captured"])
        self.assertFalse(lifecycle.capture("qoder", {"transcript_path": str(self.transcript())})["captured"])
        self.assertFalse((self.root / "vault").exists())

    def test_recall_is_workspace_scoped_and_bounded(self):
        akb.write_note(akb.knowledge_directory(), "one.md", {"title": "可复用经验", "project": "project", "project_path": str(self.workspace)}, "本项目事实" * 1000)
        akb.write_note(akb.knowledge_directory(), "two.md", {"title": "其它项目", "project": "project", "project_path": str(self.root / "other/project")}, "不应泄露")
        context = lifecycle.recall(str(self.workspace))
        self.assertIn("本项目事实", context)
        self.assertNotIn("不应泄露", context)
        self.assertLessEqual(len(context), 3000)

    def test_nonabsolute_workspace_cannot_recall(self):
        self.assertEqual(lifecycle.recall("relative"), "")

    def test_workspace_conflict_is_not_relabelled(self):
        path = self.transcript()
        event = self.event(path)
        event['cwd'] = str(self.root / 'different')
        result = lifecycle.capture('qoder', event)
        self.assertEqual(result['reason'], 'workspace_mismatch')
        self.assertFalse((self.root / 'vault').exists())

    def test_first_capture_does_not_backfill_unbounded_old_history(self):
        path = self.root / 'long.jsonl'
        rows = [{'type': 'user', 'uuid': str(index), 'message': {'role': 'user', 'content': ('OLD_HISTORY' if index < 20 else 'LATEST_DIALOGUE') * 100}} for index in range(22)]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            result = lifecycle.capture('qoder', self.event(path))
        self.assertLessEqual(len(result['paths']), 2)
        self.assertEqual(lifecycle.capture('qoder', self.event(path))['reason'], 'no_new_dialogue')

    def test_long_message_batches_are_bounded_and_cursor_is_stable(self):
        path = self.transcript('initial')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            lifecycle.capture('qoder', self.event(path))
            with path.open('a') as handle:
                handle.write(json.dumps({'type': 'user', 'uuid': 'long-new', 'message': {'role': 'user', 'content': 'LONG' * 12000}}) + '\n')
            result = lifecycle.capture('qoder', self.event(path))
        self.assertGreater(len(result['paths']), 1)
        for note in result['paths']:
            _, body = akb.read_note(Path(note))
            self.assertLess(len(body), 20000)
        self.assertEqual(lifecycle.capture('qoder', self.event(path))['reason'], 'no_new_dialogue')

    def test_initial_baseline_does_not_move_after_enqueue_failure(self):
        path = self.root / 'baseline.jsonl'
        rows = [{'type': 'user', 'uuid': name, 'message': {'role': 'user', 'content': name * 2500}} for name in ['OLDX', 'RECENT']]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': False}):
            lifecycle.capture('qoder', self.event(path))
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'user', 'uuid': 'new', 'message': {'role': 'user', 'content': 'NEWX' * 2500}}) + '\n')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            result = lifecycle.capture('qoder', self.event(path))
        bodies = '\n'.join(akb.read_note(Path(note))[1] for note in result['paths'])
        self.assertIn('RECENT', bodies)
        self.assertIn('NEWX', bodies)
        self.assertNotIn('OLDX', bodies)

    def test_successful_long_message_part_is_not_reenqueued(self):
        path = self.transcript('initial')
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}):
            lifecycle.capture('qoder', self.event(path))
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'user', 'uuid': 'long-new', 'message': {'role': 'user', 'content': 'L' * 23000}}) + '\n')
        with patch.object(akb, 'enqueue_distillation', side_effect=[{'queued': True}, {'queued': False}]):
            first = lifecycle.capture('qoder', self.event(path))
        self.assertFalse(first['queued'])
        with patch.object(akb, 'enqueue_distillation', return_value={'queued': True}) as enqueue:
            lifecycle.capture('qoder', self.event(path))
        self.assertEqual(enqueue.call_count, 1)

    def test_transcript_mutation_skips_inconsistent_snapshot(self):
        path = self.transcript()
        original = lifecycle.memory_sources.read_source

        def mutate(item):
            rows = original(item)
            path.write_text(path.read_text() + "\n", encoding="utf-8")
            return rows

        with patch.object(lifecycle.memory_sources, "read_source", side_effect=mutate):
            result = lifecycle.capture("qoder", self.event(path))
        self.assertEqual(result["reason"], "transcript_changed")
        self.assertFalse((self.root / "vault").exists())


if __name__ == "__main__":
    unittest.main()
