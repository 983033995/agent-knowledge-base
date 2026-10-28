#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import akb
import distiller


class QueueSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.env = patch.dict(os.environ, {
            "AKB_VAULT": str(self.root / "vault"),
            "AKB_SEARCH_INDEX": str(self.root / "index.json"),
            "AKB_STATE_DB": str(self.state / "state.sqlite3"),
            "AKB_DISTILLER_STATE_DIR": str(self.state),
            "AKB_DISTILLER_ENABLED": "1",
            "AKB_DISTILLER_AUTOSTART": "0",
            "AKB_DISTILLER_MAX_ATTEMPTS": "1",
        })
        self.env.start()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        self.settings = distiller.load_settings()
        self.store = distiller.queue_store(self.settings)
        self.ns = distiller.queue_namespace(self.settings)

    def tearDown(self):
        self.env.stop()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        self.temp.cleanup()

    def note(self):
        return akb.create_capture_intake(source="test", title="SQLite队列", raw_context="临时测试正文")

    def jobs(self, kind="queue"):
        with self.store.read() as tx:
            return tx.list_jobs(self.ns, kind) if tx else []

    def legacy(self, kind, key, entry):
        path = self.state / kind / (key + ".json")
        distiller.atomic_write_json(path, entry)
        return path

    def test_status_without_database_is_read_only(self):
        status = distiller.collect_status()
        self.assertEqual(status["queue"], 0)
        self.assertFalse(self.state.exists())
        path = self.legacy("queue", "old", {"note": str(self.root / "missing")})
        before = path.read_bytes()
        self.assertEqual(distiller.collect_status()["queue"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.store.db_path.exists())

    def test_status_reports_legacy_when_shared_database_not_yet_imported_queue(self):
        self.legacy("failed", "old", {"note": str(self.root / "missing")})
        with self.store.transaction() as tx:
            tx.put_document("sync:other", "placeholder", {})
        status = distiller.collect_status()
        self.assertEqual(status["failed"], 1)
        self.assertEqual(status["backend"], "legacy-json")

    def test_migration_retains_legacy_and_never_replays(self):
        note = self.note()
        path = self.legacy("queue", distiller.queue_key(note), {"note": str(note), "attempts": 3})
        before = path.read_bytes()
        distiller._migrate_queue(self.settings)
        with distiller.queue_state_lock(self.settings):
            distiller._delete_job(self.settings, "queue", path.stem)
        path.write_text("not valid json anymore", encoding="utf-8")
        distiller._migrate_queue(self.settings)
        self.assertEqual(self.jobs(), [])
        self.assertNotEqual(path.read_bytes(), before)
        self.assertTrue(path.exists())
        self.assertEqual(distiller.collect_status()["queue"], 0)

    def test_migration_corruption_rolls_back_all_jobs(self):
        note = self.note()
        first = self.legacy("queue", "a", {"note": str(note)})
        bad = self.legacy("queue", "z", {"note": str(note)})
        bad.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            distiller._migrate_queue(self.settings)
        self.assertEqual(self.jobs(), [])
        self.assertTrue(first.exists())
        bad.write_text(json.dumps({"note": str(note)}), encoding="utf-8")
        distiller._migrate_queue(self.settings)
        self.assertEqual(len(self.jobs()), 2)

    def test_transaction_rollback_and_thread_local_cleanup(self):
        with self.assertRaises(RuntimeError):
            with distiller.queue_state_lock(self.settings):
                distiller._put_job(self.settings, "queue", "rollback", {"note": "test"})
                raise RuntimeError("rollback")
        self.assertEqual(self.jobs(), [])
        with distiller.queue_state_lock(self.settings):
            distiller._put_job(self.settings, "queue", "committed", {"note": "test"})
        self.assertEqual(len(self.jobs()), 1)

    def test_two_enqueues_and_claims_allow_one_inflight_model(self):
        note = self.note()
        barrier = threading.Barrier(2)
        errors = []

        def enqueue():
            try:
                barrier.wait(timeout=3)
                distiller.enqueue(note)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(self.jobs()), 1)
        self.assertFalse((self.state / "queue").exists())
        key = self.jobs()[0][0]
        started, release = threading.Event(), threading.Event()
        results = []

        def model(*args):
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return {"keep": False}

        with patch.object(distiller, "request_model", side_effect=model) as request:
            thread = threading.Thread(target=lambda: results.append(distiller.process_entry(key, self.settings)))
            thread.start()
            self.assertTrue(started.wait(timeout=3))
            try:
                self.assertFalse(distiller.process_entry(key, self.settings))
                # This write must finish while the model remains blocked.
                self.assertTrue(distiller.enqueue(note)["queued"])
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(results, [True])
        self.assertEqual(self.jobs(), [])

    def test_live_legacy_worker_blocks_migration_without_writing_database(self):
        self.state.mkdir()
        (self.state / "worker.lock").write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        with patch.object(distiller, "_pid_alive", return_value=True):
            with self.assertRaises(distiller.DistillerError):
                distiller.enqueue(self.note())
        self.assertFalse(self.store.db_path.exists())

    def test_failed_identity_includes_context_and_legacy_entries_are_independent(self):
        note = self.note()
        self.assertNotEqual(distiller._failed_key(note, "body", "scope-a"), distiller._failed_key(note, "body", "scope-b"))
        self.legacy("failed", "a", {"note": str(note)})
        self.legacy("failed", "b", {"note": str(note)})
        distiller._migrate_queue(self.settings)
        status = distiller.collect_status()
        self.assertEqual(status["unique_failed_inputs"], 2)
        self.assertEqual(status["legacy_failed_entries"], 2)
        self.assertEqual(distiller.retry_failed()["moved"], 1)
        self.assertEqual(self.jobs("failed"), [])
        self.assertEqual(len(list((self.state / "failed").glob("*.json"))), 2)

    def test_scope_only_failures_remain_separate_records(self):
        note = self.note()
        with patch.object(distiller, "request_model", side_effect=distiller.DistillerError("invalid response")):
            distiller.enqueue(note)
            self.assertTrue(distiller.process_entry(self.jobs()[0][0], self.settings))
            akb.update_note_metadata(note, {"scope": "project", "project_path": "/test/project"})
            distiller.enqueue(note)
            self.assertTrue(distiller.process_entry(self.jobs()[0][0], self.settings))
        failed = self.jobs("failed")
        self.assertEqual(len(failed), 2)
        self.assertEqual(failed[0][1]["input_hash"], failed[1][1]["input_hash"])
        self.assertNotEqual(failed[0][1]["input_context_hash"], failed[1][1]["input_context_hash"])
        self.assertEqual(distiller.collect_status()["unique_failed_inputs"], 2)
        self.assertFalse((self.state / "failed").exists())

    def test_empty_worker_lock_is_never_reclaimed_or_imported(self):
        self.state.mkdir()
        path = self.state / "worker.lock"
        path.write_text("", encoding="utf-8")
        self.assertFalse(distiller.acquire_worker_lock(path))
        self.assertEqual(path.read_bytes(), b"")
        with self.assertRaises(distiller.DistillerError):
            distiller._migrate_queue(self.settings)
        self.assertFalse(self.store.db_path.exists())
        self.assertTrue(path.exists())

    def test_queue_write_recovers_hot_journal_after_process_crash(self):
        note = self.note()
        distiller.enqueue(note)
        code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=8')
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE jobs SET payload=?', ('x' * (5 * 1024 * 1024),))
os._exit(86)
"""
        completed = subprocess.run([sys.executable, "-c", code, str(self.store.db_path)], timeout=15)
        self.assertEqual(completed.returncode, 86)
        self.assertTrue(Path(str(self.store.db_path) + "-journal").exists())
        try:
            status = distiller.collect_status()
        except ValueError as exc:
            self.assertIn("崩溃恢复", str(exc))
        else:
            self.assertEqual(status["queue"], 1)
        self.assertTrue(distiller.enqueue(note)["queued"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0][1]["note"], str(note))
        self.assertEqual(distiller.collect_status()["queue"], 1)

    def test_history_and_realtime_share_database_not_namespace(self):
        with patch.dict(os.environ, {"AKB_STATE_DB": ""}):
            live = {"state_dir": self.state}
            history = {"state_dir": self.state / "history-batch"}
            self.assertEqual(distiller.queue_store(live).db_path, distiller.queue_store(history).db_path)
            self.assertNotEqual(distiller.queue_namespace(live), distiller.queue_namespace(history))


if __name__ == "__main__":
    unittest.main()
