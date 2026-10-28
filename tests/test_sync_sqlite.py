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
import memory_sync
from state_store import Transaction


class SyncSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "AKB_VAULT": str(self.root / "vault"),
            "AKB_SYNC_STATE_DIR": str(self.root / "sync"),
            "AKB_STATE_DB": str(self.root / "db/state.sqlite3"),
            "AKB_DISTILLER_AUTOSTART": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        akb._CONFIG_CACHE = None
        self.addCleanup(setattr, akb, "_CONFIG_CACHE", None)

    def state(self):
        return {"version": 1, "vault": str(akb.vault_path()), "source_home": str(self.root / "home"), "records": {}, "notes": {}, "files": {}, "last_sync": {"stats": {"unchanged": 2}}}

    def legacy(self):
        path = self.root / "sync/manifest.json"
        path.parent.mkdir()
        path.write_text(json.dumps(self.state()), encoding="utf-8")
        return path

    def test_legacy_state_is_read_without_creating_database(self):
        path = self.legacy()
        before = path.read_bytes()
        self.assertEqual(memory_sync._load_state(), self.state())
        self.assertEqual(memory_sync.sync_status()["last_sync"]["stats"]["unchanged"], 2)
        self.assertFalse((self.root / "db").exists())
        self.assertEqual(path.read_bytes(), before)

    def test_import_preserves_json_and_database_is_authoritative(self):
        path = self.legacy()
        before = path.read_bytes()
        self.assertFalse(memory_sync.migrate_state()["already_imported"])
        self.assertEqual(memory_sync._load_state(), self.state())
        self.assertEqual(path.read_bytes(), before)
        path.write_text("{stale", encoding="utf-8")
        self.assertTrue(memory_sync.migrate_state()["already_imported"])
        self.assertEqual(memory_sync._load_state(), self.state())

    def test_corrupt_pending_rolls_back_manifest_import(self):
        self.legacy()
        (self.root / "sync/pending.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法读取"):
            memory_sync.migrate_state()
        with memory_sync.sync_store().read() as transaction:
            if transaction:
                self.assertIsNone(transaction.get_sync_state(memory_sync.sync_namespace()))

    def test_migrated_pending_cannot_be_replayed_after_recovery(self):
        self.legacy()
        transaction = {"state": self.state(), "contents": {}, "refs": {}}
        pending = self.root / "sync/pending.json"
        pending.write_text(json.dumps(transaction), encoding="utf-8")
        before = pending.read_bytes()
        memory_sync.migrate_state()
        self.assertIsNotNone(memory_sync._load_pending())
        memory_sync._recover_pending()
        self.assertIsNone(memory_sync._load_pending())
        self.assertEqual(pending.read_bytes(), before)
        self.assertTrue(memory_sync.migrate_state()["already_imported"])
        self.assertIsNone(memory_sync._load_pending())

    def test_sqlite_commit_failure_keeps_pending_for_recovery(self):
        memory_sync.migrate_state()
        pending = {"state": self.state(), "contents": {}, "refs": {}}
        with memory_sync.sync_store().transaction() as transaction:
            transaction.put_document(memory_sync.sync_namespace(), "pending", pending)
        with patch.object(Transaction, "put_sync_state", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                memory_sync._recover_pending()
        self.assertIsNotNone(memory_sync._load_pending())
        memory_sync._recover_pending()
        self.assertEqual(memory_sync._load_state(), self.state())
        self.assertIsNone(memory_sync._load_pending())

    def test_vault_namespaces_do_not_mix(self):
        memory_sync.migrate_state()
        with memory_sync.sync_store().transaction() as transaction:
            transaction.put_sync_state(memory_sync.sync_namespace(), self.state())
        with patch.dict(os.environ, {"AKB_SYNC_STATE_DIR": str(self.root / "sync-two"), "AKB_VAULT": str(self.root / "vault-two")}):
            state = memory_sync._load_state()
            self.assertEqual(state["records"], {})
            self.assertNotEqual(state["vault"], str(self.root / "vault"))


if __name__ == "__main__":
    unittest.main()
