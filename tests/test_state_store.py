from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state/state.sqlite3")

    def test_read_missing_store_does_not_create_files(self):
        with self.store.read() as tx:
            self.assertIsNone(tx)
        self.assertFalse((self.root / "state").exists())

    def test_transaction_rolls_back_on_base_exception(self):
        with self.store.transaction() as tx:
            tx.put_document("one", "original", {"v": 1})
        with self.assertRaises(KeyboardInterrupt):
            with self.store.transaction() as tx:
                tx.put_document("one", "original", {"v": 2})
                tx.put_job("one", "queue", "task", {"note": "fixture"})
                raise KeyboardInterrupt()
        with self.store.read() as tx:
            self.assertEqual(tx.get_document("one", "original"), {"v": 1})
            self.assertEqual(tx.list_jobs("one", "queue"), [])

    def test_namespaces_are_isolated(self):
        with self.store.transaction() as tx:
            tx.put_job("realtime", "queue", "same", {"v": 1})
            tx.put_job("history", "queue", "same", {"v": 2})
        with self.store.read() as tx:
            self.assertEqual(tx.get_job("realtime", "queue", "same"), {"v": 1})
            self.assertEqual(tx.get_job("history", "queue", "same"), {"v": 2})

    def test_concurrent_increment_transactions_do_not_lose_updates(self):
        with self.store.transaction() as tx:
            tx.put_document("counter", "value", {"n": 0})
        errors = []

        def increment():
            try:
                for _ in range(10):
                    with self.store.transaction() as tx:
                        value = tx.get_document("counter", "value")
                        value["n"] += 1
                        tx.put_document("counter", "value", value)
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=increment) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        self.assertFalse(errors)
        with self.store.read() as tx:
            self.assertEqual(tx.get_document("counter", "value"), {"n": 40})

    def legacy(self, kind, key, entry):
        path = self.root / "legacy" / kind / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding="utf-8")
        return path

    def test_import_keeps_files_and_cannot_replay_consumed_jobs(self):
        path = self.legacy("queue", "one", {"note": "/fixture.md", "attempts": 2, "not_before": 1})
        self.legacy("failed", "two", {"note": "/fixture.md", "last_error": "fixture"})
        before = path.read_bytes()
        result = self.store.import_json_jobs("ns", self.root / "legacy")
        self.assertEqual((result["queue"], result["failed"]), (1, 1))
        with self.store.transaction() as tx:
            tx.delete_job("ns", "queue", "one")
        second = self.store.import_json_jobs("ns", self.root / "legacy")
        self.assertTrue(second["already_imported"])
        with self.store.read() as tx:
            self.assertIsNone(tx.get_job("ns", "queue", "one"))
        self.assertEqual(path.read_bytes(), before)

    def test_import_payload_and_audit_hash_use_same_bytes(self):
        import hashlib
        path = self.legacy("queue", "one", {"note": "/fixture-a.md"})
        original = Path.read_bytes
        reads = []

        def mutate_after_read(target):
            raw = original(target)
            if target.resolve() == path.resolve():
                reads.append(raw)
                target.write_text(json.dumps({"note": "/fixture-b.md"}), encoding="utf-8")
            return raw

        with patch.object(Path, "read_bytes", mutate_after_read):
            result = self.store.import_json_jobs("ns", self.root / "legacy")
        self.assertEqual(len(reads), 1)
        self.assertEqual(result["files"][0]["sha256"], hashlib.sha256(reads[0]).hexdigest())
        with self.store.read() as tx:
            self.assertEqual(tx.get_job("ns", "queue", "one")["note"], "/fixture-a.md")

    def test_corrupt_legacy_aborts_entire_import(self):
        self.legacy("queue", "one", {"note": "/fixture.md"})
        path = self.legacy("failed", "two", {"note": "/fixture.md"})
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法读取"):
            self.store.import_json_jobs("ns", self.root / "legacy")
        with self.store.read() as tx:
            if tx is not None:
                self.assertEqual(tx.list_jobs("ns", "queue"), [])
                self.assertIsNone(tx.migration("ns", "json-jobs:" + str((self.root / "legacy").resolve())))

    def test_sync_rows_roundtrip_and_replace_atomically(self):
        state = {"version": 1, "vault": "/fixture", "records": {"r": {"status": "active"}}, "notes": {"n": {"path": "n.md"}}, "files": {"f": {"signature": [1, 2]}}}
        with self.store.transaction() as tx:
            tx.put_sync_state("sync", state)
        with self.store.read() as tx:
            self.assertEqual(tx.get_sync_state("sync"), state)
        state["records"] = {}
        with self.store.transaction() as tx:
            tx.put_sync_state("sync", state)
        with self.store.read() as tx:
            self.assertEqual(tx.get_sync_state("sync"), state)

    def test_future_schema_refuses_writes(self):
        with self.store.transaction():
            pass
        connection = sqlite3.connect(self.store.db_path)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        with self.assertRaisesRegex(ValueError, "版本"):
            with self.store.transaction():
                pass

    def test_concurrent_first_initialization_is_serialized(self):
        for iteration in range(15):
            store = StateStore(self.root / f"first-{iteration}.sqlite3")
            barrier = threading.Barrier(8)
            errors = []

            def initialize(index):
                try:
                    barrier.wait(5)
                    with store.transaction() as tx:
                        tx.put_document("init", str(index), {"n": index})
                except BaseException as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=initialize, args=(index,)) for index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(15)
            self.assertFalse(errors)
            self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_read_existing_database_does_not_create_sidecars(self):
        with self.store.transaction() as tx:
            tx.put_document("ns", "key", {"v": 1})
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.store.db_path.parent.iterdir()}
        with self.store.read() as tx:
            self.assertEqual(tx.get_document("ns", "key"), {"v": 1})
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.store.db_path.parent.iterdir()}
        self.assertEqual(before, after)

    def test_rejected_future_schema_does_not_change_journal(self):
        self.store.db_path.parent.mkdir()
        connection = sqlite3.connect(self.store.db_path)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        before = self.store.db_path.read_bytes()
        with self.assertRaises(ValueError):
            with self.store.transaction():
                pass
        self.assertEqual(self.store.db_path.read_bytes(), before)
        connection = sqlite3.connect(self.store.db_path)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        connection.close()

    def test_explicit_finalization_converts_old_wal_store_without_losing_rows(self):
        with self.store.transaction() as tx:
            tx.put_document("ns", "key", {"v": 1})
        connection = sqlite3.connect(self.store.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        self.store.finalize_journal()
        with self.store.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(tx.get_document("ns", "key"), {"v": 1})

    def test_read_connection_rejects_writes(self):
        with self.store.transaction():
            pass
        with self.store.read() as tx:
            with self.assertRaises(sqlite3.OperationalError):
                tx.put_document("ns", "key", {})


if __name__ == "__main__":
    unittest.main()
