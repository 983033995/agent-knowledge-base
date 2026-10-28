from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import akb
import distiller
import memory_sync
import state_migration


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.environment = patch.dict(os.environ, {
            "AKB_VAULT": str(self.root / "vault"),
            "AKB_STATE_DB": str(self.root / "state/state.sqlite3"),
            "AKB_SYNC_STATE_DIR": str(self.root / "sync"),
            "AKB_DISTILLER_STATE_DIR": str(self.state),
            "AKB_SEARCH_INDEX": str(self.root / "index.json"),
            "AKB_DISTILLER_AUTOSTART": "0",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        akb._CONFIG_CACHE = None
        self.addCleanup(setattr, akb, "_CONFIG_CACHE", None)

    def legacy(self, relative, value):
        path = self.state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_migration_is_backed_up_idempotent_and_never_starts_worker(self):
        path = self.legacy("failed/old.json", {"note": str(self.root / "missing.md"), "attempts": 4, "last_error": "fixture"})
        history = self.legacy("history-batch/failed/other.json", {"note": str(self.root / "history.md"), "attempts": 1})
        before = {p: p.read_bytes() for p in (path, history)}
        with patch.object(distiller, "spawn_worker", side_effect=AssertionError("不应启动worker")):
            result = state_migration.migrate_local_state()
            repeat = state_migration.migrate_local_state()
        self.assertTrue(result["legacy_preserved"])
        self.assertEqual(result["cloud_requests"], 0)
        self.assertEqual(result["results"]["realtime"]["failed"], 1)
        self.assertEqual(result["results"]["history"]["failed"], 1)
        self.assertTrue(repeat["results"]["realtime"]["already_imported"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertTrue((Path(result["backup"]) / "migration-report.json").exists())
        self.assertEqual(result["results"]["realtime"]["integrity"], "ok")
        self.assertFalse((self.root / "vault").exists())

    def test_live_legacy_worker_blocks_before_backup_or_database_creation(self):
        self.legacy("worker.lock", {"pid": os.getpid()})
        with self.assertRaisesRegex(ValueError, "worker"):
            state_migration.migrate_local_state()
        self.assertFalse((self.state / "state.sqlite3").exists())
        self.assertFalse((self.state / "state-backups").exists())

    def test_cutover_lock_is_not_reclaimed_while_payload_is_empty(self):
        self.state.mkdir()
        original = state_migration.os.write
        observations = []

        def paused_write(descriptor, data):
            lock = self.state / "worker.lock"
            if lock.exists() and lock.stat().st_size == 0:
                observations.append(distiller.acquire_worker_lock(lock))
            return original(descriptor, data)

        with patch.object(state_migration.os, "write", side_effect=paused_write):
            with state_migration._cutover_workers([self.state]):
                self.assertEqual(observations, [False])
        self.assertFalse((self.state / "worker.lock").exists())

    def test_explicit_migration_backs_up_and_recovers_hot_database(self):
        store = distiller.queue_store(distiller.load_settings())
        with store.transaction() as transaction:
            transaction.put_document("fixture", "stable", {"v": "original"})
        code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=8')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE documents SET payload=?', ('x' * (5 * 1024 * 1024),))
os._exit(86)
"""
        completed = subprocess.run([sys.executable, "-c", code, str(store.db_path)], timeout=15)
        self.assertEqual(completed.returncode, 86)
        result = state_migration.migrate_local_state()
        self.assertTrue((Path(result["backup"]) / "recovery-0").exists())
        with store.read() as transaction:
            self.assertEqual(transaction.get_document("fixture", "stable"), {"v": "original"})
            self.assertEqual(transaction.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_batch_lock_blocks_safe_cutover(self):
        path = self.state / "history-batch/batch.lock"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "历史回填锁"):
            state_migration.migrate_local_state()
        self.assertFalse((self.state / "state.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
