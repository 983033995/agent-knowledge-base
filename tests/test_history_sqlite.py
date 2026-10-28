from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import history_batch


class HistoryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AKB_DISTILLER_STATE_DIR": str(self.root / "state"), "AKB_STATE_DB": str(self.root / "state.sqlite3"), "AKB_DISTILLER_AUTOSTART": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_empty_status_does_not_create_state(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(history_batch.batch_status(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["queue"], 0)
        self.assertEqual(data["failed"], 0)
        self.assertFalse((self.root / "state.sqlite3").exists())
        self.assertFalse((self.root / "state").exists())

    def test_manifest_import_and_updates_use_database_only(self):
        root = self.root / "state/history-batch"
        root.mkdir(parents=True)
        legacy = root / "manifest.json"
        original = {"batch_id": "fixture", "vault": str(history_batch.akb.vault_path()), "phase": "queued", "files": {}, "stats": {}}
        legacy.write_text(json.dumps(original), encoding="utf-8")
        before = legacy.read_bytes()
        self.assertEqual(history_batch.load_manifest(), original)
        self.assertFalse((self.root / "state.sqlite3").exists())
        history_batch.migrate_manifest()
        history_batch.save_manifest({**original, "phase": "done"})
        self.assertEqual(history_batch.load_manifest()["phase"], "done")
        self.assertEqual(legacy.read_bytes(), before)
        self.assertTrue(history_batch.migrate_manifest()["already_imported"])
        self.assertEqual(history_batch.load_manifest()["phase"], "done")

    def test_changing_vault_cannot_resume_previous_batch(self):
        import akb
        history_batch.migrate_manifest()
        history_batch.save_manifest({"batch_id": "fixture", "phase": "queued", "files": {"one": {"status": "queued"}}, "stats": {}})
        with patch.dict(os.environ, {"AKB_VAULT": str(self.root / "other-vault")}):
            akb._CONFIG_CACHE = None
            with self.assertRaisesRegex(ValueError, "Vault"):
                history_batch.load_manifest()

    def test_legacy_batch_without_vault_is_readonly_until_resolved(self):
        root = self.root / "state/history-batch"
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({"batch_id": "old", "phase": "queued", "files": {}, "stats": {}}), encoding="utf-8")
        history_batch.migrate_manifest()
        value = history_batch.load_manifest()
        self.assertTrue(value["legacy_scope_unverified"])
        with self.assertRaisesRegex(ValueError, "Vault"):
            history_batch.save_manifest(value)

    def test_corrupt_legacy_manifest_stops_migration(self):
        root = self.root / "state/history-batch"
        root.mkdir(parents=True)
        path = root / "manifest.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            history_batch.migrate_manifest()
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
